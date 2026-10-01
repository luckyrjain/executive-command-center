"""Lock-before-authorize sweep for automation workflow versions, policies and
approval requests: workflow publish/disable, policy revoke and approval
approve/reject authorized the caller *before* the helper they call
(`activate_workflow_version`, `disable_workflow_version`, `revoke_policy`,
`decide_approval`) took `SELECT ... FOR UPDATE` on the row it then writes.
An ownership transfer (locks the row, rewrites `owner_id`, does not bump
`version`) that committed while the request waited on that row lock left
the request writing to a row the caller could no longer see.

Now each endpoint locks the row first and authorization is evaluated
afterwards, on the committed post-transfer row, so the waiting request
answers 404 and writes nothing. See `lock_race_support` for the harness.
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

_DIGEST = "a" * 64
_GRAPH = '{"steps": [{"step_id": "s1", "step_type": "condition"}]}'


@pytest.fixture
def world() -> Iterator[RaceWorld]:
    with race_world(
        "Automation Lock Race",
        (
            "approval_requests",
            "workflow_run_steps",
            "workflow_runs",
            "automation_policies",
            "workflow_versions",
            "workflow_definitions",
        ),
    ) as w:
        yield w


def _insert_family(
    conn: Connection, w: RaceWorld, now: datetime, *, status: str
) -> tuple[str, UUID]:
    """A workflow definition plus its version 1 (both owned by B, private)."""
    workflow_id = f"race-{uuid4().hex[:12]}"
    conn.execute(
        text(
            "INSERT INTO workflow_definitions (id, workspace_id, workflow_id, created_by, "
            "created_at, updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, :wf, :b, :now, :now, :b, 'private')"
        ),
        {"id": uuid4(), "ws": w.ws, "wf": workflow_id, "b": w.b, "now": now},
    )
    version_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO workflow_versions (id, workspace_id, workflow_id, version, graph, "
            "definition_hash, status, created_by, updated_by, created_at, updated_at, "
            "owner_id, visibility) "
            "VALUES (:id, :ws, :wf, 1, CAST(:graph AS jsonb), :hash, :status, :b, :b, "
            ":now, :now, :b, 'private')"
        ),
        {
            "id": version_id,
            "ws": w.ws,
            "wf": workflow_id,
            "graph": _GRAPH,
            "hash": "0" * 64,
            "status": status,
            "b": w.b,
            "now": now,
        },
    )
    return workflow_id, version_id


def _seed_version(status: str) -> Callable[[Connection, RaceWorld, datetime], UUID]:
    def seed(conn: Connection, w: RaceWorld, now: datetime) -> UUID:
        return _insert_family(conn, w, now, status=status)[1]

    return seed


def _seed_policy(conn: Connection, w: RaceWorld, now: datetime) -> UUID:
    workflow_id, _ = _insert_family(conn, w, now, status="draft")
    policy_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO automation_policies (id, workspace_id, workflow_id, value_limit, "
            "count_limit, approval_mode, expires_at, created_by, updated_by, created_at, "
            "updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, :wf, 0, 0, 'per_run', :expires, :b, :b, :now, :now, :b, "
            "'private')"
        ),
        {
            "id": policy_id,
            "ws": w.ws,
            "wf": workflow_id,
            "expires": now + timedelta(days=30),
            "b": w.b,
            "now": now,
        },
    )
    return policy_id


def _seed_approval(conn: Connection, w: RaceWorld, now: datetime) -> UUID:
    """A pending approval gating a `waiting_approval` run at step 0, so a
    decision would also advance the run (and, on reject, insert a
    `workflow_run_steps` row) -- side effects the transfer case must not
    produce."""
    workflow_id, _ = _insert_family(conn, w, now, status="active")
    run_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO workflow_runs (id, workspace_id, workflow_id, workflow_version, "
            "status, current_step_index, queued_at, created_by, created_at, updated_at, "
            "owner_id, visibility) "
            "VALUES (:id, :ws, :wf, 1, 'waiting_approval', 0, :now, :b, :now, :now, :b, "
            "'private')"
        ),
        {"id": run_id, "ws": w.ws, "wf": workflow_id, "b": w.b, "now": now},
    )
    approval_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO approval_requests (id, workspace_id, run_id, step_index, "
            "action_digest, requested_at, expires_at, created_at, updated_at, owner_id, "
            "visibility) "
            "VALUES (:id, :ws, :run, 0, :digest, :now, :expires, :now, :now, :b, 'private')"
        ),
        {
            "id": approval_id,
            "ws": w.ws,
            "run": run_id,
            "digest": _DIGEST,
            "now": now,
            "expires": now + timedelta(days=1),
            "b": w.b,
        },
    )
    return approval_id


@dataclass(frozen=True)
class Case:
    table: str
    seed: Callable[[Connection, RaceWorld, datetime], UUID]
    path: str
    body: dict[str, Any] | None
    not_found: str
    # (column, value) the control case expects on the row after success.
    changed: tuple[str, Any]


_WF = "/api/v1/automations/workflows/{id}"
_APPROVALS = "/api/v1/automations/approvals/{id}"

CASES: dict[str, Case] = {
    "workflow_publish": Case(
        "workflow_versions",
        _seed_version("draft"),
        _WF + "/publish",
        None,
        "WORKFLOW_NOT_FOUND",
        ("status", "active"),
    ),
    "workflow_disable": Case(
        "workflow_versions",
        _seed_version("active"),
        _WF + "/disable",
        None,
        "WORKFLOW_NOT_FOUND",
        ("status", "retired"),
    ),
    "policy_revoke": Case(
        "automation_policies",
        _seed_policy,
        "/api/v1/automations/policies/{id}/revoke",
        None,
        "POLICY_NOT_FOUND",
        ("version", 2),
    ),
    "approval_approve": Case(
        "approval_requests",
        _seed_approval,
        _APPROVALS + "/approve",
        {"action_digest": _DIGEST},
        "APPROVAL_NOT_FOUND",
        ("status", "approved"),
    ),
    "approval_reject": Case(
        "approval_requests",
        _seed_approval,
        _APPROVALS + "/reject",
        None,
        "APPROVAL_NOT_FOUND",
        ("status", "rejected"),
    ),
}


def _side_effects(ws: UUID, raced: UUID) -> dict[str, Any]:
    """Counts of every side-effect table, plus the full contents of the rows
    a mutation would touch indirectly (runs and every version other than the
    raced row, which legitimately changes owner)."""
    with engine.connect() as connection:
        state: dict[str, Any] = {
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
                "workflow_run_steps",
            )
        }
        for table in ("workflow_runs", "workflow_versions"):
            state[table] = [
                dict(row)
                for row in connection.execute(
                    text(
                        f"SELECT * FROM {table} "  # noqa: S608 -- test literal
                        "WHERE workspace_id = :ws AND id <> :raced ORDER BY id"
                    ),
                    {"ws": ws, "raced": raced},
                ).mappings()
            ]
        return state


def _run(w: RaceWorld, case: Case, row_id: UUID, *, transfer: bool) -> tuple[Any, Any]:
    return race(
        w,
        table=case.table,
        row_id=row_id,
        send=lambda client: client.post(
            case.path.format(id=row_id), headers=headers(w.b_token), json=case.body
        ),
        transfer=transfer,
    )


@pytest.mark.parametrize("name", list(CASES))
def test_mutation_waiting_on_row_lock_rechecks_authorization(world: RaceWorld, name: str) -> None:
    case = CASES[name]
    with engine.begin() as connection:
        row_id = case.seed(connection, world, datetime.now(UTC))
    effects_before = _side_effects(world.ws, row_id)

    response, before = _run(world, case, row_id, transfer=True)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == case.not_found
    assert row_snapshot(case.table, row_id) == {**before, "owner_id": world.c}
    assert _side_effects(world.ws, row_id) == effects_before


@pytest.mark.parametrize("name", list(CASES))
def test_mutation_waiting_on_row_lock_without_transfer_still_proceeds(
    world: RaceWorld, name: str
) -> None:
    """Control: the same lock wait with no ownership change succeeds -- the
    404 above comes from the transfer, not from the wait itself."""
    case = CASES[name]
    with engine.begin() as connection:
        row_id = case.seed(connection, world, datetime.now(UTC))

    response, _before = _run(world, case, row_id, transfer=False)

    assert response.status_code == 200, response.text
    column, expected = case.changed
    assert row_snapshot(case.table, row_id)[column] == expected
