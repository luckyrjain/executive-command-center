"""Automation and prompt-activation writes against a concurrent member
removal or role change (ADR-0014).

Every user-facing write transaction in `automation/*` and
`ai_runtime/prompts.activate_policy` now takes the shared membership lock
first (`authz.lock_membership_for_write`) and authorizes after it. The
role-gated endpoints (workflow create, policy create, run create, kill
switch) re-check the role in-transaction (`role_action="write"`), since
their only role gate ran before the transaction began; prompt activation
re-checks its owner/admin gate in-transaction the same way. A removal or
demotion holding the lock makes the write wait, and once it commits the
write answers 403/404 and writes nothing. See `membership_lock_race_support`
for the harness.

Local workarounds (the shared harness is not modified): prompt activation
needs B to be an `admin`, so its fixture promotes B before the race, and
its admin -> member demotion is applied by patching the harness's
`_apply_change` for that one test. `prompt_versions`/`tool_definitions` are
global, not workspace-scoped, so they are fingerprinted separately, and the
test's own tool rows are deleted on teardown.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from json import dumps
from typing import Any
from uuid import uuid4

import membership_lock_race_support as support
import pytest
from membership_lock_race_support import (
    FORBIDDEN,
    ROLE_GATED,
    Case,
    Change,
    Ids,
    RaceWorld,
    assert_proceeds,
    assert_refused,
    fingerprint,
    race,
    race_world,
    refusal_params,
    row_refusals,
    seed,
    seed_nothing,
)
from sqlalchemy import Connection, text

from ecc.config import get_settings
from ecc.database import engine
from ecc.domains.ai_runtime import tools as ai_tools

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_SEEDED_TABLES = (
    "compensation_steps",
    "approval_requests",
    "workflow_run_steps",
    "workflow_runs",
    "automation_kill_switches",
    "triggers",
    "automation_policies",
    "workflow_versions",
    "workflow_definitions",
)
_WRITE_TABLES = (*_SEEDED_TABLES, "audit_events", "event_outbox", "idempotency_records")

_DIGEST = "a" * 64
_GRAPH = {"steps": [{"step_id": "s1", "step_type": "condition"}]}
_TOOL = f"race.tool.{uuid4().hex}"


@pytest.fixture
def world() -> Iterator[RaceWorld]:
    with race_world("Automation Prompts Membership Race", _SEEDED_TABLES) as w:
        yield w


def _insert_family(conn: Connection, w: RaceWorld, now: datetime, *, status: str) -> Ids:
    """A workflow definition plus its version 1, owned by A, workspace-visible.
    `workflow_id` is a slug, not a UUID; it is returned under `wf` as a
    string for path/body formatting."""
    workflow_id = f"race-{uuid4().hex[:12]}"
    conn.execute(
        text(
            "INSERT INTO workflow_definitions (id, workspace_id, workflow_id, created_by, "
            "created_at, updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, :wf, :a, :now, :now, :a, 'workspace')"
        ),
        {"id": uuid4(), "ws": w.ws, "wf": workflow_id, "a": w.a, "now": now},
    )
    version_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO workflow_versions (id, workspace_id, workflow_id, version, graph, "
            "definition_hash, status, created_by, updated_by, created_at, updated_at, "
            "owner_id, visibility) "
            "VALUES (:id, :ws, :wf, 1, CAST(:graph AS jsonb), :hash, :status, :a, :a, "
            ":now, :now, :a, 'workspace')"
        ),
        {
            "id": version_id,
            "ws": w.ws,
            "wf": workflow_id,
            "graph": dumps(_GRAPH),
            "hash": "0" * 64,
            "status": status,
            "a": w.a,
            "now": now,
        },
    )
    return {"id": version_id, "wf": workflow_id}  # type: ignore[dict-item]


def _seed_draft(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    return _insert_family(conn, w, now, status="draft")


def _seed_active(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    return _insert_family(conn, w, now, status="active")


def _seed_policy(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    family = _insert_family(conn, w, now, status="draft")
    policy_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO automation_policies (id, workspace_id, workflow_id, value_limit, "
            "count_limit, approval_mode, expires_at, created_by, updated_by, created_at, "
            "updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, :wf, 0, 0, 'per_run', :expires, :a, :a, :now, :now, :a, "
            "'workspace')"
        ),
        {
            "id": policy_id,
            "ws": w.ws,
            "wf": family["wf"],
            "expires": now + timedelta(days=30),
            "a": w.a,
            "now": now,
        },
    )
    return {"id": policy_id}


def _insert_run(conn: Connection, w: RaceWorld, now: datetime, *, status: str) -> Ids:
    family = _insert_family(conn, w, now, status="active")
    run_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO workflow_runs (id, workspace_id, workflow_id, workflow_version, "
            "status, current_step_index, queued_at, created_by, created_at, updated_at, "
            "owner_id, visibility) "
            "VALUES (:id, :ws, :wf, 1, :status, 0, :now, :a, :now, :now, :a, 'workspace')"
        ),
        {"id": run_id, "ws": w.ws, "wf": family["wf"], "status": status, "a": w.a, "now": now},
    )
    return {"id": run_id}


def _seed_queued_run(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    return _insert_run(conn, w, now, status="queued")


def _seed_paused_run(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    return _insert_run(conn, w, now, status="paused")


def _seed_approval(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    """A pending approval gating a `waiting_approval` run at step 0, so a
    decision would also advance the run (and, on reject, insert a
    `workflow_run_steps` row)."""
    run_id = _insert_run(conn, w, now, status="waiting_approval")["id"]
    approval_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO approval_requests (id, workspace_id, run_id, step_index, "
            "action_digest, requested_at, expires_at, created_at, updated_at, owner_id, "
            "visibility) "
            "VALUES (:id, :ws, :run, 0, :digest, :now, :expires, :now, :now, :a, 'workspace')"
        ),
        {
            "id": approval_id,
            "ws": w.ws,
            "run": run_id,
            "digest": _DIGEST,
            "now": now,
            "expires": now + timedelta(days=1),
            "a": w.a,
        },
    )
    return {"id": approval_id}


def _insert_tool(conn: Connection, *, version: int, status: str) -> None:
    scopes = ["read:test"]
    schema = {"type": "object"}
    handler_ref = "ecc.domains.test:handler"
    conn.execute(
        text(
            "INSERT INTO tool_definitions (id, name, version, scopes, input_schema, "
            "output_schema, handler_ref, definition_hash, status, created_at, updated_at) "
            "VALUES (:id, :name, :version, :scopes, CAST(:schema AS jsonb), "
            "CAST(:schema AS jsonb), :handler_ref, :hash, :status, now(), now())"
        ),
        {
            "id": uuid4(),
            "name": _TOOL,
            "version": version,
            "scopes": scopes,
            "schema": dumps(schema),
            "handler_ref": handler_ref,
            "hash": ai_tools.compute_definition_hash(
                input_schema=schema, output_schema=schema, scopes=scopes, handler_ref=handler_ref
            ),
            "status": status,
        },
    )


def _seed_tool(conn: Connection, _w: RaceWorld, _now: datetime) -> Ids:
    """A test-only tool family (global table): v1 active, v2 draft, so
    activating v2 would retire v1 and activate v2."""
    _insert_tool(conn, version=1, status="active")
    _insert_tool(conn, version=2, status="draft")
    return {}


def _no_body(_: Ids) -> None:
    return None


_WF = "/api/v1/automations/workflows"
_RUNS = "/api/v1/automations/runs/{id}"
_APPROVALS = "/api/v1/automations/approvals/{id}"
_RUN_ROW = row_refusals("RUN_NOT_FOUND")

CASES: dict[str, Case] = {
    "workflow_create": Case(
        seed_nothing,
        "POST",
        _WF,
        lambda _: {"workflow_id": f"race-{uuid4().hex[:12]}", "graph": _GRAPH},
        201,
        ROLE_GATED,
    ),
    "workflow_publish": Case(
        _seed_draft,
        "POST",
        _WF + "/{id}/publish",
        _no_body,
        200,
        row_refusals("WORKFLOW_NOT_FOUND"),
    ),
    "workflow_disable": Case(
        _seed_active,
        "POST",
        _WF + "/{id}/disable",
        _no_body,
        200,
        row_refusals("WORKFLOW_NOT_FOUND"),
    ),
    "policy_create": Case(
        _seed_draft,
        "POST",
        "/api/v1/automations/policies",
        lambda ids: {
            "workflow_id": ids["wf"],
            "value_limit": 0,
            "count_limit": 0,
            "approval_mode": "per_run",
        },
        201,
        ROLE_GATED,
    ),
    "policy_revoke": Case(
        _seed_policy,
        "POST",
        "/api/v1/automations/policies/{id}/revoke",
        _no_body,
        200,
        row_refusals("POLICY_NOT_FOUND"),
    ),
    "approval_approve": Case(
        _seed_approval,
        "POST",
        _APPROVALS + "/approve",
        lambda _: {"action_digest": _DIGEST},
        200,
        row_refusals("APPROVAL_NOT_FOUND"),
    ),
    "approval_reject": Case(
        _seed_approval,
        "POST",
        _APPROVALS + "/reject",
        _no_body,
        200,
        row_refusals("APPROVAL_NOT_FOUND"),
    ),
    "run_create": Case(
        _seed_active,
        "POST",
        "/api/v1/automations/runs",
        lambda ids: {"workflow_id": ids["wf"]},
        201,
        ROLE_GATED,
    ),
    "run_cancel": Case(_seed_queued_run, "POST", _RUNS + "/cancel", _no_body, 200, _RUN_ROW),
    "run_pause": Case(_seed_queued_run, "POST", _RUNS + "/pause", _no_body, 200, _RUN_ROW),
    "run_resume": Case(_seed_paused_run, "POST", _RUNS + "/resume", _no_body, 200, _RUN_ROW),
    "kill_switch_workflow": Case(
        _seed_queued_run,
        "POST",
        "/api/v1/automations/workflows/race-kill-switch/kill_switch",
        lambda _: {"active": True, "reason": "race"},
        200,
        ROLE_GATED,
    ),
    "kill_switch_global": Case(
        _seed_queued_run,
        "POST",
        "/api/v1/automations/kill_switch",
        lambda _: {"active": True, "reason": "race"},
        200,
        ROLE_GATED,
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


# ---------------------------------------------------------------------------
# Prompt/tool activation: owner/admin only, global tables
# ---------------------------------------------------------------------------

_ACTIVATE = Case(
    _seed_tool,
    "POST",
    f"/api/v1/ai/policies/{_TOOL}/activate",
    lambda _: {"version": 2, "expected_active_version": 1},
    200,
    {"demote": FORBIDDEN, "remove": FORBIDDEN},
)
_ACTIVATE_TABLES = ("audit_events", "event_outbox", "idempotency_records")


@pytest.fixture
def admin_world(world: RaceWorld) -> Iterator[RaceWorld]:
    """B promoted to `admin` (the harness world makes B a `member`, which
    activation already refuses before the race); the test tool rows are
    deleted afterwards so the global tables are left as found."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE workspace_memberships SET role = 'admin' "
                "WHERE workspace_id = :ws AND users_id = :b"
            ),
            {"ws": world.ws, "b": world.b},
        )
    try:
        yield world
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM tool_definitions WHERE name = :name"), {"name": _TOOL}
            )


def _global_fingerprint() -> str | None:
    """This test's own tool family in the global `tool_definitions` table
    (not workspace-scoped), so other tests touching the table can't make
    it flaky."""
    with engine.connect() as connection:
        return connection.execute(
            text(
                "SELECT md5(string_agg(t::text, '|' ORDER BY t::text)) "
                "FROM tool_definitions t WHERE t.name = :name"
            ),
            {"name": _TOOL},
        ).scalar_one()


def _tool_statuses() -> dict[int, str]:
    with engine.connect() as connection:
        return {
            int(row.version): str(row.status)
            for row in connection.execute(
                text("SELECT version, status FROM tool_definitions WHERE name = :name"),
                {"name": _TOOL},
            )
        }


def _apply_change_with_member_demotion(conn: Connection, w: RaceWorld, change: Any) -> None:
    if change == "demote_to_member":
        conn.execute(
            text(
                "UPDATE workspace_memberships SET role = 'member', updated_at = now() "
                "WHERE workspace_id = :ws AND users_id = :b"
            ),
            {"ws": w.ws, "b": w.b},
        )
    else:
        _harness_apply_change(conn, w, change)


_harness_apply_change = support._apply_change


@pytest.mark.parametrize("change", ["demote", "remove"])
def test_activation_waiting_on_membership_lock_rechecks_owner_or_admin(
    admin_world: RaceWorld, change: Change
) -> None:
    ids = seed(admin_world, _ACTIVATE)
    global_before = _global_fingerprint()

    assert_refused(admin_world, _ACTIVATE, ids, change, _ACTIVATE_TABLES)

    assert _global_fingerprint() == global_before
    assert _tool_statuses() == {1: "active", 2: "draft"}


def test_activation_waiting_on_membership_lock_refuses_admin_demoted_to_member(
    admin_world: RaceWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """admin -> member (not only -> viewer) must refuse: `member` still has
    `write`, so this is the owner/admin re-check itself, not the generic
    role re-check."""
    monkeypatch.setattr(support, "_apply_change", _apply_change_with_member_demotion)
    ids = seed(admin_world, _ACTIVATE)
    workspace_before = fingerprint(admin_world.ws, _ACTIVATE_TABLES)
    global_before = _global_fingerprint()

    response, blocked = race(admin_world, _ACTIVATE, ids, change="demote_to_member")  # type: ignore[arg-type]

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "INSUFFICIENT_ROLE"
    assert blocked
    assert fingerprint(admin_world.ws, _ACTIVATE_TABLES) == workspace_before
    assert _global_fingerprint() == global_before
    assert _tool_statuses() == {1: "active", 2: "draft"}


def test_activation_waiting_on_membership_lock_without_change_still_proceeds(
    admin_world: RaceWorld,
) -> None:
    assert_proceeds(admin_world, _ACTIVATE, seed(admin_world, _ACTIVATE))
    assert _tool_statuses() == {1: "retired", 2: "active"}
