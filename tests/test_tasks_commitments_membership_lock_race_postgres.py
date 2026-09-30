"""Task and commitment writes against a concurrent member removal or role
change (ADR-0014).

Every write transaction in `planning/tasks.py` and
`communication/commitments.py` now takes the shared membership lock first
(`authz.lock_membership_for_write`) and authorizes after it; the creates
re-check the role in-transaction (`role_action="write"`), since their only
role gate ran before the transaction began. A removal or demotion holding
the lock makes the write wait, and once it commits the write answers
403/404 and writes nothing. See `membership_lock_race_support` for the
harness.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime
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

_WRITE_TABLES = ("tasks", "commitments", "audit_events", "event_outbox", "idempotency_records")


@pytest.fixture
def world() -> Iterator[RaceWorld]:
    with race_world("Tasks Commitments Membership Race", ("tasks", "commitments")) as w:
        yield w


def _seed_task(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    task_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO tasks (id, workspace_id, owner_id, title, created_by, updated_by, "
            "created_at, updated_at, visibility) "
            "VALUES (:id, :ws, :a, 'Race task', :a, :a, :now, :now, 'workspace')"
        ),
        {"id": task_id, "ws": w.ws, "a": w.a, "now": now},
    )
    return {"id": task_id}


def _seed_commitment(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    commitment_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO commitments (id, workspace_id, owner_id, summary, direction, "
            "status, created_by, updated_by, created_at, updated_at, visibility) "
            "VALUES (:id, :ws, :a, 'Race commitment', 'made_by_me', 'confirmed', "
            ":a, :a, :now, :now, 'workspace')"
        ),
        {"id": commitment_id, "ws": w.ws, "a": w.a, "now": now},
    )
    return {"id": commitment_id}


def _v1(_: Ids) -> dict[str, Any]:
    return {"expected_version": 1}


_TASK_ROW = row_refusals("TASK_NOT_FOUND")
_COMMITMENT_ROW = row_refusals("COMMITMENT_NOT_FOUND")

CASES: dict[str, Case] = {
    "task_create": Case(
        seed_nothing, "POST", "/api/v1/tasks", lambda _: {"title": "Race task"}, 201, ROLE_GATED
    ),
    "task_patch": Case(
        _seed_task,
        "PATCH",
        "/api/v1/tasks/{id}",
        lambda _: {"expected_version": 1, "title": "Race probe"},
        200,
        _TASK_ROW,
    ),
    "task_complete": Case(_seed_task, "POST", "/api/v1/tasks/{id}/complete", _v1, 200, _TASK_ROW),
    "commitment_create": Case(
        seed_nothing,
        "POST",
        "/api/v1/commitments",
        lambda _: {"summary": "Race commitment", "direction": "made_by_me"},
        201,
        ROLE_GATED,
    ),
    "commitment_patch": Case(
        _seed_commitment,
        "PATCH",
        "/api/v1/commitments/{id}",
        lambda _: {"expected_version": 1, "summary": "Race probe"},
        200,
        _COMMITMENT_ROW,
    ),
    "commitment_fulfil": Case(
        _seed_commitment,
        "POST",
        "/api/v1/commitments/{id}/fulfil",
        _v1,
        200,
        _COMMITMENT_ROW,
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
