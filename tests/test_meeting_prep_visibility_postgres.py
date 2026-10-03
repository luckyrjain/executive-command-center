"""Meeting prep packs never copy rows the reader cannot read (FX1).

A pack is stored once per meeting (`visibility='workspace'`) and served to
every reader of the meeting, so the stored snapshot is built only from
`workspace`-visible rows. Each caller's response (POST/GET/refresh) adds
the private or explicitly-shared rows that caller may read, computed live
and never stored. Private rows exist with or without
`ECC_PERSONAL_DATA_ISOLATION` (grants with `narrow_visibility`,
delegations, planning), so the filter is not flag-gated.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from gmail_sync_fixtures import build_gmail_sync_world
from identity_fixtures import create_identity
from sqlalchemy import text

from ecc.auth import AuthContext
from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.ai_runtime.ollama_client import OllamaAdapter
from ecc.domains.ai_runtime.runtime import get_ollama_adapter
from ecc.domains.ai_runtime.tools import ToolResult
from ecc.domains.attention.meeting_prep_tools import get_prep_pack_tool
from ecc.domains.governance.recommendation_models import RecommendationCreate
from ecc.domains.governance.recommendation_mutations import (
    create_recommendation,
    synthetic_request,
)
from ecc.main import app

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_FLAG = "ECC_PERSONAL_DATA_ISOLATION"
# pack section -> the seeded row types whose text lands in it
_SECTIONS = ("timeline", "commitments", "decisions", "notes", "risks", "dependencies")


@dataclass(frozen=True)
class _Member:
    user_id: UUID
    account_id: UUID
    client: TestClient
    token: str


@dataclass(frozen=True)
class _World:
    workspace_id: UUID
    a: _Member
    b: _Member
    bystander: _Member
    entity_id: UUID
    meeting_id: UUID


def _headers(token: str, key: str | None = None) -> dict[str, str]:
    csrf = new(get_settings().session_secret.encode(), token.encode(), "sha256").hexdigest()
    headers = {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _member(connection: Any, workspace_id: UUID, now: datetime, role: str, seq: int) -> _Member:
    user_id = uuid4()
    token = f"session-{uuid4()}"
    account_id = create_identity(
        connection,
        workspace_id=workspace_id,
        user_id=user_id,
        email=f"{user_id}@example.test",
        now=now + timedelta(seconds=seq),
        role=role,
    )
    connection.execute(
        text(
            "INSERT INTO sessions (id, workspace_id, user_id, token_hash, expires_at, "
            "last_seen_at) VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :now)"
        ),
        {
            "id": uuid4(),
            "workspace_id": workspace_id,
            "user_id": user_id,
            "token_hash": sha256(token.encode()).hexdigest(),
            "expires_at": now + timedelta(hours=1),
            "now": now,
        },
    )
    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    return _Member(user_id=user_id, account_id=account_id, client=client, token=token)


def _insert_node(workspace_id: UUID, name: str, owner_id: UUID, visibility: str) -> UUID:
    node_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO pkos_nodes (
                    id, workspace_id, node_type, canonical_name, attributes, status,
                    confidence, version, created_at, updated_at, owner_id, visibility
                ) VALUES (
                    :id, :workspace_id, 'person', :name, '{}'::jsonb, 'active',
                    1.00, 1, :now, :now, :owner_id, :visibility
                )
                """
            ),
            {
                "id": node_id,
                "workspace_id": workspace_id,
                "name": name,
                "now": now,
                "owner_id": owner_id,
                "visibility": visibility,
            },
        )
    return node_id


@pytest.fixture(params=["false", "true"], ids=["flag_off", "flag_on"])
def world(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Iterator[_World]:
    """Bystander (the workspace's original user), A and B -- all active
    members. B owns a `workspace`-visible meeting whose only participant is
    a `workspace`-visible person node. Parametrized over the Spec A flag:
    these private rows exist without it."""
    monkeypatch.setenv(_FLAG, request.param)
    get_settings.cache_clear()
    workspace_id = uuid4()
    meeting_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Meeting Prep Visibility', 'UTC', :now)"
            ),
            {"id": workspace_id, "now": now},
        )
        bystander = _member(connection, workspace_id, now, "owner", 0)
        a = _member(connection, workspace_id, now, "member", 1)
        b = _member(connection, workspace_id, now, "member", 2)
        connection.execute(
            text(
                """
                INSERT INTO meetings (
                    id, workspace_id, title, standalone_starts_at, standalone_ends_at,
                    standalone_timezone, status, agenda, created_by, updated_by,
                    created_at, updated_at, version, owner_id, visibility
                ) VALUES (
                    :id, :workspace_id, 'Partner sync', :starts_at, :ends_at, 'UTC',
                    'planned', 'Partner review', :b, :b, :now, :now, 1, :b, 'workspace'
                )
                """
            ),
            {
                "id": meeting_id,
                "workspace_id": workspace_id,
                "starts_at": now + timedelta(days=1),
                "ends_at": now + timedelta(days=1, hours=1),
                "b": b.user_id,
                "now": now,
            },
        )
    entity_id = _insert_node(workspace_id, "Partner Person", bystander.user_id, "workspace")
    try:
        yield _World(
            workspace_id=workspace_id,
            a=a,
            b=b,
            bystander=bystander,
            entity_id=entity_id,
            meeting_id=meeting_id,
        )
    finally:
        for member in (a, b, bystander):
            member.client.close()
        with engine.begin() as connection:
            for table in (
                "resource_grants",
                "meeting_packs",
                "meeting_participants",
                "notes",
                "commitments",
                "timeline_entries",
                "risks",
                "waiting_links",
                "pkos_evidence",
                "pkos_nodes",
                "meetings",
                "event_outbox",
                "audit_events",
                "idempotency_records",
                "ai_run_steps",
                "ai_runs",
                "sessions",
                "workspace_memberships",
                "users",
            ):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                    {"workspace_id": workspace_id},
                )
            connection.execute(
                text("DELETE FROM accounts WHERE id = ANY(:ids)"),
                {"ids": [a.account_id, b.account_id, bystander.account_id]},
            )
            connection.execute(text("DELETE FROM workspaces WHERE id = :id"), {"id": workspace_id})
        get_settings.cache_clear()


def _link(world: _World, member: _Member, entity_id: UUID, meeting_id: UUID | None = None) -> str:
    response = member.client.post(
        f"/api/v1/meetings/{meeting_id or world.meeting_id}/participants",
        headers=_headers(member.token, str(uuid4())),
        json={"entity_id": str(entity_id)},
    )
    assert response.status_code == 201, response.text
    participant_id: str = response.json()["id"]
    return participant_id


def _seed(
    world: _World,
    tag: str,
    *,
    owner_id: UUID,
    visibility: str,
    entity_id: UUID | None = None,
    grant_to: UUID | None = None,
    meeting_id: UUID | None = None,
) -> dict[str, UUID]:
    """One row of every type a pack composes, each carrying `tag` in its
    displayed text. Returns resource_type -> id."""
    entity = entity_id or world.entity_id
    meeting = meeting_id or world.meeting_id
    now = datetime.now(UTC)
    ids = {
        "timeline_entries": uuid4(),
        "commitments": uuid4(),
        "notes": uuid4(),
        "decision_notes": uuid4(),
        "risks": uuid4(),
        "waiting_links": uuid4(),
    }
    base = {
        "workspace_id": world.workspace_id,
        "owner_id": owner_id,
        "visibility": visibility,
        "entity_id": entity,
        "meeting_id": meeting,
        "tag": tag,
        "now": now,
    }
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO timeline_entries (
                    id, workspace_id, entity_id, effective_at, recorded_at, event_type,
                    summary, owner_id, visibility
                ) VALUES (
                    :id, :workspace_id, :entity_id, :now, :now, 'note_created',
                    :tag, :owner_id, :visibility
                )
                """
            ),
            {**base, "id": ids["timeline_entries"]},
        )
        connection.execute(
            text(
                """
                INSERT INTO commitments (
                    id, workspace_id, owner_id, summary, direction, status,
                    counterparty_person_id, importance, pinned, created_by, updated_by,
                    created_at, updated_at, version, visibility
                ) VALUES (
                    :id, :workspace_id, :owner_id, :tag, 'made_to_me', 'active',
                    :entity_id, 'medium', false, :owner_id, :owner_id, :now, :now, 1, :visibility
                )
                """
            ),
            {**base, "id": ids["commitments"]},
        )
        for key, note_type in (("notes", "general"), ("decision_notes", "decision")):
            connection.execute(
                text(
                    """
                    INSERT INTO notes (
                        id, workspace_id, owner_id, title, body, note_type, meeting_id,
                        source_type, restricted, created_by, updated_by, created_at,
                        updated_at, version, visibility
                    ) VALUES (
                        :id, :workspace_id, :owner_id, :title, :tag, :note_type, :meeting_id,
                        'local', false, :owner_id, :owner_id, :now, :now, 1, :visibility
                    )
                    """
                ),
                {**base, "id": ids[key], "note_type": note_type, "title": tag},
            )
        connection.execute(
            text(
                """
                INSERT INTO risks (
                    id, workspace_id, description, probability, impact, status, owner_id,
                    created_by, updated_by, created_at, updated_at, version, visibility
                ) VALUES (
                    :id, :workspace_id, :tag, 3, 4, 'monitoring', :owner_id,
                    :owner_id, :owner_id, :now, :now, 1, :visibility
                )
                """
            ),
            {**base, "id": ids["risks"]},
        )
        connection.execute(
            text(
                """
                INSERT INTO waiting_links (
                    id, workspace_id, subject_type, subject_id, counterparty_entity_id,
                    direction, status, since_at, note, created_by, updated_by, created_at,
                    updated_at, version, owner_id, visibility
                ) VALUES (
                    :id, :workspace_id, 'knowledge_entity', :entity_id, :entity_id,
                    'waiting_on_them', 'open', :now, :tag, :owner_id, :owner_id, :now,
                    :now, 1, :owner_id, :visibility
                )
                """
            ),
            {**base, "id": ids["waiting_links"]},
        )
        if grant_to is not None:
            for key, resource_id in ids.items():
                resource_type = "notes" if key == "decision_notes" else key
                connection.execute(
                    text(
                        """
                        INSERT INTO resource_grants (
                            id, workspace_id, grantee_account_id, resource_type,
                            resource_id, actions, granted_by, created_at
                        ) VALUES (
                            :id, :workspace_id, :grantee, :resource_type,
                            :resource_id, ARRAY['read'], :owner_id, :now
                        )
                        """
                    ),
                    {
                        "id": uuid4(),
                        "workspace_id": world.workspace_id,
                        "grantee": grant_to,
                        "resource_type": resource_type,
                        "resource_id": resource_id,
                        "owner_id": owner_id,
                        "now": now,
                    },
                )
    return ids


def _section_text(pack: dict[str, Any], section: str) -> str:
    return json.dumps(pack[section])


def _tags_in(pack: dict[str, Any], section: str, tags: tuple[str, ...]) -> set[str]:
    blob = _section_text(pack, section)
    return {tag for tag in tags if tag in blob}


def _stored(meeting_id: UUID) -> dict[str, Any]:
    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT content, visibility FROM meeting_packs "
                "WHERE meeting_id = :m AND status IN ('fresh', 'stale')"
            ),
            {"m": meeting_id},
        ).all()
    assert len(rows) == 1
    assert rows[0][1] == "workspace"
    content: dict[str, Any] = rows[0][0]
    return content


def _prep(member: _Member, meeting_id: UUID, *, refresh: bool = False) -> dict[str, Any]:
    path = f"/api/v1/meetings/{meeting_id}/prep" + ("/refresh" if refresh else "")
    response = member.client.post(path, headers=_headers(member.token, str(uuid4())))
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


def _get(member: _Member, meeting_id: UUID) -> dict[str, Any]:
    response = member.client.get(f"/api/v1/meetings/{meeting_id}/prep")
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


_ALL = ("WS-ROW", "A-PRIVATE", "B-PRIVATE", "A-GRANTED-B")


def _assert_view(pack: dict[str, Any], expected: set[str]) -> None:
    for section in _SECTIONS:
        seen = _tags_in(pack, section, _ALL)
        assert seen == expected, (section, seen, expected)


def _seed_standard(world: _World) -> None:
    _link(world, world.b, world.entity_id)
    _seed(world, "WS-ROW", owner_id=world.a.user_id, visibility="workspace")
    _seed(world, "A-PRIVATE", owner_id=world.a.user_id, visibility="private")
    _seed(world, "B-PRIVATE", owner_id=world.b.user_id, visibility="private")
    _seed(
        world,
        "A-GRANTED-B",
        owner_id=world.a.user_id,
        visibility="shared_explicitly",
        grant_to=world.b.account_id,
    )


def test_pack_never_carries_rows_the_reader_cannot_read(world: _World) -> None:
    _seed_standard(world)

    created = _prep(world.b, world.meeting_id)
    # B sees workspace rows plus what B alone may read -- never A's private.
    _assert_view(created, {"WS-ROW", "B-PRIVATE", "A-GRANTED-B"})

    # The stored, shared snapshot holds workspace-visible rows only.
    _assert_view(_stored(world.meeting_id), {"WS-ROW"})

    # Every other reader gets the snapshot plus only their own readable rows.
    _assert_view(_get(world.bystander, world.meeting_id), {"WS-ROW"})
    _assert_view(_get(world.a, world.meeting_id), {"WS-ROW", "A-PRIVATE", "A-GRANTED-B"})
    _assert_view(_get(world.b, world.meeting_id), {"WS-ROW", "B-PRIVATE", "A-GRANTED-B"})


def test_refresh_by_another_member_keeps_snapshot_workspace_only(world: _World) -> None:
    _seed_standard(world)
    _prep(world.b, world.meeting_id)

    refreshed = _prep(world.a, world.meeting_id, refresh=True)
    _assert_view(refreshed, {"WS-ROW", "A-PRIVATE", "A-GRANTED-B"})
    _assert_view(_stored(world.meeting_id), {"WS-ROW"})
    _assert_view(_get(world.b, world.meeting_id), {"WS-ROW", "B-PRIVATE", "A-GRANTED-B"})
    _assert_view(_get(world.bystander, world.meeting_id), {"WS-ROW"})


def test_idempotent_replay_returns_only_the_callers_own_view(world: _World) -> None:
    _seed_standard(world)
    key = str(uuid4())
    path = f"/api/v1/meetings/{world.meeting_id}/prep"
    first = world.b.client.post(path, headers=_headers(world.b.token, key))
    replay = world.b.client.post(path, headers=_headers(world.b.token, key))
    assert first.status_code == replay.status_code == 201
    _assert_view(replay.json(), {"WS-ROW", "B-PRIVATE", "A-GRANTED-B"})


def test_private_rows_do_not_flip_the_shared_pack_stale(world: _World) -> None:
    _seed_standard(world)
    _prep(world.b, world.meeting_id)

    # A new private row (any member's) is not part of the shared snapshot,
    # so no reader's GET may flip its stale flag -- that would disclose
    # that someone's private data about this meeting changed.
    _seed(world, "A-PRIVATE-LATER", owner_id=world.a.user_id, visibility="private")
    for member in (world.bystander, world.a, world.b):
        assert _get(member, world.meeting_id)["status"] == "fresh"

    # Positive control: a workspace row change still marks it stale.
    _seed(world, "WS-LATER", owner_id=world.a.user_id, visibility="workspace")
    assert _get(world.bystander, world.meeting_id)["status"] == "stale"


def test_private_participant_node_stays_out_of_the_snapshot(world: _World) -> None:
    """A participant whose person node only A may read: A sees it and the
    rows about it; the snapshot and every other reader see neither."""
    private_node = _insert_node(world.workspace_id, "A-PRIVATE-PERSON", world.a.user_id, "private")
    _link(world, world.a, private_node)
    _link(world, world.a, world.entity_id)
    _seed(world, "WS-ROW", owner_id=world.a.user_id, visibility="workspace")
    _seed(
        world,
        "WS-ON-PRIVATE-NODE",
        owner_id=world.a.user_id,
        visibility="workspace",
        entity_id=private_node,
    )

    created = _prep(world.a, world.meeting_id)
    names = {p["entity_name"] for p in created["participants"]}
    assert names == {"Partner Person", "A-PRIVATE-PERSON"}
    for section in ("timeline", "commitments", "dependencies"):
        assert "WS-ON-PRIVATE-NODE" in _section_text(created, section)

    stored = _stored(world.meeting_id)
    for pack in (stored, _get(world.bystander, world.meeting_id), _get(world.b, world.meeting_id)):
        assert {p["entity_name"] for p in pack["participants"]} == {"Partner Person"}
        blob = json.dumps(pack)
        assert "A-PRIVATE-PERSON" not in blob
        assert str(private_node) not in blob
        # Rows keyed to the private node stay out; its meeting-scoped notes
        # and workspace-wide risks are ordinary workspace rows.
        for section in ("timeline", "commitments", "dependencies"):
            assert "WS-ROW" in _section_text(pack, section)
            assert "WS-ON-PRIVATE-NODE" not in _section_text(pack, section)


def _without_request_ids(pack: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in pack.items() if k not in {"request_id", "correlation_id"}}


def test_workspace_only_pack_is_unchanged(world: _World) -> None:
    """Flag on/off parity: with only workspace rows, every reader gets the
    identical pack and it matches the stored snapshot."""
    _link(world, world.b, world.entity_id)
    _seed(world, "WS-ROW", owner_id=world.a.user_id, visibility="workspace")
    created = _prep(world.b, world.meeting_id)
    _assert_view(created, {"WS-ROW"})
    stored = _stored(world.meeting_id)
    for member in (world.a, world.b, world.bystander):
        got = _get(member, world.meeting_id)
        assert _without_request_ids(got) == _without_request_ids(created)
        for section in (*_SECTIONS, "participants", "evidence_gaps"):
            assert got[section] == stored[section]


# --- email-derived rows (Spec A flag on, real Gmail sync world) --------------------

_FLAG_ON = {_FLAG: "true"}
_EXTRA_CLEANUP = (
    "meeting_packs",
    "idempotency_records",
    "meeting_participants",
    "meetings",
    "recommendation_feedback",
)
_RISK_SECRET = "CONFIDENTIAL-acquisition-may-slip"
_COMMITMENT_SECRET = "CONFIDENTIAL-send-term-sheet"


def _confirm_email_recommendation(
    world: Any, target_type: str, proposed_fields: dict[str, Any]
) -> UUID:
    """A private `email_action_detected` recommendation of A's, confirmed
    by A -- the detector's own path to a private email-derived row."""
    a = world.a
    auth = AuthContext(workspace_id=world.workspace_id, user_id=a.user_id, timezone="UTC")
    with SessionFactory() as session:
        rec = create_recommendation(
            session,
            auth,
            RecommendationCreate(
                recommendation_type="email_action_detected",
                target_type=target_type,
                target_id=None,
                proposed_action={"operation": "create", "value": None},
                proposed_fields=proposed_fields,
                rationale="Detected.",
                confidence=0.8,
                evidence_ids=[],
                source="ai",
            ),
            synthetic_request(uuid4(), uuid4()),
            f"fx1:{uuid4()}",
            visibility="private",
        )

    def _version() -> int:
        with engine.begin() as connection:
            return int(
                connection.execute(
                    text("SELECT version FROM recommendations WHERE id = :id"), {"id": rec.id}
                ).scalar_one()
            )

    client = world.client("a")
    published = client.post(
        f"/api/v1/recommendations/{rec.id}/publish",
        json={"expected_version": _version()},
        headers=world.headers("a", idempotency_key=str(uuid4())),
    )
    assert published.status_code == 200, published.text
    confirmed = client.post(
        f"/api/v1/recommendations/{rec.id}/confirm",
        json={"expected_version": _version(), "target_expected_version": None},
        headers=world.headers("a", idempotency_key=str(uuid4())),
    )
    assert confirmed.status_code == 200, confirmed.text
    target_id = UUID(confirmed.json()["execution_result"]["target_id"])
    with engine.begin() as connection:
        visibility = connection.execute(
            text(f"SELECT visibility FROM {target_type}s WHERE id = :id"),  # noqa: S608
            {"id": target_id},
        ).scalar_one()
    assert visibility == "private"
    return target_id


def _world_meeting(world: Any, key: str) -> UUID:
    now = datetime.now(UTC)
    created = world.client(key).post(
        "/api/v1/meetings",
        headers=world.headers(key, idempotency_key=str(uuid4())),
        json={
            "title": f"{key} partner sync",
            "starts_at": (now + timedelta(hours=1)).isoformat(),
            "ends_at": (now + timedelta(hours=2)).isoformat(),
            "timezone": "UTC",
        },
    )
    assert created.status_code == 201, created.text
    meeting_id = UUID(created.json()["id"])
    linked = world.client(key).post(
        f"/api/v1/meetings/{meeting_id}/participants",
        headers=world.headers(key, idempotency_key=str(uuid4())),
        json={"entity_id": str(world.shared_person_node_id)},
    )
    assert linked.status_code == 201, linked.text
    return meeting_id


def _world_prep(world: Any, key: str, meeting_id: UUID) -> str:
    response = world.client(key).post(
        f"/api/v1/meetings/{meeting_id}/prep",
        headers=world.headers(key, idempotency_key=str(uuid4())),
    )
    assert response.status_code == 201, response.text
    return response.text


def test_email_derived_private_rows_stay_with_their_owner() -> None:
    with build_gmail_sync_world(
        env=_FLAG_ON, extra_cleanup_tables=_EXTRA_CLEANUP, bystander=True
    ) as world:
        _confirm_email_recommendation(
            world, "risk", {"description": _RISK_SECRET, "probability": 3, "impact": 4}
        )
        _confirm_email_recommendation(
            world,
            "commitment",
            {
                "summary": _COMMITMENT_SECRET,
                "direction": "made_by_me",
                "counterparty_person_id": str(world.shared_person_node_id),
            },
        )
        assert world.bystander_user_id is not None
        bystander_client, _ = world.harness.client_for(world.workspace_id, world.bystander_user_id)

        # B's own meeting with the shared correspondent: nothing of A's.
        b_meeting = _world_meeting(world, "b")
        b_pack = _world_prep(world, "b", b_meeting)
        stored_b = json.dumps(_stored(b_meeting))
        bystander_view = bystander_client.get(f"/api/v1/meetings/{b_meeting}/prep")
        assert bystander_view.status_code == 200, bystander_view.text
        for blob in (b_pack, stored_b, bystander_view.text):
            assert _RISK_SECRET not in blob
            assert _COMMITMENT_SECRET not in blob

        # A's own meeting: A sees both, the stored pack and the bystander don't.
        a_meeting = _world_meeting(world, "a")
        a_pack = _world_prep(world, "a", a_meeting)
        assert _RISK_SECRET in a_pack
        assert _COMMITMENT_SECRET in a_pack
        stored_a = json.dumps(_stored(a_meeting))
        bystander_view = bystander_client.get(f"/api/v1/meetings/{a_meeting}/prep")
        assert bystander_view.status_code == 200, bystander_view.text
        for blob in (stored_a, bystander_view.text):
            assert _RISK_SECRET not in blob
            assert _COMMITMENT_SECRET not in blob
        assert _RISK_SECRET in world.client("a").get(f"/api/v1/meetings/{a_meeting}/prep").text


def test_callers_rows_merge_in_pack_order_within_the_section_limit(world: _World) -> None:
    """Risks cap at 10, ordered by review date: a reader's own earlier-due
    private risk takes its place at the top of *their* view only."""
    _link(world, world.b, world.entity_id)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        for i in range(10):
            connection.execute(
                text(
                    """
                    INSERT INTO risks (
                        id, workspace_id, description, probability, impact, status, owner_id,
                        created_by, updated_by, created_at, updated_at, version, visibility,
                        review_at
                    ) VALUES (
                        :id, :workspace_id, :description, 2, 2, 'monitoring', :owner_id,
                        :owner_id, :owner_id, :now, :now, 1, :visibility, :review_at
                    )
                    """
                ),
                {
                    "id": uuid4(),
                    "workspace_id": world.workspace_id,
                    "description": f"WS-RISK-{i}",
                    "owner_id": world.a.user_id,
                    "now": now,
                    "visibility": "workspace",
                    "review_at": now + timedelta(days=i + 1),
                },
            )
        connection.execute(
            text(
                """
                INSERT INTO risks (
                    id, workspace_id, description, probability, impact, status, owner_id,
                    created_by, updated_by, created_at, updated_at, version, visibility, review_at
                ) VALUES (
                    :id, :workspace_id, 'A-PRIVATE-FIRST', 5, 5, 'monitoring', :owner_id,
                    :owner_id, :owner_id, :now, :now, 1, 'private', :review_at
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": world.workspace_id,
                "owner_id": world.a.user_id,
                "now": now,
                "review_at": now,
            },
        )

    _prep(world.b, world.meeting_id)
    shared = [r["description"] for r in _get(world.bystander, world.meeting_id)["risks"]]
    assert shared == [f"WS-RISK-{i}" for i in range(10)]
    own = [r["description"] for r in _get(world.a, world.meeting_id)["risks"]]
    assert own == ["A-PRIVATE-FIRST", *(f"WS-RISK-{i}" for i in range(9))]


def test_timeline_merges_callers_rows_in_pack_order(world: _World) -> None:
    """Timeline is newest-first: a reader's private entry dated between two
    workspace entries sits between them in their view only."""
    _link(world, world.b, world.entity_id)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        for summary, visibility, days_ago in (
            ("WS-NEWEST", "workspace", 1),
            ("A-PRIVATE-MIDDLE", "private", 2),
            ("WS-OLDEST", "workspace", 3),
        ):
            connection.execute(
                text(
                    """
                    INSERT INTO timeline_entries (
                        id, workspace_id, entity_id, effective_at, recorded_at, event_type,
                        summary, owner_id, visibility
                    ) VALUES (
                        :id, :workspace_id, :entity_id, :effective_at, :now, 'note_created',
                        :summary, :owner_id, :visibility
                    )
                    """
                ),
                {
                    "id": uuid4(),
                    "workspace_id": world.workspace_id,
                    "entity_id": world.entity_id,
                    "effective_at": now - timedelta(days=days_ago),
                    "now": now,
                    "summary": summary,
                    "owner_id": world.a.user_id,
                    "visibility": visibility,
                },
            )

    _prep(world.b, world.meeting_id)
    shared = [t["summary"] for t in _get(world.bystander, world.meeting_id)["timeline"]]
    assert shared == ["WS-NEWEST", "WS-OLDEST"]
    own = [t["summary"] for t in _get(world.a, world.meeting_id)["timeline"]]
    assert own == ["WS-NEWEST", "A-PRIVATE-MIDDLE", "WS-OLDEST"]


def _insert_evidence(world: _World, *, owner_id: UUID, visibility: str, state: str) -> UUID:
    evidence_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO pkos_evidence (
                    id, workspace_id, node_id, source_type, source_ref, sha256,
                    captured_at, evidence_state, owner_id, visibility
                ) VALUES (
                    :id, :workspace_id, :node_id, 'test', :source_ref, :sha256,
                    :captured_at, :state, :owner_id, :visibility
                )
                """
            ),
            {
                "id": evidence_id,
                "workspace_id": world.workspace_id,
                "node_id": world.entity_id,
                "source_ref": f"test:{evidence_id}",
                "sha256": sha256(str(evidence_id).encode()).hexdigest(),
                "captured_at": datetime.now(UTC) - timedelta(days=1),
                "state": state,
                "owner_id": owner_id,
                "visibility": visibility,
            },
        )
    return evidence_id


def _gap_ids(pack: dict[str, Any]) -> set[UUID]:
    return {UUID(gap["id"]) for gap in pack["evidence_gaps"]}


@pytest.mark.parametrize("world", ["true"], indirect=True, ids=["flag_on"])
def test_callers_private_evidence_stays_in_their_view_and_never_flips_stale(
    world: _World,
) -> None:
    _link(world, world.b, world.entity_id)
    shared_gap = _insert_evidence(
        world, owner_id=world.a.user_id, visibility="workspace", state="missing"
    )
    a_gap = _insert_evidence(world, owner_id=world.a.user_id, visibility="private", state="missing")
    b_gap = _insert_evidence(world, owner_id=world.b.user_id, visibility="private", state="missing")

    assert _gap_ids(_prep(world.b, world.meeting_id)) == {shared_gap, b_gap}
    assert _gap_ids(_stored(world.meeting_id)) == {shared_gap}

    # A's private *available* evidence (no gap to show) must not change the
    # shared fingerprint either: no reader's GET flips the pack stale.
    _insert_evidence(world, owner_id=world.a.user_id, visibility="private", state="available")
    a_view = _get(world.a, world.meeting_id)
    assert a_view["status"] == "fresh"
    assert _gap_ids(a_view) == {shared_gap, a_gap}
    for member in (world.bystander, world.b):
        view = _get(member, world.meeting_id)
        assert view["status"] == "fresh"
        assert a_gap not in _gap_ids(view)
    assert _gap_ids(_get(world.bystander, world.meeting_id)) == {shared_gap}


@pytest.mark.parametrize("world", ["false"], indirect=True, ids=["flag_off"])
def test_flag_off_private_participant_evidence_gaps_stay_with_its_owner(world: _World) -> None:
    """Flag off (pre-FX1 behaviour for evidence): the owner of a private
    participant node still sees that node's evidence gaps; nobody else
    does, and the stored pack does not carry them."""
    private_node = _insert_node(world.workspace_id, "A-PRIVATE-PERSON", world.a.user_id, "private")
    _link(world, world.a, private_node)
    _link(world, world.a, world.entity_id)
    shared_gap = _insert_evidence(
        world, owner_id=world.a.user_id, visibility="workspace", state="missing"
    )
    private_node_gap = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO pkos_evidence (
                    id, workspace_id, node_id, source_type, source_ref, sha256,
                    captured_at, evidence_state, owner_id, visibility
                ) VALUES (
                    :id, :workspace_id, :node_id, 'test', :source_ref, :sha256,
                    :now, 'missing', :owner_id, 'workspace'
                )
                """
            ),
            {
                "id": private_node_gap,
                "workspace_id": world.workspace_id,
                "node_id": private_node,
                "source_ref": f"test:{private_node_gap}",
                "sha256": sha256(str(private_node_gap).encode()).hexdigest(),
                "now": datetime.now(UTC),
                "owner_id": world.a.user_id,
            },
        )

    assert _gap_ids(_prep(world.a, world.meeting_id)) == {shared_gap, private_node_gap}
    assert _gap_ids(_stored(world.meeting_id)) == {shared_gap}
    assert _gap_ids(_get(world.a, world.meeting_id)) == {shared_gap, private_node_gap}
    for member in (world.b, world.bystander):
        view = _get(member, world.meeting_id)
        assert _gap_ids(view) == {shared_gap}
        assert view["status"] == "fresh"


# --- the AI tool feeds only shared artifacts ----------------------------------------

_PRIVATE_MARKERS = ("A-PRIVATE", "A-PRIVATE-PERSON", "WS-ON-PRIVATE-NODE")


def _seed_private_participant_world(world: _World) -> str:
    """A links a private person node plus the workspace one; rows of every
    type exist for A (private), on the private node (workspace rows), and
    workspace-wide. Returns the workspace participant's id."""
    private_node = _insert_node(world.workspace_id, "A-PRIVATE-PERSON", world.a.user_id, "private")
    _link(world, world.a, private_node)
    shared_participant = _link(world, world.a, world.entity_id)
    _seed(world, "WS-ROW", owner_id=world.a.user_id, visibility="workspace")
    _seed(world, "A-PRIVATE", owner_id=world.a.user_id, visibility="private")
    _seed(
        world,
        "WS-ON-PRIVATE-NODE",
        owner_id=world.a.user_id,
        visibility="workspace",
        entity_id=private_node,
    )
    # Notes and risks aren't keyed to a participant: the ones `_seed` just
    # wrote are ordinary workspace rows, so give them a neutral marker.
    with engine.begin() as connection:
        for table, column in (("notes", "body"), ("notes", "title"), ("risks", "description")):
            connection.execute(
                text(
                    f"UPDATE {table} SET {column} = 'WS-UNKEYED' "  # noqa: S608 -- literals
                    f"WHERE workspace_id = :w AND {column} = 'WS-ON-PRIVATE-NODE'"
                ),
                {"w": world.workspace_id},
            )
    return shared_participant


def _tool_output(world: _World, member: _Member) -> str:
    auth = AuthContext(workspace_id=world.workspace_id, user_id=member.user_id, timezone="UTC")
    with SessionFactory() as session:
        result = get_prep_pack_tool(session, auth, world.meeting_id)
        session.rollback()
    assert isinstance(result, ToolResult), result
    return json.dumps(result.output)


def test_prep_pack_tool_returns_the_shared_snapshot_for_every_caller(world: _World) -> None:
    """`meeting.get_prep_pack` only ever feeds `meeting.prep_summary`, whose
    summary is stored in the shared pack and in a workspace-visible run, so
    it must return the shared snapshot -- identical for A (who owns the
    private rows and node) and B."""
    _seed_private_participant_world(world)
    outputs = {key: _tool_output(world, member) for key, member in (("a", world.a), ("b", world.b))}
    for output in outputs.values():
        assert "WS-ROW" in output
        for marker in _PRIVATE_MARKERS:
            assert marker not in output
    assert outputs["a"] == outputs["b"]


def test_stored_enrichment_never_sees_the_generators_private_rows(
    world: _World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: A generates the pack with AI enrichment on. The model's
    prompt (built from the tool's output), the persisted run steps and the
    stored pack carry no private data of A's -- while A's own response
    still shows it (A's per-request view)."""
    shared_participant = _seed_private_participant_world(world)
    prompts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        prompts.append(request.content.decode())
        body = json.dumps(
            {
                "model": "m",
                "created_at": "now",
                "response": json.dumps(
                    {"summary_text": "Partner review.", "cited_evidence_ids": [shared_participant]}
                ),
                "done": True,
                "eval_count": 12,
                "prompt_eval_count": 40,
            }
        )
        return httpx.Response(
            200, content=(body + "\n").encode(), headers={"content-type": "application/x-ndjson"}
        )

    adapter = OllamaAdapter(transport=httpx.MockTransport(handler))
    monkeypatch.setenv("ECC_MEETING_PREP_AI_ENRICHMENT_ENABLED", "true")
    get_settings.cache_clear()
    app.dependency_overrides[get_ollama_adapter] = lambda: adapter
    try:
        created = _prep(world.a, world.meeting_id)
    finally:
        app.dependency_overrides.pop(get_ollama_adapter, None)

    assert created["enrichment"]["available"] is True, created["enrichment"]
    assert prompts, "the enrichment run never called the model"
    prompt_text = "".join(prompts)
    assert "WS-ROW" in prompt_text
    with engine.begin() as connection:
        steps = json.dumps(
            [
                row[0]
                for row in connection.execute(
                    text("SELECT trace FROM ai_run_steps WHERE workspace_id = :w"),
                    {"w": world.workspace_id},
                )
            ]
        )
    stored = json.dumps(_stored(world.meeting_id))
    for marker in _PRIVATE_MARKERS:
        assert marker not in prompt_text
        assert marker not in steps
        assert marker not in stored
    assert "A-PRIVATE" in json.dumps(created)  # A's own view is unaffected


def test_callers_participants_keep_link_order(world: _World) -> None:
    """Shared and private participants merge back into link order."""
    first = _insert_node(world.workspace_id, "A-PRIVATE-FIRST", world.a.user_id, "private")
    _link(world, world.a, first)
    _link(world, world.a, world.entity_id)
    last = _insert_node(world.workspace_id, "A-PRIVATE-LAST", world.a.user_id, "private")
    _link(world, world.a, last)
    created = _prep(world.a, world.meeting_id)
    expected = ["A-PRIVATE-FIRST", "Partner Person", "A-PRIVATE-LAST"]
    assert [p["entity_name"] for p in created["participants"]] == expected
    got = _get(world.a, world.meeting_id)
    assert [p["entity_name"] for p in got["participants"]] == expected


# --- rows narrowed after the pack was generated (FX1 M1) ---------------------------

_NARROWABLE = (
    "timeline_entries",
    "commitments",
    "notes",
    "decision_notes",
    "risks",
    "waiting_links",
)


def _narrow(world: _World, table: str, row_id: UUID, *, owner_id: UUID) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                f"UPDATE {table} SET visibility = 'private', owner_id = :owner_id "  # noqa: S608
                "WHERE workspace_id = :workspace_id AND id = :id"
            ),
            {"owner_id": owner_id, "workspace_id": world.workspace_id, "id": row_id},
        )


def _narrow_all(world: _World, ids: dict[str, UUID], *, owner_id: UUID) -> None:
    for key in _NARROWABLE:
        _narrow(world, "notes" if key == "decision_notes" else key, ids[key], owner_id=owner_id)


def _set_stored_enrichment(meeting_id: UUID, summary: str) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE meeting_packs SET content = jsonb_set(content, '{enrichment}', "
                "CAST(:enrichment AS jsonb)) WHERE meeting_id = :m AND status IN ('fresh', 'stale')"
            ),
            {
                "m": meeting_id,
                "enrichment": json.dumps(
                    {"available": True, "summary": summary, "error_code": None}
                ),
            },
        )


def test_rows_narrowed_after_generation_leave_every_other_readers_view(world: _World) -> None:
    """A stored row narrowed to A's private after the pack was generated
    flips the pack stale; until refreshed, only readers who can still read
    the row (A) get it. The stored snapshot itself is left as generated."""
    _link(world, world.b, world.entity_id)
    ids = _seed(world, "WS-ROW", owner_id=world.a.user_id, visibility="workspace")
    _seed(world, "WS-KEPT", owner_id=world.a.user_id, visibility="workspace")
    _prep(world.b, world.meeting_id)

    _narrow_all(world, ids, owner_id=world.a.user_id)

    for member in (world.bystander, world.b):
        view = _get(member, world.meeting_id)
        assert view["status"] == "stale"
        for section in _SECTIONS:
            assert _tags_in(view, section, ("WS-ROW", "WS-KEPT")) == {"WS-KEPT"}, section
    a_view = _get(world.a, world.meeting_id)
    for section in _SECTIONS:
        assert _tags_in(a_view, section, ("WS-ROW", "WS-KEPT")) == {"WS-ROW", "WS-KEPT"}, section
    stored = json.dumps(_stored(world.meeting_id))  # the snapshot is left as generated
    assert "WS-ROW" in stored
    assert "WS-KEPT" in stored


def test_rows_deleted_or_restricted_after_generation_are_dropped(world: _World) -> None:
    _link(world, world.b, world.entity_id)
    ids = _seed(world, "WS-ROW", owner_id=world.a.user_id, visibility="workspace")
    _prep(world.b, world.meeting_id)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE notes SET restricted = true WHERE id = ANY(:ids)"),
            {"ids": [ids["notes"], ids["decision_notes"]]},
        )
        connection.execute(text("DELETE FROM risks WHERE id = :id"), {"id": ids["risks"]})

    for member in (world.a, world.b, world.bystander):
        view = _get(member, world.meeting_id)
        for section in ("notes", "decisions", "risks"):
            assert "WS-ROW" not in _section_text(view, section), (member, section)
        for section in ("timeline", "commitments", "dependencies"):
            assert "WS-ROW" in _section_text(view, section), (member, section)


def test_participant_node_narrowed_after_generation_drops_it_and_its_rows(world: _World) -> None:
    _link(world, world.b, world.entity_id)
    _seed(world, "WS-ROW", owner_id=world.a.user_id, visibility="workspace")
    _prep(world.b, world.meeting_id)
    _narrow(world, "pkos_nodes", world.entity_id, owner_id=world.a.user_id)

    for member in (world.bystander, world.b):
        view = _get(member, world.meeting_id)
        assert view["participants"] == []
        assert "Partner Person" not in json.dumps(view)
        for section in ("timeline", "commitments", "dependencies"):
            assert "WS-ROW" not in _section_text(view, section)
        # Meeting-scoped notes and workspace-wide risks are not about the node.
        for section in ("notes", "decisions", "risks"):
            assert "WS-ROW" in _section_text(view, section)
    a_view = _get(world.a, world.meeting_id)
    assert {p["entity_name"] for p in a_view["participants"]} == {"Partner Person"}
    for section in _SECTIONS:
        assert "WS-ROW" in _section_text(a_view, section)


@pytest.mark.parametrize("world", ["true"], indirect=True, ids=["flag_on"])
def test_evidence_gap_narrowed_after_generation_is_dropped(world: _World) -> None:
    _link(world, world.b, world.entity_id)
    gap = _insert_evidence(world, owner_id=world.a.user_id, visibility="workspace", state="missing")
    _prep(world.b, world.meeting_id)
    _narrow(world, "pkos_evidence", gap, owner_id=world.a.user_id)

    assert _gap_ids(_get(world.bystander, world.meeting_id)) == set()
    assert _gap_ids(_get(world.b, world.meeting_id)) == set()
    assert _gap_ids(_get(world.a, world.meeting_id)) == {gap}


def test_stored_summary_is_withheld_once_a_row_is_dropped(world: _World) -> None:
    _link(world, world.b, world.entity_id)
    ids = _seed(world, "WS-ROW", owner_id=world.a.user_id, visibility="workspace")
    _prep(world.b, world.meeting_id)
    _set_stored_enrichment(world.meeting_id, "SUMMARY-QUOTING-WS-ROW")

    # Control: nothing dropped, the stored summary is served.
    assert _get(world.bystander, world.meeting_id)["enrichment"]["available"] is True

    _narrow(world, "risks", ids["risks"], owner_id=world.a.user_id)
    for member in (world.bystander, world.b):
        enrichment = _get(member, world.meeting_id)["enrichment"]
        assert enrichment == {
            "available": False,
            "summary": None,
            "error_code": "evidence_unavailable",
        }
    # A can still read every stored row, so A keeps the summary.
    assert _get(world.a, world.meeting_id)["enrichment"]["summary"] == "SUMMARY-QUOTING-WS-ROW"


@pytest.mark.parametrize("refresh", [False, True], ids=["create", "refresh"])
def test_idempotent_replay_drops_rows_narrowed_since_the_first_call(
    world: _World, refresh: bool
) -> None:
    _link(world, world.b, world.entity_id)
    ids = _seed(world, "WS-ROW", owner_id=world.a.user_id, visibility="workspace")
    if refresh:
        _prep(world.a, world.meeting_id)
    key = str(uuid4())
    path = f"/api/v1/meetings/{world.meeting_id}/prep" + ("/refresh" if refresh else "")
    first = world.b.client.post(path, headers=_headers(world.b.token, key))
    assert first.status_code == 201, first.text
    _assert_view(first.json(), {"WS-ROW"})

    _narrow_all(world, ids, owner_id=world.a.user_id)
    replay = world.b.client.post(path, headers=_headers(world.b.token, key))
    assert replay.status_code == 201, replay.text
    assert replay.json()["id"] == first.json()["id"]
    _assert_view(replay.json(), set())


def test_row_narrowed_to_an_explicit_share_reaches_only_its_grantee(world: _World) -> None:
    _link(world, world.b, world.entity_id)
    ids = _seed(world, "WS-ROW", owner_id=world.bystander.user_id, visibility="workspace")
    _prep(world.b, world.meeting_id)
    now = datetime.now(UTC)
    with engine.begin() as connection:
        for key in _NARROWABLE:
            table = "notes" if key == "decision_notes" else key
            connection.execute(
                text(
                    f"UPDATE {table} SET visibility = 'shared_explicitly' "  # noqa: S608
                    "WHERE workspace_id = :workspace_id AND id = :id"
                ),
                {"workspace_id": world.workspace_id, "id": ids[key]},
            )
            connection.execute(
                text(
                    """
                    INSERT INTO resource_grants (
                        id, workspace_id, grantee_account_id, resource_type,
                        resource_id, actions, granted_by, created_at
                    ) VALUES (
                        :id, :workspace_id, :grantee, :resource_type,
                        :resource_id, ARRAY['read'], :granted_by, :now
                    )
                    """
                ),
                {
                    "id": uuid4(),
                    "workspace_id": world.workspace_id,
                    "grantee": world.a.account_id,
                    "resource_type": table,
                    "resource_id": ids[key],
                    "granted_by": world.bystander.user_id,
                    "now": now,
                },
            )

    _assert_view(_get(world.b, world.meeting_id), set())
    _assert_view(_get(world.a, world.meeting_id), {"WS-ROW"})


@pytest.mark.parametrize("refresh", [False, True], ids=["create", "refresh"])
def test_enrichment_summarizes_exactly_the_stored_snapshot(
    world: _World, monkeypatch: pytest.MonkeyPatch, refresh: bool
) -> None:
    """A workspace row written while the enrichment run is in flight never
    reaches the model: the run summarizes the content its create/refresh
    stores, so the read-time re-check (which only sees stored rows) covers
    everything the summary can quote."""
    import ecc.domains.attention.meeting_prep_tools as tools_mod

    _link(world, world.b, world.entity_id)
    _seed(world, "WS-KEPT", owner_id=world.a.user_id, visibility="workspace")
    if refresh:
        _prep(world.a, world.meeting_id)
    late: dict[str, UUID] = {}
    real_pinned = tools_mod.pinned_pack_content

    def racing_pinned(*args: Any, **kwargs: Any) -> Any:
        if not late:
            late.update(_seed(world, "LATE-ROW", owner_id=world.a.user_id, visibility="workspace"))
        return real_pinned(*args, **kwargs)

    monkeypatch.setattr(tools_mod, "pinned_pack_content", racing_pinned)
    prompts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        prompts.append(request.content.decode())
        body = json.dumps(
            {
                "model": "m",
                "created_at": "now",
                "response": json.dumps(
                    {"summary_text": "Partner review.", "cited_evidence_ids": []}
                ),
                "done": True,
                "eval_count": 12,
                "prompt_eval_count": 40,
            }
        )
        return httpx.Response(
            200, content=(body + "\n").encode(), headers={"content-type": "application/x-ndjson"}
        )

    adapter = OllamaAdapter(transport=httpx.MockTransport(handler))
    monkeypatch.setenv("ECC_MEETING_PREP_AI_ENRICHMENT_ENABLED", "true")
    get_settings.cache_clear()
    app.dependency_overrides[get_ollama_adapter] = lambda: adapter
    try:
        created = _prep(world.b, world.meeting_id, refresh=refresh)
    finally:
        app.dependency_overrides.pop(get_ollama_adapter, None)

    assert late, "the race hook never ran"
    assert created["enrichment"]["available"] is True, created["enrichment"]
    prompt_text = "".join(prompts)
    assert "WS-KEPT" in prompt_text
    assert "LATE-ROW" not in prompt_text
    assert "LATE-ROW" not in json.dumps(_stored(world.meeting_id))


def _summary_adapter(summary: str) -> OllamaAdapter:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.dumps(
            {
                "model": "m",
                "created_at": "now",
                "response": json.dumps({"summary_text": summary, "cited_evidence_ids": []}),
                "done": True,
                "eval_count": 12,
                "prompt_eval_count": 40,
            }
        )
        return httpx.Response(
            200, content=(body + "\n").encode(), headers={"content-type": "application/x-ndjson"}
        )

    return OllamaAdapter(transport=httpx.MockTransport(handler))


@pytest.mark.parametrize("refresh", [False, True], ids=["create", "refresh"])
def test_replay_withholds_summary_when_a_stored_row_outside_the_cached_view_narrows(
    world: _World, monkeypatch: pytest.MonkeyPatch, refresh: bool
) -> None:
    """The cached replay body is the caller's capped merged view: B's own
    newer private notes push the stored workspace note out of it. Narrowing
    that stored note must still withhold the summary on replay, as on GET."""
    _link(world, world.b, world.entity_id)
    ws = _seed(world, "WS-ROW", owner_id=world.a.user_id, visibility="workspace")
    for i in range(10):  # 20 newer notes: B's merged notes section is full
        _seed(world, f"B-PRIVATE-{i}", owner_id=world.b.user_id, visibility="private")
    if refresh:
        _prep(world.a, world.meeting_id)

    monkeypatch.setenv("ECC_MEETING_PREP_AI_ENRICHMENT_ENABLED", "true")
    get_settings.cache_clear()
    adapter = _summary_adapter("Quotes WS-ROW note.")
    app.dependency_overrides[get_ollama_adapter] = lambda: adapter
    key = str(uuid4())
    path = f"/api/v1/meetings/{world.meeting_id}/prep" + ("/refresh" if refresh else "")
    try:
        first = world.b.client.post(path, headers=_headers(world.b.token, key))
    finally:
        app.dependency_overrides.pop(get_ollama_adapter, None)
    assert first.status_code == 201, first.text
    assert first.json()["enrichment"]["available"] is True
    assert "WS-ROW" not in _section_text(first.json(), "notes")
    assert "WS-ROW" in json.dumps(_stored(world.meeting_id)["notes"])

    # Control: nothing narrowed yet, the replay keeps the summary.
    again = world.b.client.post(path, headers=_headers(world.b.token, key))
    assert again.json()["enrichment"]["available"] is True

    _narrow(world, "notes", ws["notes"], owner_id=world.a.user_id)
    assert _get(world.b, world.meeting_id)["enrichment"]["available"] is False
    replay = world.b.client.post(path, headers=_headers(world.b.token, key))
    assert replay.status_code == 201, replay.text
    assert replay.json()["enrichment"] == {
        "available": False,
        "summary": None,
        "error_code": "evidence_unavailable",
    }
