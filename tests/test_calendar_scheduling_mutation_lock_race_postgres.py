"""Lock-before-authorize sweep for calendar events and meetings: the event
and meeting PATCH and archive/restore paths authorized the caller *before*
taking `SELECT ... FOR UPDATE` on the row they then write. An ownership
transfer (locks the row, rewrites `owner_id`, does not bump `version`) that
committed while the request waited on that row lock left the request writing
to a row the caller could no longer see.

Now each row is locked first and authorization is evaluated afterwards, on
the committed post-transfer row, so the waiting request answers 404 and
writes nothing. See `lock_race_support` for the harness.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from lock_race_support import RaceWorld, headers, race, race_world, row_snapshot
from sqlalchemy import Connection, text

from ecc.config import get_settings
from ecc.database import engine

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


@pytest.fixture
def world() -> Iterator[RaceWorld]:
    with race_world("Calendar Scheduling Lock Race", ("meetings", "calendar_events")) as w:
        yield w


def _seed_event(conn: Connection, w: RaceWorld, now: datetime, *, archived: bool) -> UUID:
    event_id = uuid4()
    starts = now + timedelta(days=1)
    conn.execute(
        text(
            "INSERT INTO calendar_events (id, workspace_id, title, starts_at, ends_at, "
            "timezone, created_by, updated_by, created_at, updated_at, owner_id, "
            "visibility, archived_at) "
            "VALUES (:id, :ws, 'Race event', :starts, :ends, 'UTC', :b, :b, :now, :now, "
            ":b, 'private', :archived_at)"
        ),
        {
            "id": event_id,
            "ws": w.ws,
            "b": w.b,
            "now": now,
            "starts": starts,
            "ends": starts + timedelta(hours=1),
            "archived_at": now if archived else None,
        },
    )
    return event_id


def _seed_meeting(conn: Connection, w: RaceWorld, now: datetime, *, archived: bool) -> UUID:
    meeting_id = uuid4()
    starts = now + timedelta(days=1)
    conn.execute(
        text(
            "INSERT INTO meetings (id, workspace_id, title, standalone_starts_at, "
            "standalone_ends_at, standalone_timezone, created_by, updated_by, created_at, "
            "updated_at, owner_id, visibility, archived_at) "
            "VALUES (:id, :ws, 'Race meeting', :starts, :ends, 'UTC', :b, :b, :now, :now, "
            ":b, 'private', :archived_at)"
        ),
        {
            "id": meeting_id,
            "ws": w.ws,
            "b": w.b,
            "now": now,
            "starts": starts,
            "ends": starts + timedelta(hours=1),
            "archived_at": now if archived else None,
        },
    )
    return meeting_id


@dataclass(frozen=True)
class Case:
    table: str
    archived: bool
    method: str
    path: str
    body: dict[str, Any]
    not_found: str

    def seed(self, conn: Connection, w: RaceWorld, now: datetime) -> UUID:
        seeder: Callable[..., UUID] = (
            _seed_event if self.table == "calendar_events" else _seed_meeting
        )
        return seeder(conn, w, now, archived=self.archived)


_V1 = {"expected_version": 1}
_EVENTS = "/api/v1/calendar/events/{id}"
_MEETINGS = "/api/v1/meetings/{id}"

CASES: dict[str, Case] = {
    "event_patch": Case(
        "calendar_events",
        False,
        "PATCH",
        _EVENTS,
        {"expected_version": 1, "title": "race probe"},
        "CALENDAR_EVENT_NOT_FOUND",
    ),
    "event_archive": Case(
        "calendar_events", False, "POST", _EVENTS + "/archive", _V1, "CALENDAR_EVENT_NOT_FOUND"
    ),
    "event_restore": Case(
        "calendar_events", True, "POST", _EVENTS + "/restore", _V1, "CALENDAR_EVENT_NOT_FOUND"
    ),
    "meeting_patch": Case(
        "meetings",
        False,
        "PATCH",
        _MEETINGS,
        {"expected_version": 1, "title": "race probe"},
        "MEETING_NOT_FOUND",
    ),
    "meeting_archive": Case(
        "meetings", False, "POST", _MEETINGS + "/archive", _V1, "MEETING_NOT_FOUND"
    ),
    "meeting_restore": Case(
        "meetings", True, "POST", _MEETINGS + "/restore", _V1, "MEETING_NOT_FOUND"
    ),
}


def _side_effect_counts(ws: UUID) -> dict[str, int]:
    with engine.connect() as connection:
        return {
            table: int(
                connection.execute(
                    text(f"SELECT count(*) FROM {table} WHERE workspace_id = :ws"),  # noqa: S608
                    {"ws": ws},
                ).scalar_one()
            )
            for table in ("audit_events", "event_outbox", "idempotency_records")
        }


def _run(w: RaceWorld, case: Case, row_id: UUID, *, transfer: bool) -> tuple[Any, Any]:
    return race(
        w,
        table=case.table,
        row_id=row_id,
        send=lambda client: client.request(
            case.method,
            case.path.format(id=row_id),
            headers=headers(w.b_token),
            json=case.body,
        ),
        transfer=transfer,
    )


@pytest.mark.parametrize("name", list(CASES))
def test_mutation_waiting_on_row_lock_rechecks_authorization(world: RaceWorld, name: str) -> None:
    case = CASES[name]
    with engine.begin() as connection:
        row_id = case.seed(connection, world, datetime.now(UTC))
    counts_before = _side_effect_counts(world.ws)

    response, before = _run(world, case, row_id, transfer=True)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == case.not_found
    assert row_snapshot(case.table, row_id) == {**before, "owner_id": world.c}
    assert _side_effect_counts(world.ws) == counts_before


@pytest.mark.parametrize("name", list(CASES))
def test_mutation_waiting_on_row_lock_without_transfer_still_proceeds(
    world: RaceWorld, name: str
) -> None:
    """Control: the same lock wait with no ownership change succeeds -- the
    404 above comes from the transfer, not from the wait itself."""
    case = CASES[name]
    with engine.begin() as connection:
        row_id = case.seed(connection, world, datetime.now(UTC))

    response, before = _run(world, case, row_id, transfer=False)

    assert response.status_code == 200, response.text
    assert row_snapshot(case.table, row_id)["version"] == before["version"] + 1
