"""Phase 5 Automation Task 6: global/per-workflow kill switches
(`docs/runbooks/PHASE-5-RECOVERY.md`'s "Kill switches and their recovery
interaction" section, `docs/phases/PHASE-005-automation.md`'s Rollback
plan, `ecc.domains.automation.kill_switches`).

Covers, per this task's own required minimum:

1. A global kill switch blocks every workflow in the workspace from
   starting a new run (`POST /automations/runs` -> 409 `KILL_SWITCH_ACTIVE`).
2. A per-workflow kill switch blocks only that one workflow.
3. Reactivating after deactivation works.
4. Cross-workspace isolation -- a kill switch in workspace A never affects
   workspace B's identical `workflow_id`.
5. `claim_next_run` never returns a killed workflow's row, proven under a
   real crash/restart simulation: a run already claimed (`leased`/
   `running`) with an expired lease is never reclaimed once a kill switch
   activates, exactly like the recovery runbook requires ("never claimed,
   reclaimed, or resumed regardless of lease state").
6. `process_claimed_run`'s per-step loop stops a run mid-dispatch
   (`needs_review`) the moment a kill switch is discovered, before its
   next not-yet-dispatched step.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.automation import kill_switches as automation_kill_switches
from ecc.domains.automation import policy as automation_policy
from ecc.domains.automation import worker as automation_worker
from ecc.domains.automation import workflows as automation_workflows
from ecc.domains.automation.adapters import AdapterRegistry
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


# ---------------------------------------------------------------------------
# Test-only fakes.
# ---------------------------------------------------------------------------


class _Input(BaseModel):
    value: str = ""


class _Output(BaseModel):
    value: str


class _EchoAdapter:
    adapter_id = "test.kill-switch-echo"
    input_schema = _Input
    output_schema = _Output
    reversible = True
    high_impact_categories: frozenset[str] = frozenset()

    def __init__(self) -> None:
        self.execute_calls = 0

    def simulate(self, action_input: _Input) -> _Output:  # noqa: D102
        return _Output(value=action_input.value)

    def execute(self, action_input: _Input) -> _Output:  # noqa: D102
        self.execute_calls += 1
        return _Output(value=action_input.value)


def _action_step(step_id: str, action_ref: str) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "step_type": "action",
        "action_ref": action_ref,
        "input_mapping": {},
        "on_success": "succeeded",
        "on_failure": "failed",
    }


def _chained_graph(*steps: dict[str, Any]) -> dict[str, Any]:
    steps = list(steps)  # type: ignore[assignment]
    for index, step in enumerate(steps[:-1]):
        step["on_success"] = steps[index + 1]["step_id"]
    return {"steps": steps}


def _make_registry(*adapters: Any) -> AdapterRegistry:
    registry = AdapterRegistry()
    for adapter in adapters:
        registry.register(adapter)
    return registry


# ---------------------------------------------------------------------------
# Fixtures / helpers.
# ---------------------------------------------------------------------------


def _seed_workspace(workspace_id: UUID, user_id: UUID, name: str) -> str:
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, :name, 'Asia/Kolkata', :now)"
            ),
            {"id": workspace_id, "name": name, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO users (id, workspace_id, email, password_hash, created_at) "
                "VALUES (:id, :workspace_id, :email, 'test-password-hash', :now)"
            ),
            {
                "id": user_id,
                "workspace_id": workspace_id,
                "email": f"{user_id}@example.test",
                "now": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                "expires_at, last_seen_at) "
                "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :now)"
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


@pytest.fixture
def kill_switch_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = _seed_workspace(workspace_id, user_id, "Automation Kill Switch Test")

    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    try:
        yield client, workspace_id, user_id, token
    finally:
        client.close()
        _cleanup_workspace(workspace_id)


def _cleanup_workspace(workspace_id: UUID) -> None:
    with engine.begin() as connection:
        for table in (
            "automation_kill_switches",
            "compensation_steps",
            "approval_requests",
            "workflow_run_steps",
            "workflow_runs",
            "triggers",
            "automation_policies",
            "workflow_versions",
            "workflow_definitions",
            "event_outbox",
            "audit_events",
            "idempotency_records",
            "sessions",
            "users",
        ):
            connection.execute(
                text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                {"workspace_id": workspace_id},
            )
        connection.execute(
            text("DELETE FROM workspaces WHERE id = :workspace_id"), {"workspace_id": workspace_id}
        )


def _headers(token: str, key: str | None = None) -> dict[str, str]:
    from hmac import new

    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    headers = {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _publish_workflow(
    workspace_id: UUID, user_id: UUID, workflow_id: str, *, steps: int = 1
) -> automation_workflows.WorkflowVersion:
    graph = _chained_graph(
        *[_action_step(f"s{i + 1}", "test.kill-switch-echo") for i in range(steps)]
    )
    with SessionFactory() as session, session.begin():
        automation_workflows.create_workflow_draft(
            session,
            workspace_id,
            user_id,
            workflow_id=workflow_id,
            graph=graph,
            trigger_refs=[],
            policy_ref=None,
        )
    with SessionFactory() as session, session.begin():
        policy_row = automation_policy.create_policy(
            session,
            workspace_id,
            user_id,
            workflow_id=workflow_id,
            action_types=[],
            data_classes=[],
            value_limit=Decimal("1000000"),
            count_limit=1000,
            rate_limit=None,
            schedule=None,
            approval_mode="bounded_recurring",
        )
    with SessionFactory() as session, session.begin():
        draft = automation_workflows.create_workflow_draft(
            session,
            workspace_id,
            user_id,
            workflow_id=workflow_id,
            graph=graph,
            trigger_refs=[],
            policy_ref=policy_row.id,
        )
        activated = automation_workflows.activate_workflow_version(session, workspace_id, draft.id)
    assert isinstance(activated, automation_workflows.WorkflowVersion)
    return activated


# ---------------------------------------------------------------------------
# 1. Global kill switch blocks every workflow via HTTP.
# ---------------------------------------------------------------------------


def test_global_kill_switch_blocks_every_workflow_via_http(
    kill_switch_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = kill_switch_test_context
    workflow_a = f"test.global-a.{uuid4().hex}"
    workflow_b = f"test.global-b.{uuid4().hex}"
    _publish_workflow(workspace_id, user_id, workflow_a)
    _publish_workflow(workspace_id, user_id, workflow_b)

    activate = client.post(
        "/api/v1/automations/kill_switch",
        json={"active": True, "reason": "incident"},
        headers=_headers(token, key="activate-global-1"),
    )
    assert activate.status_code == 200
    assert activate.json()["active"] is True
    assert activate.json()["workflow_id"] is None

    for workflow_id in (workflow_a, workflow_b):
        response = client.post(
            "/api/v1/automations/runs",
            json={"workflow_id": workflow_id},
            headers=_headers(token, key=f"create-run-{workflow_id}"),
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "KILL_SWITCH_ACTIVE"

    with SessionFactory() as session:
        runs = automation_worker.list_runs(session, workspace_id)
    assert runs == []


# ---------------------------------------------------------------------------
# 2. Per-workflow kill switch blocks only that workflow.
# ---------------------------------------------------------------------------


def test_per_workflow_kill_switch_blocks_only_that_workflow(
    kill_switch_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = kill_switch_test_context
    killed_workflow = f"test.killed.{uuid4().hex}"
    live_workflow = f"test.live.{uuid4().hex}"
    _publish_workflow(workspace_id, user_id, killed_workflow)
    _publish_workflow(workspace_id, user_id, live_workflow)

    activate = client.post(
        f"/api/v1/automations/workflows/{killed_workflow}/kill_switch",
        json={"active": True, "reason": "bad deploy"},
        headers=_headers(token, key="activate-per-workflow-1"),
    )
    assert activate.status_code == 200
    assert activate.json()["workflow_id"] == killed_workflow

    blocked = client.post(
        "/api/v1/automations/runs",
        json={"workflow_id": killed_workflow},
        headers=_headers(token, key="create-run-killed"),
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "KILL_SWITCH_ACTIVE"

    allowed = client.post(
        "/api/v1/automations/runs",
        json={"workflow_id": live_workflow},
        headers=_headers(token, key="create-run-live"),
    )
    assert allowed.status_code == 201


# ---------------------------------------------------------------------------
# 3. Reactivating after deactivation works.
# ---------------------------------------------------------------------------


def test_reactivating_after_deactivation_works(
    kill_switch_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = kill_switch_test_context
    workflow_id = f"test.reactivate.{uuid4().hex}"
    _publish_workflow(workspace_id, user_id, workflow_id)

    client.post(
        f"/api/v1/automations/workflows/{workflow_id}/kill_switch",
        json={"active": True, "reason": "first incident"},
        headers=_headers(token, key="reactivate-1"),
    )
    deactivate = client.post(
        f"/api/v1/automations/workflows/{workflow_id}/kill_switch",
        json={"active": False},
        headers=_headers(token, key="reactivate-2"),
    )
    assert deactivate.status_code == 200
    assert deactivate.json()["active"] is False

    allowed = client.post(
        "/api/v1/automations/runs",
        json={"workflow_id": workflow_id},
        headers=_headers(token, key="reactivate-run-1"),
    )
    assert allowed.status_code == 201

    reactivate = client.post(
        f"/api/v1/automations/workflows/{workflow_id}/kill_switch",
        json={"active": True, "reason": "second incident"},
        headers=_headers(token, key="reactivate-3"),
    )
    assert reactivate.status_code == 200
    assert reactivate.json()["active"] is True

    blocked_again = client.post(
        "/api/v1/automations/runs",
        json={"workflow_id": workflow_id},
        headers=_headers(token, key="reactivate-run-2"),
    )
    assert blocked_again.status_code == 409

    with SessionFactory() as session:
        history = automation_kill_switches.list_kill_switches(session, workspace_id)
    # Two rows: the first (deactivated) activation window, and the second
    # (still active) one -- module docstring's "append-only-ish history"
    # property, not a single row silently overwritten.
    assert len(history) == 2
    assert sum(1 for row in history if row.active) == 1


# ---------------------------------------------------------------------------
# 4. Cross-workspace isolation.
# ---------------------------------------------------------------------------


def test_cross_workspace_kill_switch_isolation() -> None:
    workspace_a = uuid4()
    user_a = uuid4()
    token_a = _seed_workspace(workspace_a, user_a, "Kill Switch Isolation A")
    workspace_b = uuid4()
    user_b = uuid4()
    token_b = _seed_workspace(workspace_b, user_b, "Kill Switch Isolation B")

    client_a = TestClient(app)
    client_a.cookies.set("ecc_session", token_a)
    client_b = TestClient(app)
    client_b.cookies.set("ecc_session", token_b)

    try:
        shared_workflow_id = f"test.shared.{uuid4().hex}"
        _publish_workflow(workspace_a, user_a, shared_workflow_id)
        _publish_workflow(workspace_b, user_b, shared_workflow_id)

        activate = client_a.post(
            "/api/v1/automations/kill_switch",
            json={"active": True, "reason": "workspace A only"},
            headers=_headers(token_a, key="isolation-activate-1"),
        )
        assert activate.status_code == 200

        blocked_in_a = client_a.post(
            "/api/v1/automations/runs",
            json={"workflow_id": shared_workflow_id},
            headers=_headers(token_a, key="isolation-run-a"),
        )
        assert blocked_in_a.status_code == 409

        allowed_in_b = client_b.post(
            "/api/v1/automations/runs",
            json={"workflow_id": shared_workflow_id},
            headers=_headers(token_b, key="isolation-run-b"),
        )
        assert allowed_in_b.status_code == 201

        with SessionFactory() as session:
            assert (
                automation_kill_switches.is_workflow_killed(
                    session, workspace_a, shared_workflow_id
                )
                is True
            )
            assert (
                automation_kill_switches.is_workflow_killed(
                    session, workspace_b, shared_workflow_id
                )
                is False
            )
    finally:
        client_a.close()
        client_b.close()
        _cleanup_workspace(workspace_a)
        _cleanup_workspace(workspace_b)


# ---------------------------------------------------------------------------
# 5. claim_next_run under a real crash/restart simulation.
# ---------------------------------------------------------------------------


def test_claim_next_run_never_reclaims_a_killed_workflows_expired_lease(
    kill_switch_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """Simulates a worker crash: a run is claimed (`leased`), its lease is
    forced into the past (the exact state a real crashed worker's
    unrenewed lease reaches after `LEASE_DURATION_SECONDS`), and only then
    is the kill switch activated -- proving the recovery runbook's own
    claim verbatim: a killed workflow's row is "never claimed, reclaimed,
    or resumed regardless of lease state, including across a crash/restart
    cycle."
    """
    client, workspace_id, user_id, token = kill_switch_test_context
    workflow_id = f"test.crash-kill.{uuid4().hex}"
    _publish_workflow(workspace_id, user_id, workflow_id)

    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun)

    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-crashed")
    assert claimed is not None
    assert claimed.id == queued.id

    # Force the lease into the past -- the exact state a crashed worker's
    # unrenewed lease reaches.
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE workflow_runs SET leased_until = now() - interval '1 second' WHERE id = :id"
            ),
            {"id": queued.id},
        )

    # Confirm the ordinary (no kill switch) recovery path *would* reclaim
    # this row -- establishes the baseline before activating the switch.
    with SessionFactory() as session:
        reclaimable = automation_worker.claim_next_run(session, "worker-b")
    assert reclaimable is not None
    assert reclaimable.id == queued.id
    # Re-expire the lease again (claim_next_run above renewed it).
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE workflow_runs SET leased_until = now() - interval '1 second' WHERE id = :id"
            ),
            {"id": queued.id},
        )

    with SessionFactory() as session, session.begin():
        automation_kill_switches.activate_kill_switch(
            session, workspace_id, workflow_id, "post-claim incident", user_id
        )

    with SessionFactory() as session:
        never_reclaimed = automation_worker.claim_next_run(session, "worker-c")
    assert never_reclaimed is None

    # Also true for a never-claimed queued run under a global switch.
    with SessionFactory() as session, session.begin():
        second_queued = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    # enqueue_run itself now rejects a killed workflow -- WorkflowKilled,
    # not a real row.
    assert isinstance(second_queued, automation_worker.WorkflowKilled)


# ---------------------------------------------------------------------------
# 6. process_claimed_run stops mid-dispatch when a kill switch activates.
# ---------------------------------------------------------------------------


def test_process_claimed_run_stops_mid_dispatch_on_kill_switch(
    kill_switch_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = kill_switch_test_context
    workflow_id = f"test.mid-dispatch-kill.{uuid4().hex}"
    _publish_workflow(workspace_id, user_id, workflow_id, steps=2)
    echo = _EchoAdapter()
    registry = _make_registry(echo)

    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun)

    # Dispatch just step 0 manually (mirrors process_claimed_run's own
    # per-step shape) so a kill switch can be activated strictly between
    # step 0 and step 1.
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
        assert claimed is not None
        outcome = automation_worker.run_step(session, claimed, 0, registry)
    assert outcome.status == "succeeded"
    assert echo.execute_calls == 1

    with SessionFactory() as session, session.begin():
        automation_kill_switches.activate_kill_switch(
            session, workspace_id, workflow_id, "stop mid-run", user_id
        )

    with SessionFactory() as session:
        run = automation_worker.get_run(session, workspace_id, queued.id)
        assert run is not None
        finished = automation_worker.process_claimed_run(session, run, registry, "worker-a")

    assert finished.status == "needs_review"
    assert echo.execute_calls == 1  # step 1 never dispatched
    with SessionFactory() as session:
        steps = automation_worker.list_run_steps(session, workspace_id, queued.id)
    assert {step.step_index for step in steps} == {0}
