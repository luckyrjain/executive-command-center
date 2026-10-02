"""Authorization before the idempotency cache for the calendar event,
meeting, task and commitment PATCH and lifecycle endpoints.

Each of these took the membership and idempotency locks and then served a
cached response for a repeated `Idempotency-Key` before reading or
authorizing the target row. A caller who had since lost access -- demoted
to `viewer`, suspended, or no longer able to see the row after an ownership
transfer -- could replay the key and get the cached success back. The cache
is now read only after the locked read (404) and write (403) checks pass,
but still before the version/state checks, so a still-authorized replay of
a successful write (which finds the version already bumped) keeps getting
its cached 200.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any, Literal
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from lock_race_support import RaceWorld, headers, race_world, row_snapshot
from sqlalchemy import Connection, text

from ecc.config import get_settings
from ecc.database import engine
from ecc.main import app

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


@pytest.fixture
def world() -> Iterator[RaceWorld]:
    with race_world(
        "Core Idempotent Replay Authz",
        ("meetings", "calendar_events", "tasks", "commitments"),
    ) as w:
        yield w


def _c_token(w: RaceWorld) -> str:
    """A session for C (`member`)."""
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
                "user_id": w.c,
                "token_hash": sha256(token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "now": now,
            },
        )
    return token


_INSERTS = {
    "calendar_events": (
        "INSERT INTO calendar_events (id, workspace_id, title, starts_at, ends_at, "
        "timezone, created_by, updated_by, created_at, updated_at, owner_id, "
        "visibility, archived_at) "
        "VALUES (:id, :ws, 'Replay event', :starts, :ends, 'UTC', :owner, :owner, "
        ":now, :now, :owner, :visibility, :archived_at)"
    ),
    "meetings": (
        "INSERT INTO meetings (id, workspace_id, title, standalone_starts_at, "
        "standalone_ends_at, standalone_timezone, created_by, updated_by, created_at, "
        "updated_at, owner_id, visibility, archived_at) "
        "VALUES (:id, :ws, 'Replay meeting', :starts, :ends, 'UTC', :owner, :owner, "
        ":now, :now, :owner, :visibility, :archived_at)"
    ),
    "tasks": (
        "INSERT INTO tasks (id, workspace_id, owner_id, title, created_by, updated_by, "
        "created_at, updated_at, visibility, archived_at) "
        "VALUES (:id, :ws, :owner, 'Replay task', :owner, :owner, :now, :now, "
        ":visibility, :archived_at)"
    ),
    "commitments": (
        "INSERT INTO commitments (id, workspace_id, owner_id, summary, direction, "
        "status, created_by, updated_by, created_at, updated_at, visibility, "
        "archived_at, pre_archive_status) "
        "VALUES (:id, :ws, :owner, 'Replay commitment', 'made_by_me', :status, "
        ":owner, :owner, :now, :now, :visibility, :archived_at, :pre_archive_status)"
    ),
}


@dataclass(frozen=True)
class Case:
    table: str
    method: Literal["PATCH", "POST"]
    path: str
    not_found: str
    body: dict[str, Any] = field(default_factory=lambda: {"expected_version": 1})
    archived: bool = False
    status: str | None = None

    def seed(self, conn: Connection, w: RaceWorld, *, owner: UUID, visibility: str) -> UUID:
        row_id = uuid4()
        now = datetime.now(UTC)
        starts = now + timedelta(days=1)
        params: dict[str, Any] = {
            "id": row_id,
            "ws": w.ws,
            "owner": owner,
            "visibility": visibility,
            "now": now,
            "starts": starts,
            "ends": starts + timedelta(hours=1),
            "archived_at": now if self.archived else None,
        }
        if self.table == "commitments":
            params["status"] = self.status
            params["pre_archive_status"] = self.status if self.archived else None
        conn.execute(text(_INSERTS[self.table]), params)
        return row_id


_PATCH = {"expected_version": 1, "title": "replay probe"}
_EVENTS = "/api/v1/calendar/events/{id}"
_MEETINGS = "/api/v1/meetings/{id}"
_TASKS = "/api/v1/tasks/{id}"
_COMMITMENTS = "/api/v1/commitments/{id}"
_EVENT_404 = "CALENDAR_EVENT_NOT_FOUND"
_MEETING_404 = "MEETING_NOT_FOUND"
_TASK_404 = "TASK_NOT_FOUND"
_COMMITMENT_404 = "COMMITMENT_NOT_FOUND"

CASES: dict[str, Case] = {
    # calendar/events.py: update_calendar_event, _lifecycle
    "event_patch": Case("calendar_events", "PATCH", _EVENTS, _EVENT_404, _PATCH),
    "event_archive": Case("calendar_events", "POST", _EVENTS + "/archive", _EVENT_404),
    "event_restore": Case(
        "calendar_events", "POST", _EVENTS + "/restore", _EVENT_404, archived=True
    ),
    # scheduling/meetings.py: update_meeting, _lifecycle
    "meeting_patch": Case("meetings", "PATCH", _MEETINGS, _MEETING_404, _PATCH),
    "meeting_archive": Case("meetings", "POST", _MEETINGS + "/archive", _MEETING_404),
    "meeting_restore": Case(
        "meetings", "POST", _MEETINGS + "/restore", _MEETING_404, archived=True
    ),
    # planning/tasks.py: update_task, _lifecycle_task
    "task_patch": Case("tasks", "PATCH", _TASKS, _TASK_404, _PATCH),
    "task_complete": Case("tasks", "POST", _TASKS + "/complete", _TASK_404),
    "task_cancel": Case("tasks", "POST", _TASKS + "/cancel", _TASK_404),
    "task_archive": Case("tasks", "POST", _TASKS + "/archive", _TASK_404),
    "task_restore": Case("tasks", "POST", _TASKS + "/restore", _TASK_404, archived=True),
    # communication/commitments.py: _mutate_commitment, _lifecycle
    "commitment_patch": Case(
        "commitments",
        "PATCH",
        _COMMITMENTS,
        _COMMITMENT_404,
        {"expected_version": 1, "summary": "replay probe"},
        status="confirmed",
    ),
    "commitment_confirm": Case(
        "commitments", "POST", _COMMITMENTS + "/confirm", _COMMITMENT_404, status="detected"
    ),
    "commitment_fulfil": Case(
        "commitments", "POST", _COMMITMENTS + "/fulfil", _COMMITMENT_404, status="confirmed"
    ),
    "commitment_cancel": Case(
        "commitments", "POST", _COMMITMENTS + "/cancel", _COMMITMENT_404, status="confirmed"
    ),
    "commitment_break": Case(
        "commitments", "POST", _COMMITMENTS + "/break", _COMMITMENT_404, status="confirmed"
    ),
    "commitment_archive": Case(
        "commitments", "POST", _COMMITMENTS + "/archive", _COMMITMENT_404, status="confirmed"
    ),
    "commitment_restore": Case(
        "commitments",
        "POST",
        _COMMITMENTS + "/restore",
        _COMMITMENT_404,
        archived=True,
        status="confirmed",
    ),
}


def _side_effect_counts(ws: UUID) -> dict[str, int]:
    """Counts of the rows a write emits. `rejected_mutation_audit_middleware`
    records every rejected POST/PATCH under /api/v1/tasks as a
    `task.mutation_rejected` audit row after the request's transaction;
    that row is the expected trace of a refusal, not a side effect of the
    write, so it is left out."""
    with engine.connect() as connection:
        counts = {
            table: int(
                connection.execute(
                    text(f"SELECT count(*) FROM {table} WHERE workspace_id = :ws"),  # noqa: S608
                    {"ws": ws},
                ).scalar_one()
            )
            for table in ("audit_events", "event_outbox", "idempotency_records")
        }
        counts["audit_events"] -= int(
            connection.execute(
                text(
                    "SELECT count(*) FROM audit_events WHERE workspace_id = :ws "
                    "AND event_type = 'task.mutation_rejected'"
                ),
                {"ws": ws},
            ).scalar_one()
        )
        return counts


def _without_request_id(body: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in body.items() if key != "request_id"}


def _send_body(
    client: TestClient,
    case: Case,
    row_id: UUID,
    request_headers: dict[str, str],
    body: dict[str, Any],
) -> httpx.Response:
    return client.request(
        case.method, case.path.format(id=row_id), headers=request_headers, json=body
    )


def _send(
    client: TestClient, case: Case, row_id: UUID, request_headers: dict[str, str]
) -> httpx.Response:
    return _send_body(client, case, row_id, request_headers, case.body)


@pytest.mark.parametrize("loss", ["ownership_transfer", "suspension"])
@pytest.mark.parametrize("name", list(CASES))
def test_idempotent_replay_is_authorized_before_the_cache_is_served(
    world: RaceWorld, name: str, loss: str
) -> None:
    case = CASES[name]
    with engine.begin() as connection:
        row_id = case.seed(connection, world, owner=world.b, visibility="private")
    request_headers = headers(world.b_token)
    client = TestClient(app)
    client.cookies.set("ecc_session", world.b_token)
    try:
        first = _send(client, case, row_id, request_headers)
        assert first.status_code == 200, first.text

        # Still authorized: the same key replays the cached response even
        # though the row's version has moved past `expected_version`.
        replay = _send(client, case, row_id, request_headers)
        assert replay.status_code == 200, replay.text
        assert _without_request_id(replay.json()) == _without_request_id(first.json())

        # B loses read access: the same key must no longer replay the success.
        with engine.begin() as connection:
            if loss == "ownership_transfer":
                # The row stays private, now owned by C.
                connection.execute(
                    text(f"UPDATE {case.table} SET owner_id = :c WHERE id = :id"),  # noqa: S608
                    {"c": world.c, "id": row_id},
                )
            else:
                connection.execute(
                    text(
                        "UPDATE workspace_memberships SET status = 'suspended' "
                        "WHERE workspace_id = :ws AND users_id = :b"
                    ),
                    {"ws": world.ws, "b": world.b},
                )
        row_before = row_snapshot(case.table, row_id)
        counts_before = _side_effect_counts(world.ws)
        refused = _send(client, case, row_id, request_headers)
    finally:
        client.close()

    assert refused.status_code == 404, refused.text
    assert refused.json()["error"]["code"] == case.not_found
    assert row_snapshot(case.table, row_id) == row_before
    assert _side_effect_counts(world.ws) == counts_before


@pytest.mark.parametrize("name", list(CASES))
def test_idempotent_replay_after_demotion_is_refused_by_the_locked_write_check(
    world: RaceWorld, name: str
) -> None:
    """The demoted caller can still see the row, so only the locked write
    check -- which now runs before the idempotency cache -- refuses the
    replay."""
    case = CASES[name]
    with engine.begin() as connection:
        # Workspace-visible and owned by A: C (`member`) can write it by role
        # alone, and as a `viewer` can still read it.
        row_id = case.seed(connection, world, owner=world.a, visibility="workspace")
    c_token = _c_token(world)
    request_headers = headers(c_token)
    client = TestClient(app)
    client.cookies.set("ecc_session", c_token)
    try:
        first = _send(client, case, row_id, request_headers)
        assert first.status_code == 200, first.text

        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE workspace_memberships SET role = 'viewer' "
                    "WHERE workspace_id = :ws AND users_id = :c"
                ),
                {"ws": world.ws, "c": world.c},
            )
        row_before = row_snapshot(case.table, row_id)
        counts_before = _side_effect_counts(world.ws)
        refused = _send(client, case, row_id, request_headers)
    finally:
        client.close()

    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "INSUFFICIENT_ROLE"
    assert row_snapshot(case.table, row_id) == row_before
    assert _side_effect_counts(world.ws) == counts_before


# The same key reused with a different body: a different title for the PATCH,
# a reason the first request did not send for the lifecycle action.
_DIFFERENT_BODY: dict[str, dict[str, Any]] = {
    "event_patch": {"expected_version": 1, "title": "a different title"},
    "task_complete": {"expected_version": 1, "reason": "a different reason"},
}


@pytest.mark.parametrize("loss", ["demotion", "ownership_transfer"])
@pytest.mark.parametrize("name", list(_DIFFERENT_BODY))
def test_reused_key_with_a_different_body_after_losing_access_is_refused_not_conflicted(
    world: RaceWorld, name: str, loss: str
) -> None:
    """The conflict check lives in the cache read, which now runs after the
    locked read (404) and write (403) checks: a caller who has lost access
    learns nothing about the key's earlier request -- not even that it was
    different."""
    case = CASES[name]
    if loss == "demotion":
        # Workspace-visible and owned by A: C (`member`) writes it by role
        # alone, and as a `viewer` can still read it.
        owner, visibility, actor = world.a, "workspace", world.c
        token = _c_token(world)
    else:
        owner, visibility, actor = world.b, "private", world.b
        token = world.b_token
    with engine.begin() as connection:
        row_id = case.seed(connection, world, owner=owner, visibility=visibility)
    request_headers = headers(token)
    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    try:
        first = _send(client, case, row_id, request_headers)
        assert first.status_code == 200, first.text

        with engine.begin() as connection:
            if loss == "demotion":
                connection.execute(
                    text(
                        "UPDATE workspace_memberships SET role = 'viewer' "
                        "WHERE workspace_id = :ws AND users_id = :actor"
                    ),
                    {"ws": world.ws, "actor": actor},
                )
            else:
                # The row stays private, now owned by C.
                connection.execute(
                    text(f"UPDATE {case.table} SET owner_id = :c WHERE id = :id"),  # noqa: S608
                    {"c": world.c, "id": row_id},
                )
        row_before = row_snapshot(case.table, row_id)
        counts_before = _side_effect_counts(world.ws)
        refused = _send_body(client, case, row_id, request_headers, _DIFFERENT_BODY[name])
    finally:
        client.close()

    if loss == "demotion":
        assert refused.status_code == 403, refused.text
        assert refused.json()["error"]["code"] == "INSUFFICIENT_ROLE"
    else:
        assert refused.status_code == 404, refused.text
        assert refused.json()["error"]["code"] == case.not_found
    assert row_snapshot(case.table, row_id) == row_before
    assert _side_effect_counts(world.ws) == counts_before


@pytest.mark.parametrize("name", list(_DIFFERENT_BODY))
def test_authorized_reuse_of_a_key_with_a_different_body_is_an_idempotency_conflict(
    world: RaceWorld, name: str
) -> None:
    """The cache read still runs before the version check: the row's
    version has moved past `expected_version`, but the reused key answers
    409 IDEMPOTENCY_CONFLICT, not VERSION_CONFLICT."""
    case = CASES[name]
    with engine.begin() as connection:
        row_id = case.seed(connection, world, owner=world.b, visibility="private")
    request_headers = headers(world.b_token)
    client = TestClient(app)
    client.cookies.set("ecc_session", world.b_token)
    try:
        first = _send(client, case, row_id, request_headers)
        assert first.status_code == 200, first.text
        row_before = row_snapshot(case.table, row_id)
        counts_before = _side_effect_counts(world.ws)
        conflicted = _send_body(client, case, row_id, request_headers, _DIFFERENT_BODY[name])
    finally:
        client.close()

    assert conflicted.status_code == 409, conflicted.text
    assert conflicted.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert row_snapshot(case.table, row_id) == row_before
    assert _side_effect_counts(world.ws) == counts_before
