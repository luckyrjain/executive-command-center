"""Lock-before-authorize sweep for tasks: the task PATCH path and the two
shared write helpers behind every task lifecycle action
(`lifecycle_task_write`) and a recommendation's non-terminal `set_status`
(`set_task_status_write`) authorized the caller *before* taking
`SELECT ... FOR UPDATE` on the row they then write. An ownership transfer
(locks the row, rewrites `owner_id`, does not bump `version`) that committed
while the request waited on that row lock left the request writing to a row
the caller could no longer see.

Now each row is locked first and authorization is evaluated afterwards, on
the committed post-transfer row, so the waiting request answers 404 and
writes nothing. The helpers are also reached through the recommendation
confirm cascade, so that path is raced too. See `lock_race_support` for the
harness.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from json import dumps
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
    with race_world(
        "Planning Tasks Lock Race", ("recommendation_feedback", "recommendations", "tasks")
    ) as w:
        yield w


def _seed_task(conn: Connection, w: RaceWorld, now: datetime, *, archived: bool) -> UUID:
    task_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO tasks (id, workspace_id, owner_id, title, created_by, updated_by, "
            "created_at, updated_at, visibility, archived_at) "
            "VALUES (:id, :ws, :b, 'Race task', :b, :b, :now, :now, 'private', :archived_at)"
        ),
        {
            "id": task_id,
            "ws": w.ws,
            "b": w.b,
            "now": now,
            "archived_at": now if archived else None,
        },
    )
    return task_id


def _seed_recommendation(
    conn: Connection, w: RaceWorld, now: datetime, task_id: UUID, value: str
) -> UUID:
    recommendation_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO recommendations (id, workspace_id, recommendation_type, target_type, "
            "target_id, proposed_action, rationale, confidence, status, source, created_by, "
            "updated_by, created_at, updated_at, version, owner_id, visibility, "
            "expected_version) "
            "VALUES (:id, :ws, 'task_status', 'task', :task_id, "
            "CAST(:action AS jsonb), 'Race rationale', 0.9, 'pending_confirmation', 'rule', "
            ":b, :b, :now, :now, 1, :b, 'private', 1)"
        ),
        {
            "id": recommendation_id,
            "ws": w.ws,
            "task_id": task_id,
            "action": dumps({"operation": "set_status", "value": value}),
            "b": w.b,
            "now": now,
        },
    )
    return recommendation_id


@dataclass(frozen=True)
class Case:
    archived: bool
    path: str
    body: dict[str, Any]
    method: str = "POST"
    # Set for the recommendation-confirm cases: the `set_status` value the
    # recommendation proposes for the task.
    confirm_value: str | None = None


_V1 = {"expected_version": 1}
_TASKS = "/api/v1/tasks/{id}"
_CONFIRM = "/api/v1/recommendations/{id}/confirm"
_CONFIRM_BODY = {"expected_version": 1, "target_expected_version": 1}

CASES: dict[str, Case] = {
    "task_patch": Case(False, _TASKS, {"expected_version": 1, "title": "race probe"}, "PATCH"),
    "task_complete": Case(False, _TASKS + "/complete", _V1),
    "task_cancel": Case(False, _TASKS + "/cancel", _V1),
    "task_archive": Case(False, _TASKS + "/archive", _V1),
    "task_restore": Case(True, _TASKS + "/restore", _V1),
    # `set_task_status_write` via the confirm cascade.
    "confirm_set_status": Case(False, _CONFIRM, _CONFIRM_BODY, confirm_value="in_progress"),
    # `lifecycle_task_write` via the confirm cascade.
    "confirm_complete": Case(False, _CONFIRM, _CONFIRM_BODY, confirm_value="completed"),
}


def _side_effect_counts(ws: UUID) -> dict[str, int]:
    with engine.connect() as connection:
        counts = {
            table: int(
                connection.execute(
                    text(f"SELECT count(*) FROM {table} WHERE workspace_id = :ws"),  # noqa: S608
                    {"ws": ws},
                ).scalar_one()
            )
            for table in (
                "audit_events",
                "event_outbox",
                "idempotency_records",
                "recommendation_feedback",
            )
        }
        # `rejected_mutation_audit_middleware` records every rejected
        # POST/PATCH under /api/v1/tasks as its own audit row, written after
        # the request's transaction; that row is the expected trace of the
        # 404, not a side effect of the mutation, so it is counted apart.
        counts["task_mutation_rejected"] = int(
            connection.execute(
                text(
                    "SELECT count(*) FROM audit_events WHERE workspace_id = :ws "
                    "AND event_type = 'task.mutation_rejected' AND failure_code = 'HTTP_404'"
                ),
                {"ws": ws},
            ).scalar_one()
        )
        counts["audit_events"] -= counts["task_mutation_rejected"]
        return counts


def _seed(w: RaceWorld, case: Case) -> tuple[UUID, UUID]:
    """Returns (task id, id the request path addresses)."""
    now = datetime.now(UTC)
    with engine.begin() as connection:
        task_id = _seed_task(connection, w, now, archived=case.archived)
        if case.confirm_value is None:
            return task_id, task_id
        return task_id, _seed_recommendation(connection, w, now, task_id, case.confirm_value)


def _run(
    w: RaceWorld, case: Case, task_id: UUID, path_id: UUID, *, transfer: bool
) -> tuple[Any, Any]:
    return race(
        w,
        table="tasks",
        row_id=task_id,
        send=lambda client: client.request(
            case.method,
            case.path.format(id=path_id),
            headers=headers(w.b_token),
            json=case.body,
        ),
        transfer=transfer,
    )


@pytest.mark.parametrize("name", list(CASES))
def test_mutation_waiting_on_row_lock_rechecks_authorization(world: RaceWorld, name: str) -> None:
    case = CASES[name]
    task_id, path_id = _seed(world, case)
    recommendation_before = (
        row_snapshot("recommendations", path_id) if case.confirm_value is not None else None
    )
    counts_before = _side_effect_counts(world.ws)

    response, before = _run(world, case, task_id, path_id, transfer=True)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "TASK_NOT_FOUND"
    assert row_snapshot("tasks", task_id) == {**before, "owner_id": world.c}
    rejected_rows = 1 if case.path.startswith("/api/v1/tasks") else 0
    assert _side_effect_counts(world.ws) == {
        **counts_before,
        "task_mutation_rejected": counts_before["task_mutation_rejected"] + rejected_rows,
    }
    if recommendation_before is not None:
        assert row_snapshot("recommendations", path_id) == recommendation_before


@pytest.mark.parametrize("name", list(CASES))
def test_mutation_waiting_on_row_lock_without_transfer_still_proceeds(
    world: RaceWorld, name: str
) -> None:
    """Control: the same lock wait with no ownership change succeeds -- the
    404 above comes from the transfer, not from the wait itself."""
    case = CASES[name]
    task_id, path_id = _seed(world, case)

    response, before = _run(world, case, task_id, path_id, transfer=False)

    assert response.status_code == 200, response.text
    assert row_snapshot("tasks", task_id)["version"] == before["version"] + 1
    if case.confirm_value is not None:
        assert response.json()["status"] == "executed"
        assert row_snapshot("tasks", task_id)["status"] == case.confirm_value
