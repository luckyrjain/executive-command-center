"""Authorization before the idempotency cache for the attention domain's
`Idempotency-Key` writes.

Each of these endpoints used to read the idempotency cache right after the
membership and idempotency locks, before the locked row read and its
read/write checks. A caller who had since lost access -- the row transferred
away from them, suspended, or demoted to `viewer` -- could replay a same-key
request and get the cached success back. The cache is now read only after
the locked read (404) and write (403) checks pass, and still before the
version/state checks, so a same-key replay by a caller who keeps access
still gets the cached response even though its own write already moved the
row on.

Meeting prep authorizes via the meeting, plan blocks via the plan, risk
reviews via the risk and attention feedback via its item; `create_prep` and
`refresh_prep` are covered on both their single-transaction path and the
multi-transaction AI-enrichment path.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from lock_race_support import RaceWorld, headers, race_world
from sqlalchemy import Connection, text

from ecc.config import get_settings
from ecc.database import engine
from ecc.domains.attention import meeting_prep as meeting_prep_module
from ecc.domains.attention.meeting_prep import EnrichmentOut
from ecc.main import app

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_ENRICHMENT_ENV = "ECC_MEETING_PREP_AI_ENRICHMENT_ENABLED"

_SIDE_EFFECT_TABLES = (
    "audit_events",
    "event_outbox",
    "idempotency_records",
    "plans",
    "plan_blocks",
    "risk_reviews",
    "waiting_links",
    "meeting_participants",
    "meeting_packs",
    "attention_feedback",
)


@pytest.fixture
def world() -> Iterator[RaceWorld]:
    with race_world(
        "Attention Idempotent Replay Authz",
        (
            "attention_feedback",
            "attention_items",
            "meeting_packs",
            "meeting_participants",
            "meetings",
            "ai_runs",
            "risk_reviews",
            "risks",
            "waiting_links",
            "pkos_nodes",
            "plan_blocks",
            "plans",
        ),
    ) as w:
        yield w


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    yield
    get_settings.cache_clear()


def _token_for(w: RaceWorld, user_id: UUID) -> str:
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                "expires_at, last_seen_at) "
                "VALUES (:id, :ws, :user_id, :token_hash, :expires_at, :now)"
            ),
            {
                "id": uuid4(),
                "ws": w.ws,
                "user_id": user_id,
                "token_hash": sha256(token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "now": now,
            },
        )
    return token


def _set_enrichment(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> None:
    """Pins the meeting-prep enrichment flag; when on, stubs the model call
    so nothing reaches Ollama."""
    monkeypatch.setenv(_ENRICHMENT_ENV, "true" if enabled else "false")
    get_settings.cache_clear()
    if enabled:
        monkeypatch.setattr(meeting_prep_module, "_resolve_ollama_adapter", lambda _request: None)
        monkeypatch.setattr(
            meeting_prep_module,
            "_compute_enrichment",
            lambda *_args, **_kwargs: EnrichmentOut(
                available=False, summary=None, error_code="model_unavailable"
            ),
        )


# ---------------------------------------------------------------------------
# Seeds: each inserts the target row owned by `owner` with `visibility` and
# returns the ids the request path needs. `id` is always the row whose
# ownership decides access (the parent, for blocks/participants/packs).
# ---------------------------------------------------------------------------

Seed = Callable[[Connection, RaceWorld, UUID, str], dict[str, UUID]]


def _seed_plan(conn: Connection, w: RaceWorld, owner: UUID, visibility: str) -> dict[str, UUID]:
    plan_id, block_id = uuid4(), uuid4()
    now = datetime.now(UTC)
    today = date.today()
    conn.execute(
        text(
            "INSERT INTO plans (id, workspace_id, user_id, period_start, period_end, "
            "policy_version, capacity_minutes, created_by, updated_by, created_at, "
            "updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, :owner, :start, :end, 1, 480, :owner, :owner, :now, :now, "
            ":owner, :visibility)"
        ),
        {
            "id": plan_id,
            "ws": w.ws,
            "owner": owner,
            "visibility": visibility,
            "now": now,
            "start": today,
            "end": today + timedelta(days=6),
        },
    )
    starts = datetime.combine(today + timedelta(days=1), datetime.min.time(), UTC).replace(hour=10)
    conn.execute(
        text(
            "INSERT INTO plan_blocks (id, workspace_id, plan_id, source_type, starts_at, "
            "ends_at, rationale, created_at, updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, :plan, 'constraint', :starts, :ends, 'Replay block', "
            ":now, :now, :owner, :visibility)"
        ),
        {
            "id": block_id,
            "ws": w.ws,
            "plan": plan_id,
            "starts": starts,
            "ends": starts + timedelta(hours=1),
            "now": now,
            "owner": owner,
            "visibility": visibility,
        },
    )
    return {"id": plan_id, "block": block_id}


def _seed_risk(conn: Connection, w: RaceWorld, owner: UUID, visibility: str) -> dict[str, UUID]:
    risk_id = uuid4()
    now = datetime.now(UTC)
    conn.execute(
        text(
            "INSERT INTO risks (id, workspace_id, description, probability, impact, "
            "owner_id, created_by, updated_by, created_at, updated_at, visibility) "
            "VALUES (:id, :ws, 'Replay risk', 3, 3, :owner, :owner, :owner, :now, :now, "
            ":visibility)"
        ),
        {"id": risk_id, "ws": w.ws, "owner": owner, "visibility": visibility, "now": now},
    )
    return {"id": risk_id}


def _seed_node(conn: Connection, w: RaceWorld, name: str) -> UUID:
    """A workspace-visible entity owned by A, readable by every member."""
    node_id = uuid4()
    now = datetime.now(UTC)
    conn.execute(
        text(
            "INSERT INTO pkos_nodes (id, workspace_id, node_type, canonical_name, "
            "created_at, updated_at, owner_id) "
            "VALUES (:id, :ws, 'person', :name, :now, :now, :a)"
        ),
        {"id": node_id, "ws": w.ws, "name": name, "a": w.a, "now": now},
    )
    return node_id


def _seed_waiting_link(
    conn: Connection, w: RaceWorld, owner: UUID, visibility: str
) -> dict[str, UUID]:
    node_id, link_id = _seed_node(conn, w, "Replay Counterparty"), uuid4()
    now = datetime.now(UTC)
    conn.execute(
        text(
            "INSERT INTO waiting_links (id, workspace_id, subject_type, subject_id, "
            "counterparty_entity_id, direction, since_at, created_by, updated_by, "
            "created_at, updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, 'knowledge_entity', :node, :node, 'waiting_on_them', "
            ":now, :owner, :owner, :now, :now, :owner, :visibility)"
        ),
        {
            "id": link_id,
            "ws": w.ws,
            "node": node_id,
            "owner": owner,
            "visibility": visibility,
            "now": now,
        },
    )
    return {"id": link_id}


def _seed_meeting(conn: Connection, w: RaceWorld, owner: UUID, visibility: str) -> dict[str, UUID]:
    meeting_id = uuid4()
    now = datetime.now(UTC)
    conn.execute(
        text(
            """
            INSERT INTO meetings (
                id, workspace_id, title, standalone_starts_at, standalone_ends_at,
                standalone_timezone, status, agenda, created_by, updated_by,
                created_at, updated_at, version, owner_id, visibility
            ) VALUES (
                :id, :ws, 'Replay review', :starts_at, :ends_at, 'UTC',
                'planned', 'Replay agenda', :owner, :owner, :now, :now, 1, :owner, :visibility
            )
            """
        ),
        {
            "id": meeting_id,
            "ws": w.ws,
            "owner": owner,
            "visibility": visibility,
            "starts_at": now + timedelta(days=1),
            "ends_at": now + timedelta(days=1, hours=1),
            "now": now,
        },
    )
    return {"id": meeting_id, "entity": _seed_node(conn, w, "Replay Attendee")}


def _seed_attention_item(
    conn: Connection, w: RaceWorld, owner: UUID, visibility: str
) -> dict[str, UUID]:
    item_id = uuid4()
    now = datetime.now(UTC)
    conn.execute(
        text(
            """
            INSERT INTO attention_items (
                id, workspace_id, entity_type, entity_id, source_entity_version,
                score, confidence, factors, explanation, generated_at, expires_at,
                pinned, policy_version, owner_id, visibility
            ) VALUES (
                :id, :ws, 'task', :entity_id, 1, 90, 1.0,
                '[]'::jsonb, 'Replay item', :now, :expires_at,
                false, 1, :owner, :visibility
            )
            """
        ),
        {
            "id": item_id,
            "ws": w.ws,
            "entity_id": uuid4(),
            "now": now,
            "expires_at": now + timedelta(minutes=30),
            "owner": owner,
            "visibility": visibility,
        },
    )
    return {"id": item_id}


def _v1(_: dict[str, UUID]) -> dict[str, Any]:
    return {"expected_version": 1}


def _move_body(_: dict[str, UUID]) -> dict[str, Any]:
    starts = datetime.combine(date.today() + timedelta(days=1), datetime.min.time(), UTC)
    starts = starts.replace(hour=14)
    return {
        "expected_version": 1,
        "starts_at": starts.isoformat(),
        "ends_at": (starts + timedelta(hours=1)).isoformat(),
    }


def _no_body(_: dict[str, UUID]) -> dict[str, Any] | None:
    return None


@dataclass(frozen=True)
class Case:
    table: str  # the row whose ownership decides access
    seed: Seed
    method: str
    path: str  # formatted with the seed's ids
    body: Callable[[dict[str, UUID]], dict[str, Any] | None]
    not_found: str
    ok_status: int = 200
    needs_pack: bool = False  # refresh_prep: create a pack first, under another key
    enrichment: bool = False


CASES: dict[str, Case] = {
    "plan_accept": Case(
        "plans", _seed_plan, "POST", "/api/v1/plans/{id}/accept", _v1, "PLAN_NOT_FOUND"
    ),
    "plan_supersede": Case(
        "plans", _seed_plan, "POST", "/api/v1/plans/{id}/supersede", _v1, "PLAN_NOT_FOUND"
    ),
    # Replan creates a new plan version; the replay is authorized against the
    # plan the caller targeted, not the one the original call created.
    "plan_replan": Case(
        "plans",
        _seed_plan,
        "POST",
        "/api/v1/plans/{id}/propose",
        _v1,
        "PLAN_NOT_FOUND",
        ok_status=201,
    ),
    "plan_block_move": Case(
        "plans",
        _seed_plan,
        "POST",
        "/api/v1/plans/{id}/blocks/{block}/move",
        _move_body,
        "PLAN_NOT_FOUND",
    ),
    # A replay of a successful remove finds the block gone; it is authorized
    # against (and then served for) the plan.
    "plan_block_remove": Case(
        "plans",
        _seed_plan,
        "POST",
        "/api/v1/plans/{id}/blocks/{block}/remove",
        _v1,
        "PLAN_NOT_FOUND",
    ),
    "risk_review": Case(
        "risks",
        _seed_risk,
        "POST",
        "/api/v1/risks/{id}/review",
        lambda _: {"expected_version": 1, "outcome": "no_change"},
        "RISK_NOT_FOUND",
        ok_status=201,
    ),
    "waiting_patch": Case(
        "waiting_links",
        _seed_waiting_link,
        "PATCH",
        "/api/v1/waiting/{id}",
        lambda _: {"expected_version": 1, "note": "replay probe"},
        "WAITING_LINK_NOT_FOUND",
    ),
    # A direction change supersedes the link with a new row.
    "waiting_patch_direction": Case(
        "waiting_links",
        _seed_waiting_link,
        "PATCH",
        "/api/v1/waiting/{id}",
        lambda _: {"expected_version": 1, "direction": "waiting_on_me"},
        "WAITING_LINK_NOT_FOUND",
    ),
    "meeting_add_participant": Case(
        "meetings",
        _seed_meeting,
        "POST",
        "/api/v1/meetings/{id}/participants",
        lambda ids: {"entity_id": str(ids["entity"])},
        "MEETING_NOT_FOUND",
        ok_status=201,
    ),
    "meeting_create_prep": Case(
        "meetings",
        _seed_meeting,
        "POST",
        "/api/v1/meetings/{id}/prep",
        _no_body,
        "MEETING_NOT_FOUND",
        ok_status=201,
    ),
    "meeting_create_prep_enriched": Case(
        "meetings",
        _seed_meeting,
        "POST",
        "/api/v1/meetings/{id}/prep",
        _no_body,
        "MEETING_NOT_FOUND",
        ok_status=201,
        enrichment=True,
    ),
    "meeting_refresh_prep": Case(
        "meetings",
        _seed_meeting,
        "POST",
        "/api/v1/meetings/{id}/prep/refresh",
        _no_body,
        "MEETING_NOT_FOUND",
        ok_status=201,
        needs_pack=True,
    ),
    "meeting_refresh_prep_enriched": Case(
        "meetings",
        _seed_meeting,
        "POST",
        "/api/v1/meetings/{id}/prep/refresh",
        _no_body,
        "MEETING_NOT_FOUND",
        ok_status=201,
        needs_pack=True,
        enrichment=True,
    ),
    "attention_feedback": Case(
        "attention_items",
        _seed_attention_item,
        "POST",
        "/api/v1/attention/{id}/feedback",
        lambda _: {"label": "useful", "reason": "replay probe"},
        "ATTENTION_ITEM_NOT_FOUND",
        ok_status=201,
    ),
}

# Attention feedback's membership lock already requires the `write` role,
# so a demoted caller was refused before the cache even before this fix.
_DEMOTION_CASES = [name for name in CASES if name != "attention_feedback"]


def _side_effect_counts(ws: UUID) -> dict[str, int]:
    with engine.connect() as connection:
        return {
            table: int(
                connection.execute(
                    text(f"SELECT count(*) FROM {table} WHERE workspace_id = :ws"),  # noqa: S608
                    {"ws": ws},
                ).scalar_one()
            )
            for table in _SIDE_EFFECT_TABLES
        }


def _without_request_id(body: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in body.items() if key != "request_id"}


def _send(
    client: TestClient, case: Case, ids: dict[str, UUID], request_headers: dict[str, str]
) -> Any:
    body = case.body(ids)
    return client.request(
        case.method,
        case.path.format(**ids),
        headers=request_headers,
        **({"json": body} if body is not None else {}),
    )


def _prepare(
    client: TestClient,
    case: Case,
    ids: dict[str, UUID],
    token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_enrichment(monkeypatch, enabled=False)
    if case.needs_pack:
        created = client.post(f"/api/v1/meetings/{ids['id']}/prep", headers=headers(token))
        assert created.status_code == 201, created.text
    _set_enrichment(monkeypatch, enabled=case.enrichment)


def _set_membership(w: RaceWorld, user_id: UUID, column: str, value: str) -> None:
    assert column in ("role", "status")
    with engine.begin() as connection:
        connection.execute(
            text(
                f"UPDATE workspace_memberships SET {column} = :value "  # noqa: S608
                "WHERE workspace_id = :ws AND users_id = :user_id"
            ),
            {"value": value, "ws": w.ws, "user_id": user_id},
        )


@pytest.mark.parametrize("change", ["transferred", "suspended"])
@pytest.mark.parametrize("name", list(CASES))
def test_idempotent_replay_is_authorized_before_the_cache_is_served(
    world: RaceWorld, monkeypatch: pytest.MonkeyPatch, name: str, change: str
) -> None:
    """B writes its own private row, then (still authorized) replays the same
    key and gets the cached response; once B can no longer see the row the
    same key is refused with the row's 404 and writes nothing."""
    case = CASES[name]
    with engine.begin() as connection:
        ids = case.seed(connection, world, world.b, "private")
    request_headers = headers(world.b_token)
    client = TestClient(app)
    client.cookies.set("ecc_session", world.b_token)
    try:
        _prepare(client, case, ids, world.b_token, monkeypatch)
        first = _send(client, case, ids, request_headers)
        assert first.status_code == case.ok_status, first.text

        # Still authorized: the same key replays the cached response even
        # though the row is no longer in its pre-write state.
        counts_after_first = _side_effect_counts(world.ws)
        replay = _send(client, case, ids, request_headers)
        assert replay.status_code == case.ok_status, replay.text
        assert _without_request_id(replay.json()) == _without_request_id(first.json())
        assert _side_effect_counts(world.ws) == counts_after_first

        if change == "transferred":
            # The row stays private but now belongs to C: B can't see it.
            with engine.begin() as connection:
                connection.execute(
                    text(f"UPDATE {case.table} SET owner_id = :c WHERE id = :id"),  # noqa: S608
                    {"c": world.c, "id": ids["id"]},
                )
        else:
            _set_membership(world, world.b, "status", "suspended")
        counts_before = _side_effect_counts(world.ws)
        refused = _send(client, case, ids, request_headers)
    finally:
        client.close()

    if change == "suspended" and name == "attention_feedback":
        # Its `write` role check on the membership lock refuses first.
        assert refused.status_code == 403, refused.text
    else:
        assert refused.status_code == 404, refused.text
        assert refused.json()["error"]["code"] == case.not_found
    assert _side_effect_counts(world.ws) == counts_before


@pytest.mark.parametrize("name", _DEMOTION_CASES)
def test_idempotent_replay_after_demotion_is_refused_by_the_locked_write_check(
    world: RaceWorld, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """C (`member`) writes A's workspace-visible row by role alone, then is
    demoted to `viewer`: C can still read the row, so only the locked write
    check -- which now runs before the cache -- refuses the replay."""
    case = CASES[name]
    with engine.begin() as connection:
        ids = case.seed(connection, world, world.a, "workspace")
    c_token = _token_for(world, world.c)
    request_headers = headers(c_token)
    client = TestClient(app)
    client.cookies.set("ecc_session", c_token)
    try:
        _prepare(client, case, ids, c_token, monkeypatch)
        first = _send(client, case, ids, request_headers)
        assert first.status_code == case.ok_status, first.text

        _set_membership(world, world.c, "role", "viewer")
        counts_before = _side_effect_counts(world.ws)
        refused = _send(client, case, ids, request_headers)
    finally:
        client.close()

    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "INSUFFICIENT_ROLE"
    assert _side_effect_counts(world.ws) == counts_before


def test_feedback_replay_still_served_after_its_item_is_deleted(world: RaceWorld) -> None:
    """A refresh hard-deletes items whose source closed; a same-key retry of
    feedback that already landed still gets its cached 201."""
    with engine.begin() as connection:
        ids = _seed_attention_item(connection, world, world.b, "private")
    case = CASES["attention_feedback"]
    request_headers = headers(world.b_token)
    client = TestClient(app)
    client.cookies.set("ecc_session", world.b_token)
    try:
        first = _send(client, case, ids, request_headers)
        assert first.status_code == 201, first.text
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM attention_items WHERE id = :id"), {"id": ids["id"]}
            )
        counts_before = _side_effect_counts(world.ws)
        replay = _send(client, case, ids, request_headers)
        # A fresh key against the deleted item is still a plain 404.
        fresh = _send(client, case, ids, headers(world.b_token))
    finally:
        client.close()

    assert replay.status_code == 201, replay.text
    assert _without_request_id(replay.json()) == _without_request_id(first.json())
    assert fresh.status_code == 404, fresh.text
    assert fresh.json()["error"]["code"] == "ATTENTION_ITEM_NOT_FOUND"
    assert _side_effect_counts(world.ws) == counts_before
