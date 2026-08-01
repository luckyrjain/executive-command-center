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
7. Security audit batch C: a `workflow_id` path parameter longer than the
   column's own `sa.String(200)` is a 422 on both the activate and the
   status route, never an uncaught `DataError`/500; exactly 200 characters
   is still accepted.

Plus, added by the run-state audit that found the gap in this module's own
"no auto-resume on re-enable" claim (`kill_switches.py`'s corrected
docstring):

8. **Activation parks already-`'queued'` runs** (never-claimed and
   retry-pending alike, per-workflow and global scope) into `needs_review`,
   so a later deactivation genuinely resumes nothing -- the property the
   recovery runbook documents, previously true only for runs a worker
   happened to observe being killed.

Plus, added by the test-coverage audit that found this module's HTTP surface
had **no idempotency coverage of either kind** -- neither same-key replay nor
same-key-different-body conflict -- despite `kill_switches.py` carrying its
own hand-copied `_request_hash`/`_load_cached`/`_store_idempotency` trio
(each Phase 5 module has an independent copy, so coverage of one proves
nothing about the others):

9. **Idempotency-Key replay and `IDEMPOTENCY_CONFLICT`** on the activate/
   deactivate routes. This table is append-only by design -- a genuine
   reactivate inserts a *fresh* row -- so a replay bug here does not merely
   return a stale body, it appends a duplicate activation window and makes
   "who stopped what, when" permanently ambiguous in the one audit trail an
   incident review reads.
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
from identity_fixtures import create_identity
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
        create_identity(
            connection,
            workspace_id=workspace_id,
            user_id=user_id,
            email=f"{user_id}@example.test",
            now=now,
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


# ---------------------------------------------------------------------------
# 7. workflow_id path-parameter length validation (security audit batch C).
# ---------------------------------------------------------------------------


def test_kill_switch_rejects_overlong_workflow_id_with_422(
    kill_switch_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """`automation_kill_switches.workflow_id` is `sa.String(200)` (migration
    `0042_phase5_compensation_retry_kill_switch.py`), but the `workflow_id`
    path parameter carried no length constraint -- so an over-long value
    passed FastAPI validation, reached the `INSERT`, and surfaced as an
    uncaught `DataError`/500: a client's malformed input reported as a server
    fault. `Path(max_length=200)` (mirroring `policy.list_policies_endpoint`/
    `triggers.list_triggers_endpoint`'s own `Query(max_length=200)` on the
    identical field) makes it an ordinary 422 instead.

    Asserted on both routes that take this path parameter -- the `POST`
    activate route (the one that actually reached the database) and the `GET`
    status route (which only reads, but must agree on what a valid
    `workflow_id` is, or the two disagree about the same identifier).
    Exactly-200 characters is asserted to still be accepted, so this pins the
    boundary rather than merely "long values fail."
    """
    client, _workspace_id, _user_id, token = kill_switch_test_context
    overlong = "w" * 201

    activate = client.post(
        f"/api/v1/automations/workflows/{overlong}/kill_switch",
        json={"active": True, "reason": "over-long workflow_id"},
        headers=_headers(token, key="overlong-workflow-id-1"),
    )
    assert activate.status_code == 422

    status = client.get(f"/api/v1/automations/workflows/{overlong}/kill_switch")
    assert status.status_code == 422

    # The boundary itself is still valid -- 200 characters fits the column.
    at_limit = "w" * 200
    accepted = client.post(
        f"/api/v1/automations/workflows/{at_limit}/kill_switch",
        json={"active": True, "reason": "exactly at the limit"},
        headers=_headers(token, key="at-limit-workflow-id-1"),
    )
    assert accepted.status_code == 200
    assert accepted.json()["workflow_id"] == at_limit


# ---------------------------------------------------------------------------
# 8. Activation parks already-queued runs, so deactivation never silently
#    auto-resumes anything (this module's own corrected docstring).
# ---------------------------------------------------------------------------


def test_activating_a_kill_switch_parks_already_queued_runs_for_review(
    kill_switch_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """Regression test for the gap an adversarial review found in this
    module's own "no auto-resume on re-enable" claim (see its corrected
    docstring). That claim rested on a killed run landing in `needs_review`,
    which is never claimable -- true only for a run a worker happened to be
    actively dispatching when the switch fired. A run already sitting in
    `'queued'` was never touched by anything: `enqueue_run`'s kill-switch
    rejection only blocks *new* rows. So it waited out the whole incident
    invisibly and the very first poll cycle after deactivation claimed and
    dispatched it, with zero operator review -- exactly what the recovery
    runbook says must not happen.

    Activation now parks it in `needs_review` up front, so the "requires
    explicit operator action to resume" property holds for real.
    """
    _client, workspace_id, user_id, _token = kill_switch_test_context
    workflow_id = f"test.park-queued.{uuid4().hex}"
    _publish_workflow(workspace_id, user_id, workflow_id)
    echo = _EchoAdapter()

    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun)
    assert queued.status == "queued"

    with SessionFactory() as session, session.begin():
        automation_kill_switches.activate_kill_switch(
            session, workspace_id, workflow_id, "incident", user_id
        )

    with SessionFactory() as session:
        parked = automation_worker.get_run(session, workspace_id, queued.id)
    assert parked is not None
    assert parked.status == "needs_review"  # visible to an operator, not silently queued
    assert parked.finished_at is None  # needs_review is not terminal
    assert parked.leased_by is None

    with SessionFactory() as session, session.begin():
        assert (
            automation_kill_switches.deactivate_kill_switch(
                session, workspace_id, workflow_id, user_id
            )
            is True
        )

    # The whole point: deactivation resumes nothing. The run stays parked
    # and no poll cycle picks it up.
    with SessionFactory() as session:
        after = automation_worker.get_run(session, workspace_id, queued.id)
    assert after is not None
    assert after.status == "needs_review"
    with SessionFactory() as session:
        assert automation_worker.claim_next_run(session, "worker-a") is None
    assert echo.execute_calls == 0


def test_activating_a_global_kill_switch_parks_every_workflows_queued_runs(
    kill_switch_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """The global-scope half of the fix, plus the retry-pending case the
    review named explicitly: `worker.run_step`'s bounded-retry path parks a
    run back in `'queued'` with a `next_attempt_at`, so a retry-pending run
    is exactly as invisible-and-silently-resumable as a never-claimed one.
    A global switch (`workflow_id IS NULL`) covers every workflow in the
    workspace, mirroring `is_workflow_killed`'s own "either scope" semantics.
    """
    _client, workspace_id, user_id, _token = kill_switch_test_context
    workflow_a = f"test.park-global-a.{uuid4().hex}"
    workflow_b = f"test.park-global-b.{uuid4().hex}"
    _publish_workflow(workspace_id, user_id, workflow_a)
    _publish_workflow(workspace_id, user_id, workflow_b)

    with SessionFactory() as session, session.begin():
        run_a = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_a
        )
        run_b = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_b
        )
    assert isinstance(run_a, automation_worker.WorkflowRun)
    assert isinstance(run_b, automation_worker.WorkflowRun)

    # Make run_b look exactly like a retry-pending run (the shape run_step's
    # TransientAdapterError branch leaves behind): still 'queued', lease
    # released, backoff not yet elapsed.
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE workflow_runs SET next_attempt_at = now() + interval '1 hour' "
                "WHERE id = :id"
            ),
            {"id": run_b.id},
        )

    with SessionFactory() as session, session.begin():
        automation_kill_switches.activate_kill_switch(
            session, workspace_id, None, "global incident", user_id
        )

    with SessionFactory() as session:
        statuses = {
            run.workflow_id: run.status
            for run in automation_worker.list_runs(session, workspace_id)
        }
    assert statuses == {workflow_a: "needs_review", workflow_b: "needs_review"}

    with SessionFactory() as session, session.begin():
        automation_kill_switches.deactivate_kill_switch(session, workspace_id, None, user_id)

    with SessionFactory() as session:
        assert automation_worker.claim_next_run(session, "worker-a") is None
    with SessionFactory() as session:
        still_parked = {run.status for run in automation_worker.list_runs(session, workspace_id)}
    assert still_parked == {"needs_review"}

    # And each is now individually actionable by an operator (`cancel_run`'s
    # needs_review escape hatch), rather than merely stuck.
    with SessionFactory() as session, session.begin():
        cancelled = automation_worker.cancel_run(session, workspace_id, run_a.id)
    assert isinstance(cancelled, automation_worker.WorkflowRun)
    assert cancelled.status == "cancelled"


# ---------------------------------------------------------------------------
# 9. Idempotency-Key handling on the kill-switch routes.
# ---------------------------------------------------------------------------


def test_kill_switch_same_key_different_body_is_idempotency_conflict(
    kill_switch_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """Reusing one `Idempotency-Key` across two *materially different*
    kill-switch bodies is not a retry, and must 409 `IDEMPOTENCY_CONFLICT`
    rather than either silently replaying the first response or silently
    applying the second write.

    Why this endpoint specifically, out of the several Phase 5 routes that
    had no conflict coverage at all: `automation_kill_switches` is
    append-only by design (`kill_switches.py`'s "Append-only-ish history"
    section -- a real reactivate inserts a *fresh* row so each activation
    window keeps its own `activated_by`/`activated_at`). That makes this the
    one route where a mishandled key reuse corrupts an audit trail rather
    than just a response body: an operator's client retrying with a stale key
    could append a second activation row, and the incident review afterwards
    could no longer say who stopped what or when.

    The two bodies chosen are `active: true` and `active: false` -- the
    maximum-consequence divergence available on this route, since replaying
    the wrong one of those reports the exact opposite of the system's real
    state to the operator holding the stop button. Both halves are asserted:
    the 409 itself, that the switch is genuinely *still active* (the
    deactivation the second call asked for was not applied), and that the
    history table gained no extra row. The `record_idempotency_conflict`
    observability signal is asserted too, matching every other conflict test
    in this codebase (`test_attention_capacity_postgres.py`'s own pair) --
    `kill_switches._load_cached` passes the domain label
    `"automation_kill_switch"`, distinct from every sibling module's own.
    """
    from ecc.observability import render_metrics

    client, workspace_id, user_id, token = kill_switch_test_context
    workflow_id = f"test.idempotency-conflict.{uuid4().hex}"
    _publish_workflow(workspace_id, user_id, workflow_id)

    headers = _headers(token, key="kill-switch-conflict-key")
    activated = client.post(
        f"/api/v1/automations/workflows/{workflow_id}/kill_switch",
        json={"active": True, "reason": "incident"},
        headers=headers,
    )
    assert activated.status_code == 200, activated.text
    assert activated.json()["active"] is True

    conflicting = client.post(
        f"/api/v1/automations/workflows/{workflow_id}/kill_switch",
        json={"active": False},
        headers=headers,
    )
    assert conflicting.status_code == 409
    assert conflicting.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert 'ecc_idempotency_conflicts_total{domain="automation_kill_switch"}' in render_metrics()

    # The rejected call changed nothing: the switch is still active (so new
    # runs are still blocked), and the append-only history still holds
    # exactly the one activation row the first call inserted.
    with SessionFactory() as session:
        assert (
            automation_kill_switches.is_workflow_killed(session, workspace_id, workflow_id) is True
        )
        history = automation_kill_switches.list_kill_switches(session, workspace_id)
    assert len(history) == 1
    assert history[0].active is True
    assert history[0].deactivated_at is None

    blocked = client.post(
        "/api/v1/automations/runs",
        json={"workflow_id": workflow_id},
        headers=_headers(token, key="conflict-run-1"),
    )
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "KILL_SWITCH_ACTIVE"


def test_kill_switch_replay_never_appends_a_duplicate_history_row(
    kill_switch_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """A same-key, same-body retry of each leg of a full activate ->
    deactivate -> reactivate cycle replays byte-identically and leaves the
    append-only history at exactly the two rows the cycle really produced.

    The bug class this guards against is specific to this table's shape. Every
    other Phase 5 route's worst replay outcome is a stale response body; here
    a replay that reaches `activate_kill_switch` after a deactivation inserts
    a *third* row (the module deliberately does that for a genuine reactivate,
    which is the correct behaviour for a real second incident and the wrong
    one for a retried HTTP request). The row count, not the response body, is
    therefore the load-bearing assertion -- an audit trail that says a
    workflow was killed three times when an operator killed it twice cannot
    be reconciled against anything afterwards.

    Each leg is replayed under its own key, because a *different* key with
    the same body is a genuinely new request and would legitimately reach the
    domain function -- the point is that the cached-response path, not
    `activate_kill_switch`'s own already-active no-op, is what makes the
    retry safe.
    """
    client, workspace_id, user_id, token = kill_switch_test_context
    workflow_id = f"test.idempotency-replay.{uuid4().hex}"
    _publish_workflow(workspace_id, user_id, workflow_id)
    ignored = {"request_id", "correlation_id"}

    def _post(key: str, body: dict[str, Any]) -> dict[str, Any]:
        response = client.post(
            f"/api/v1/automations/workflows/{workflow_id}/kill_switch",
            json=body,
            headers=_headers(token, key=key),
        )
        assert response.status_code == 200, response.text
        return {k: v for k, v in response.json().items() if k not in ignored}

    activate_body = {"active": True, "reason": "first incident"}
    first_activate = _post("replay-activate", activate_body)
    replayed_activate = _post("replay-activate", activate_body)
    assert first_activate == replayed_activate
    assert first_activate["active"] is True

    deactivate_body: dict[str, Any] = {"active": False}
    first_deactivate = _post("replay-deactivate", deactivate_body)
    replayed_deactivate = _post("replay-deactivate", deactivate_body)
    assert first_deactivate == replayed_deactivate
    assert first_deactivate["active"] is False

    reactivate_body = {"active": True, "reason": "second incident"}
    first_reactivate = _post("replay-reactivate", reactivate_body)
    replayed_reactivate = _post("replay-reactivate", reactivate_body)
    assert first_reactivate == replayed_reactivate
    assert first_reactivate["active"] is True
    # A genuine reactivate is a new activation window, so its response must
    # not be the first activation's row replayed back.
    assert first_reactivate["reason"] == "second incident"

    with SessionFactory() as session:
        history = automation_kill_switches.list_kill_switches(session, workspace_id)
    # Exactly two activation windows -- one per real activate -- not three or
    # four, despite six HTTP calls having been made.
    assert len(history) == 2
    assert sum(1 for row in history if row.active) == 1
    assert {row.reason for row in history} == {"first incident", "second incident"}
