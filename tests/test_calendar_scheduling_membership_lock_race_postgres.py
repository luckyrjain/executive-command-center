"""Calendar event and meeting writes against a concurrent member removal or
role change (ADR-0014).

Every write transaction in `calendar/events.py` and `scheduling/meetings.py`
now takes the shared membership lock first (`authz.lock_membership_for_write`)
and authorizes after it; the creates re-check the role in-transaction
(`role_action="write"`), since their only role gate ran before the
transaction began. A removal or demotion holding the lock makes the write
wait, and once it commits the write answers 403/404 and writes nothing. See
`membership_lock_race_support` for the harness.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from membership_lock_race_support import (
    ROLE_GATED,
    Case,
    Change,
    Ids,
    RaceWorld,
    assert_proceeds,
    assert_refused,
    race_world,
    refusal_params,
    row_refusals,
    seed,
    seed_nothing,
)
from sqlalchemy import Connection, text

from ecc.config import get_settings

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_WRITE_TABLES = (
    "calendar_events",
    "meetings",
    "audit_events",
    "event_outbox",
    "idempotency_records",
)


@pytest.fixture
def world() -> Iterator[RaceWorld]:
    with race_world("Calendar Scheduling Membership Race", ("meetings", "calendar_events")) as w:
        yield w


def _insert_event(conn: Connection, w: RaceWorld, now: datetime, *, archived: bool) -> Ids:
    event_id = uuid4()
    starts = now + timedelta(days=1)
    conn.execute(
        text(
            "INSERT INTO calendar_events (id, workspace_id, title, starts_at, ends_at, "
            "timezone, created_by, updated_by, created_at, updated_at, owner_id, "
            "visibility, archived_at, pre_archive_status) "
            "VALUES (:id, :ws, 'Race event', :starts, :ends, 'UTC', :a, :a, :now, :now, "
            ":a, 'workspace', :archived_at, :pre_archive_status)"
        ),
        {
            "id": event_id,
            "ws": w.ws,
            "a": w.a,
            "now": now,
            "starts": starts,
            "ends": starts + timedelta(hours=1),
            "archived_at": now if archived else None,
            "pre_archive_status": "confirmed" if archived else None,
        },
    )
    return {"id": event_id}


def _insert_meeting(conn: Connection, w: RaceWorld, now: datetime, *, archived: bool) -> Ids:
    meeting_id = uuid4()
    starts = now + timedelta(days=1)
    conn.execute(
        text(
            "INSERT INTO meetings (id, workspace_id, title, standalone_starts_at, "
            "standalone_ends_at, standalone_timezone, created_by, updated_by, created_at, "
            "updated_at, owner_id, visibility, archived_at, pre_archive_status) "
            "VALUES (:id, :ws, 'Race meeting', :starts, :ends, 'UTC', :a, :a, :now, :now, "
            ":a, 'workspace', :archived_at, :pre_archive_status)"
        ),
        {
            "id": meeting_id,
            "ws": w.ws,
            "a": w.a,
            "now": now,
            "starts": starts,
            "ends": starts + timedelta(hours=1),
            "archived_at": now if archived else None,
            "pre_archive_status": "planned" if archived else None,
        },
    )
    return {"id": meeting_id}


def _seed_event(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    return _insert_event(conn, w, now, archived=False)


def _seed_archived_event(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    return _insert_event(conn, w, now, archived=True)


def _seed_meeting(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    return _insert_meeting(conn, w, now, archived=False)


def _seed_archived_meeting(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    return _insert_meeting(conn, w, now, archived=True)


def _v1(_: Ids) -> dict[str, Any]:
    return {"expected_version": 1}


def _timed(title: str) -> dict[str, Any]:
    return {
        "title": title,
        "starts_at": "2030-01-01T09:00:00+00:00",
        "ends_at": "2030-01-01T10:00:00+00:00",
        "timezone": "UTC",
    }


def _rename(_: Ids) -> dict[str, Any]:
    return {"expected_version": 1, "title": "Race probe"}


_EVENT_ROW = row_refusals("CALENDAR_EVENT_NOT_FOUND")
_MEETING_ROW = row_refusals("MEETING_NOT_FOUND")
_EVENTS = "/api/v1/calendar/events"
_MEETINGS = "/api/v1/meetings"

CASES: dict[str, Case] = {
    "event_create": Case(
        seed_nothing, "POST", _EVENTS, lambda _: _timed("Race event"), 201, ROLE_GATED
    ),
    "event_patch": Case(_seed_event, "PATCH", f"{_EVENTS}/{{id}}", _rename, 200, _EVENT_ROW),
    "event_archive": Case(_seed_event, "POST", f"{_EVENTS}/{{id}}/archive", _v1, 200, _EVENT_ROW),
    "event_restore": Case(
        _seed_archived_event, "POST", f"{_EVENTS}/{{id}}/restore", _v1, 200, _EVENT_ROW
    ),
    "meeting_create": Case(
        seed_nothing, "POST", _MEETINGS, lambda _: _timed("Race meeting"), 201, ROLE_GATED
    ),
    # A meeting linked to A's calendar event: the create also reads the event
    # through `get_calendar_event_summary` inside the same transaction.
    "meeting_create_linked": Case(
        _seed_event,
        "POST",
        _MEETINGS,
        lambda ids: {"title": "Race meeting", "calendar_event_id": str(ids["id"])},
        201,
        ROLE_GATED,
    ),
    "meeting_patch": Case(
        _seed_meeting, "PATCH", f"{_MEETINGS}/{{id}}", _rename, 200, _MEETING_ROW
    ),
    "meeting_archive": Case(
        _seed_meeting, "POST", f"{_MEETINGS}/{{id}}/archive", _v1, 200, _MEETING_ROW
    ),
    "meeting_restore": Case(
        _seed_archived_meeting, "POST", f"{_MEETINGS}/{{id}}/restore", _v1, 200, _MEETING_ROW
    ),
}


@pytest.mark.parametrize(("name", "change"), refusal_params(CASES))
def test_write_waiting_on_membership_lock_rechecks_authorization(
    world: RaceWorld, name: str, change: Change
) -> None:
    case = CASES[name]
    assert_refused(world, case, seed(world, case), change, _WRITE_TABLES)


@pytest.mark.parametrize("name", list(CASES))
def test_write_waiting_on_membership_lock_without_change_still_proceeds(
    world: RaceWorld, name: str
) -> None:
    case = CASES[name]
    assert_proceeds(world, case, seed(world, case))
