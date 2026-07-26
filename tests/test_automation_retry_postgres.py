"""Phase 5 Automation Task 6: bounded retry
(`docs/phases/phase-005/EXECUTION-CONTRACT.md`'s "Retries use bounded
exponential backoff only for classified transient failures").

A new, dedicated test module (mirroring how `test_automation_adapters_
postgres.py`/`test_automation_scheduler_postgres.py` each got their own
file rather than growing `test_automation_worker_postgres.py` further) --
this task's own new mechanic, `TransientAdapterError`-driven retry, gets
its own coverage:

1. A step whose adapter raises `TransientAdapterError` twice then succeeds
   ends `succeeded` with `attempt_count == 2`.
2. A step whose adapter always raises `TransientAdapterError` ends `failed`
   after exactly `MAX_RETRY_ATTEMPTS` retries, never retried a further
   time (`execute()`'s call count stops growing once `failed`).
3. `claim_next_run`'s widened predicate honors `next_attempt_at` -- a
   retry-pending run is not reclaimed before its backoff window elapses,
   and is reclaimed once it has.
4. `_retry_backoff_seconds`'s own schedule (2s/4s/8s for attempts 1/2/3).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.automation import policy as automation_policy
from ecc.domains.automation import worker as automation_worker
from ecc.domains.automation import workflows as automation_workflows
from ecc.domains.automation.adapters import AdapterRegistry, TransientAdapterError

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


# ---------------------------------------------------------------------------
# Test-only fakes.
# ---------------------------------------------------------------------------


class EchoInput(BaseModel):
    value: str = ""


class EchoOutput(BaseModel):
    value: str


class TransientNTimesThenSucceedAdapter:
    """Raises `TransientAdapterError` for its first `fail_count` calls, then
    succeeds -- proves the "eventually succeeds after N transient
    failures" path.
    """

    adapter_id = "test.transient-n-then-succeed"
    input_schema = EchoInput
    output_schema = EchoOutput
    reversible = True
    high_impact_categories: frozenset[str] = frozenset()

    def __init__(self, fail_count: int) -> None:
        self._fail_count = fail_count
        self.execute_calls = 0

    def simulate(self, action_input: EchoInput) -> EchoOutput:  # noqa: D102
        return EchoOutput(value=action_input.value)

    def execute(self, action_input: EchoInput) -> EchoOutput:  # noqa: D102
        self.execute_calls += 1
        if self.execute_calls <= self._fail_count:
            raise TransientAdapterError(f"transient failure #{self.execute_calls}")
        return EchoOutput(value=action_input.value)


class AlwaysTransientAdapter:
    """Never succeeds -- proves bounded exhaustion."""

    adapter_id = "test.always-transient"
    input_schema = EchoInput
    output_schema = EchoOutput
    reversible = True
    high_impact_categories: frozenset[str] = frozenset()

    def __init__(self) -> None:
        self.execute_calls = 0

    def simulate(self, action_input: EchoInput) -> EchoOutput:  # noqa: D102
        return EchoOutput(value=action_input.value)

    def execute(self, action_input: EchoInput) -> EchoOutput:  # noqa: D102
        self.execute_calls += 1
        raise TransientAdapterError("always fails")


def _make_registry(*adapters: Any) -> AdapterRegistry:
    registry = AdapterRegistry()
    for adapter in adapters:
        registry.register(adapter)
    return registry


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


@pytest.fixture
def retry_test_context() -> Iterator[tuple[UUID, UUID]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Automation Retry Test', 'Asia/Kolkata', :now)"
            ),
            {"id": workspace_id, "now": now},
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

    try:
        yield workspace_id, user_id
    finally:
        _cleanup_workspace(workspace_id)


def _cleanup_workspace(workspace_id: UUID) -> None:
    with engine.begin() as connection:
        for table in (
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


def _publish_workflow(
    workspace_id: UUID, user_id: UUID, workflow_id: str, graph: dict[str, Any]
) -> automation_workflows.WorkflowVersion:
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


def _enqueue_and_claim(
    workspace_id: UUID, user_id: UUID, workflow_id: str, worker_id: str = "worker-a"
) -> automation_worker.WorkflowRun:
    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun)
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, worker_id)
    assert claimed is not None
    return claimed


# ---------------------------------------------------------------------------
# 1. Eventually succeeds after transient failures.
# ---------------------------------------------------------------------------


def test_retry_succeeds_after_two_transient_failures_attempt_count_is_two(
    retry_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = retry_test_context
    workflow_id = f"test.retry-succeed.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.transient-n-then-succeed"))
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    adapter = TransientNTimesThenSucceedAdapter(fail_count=2)
    registry = _make_registry(adapter)

    run = _enqueue_and_claim(workspace_id, user_id, workflow_id)

    # Attempt 1: transient failure -> retrying, attempt_count == 1.
    with SessionFactory() as session:
        outcome_1 = automation_worker.run_step(session, run, 0, registry)
    assert outcome_1.status == "retrying"
    assert adapter.execute_calls == 1
    with SessionFactory() as session:
        run = automation_worker.get_run(session, workspace_id, run.id)
        assert run is not None
        steps = automation_worker.list_run_steps(session, workspace_id, run.id)
    assert run.status == "queued"
    assert run.next_attempt_at is not None
    assert run.next_attempt_at > datetime.now(UTC)
    assert steps[0].status == "retrying"
    assert steps[0].attempt_count == 1

    # Attempt 2: transient failure again -> retrying, attempt_count == 2.
    with SessionFactory() as session:
        outcome_2 = automation_worker.run_step(session, run, 0, registry)
    assert outcome_2.status == "retrying"
    assert adapter.execute_calls == 2
    with SessionFactory() as session:
        steps = automation_worker.list_run_steps(session, workspace_id, run.id)
    assert steps[0].attempt_count == 2

    # Attempt 3: succeeds -- attempt_count stays 2 (never incremented on
    # success), execute() called exactly 3 times total.
    with SessionFactory() as session:
        run = automation_worker.get_run(session, workspace_id, run.id)
        assert run is not None
        outcome_3 = automation_worker.run_step(session, run, 0, registry)
    assert outcome_3.status == "succeeded"
    assert adapter.execute_calls == 3
    with SessionFactory() as session:
        steps = automation_worker.list_run_steps(session, workspace_id, run.id)
    assert steps[0].status == "succeeded"
    assert steps[0].attempt_count == 2


# ---------------------------------------------------------------------------
# 2. Bounded exhaustion -- fails after MAX_RETRY_ATTEMPTS, never retried again.
# ---------------------------------------------------------------------------


def test_retry_exhausts_after_max_attempts_then_fails_and_never_retries_again(
    retry_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = retry_test_context
    workflow_id = f"test.retry-exhaust.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.always-transient"))
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    adapter = AlwaysTransientAdapter()
    registry = _make_registry(adapter)

    run = _enqueue_and_claim(workspace_id, user_id, workflow_id)

    # MAX_RETRY_ATTEMPTS attempts, each transiently failing -> 'retrying'.
    for expected_attempt in range(1, automation_worker.MAX_RETRY_ATTEMPTS + 1):
        with SessionFactory() as session:
            outcome = automation_worker.run_step(session, run, 0, registry)
        assert outcome.status == "retrying", f"attempt {expected_attempt} unexpectedly not retrying"
        with SessionFactory() as session:
            run = automation_worker.get_run(session, workspace_id, run.id)
            assert run is not None
            steps = automation_worker.list_run_steps(session, workspace_id, run.id)
        assert steps[0].attempt_count == expected_attempt

    assert adapter.execute_calls == automation_worker.MAX_RETRY_ATTEMPTS

    # One more attempt: exhausted -- unconditional, bounded 'failed'.
    with SessionFactory() as session:
        final_outcome = automation_worker.run_step(session, run, 0, registry)
    assert final_outcome.status == "failed"
    assert final_outcome.error_class == "TransientAdapterError"
    assert adapter.execute_calls == automation_worker.MAX_RETRY_ATTEMPTS + 1
    with SessionFactory() as session:
        steps = automation_worker.list_run_steps(session, workspace_id, run.id)
    assert steps[0].status == "failed"
    assert steps[0].attempt_count == automation_worker.MAX_RETRY_ATTEMPTS

    # Calling run_step again for the same (now-failed) step never calls
    # execute() a further time -- "never retried a further time," proven
    # directly, not merely by absence of an exception.
    with SessionFactory() as session:
        run = automation_worker.get_run(session, workspace_id, run.id)
        assert run is not None
        repeat_outcome = automation_worker.run_step(session, run, 0, registry)
    assert repeat_outcome.status == "failed"
    assert adapter.execute_calls == automation_worker.MAX_RETRY_ATTEMPTS + 1


# ---------------------------------------------------------------------------
# 3. claim_next_run honors next_attempt_at.
# ---------------------------------------------------------------------------


def test_claim_next_run_does_not_reclaim_before_next_attempt_at_but_does_after(
    retry_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = retry_test_context
    workflow_id = f"test.retry-claim-gate.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.always-transient"))
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    adapter = AlwaysTransientAdapter()
    registry = _make_registry(adapter)

    run = _enqueue_and_claim(workspace_id, user_id, workflow_id)
    with SessionFactory() as session:
        automation_worker.run_step(session, run, 0, registry)

    # Not due yet -- claim_next_run must not reclaim this run.
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-b")
    assert claimed is None

    # Simulate the backoff window elapsing (no real sleep needed --
    # directly advance next_attempt_at into the past, exactly the state a
    # real clock reaching it would produce).
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE workflow_runs SET next_attempt_at = now() - interval '1 second' "
                "WHERE id = :id"
            ),
            {"id": run.id},
        )

    with SessionFactory() as session:
        reclaimed = automation_worker.claim_next_run(session, "worker-b")
    assert reclaimed is not None
    assert reclaimed.id == run.id


# ---------------------------------------------------------------------------
# 4. Backoff schedule.
# ---------------------------------------------------------------------------


def test_retry_backoff_seconds_schedule() -> None:
    assert automation_worker._retry_backoff_seconds(1) == 2
    assert automation_worker._retry_backoff_seconds(2) == 4
    assert automation_worker._retry_backoff_seconds(3) == 8
