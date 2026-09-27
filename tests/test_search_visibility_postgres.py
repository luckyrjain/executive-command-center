"""`GET /api/v1/search` honors resource visibility (Security Remediation
Spec A, T21c / plan note N31).

Before this fix every candidate branch in `ecc.search` filtered by
`workspace_id` only, so any member -- including a workspace owner/admin, who
gets no special read on another member's private rows under `authz` -- could
search for another member's `private` (or ungranted `shared_explicitly`)
task, commitment, note, meeting, calendar event or risk and receive its id,
title and a body snippet. Search now applies the same
`authz.visible_resource_filter_sql` predicate every list endpoint uses
(owner, active grantee of a `shared_explicitly` row, or `workspace`-visible),
unflagged: `shared_explicitly` rows are reachable today without
`ECC_PERSONAL_DATA_ISOLATION` (`POST /sharing/grants` with
`narrow_visibility=true`), so this was a pre-existing leak for every
non-workspace row, not a Spec A flag-on-only one.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from gmail_sync_fixtures import GmailSyncWorld, build_gmail_sync_world
from identity_fixtures import create_identity
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ecc.config import get_settings
from ecc.database import engine
from ecc.main import app

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

ENTITY_TYPES = ("task", "commitment", "note", "meeting", "calendar_event", "risk")

# Row "kinds" seeded per entity type, all owned by member A.
#  - private: visibility='private'
#  - private_granted: 'private' *with* an active grant to G -- authz step 3
#    denies private rows regardless of grants, so G must NOT see it
#  - shared_granted: 'shared_explicitly' with an active grant to G
#  - shared_revoked / shared_expired: 'shared_explicitly', G's grant revoked /
#    expired
#  - shared_ungranted: 'shared_explicitly' with no grant at all
#  - workspace: visibility='workspace'
KINDS = (
    "private",
    "private_granted",
    "shared_granted",
    "shared_revoked",
    "shared_expired",
    "shared_ungranted",
    "workspace",
)
_VISIBILITY = {
    "private": "private",
    "private_granted": "private",
    "shared_granted": "shared_explicitly",
    "shared_revoked": "shared_explicitly",
    "shared_expired": "shared_explicitly",
    "shared_ungranted": "shared_explicitly",
    "workspace": "workspace",
}
_GRANT_STATE = {
    "private_granted": "active",
    "shared_granted": "active",
    "shared_revoked": "revoked",
    "shared_expired": "expired",
}
_TABLE = {
    "task": "tasks",
    "commitment": "commitments",
    "note": "notes",
    "meeting": "meetings",
    "calendar_event": "calendar_events",
    "risk": "risks",
}

# Who is expected to see which kinds.
EXPECTED_VISIBLE = {
    "row_owner": set(KINDS),
    "grantee": {"shared_granted", "workspace"},
    "bystander": {"workspace"},
    "workspace_owner": {"workspace"},
    "workspace_admin": {"workspace"},
    "viewer": {"workspace"},
}


@dataclass
class _World:
    workspace_id: UUID
    user_ids: dict[str, UUID]
    account_ids: dict[str, UUID]
    tokens: dict[str, str]
    suffix: str
    # rows[entity_type][kind] -> id
    rows: dict[str, dict[str, UUID]] = field(default_factory=dict)
    clients: dict[str, TestClient] = field(default_factory=dict)

    def query(self, entity_type: str) -> str:
        return f"zq{self.suffix}{entity_type.replace('_', '')}"

    def title(self, entity_type: str, kind: str) -> str:
        return f"{self.query(entity_type)} {kind.replace('_', ' ')} heading"

    def secret(self, entity_type: str, kind: str) -> str:
        return f"secretbody{self.suffix}{entity_type.replace('_', '')}{kind.replace('_', '')}"

    def client(self, who: str) -> TestClient:
        if who not in self.clients:
            client = TestClient(app)
            client.cookies.set("ecc_session", self.tokens[who])
            self.clients[who] = client
        return self.clients[who]


def _insert_session(conn: Connection, *, workspace_id: UUID, user_id: UUID, now: datetime) -> str:
    token = f"session-{uuid4()}"
    conn.execute(
        text(
            """
            INSERT INTO sessions (id, workspace_id, user_id, token_hash, expires_at, last_seen_at)
            VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :now)
            """
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
    return token


def _insert_row(
    conn: Connection,
    *,
    entity_type: str,
    workspace_id: UUID,
    owner_id: UUID,
    title: str,
    body: str,
    visibility: str,
    now: datetime,
    calendar_event_id: UUID | None = None,
) -> UUID:
    row_id = uuid4()
    params: dict[str, Any] = {
        "id": row_id,
        "workspace_id": workspace_id,
        "owner_id": owner_id,
        "title": title,
        "body": body,
        "visibility": visibility,
        "now": now,
        "starts_at": now + timedelta(days=1),
        "ends_at": now + timedelta(days=1, hours=1),
        "calendar_event_id": calendar_event_id,
    }
    statements = {
        "task": """
            INSERT INTO tasks (
                id, workspace_id, owner_id, title, description, status, manual_priority,
                pinned, source_type, created_by, updated_by, created_at, updated_at, version,
                visibility
            ) VALUES (
                :id, :workspace_id, :owner_id, :title, :body, 'planned', 'medium',
                false, 'local', :owner_id, :owner_id, :now, :now, 1, :visibility
            )
        """,
        "commitment": """
            INSERT INTO commitments (
                id, workspace_id, owner_id, summary, description, direction, status,
                due_at, importance, confidence, pinned, created_by, updated_by,
                created_at, updated_at, version, visibility
            ) VALUES (
                :id, :workspace_id, :owner_id, :title, :body, 'made_by_me', 'active',
                :starts_at, 'medium', 0.5, false, :owner_id, :owner_id,
                :now, :now, 1, :visibility
            )
        """,
        "note": """
            INSERT INTO notes (
                id, workspace_id, owner_id, title, body, note_type, source_type,
                created_by, updated_by, created_at, updated_at, version, visibility
            ) VALUES (
                :id, :workspace_id, :owner_id, :title, :body, 'general', 'local',
                :owner_id, :owner_id, :now, :now, 1, :visibility
            )
        """,
        "meeting": """
            INSERT INTO meetings (
                id, workspace_id, calendar_event_id, title, standalone_starts_at,
                standalone_ends_at, standalone_timezone, status, agenda, created_by,
                updated_by, created_at, updated_at, version, owner_id, visibility
            ) VALUES (
                :id, :workspace_id, :calendar_event_id, :title,
                CASE WHEN CAST(:calendar_event_id AS uuid) IS NULL
                     THEN CAST(:starts_at AS timestamptz) END,
                CASE WHEN CAST(:calendar_event_id AS uuid) IS NULL
                     THEN CAST(:ends_at AS timestamptz) END,
                CASE WHEN CAST(:calendar_event_id AS uuid) IS NULL THEN 'UTC' END,
                'planned', :body, :owner_id, :owner_id, :now, :now, 1, :owner_id, :visibility
            )
        """,
        "calendar_event": """
            INSERT INTO calendar_events (
                id, workspace_id, external_source, title, description, starts_at, ends_at,
                all_day, timezone, status, source_authoritative, created_by, updated_by,
                created_at, updated_at, version, owner_id, visibility
            ) VALUES (
                :id, :workspace_id, 'local', :title, :body, :starts_at, :ends_at,
                false, 'UTC', 'confirmed', true, :owner_id, :owner_id,
                :now, :now, 1, :owner_id, :visibility
            )
        """,
        "risk": """
            INSERT INTO risks (
                id, workspace_id, owner_id, description, mitigation, probability, impact,
                status, pinned, created_by, updated_by, created_at, updated_at, version,
                visibility
            ) VALUES (
                :id, :workspace_id, :owner_id, :title, :body, 3, 3,
                'identified', false, :owner_id, :owner_id, :now, :now, 1, :visibility
            )
        """,
    }
    conn.execute(text(statements[entity_type]), params)
    return row_id


def _insert_grant(
    conn: Connection,
    *,
    workspace_id: UUID,
    grantee_account_id: UUID,
    granted_by: UUID,
    resource_type: str,
    resource_id: UUID,
    state: str,
    now: datetime,
) -> None:
    conn.execute(
        text(
            """
            INSERT INTO resource_grants (
                id, workspace_id, grantee_account_id, resource_type, resource_id,
                actions, granted_by, expires_at, revoked_at, created_at
            ) VALUES (
                :id, :workspace_id, :grantee, :resource_type, :resource_id,
                ARRAY['read'], :granted_by, :expires_at, :revoked_at, :now
            )
            """
        ),
        {
            "id": uuid4(),
            "workspace_id": workspace_id,
            "grantee": grantee_account_id,
            "resource_type": resource_type,
            "resource_id": resource_id,
            "granted_by": granted_by,
            "expires_at": now - timedelta(hours=1) if state == "expired" else None,
            "revoked_at": now if state == "revoked" else None,
            "now": now,
        },
    )


_ROLES = {
    # The workspace owner joins first, so it is the workspace's original user.
    "workspace_owner": "owner",
    "workspace_admin": "admin",
    "row_owner": "member",
    "grantee": "member",
    "bystander": "member",
    "viewer": "viewer",
}


@pytest.fixture
def world() -> Iterator[_World]:
    workspace_id = uuid4()
    now = datetime.now(UTC)
    suffix = uuid4().hex[:10]
    user_ids = {who: uuid4() for who in _ROLES}
    account_ids: dict[str, UUID] = {}
    tokens: dict[str, str] = {}
    world = _World(
        workspace_id=workspace_id,
        user_ids=user_ids,
        account_ids=account_ids,
        tokens=tokens,
        suffix=suffix,
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Search Visibility', 'UTC', :now)"
            ),
            {"id": workspace_id, "now": now},
        )
        for offset, (who, role) in enumerate(_ROLES.items()):
            account_ids[who] = create_identity(
                conn,
                workspace_id=workspace_id,
                user_id=user_ids[who],
                role=role,
                now=now + timedelta(seconds=offset),
            )
            tokens[who] = _insert_session(
                conn, workspace_id=workspace_id, user_id=user_ids[who], now=now
            )
        for entity_type in ENTITY_TYPES:
            world.rows[entity_type] = {}
            for kind in KINDS:
                row_id = _insert_row(
                    conn,
                    entity_type=entity_type,
                    workspace_id=workspace_id,
                    owner_id=user_ids["row_owner"],
                    title=world.title(entity_type, kind),
                    body=world.secret(entity_type, kind),
                    visibility=_VISIBILITY[kind],
                    now=now,
                )
                world.rows[entity_type][kind] = row_id
                if kind in _GRANT_STATE:
                    _insert_grant(
                        conn,
                        workspace_id=workspace_id,
                        grantee_account_id=account_ids["grantee"],
                        granted_by=user_ids["row_owner"],
                        resource_type=_TABLE[entity_type],
                        resource_id=row_id,
                        state=_GRANT_STATE[kind],
                        now=now,
                    )
    try:
        yield world
    finally:
        for client in world.clients.values():
            client.close()
        with engine.begin() as conn:
            for table in (
                "resource_grants",
                "meetings",
                "calendar_events",
                "tasks",
                "commitments",
                "notes",
                "risks",
                "audit_events",
                "sessions",
                "workspace_memberships",
                "users",
            ):
                conn.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :id"),  # noqa: S608
                    {"id": workspace_id},
                )
            conn.execute(text("DELETE FROM workspaces WHERE id = :id"), {"id": workspace_id})
            conn.execute(
                text("DELETE FROM accounts WHERE id = ANY(:ids)"),
                {"ids": list(account_ids.values())},
            )


def _search(client: TestClient, q: str, **params: Any) -> dict[str, Any]:
    response = client.get("/api/v1/search", params={"q": q, "limit": 100, **params})
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _hit_kinds(world: _World, entity_type: str, body: dict[str, Any]) -> set[str]:
    by_id = {row_id: kind for kind, row_id in world.rows[entity_type].items()}
    return {by_id[UUID(item["entity_id"])] for item in body["items"]}


@pytest.fixture(params=["off", "on"])
def isolation_flag(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[str]:
    """Search honors visibility regardless of `ECC_PERSONAL_DATA_ISOLATION`."""
    monkeypatch.setenv("ECC_PERSONAL_DATA_ISOLATION", "true" if request.param == "on" else "false")
    get_settings.cache_clear()
    try:
        yield request.param
    finally:
        monkeypatch.undo()
        get_settings.cache_clear()


@pytest.mark.parametrize("who", list(EXPECTED_VISIBLE))
@pytest.mark.parametrize("entity_type", ENTITY_TYPES)
def test_search_returns_only_rows_the_caller_can_read(
    world: _World, isolation_flag: str, entity_type: str, who: str
) -> None:
    body = _search(world.client(who), world.query(entity_type), **{"types[]": entity_type})

    assert _hit_kinds(world, entity_type, body) == EXPECTED_VISIBLE[who]
    assert body["next_cursor"] is None
    # Neither the title nor any snippet/highlight of a hidden row appears
    # anywhere in the response.
    raw = str(body)
    for kind in set(KINDS) - EXPECTED_VISIBLE[who]:
        assert world.title(entity_type, kind) not in raw
        assert world.secret(entity_type, kind) not in raw
        assert str(world.rows[entity_type][kind]) not in raw


@pytest.mark.parametrize("who", ["bystander", "workspace_owner", "grantee"])
def test_hidden_row_body_text_never_matches(world: _World, who: str) -> None:
    """Searching for text that only occurs in a hidden row's body (a
    full-text match) never returns that row or its snippet. (Other,
    readable rows may still fuzzy-match on shared trigrams.)"""
    for entity_type in ENTITY_TYPES:
        for kind in ("private", "shared_ungranted", "shared_revoked"):
            secret = world.secret(entity_type, kind)
            body = _search(world.client(who), secret)
            ids = {UUID(item["entity_id"]) for item in body["items"]}
            assert world.rows[entity_type][kind] not in ids
            assert secret not in str(body)
            assert "full_text" not in {f for item in body["items"] for f in item["matched_fields"]}


def test_pagination_does_not_reveal_hidden_rows(world: _World) -> None:
    """A page of size 1 must not report a next page that exists only because
    of rows the caller cannot read (a count/existence side channel)."""
    body = _search(world.client("bystander"), world.query("task"), limit=1, **{"types[]": "task"})
    assert [UUID(item["entity_id"]) for item in body["items"]] == [world.rows["task"]["workspace"]]
    assert body["next_cursor"] is None

    # The grantee sees exactly two rows, paging one at a time.
    client = world.client("grantee")
    seen: list[UUID] = []
    cursor: str | None = None
    for _ in range(5):
        params: dict[str, Any] = {"limit": 1, "types[]": "task"}
        if cursor:
            params["cursor"] = cursor
        body = _search(client, world.query("task"), **params)
        seen.extend(UUID(item["entity_id"]) for item in body["items"])
        cursor = body["next_cursor"]
        if cursor is None:
            break
    assert set(seen) == {world.rows["task"]["shared_granted"], world.rows["task"]["workspace"]}
    assert len(seen) == 2


def test_multi_type_search_filters_every_branch(world: _World) -> None:
    """Default (all-types) search: every branch applies visibility, not
    just the one a `types[]` filter selects."""
    q = f"zq{world.suffix}"
    body = _search(world.client("bystander"), q)
    expected = {world.rows[entity_type]["workspace"] for entity_type in ENTITY_TYPES}
    assert {UUID(item["entity_id"]) for item in body["items"]} == expected

    owner_body = _search(world.client("row_owner"), q)
    assert len(owner_body["items"]) == len(ENTITY_TYPES) * len(KINDS)


def test_meeting_does_not_leak_linked_private_calendar_event_time(world: _World) -> None:
    """A workspace-visible meeting linked to A's private calendar event must
    not surface that event's `starts_at` as the meeting's
    `timestamp_context` to someone who cannot read the event -- the same
    rule `calendar.events.get_calendar_event_summary` applies to the
    meetings API."""
    now = datetime.now(UTC)
    with engine.begin() as conn:
        event_id = _insert_row(
            conn,
            entity_type="calendar_event",
            workspace_id=world.workspace_id,
            owner_id=world.user_ids["row_owner"],
            title=f"hiddenevent{world.suffix}",
            body="private event",
            visibility="private",
            now=now,
        )
        meeting_id = _insert_row(
            conn,
            entity_type="meeting",
            workspace_id=world.workspace_id,
            owner_id=world.user_ids["row_owner"],
            title=f"linkedmeeting{world.suffix}",
            body="workspace meeting linked to a private event",
            visibility="workspace",
            now=now,
            calendar_event_id=event_id,
        )

    def _timestamp(who: str) -> str | None:
        body = _search(world.client(who), f"linkedmeeting{world.suffix}", **{"types[]": "meeting"})
        (item,) = [i for i in body["items"] if UUID(i["entity_id"]) == meeting_id]
        value: str | None = item["timestamp_context"]
        return value

    assert _timestamp("row_owner") is not None
    assert _timestamp("bystander") is None


def test_inactive_member_gets_no_results(world: _World) -> None:
    """A suspended member whose session is still live reads nothing --
    `visible_resource_filter_sql` denies every row without an active
    membership, like every list endpoint."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE workspace_memberships SET status = 'suspended' "
                "WHERE workspace_id = :ws AND users_id = :user_id"
            ),
            {"ws": world.workspace_id, "user_id": world.user_ids["bystander"]},
        )
    body = _search(world.client("bystander"), world.query("task"))
    assert body["items"] == []


# --- email-derived private task (Spec A flag on) --------------------------------


@pytest.fixture
def gmail_world_on() -> Iterator[GmailSyncWorld]:
    with build_gmail_sync_world(
        env={"ECC_PERSONAL_DATA_ISOLATION": "true"}, bystander=True
    ) as gmail_world:
        yield gmail_world


def test_email_derived_private_task_not_searchable_by_other_members(
    gmail_world_on: GmailSyncWorld,
) -> None:
    """A's Gmail sync (isolation on) produced a private
    `email_action_detected` recommendation; confirming it yields a task
    private to A carrying the recommendation's content. B (another Gmail
    member) and the bystander workspace owner must not find that task by
    its title or its email-derived description."""
    world = gmail_world_on
    (recommendation_id, *_) = world.a.recommendation_ids
    with engine.begin() as conn:
        rec = (
            conn.execute(
                text(
                    "SELECT proposed_fields, rationale, owner_id, visibility "
                    "FROM recommendations WHERE id = :id"
                ),
                {"id": recommendation_id},
            )
            .mappings()
            .one()
        )
        assert (rec["owner_id"], rec["visibility"]) == (world.a.user_id, "private")
        marker = f"emailtask{uuid4().hex[:10]}"
        proposed = rec["proposed_fields"] or {}
        title = f"{marker} {proposed.get('title') or 'Reply to the request'}"
        task_id = _insert_row(
            conn,
            entity_type="task",
            workspace_id=world.workspace_id,
            owner_id=world.a.user_id,
            title=title,
            body=f"{marker}body {rec['rationale']}",
            visibility="private",
            now=datetime.now(UTC),
        )

    def _ids(user_id: UUID, q: str) -> set[UUID]:
        client, _ = world.harness.client_for(world.workspace_id, user_id)
        response = client.get("/api/v1/search", params={"q": q, "limit": 100})
        assert response.status_code == 200, response.text
        return {UUID(item["entity_id"]) for item in response.json()["items"]}

    assert world.bystander_user_id is not None
    for q in (marker, f"{marker}body"):
        assert task_id in _ids(world.a.user_id, q)
        for other in (world.b.user_id, world.bystander_user_id):
            assert task_id not in _ids(other, q)
