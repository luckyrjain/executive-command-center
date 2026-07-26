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

Plus the two retry-resume seam guarantees added by the post-Task-6 audit of
`run_step`'s existing-row branch (worker.py's own "Task 6 retry-resume,
corrected" docstring section). Both are the direct analogues, for a
resumed attempt, of properties `test_automation_worker_postgres.py` already
proves for a first attempt:

5. **A resumed attempt commits `'dispatched'` before calling `execute()`,
   so a crash inside that call resolves to `'unknown'`, never a second
   `execute()`.** The retry-path analogue of
   `test_unknown_outcome_surfaces_without_retry` (the brand-new-row
   crash-in-the-gap case) and of `DigestVisibilityProbeAdapter`'s
   independent-connection durability probe. Before the fix the row stayed
   at `'retrying'` across `execute()`, so the next worker to claim the run
   after a mid-`execute()` crash took the identical re-dispatch branch and
   called `execute()` again -- once per lease expiry, unbounded.
6. **A resumed attempt re-checks policy usability.** The retry-path
   analogue of `test_revoking_policy_mid_run_blocks_the_next_step_but_not_
   the_one_already_succeeded` (which revokes between two *different*
   steps); this revokes between two attempts of the *same* step. Before
   the fix `_evaluate_dispatch_gate` ran only on first dispatch, so a
   policy revoked during the backoff window was never re-consulted.
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


class SimulatedProcessDeath(BaseException):
    """Deliberately **not** an `Exception` subclass.

    `run_step`'s two `except` clauses catch `TransientAdapterError` and
    `Exception`; a `BaseException` that is neither propagates straight out
    without `run_step` recording any outcome and without its session ever
    committing again -- which is exactly the observable database aftermath
    of the process dying mid-`execute()`, the one crash timing Decision 3's
    commit-before-`execute()` rule exists to cover. Raising this instead of
    genuinely killing a process is what makes the crash assertion
    deterministic: the test can still read the committed row afterwards.
    """


def _committed_step_status_over_independent_connection(run_id: UUID, step_index: int) -> str | None:
    """Reads one `workflow_run_steps` row's `status` over a brand-new
    connection -- never the caller's own `session`, so it cannot see that
    session's uncommitted work through the same transaction.

    This is the load-bearing mechanic behind the two probe adapters below,
    exactly as in `test_automation_worker_postgres.py`'s
    `DigestVisibilityProbeAdapter`: under this database's default READ
    COMMITTED isolation an uncommitted write is invisible outside its own
    transaction, so a probe called from inside `adapter.execute()` that
    observes `'dispatched'` proves `run_step` really did `COMMIT` that
    transition *before* calling `execute()`, rather than merely `flush()`
    it or (the bug being fixed) never write it at all.
    """
    with engine.connect() as independent_connection:
        row = (
            independent_connection.execute(
                text(
                    "SELECT status FROM workflow_run_steps "
                    "WHERE run_id = :run_id AND step_index = :step_index"
                ),
                {"run_id": run_id, "step_index": step_index},
            )
            .mappings()
            .one_or_none()
        )
    return row["status"] if row is not None else None


class _RetryDispatchProbeAdapter:
    """Shared base for the two probes below: on every `execute()` call it
    records the committed `status` an independent connection sees for its own
    step row, raises `TransientAdapterError` on call 1 (driving the step to
    `'retrying'`), and delegates call 2 -- the resumed attempt, the one this
    fix is about -- to `_second_attempt`.
    """

    input_schema = EchoInput
    output_schema = EchoOutput
    reversible = True
    high_impact_categories: frozenset[str] = frozenset()

    def __init__(self, run_id: UUID, step_index: int) -> None:
        self._run_id = run_id
        self._step_index = step_index
        self.execute_calls = 0
        self.statuses_seen_from_independent_connection: list[str | None] = []

    def simulate(self, action_input: EchoInput) -> EchoOutput:  # noqa: D102
        return EchoOutput(value=action_input.value)

    def _second_attempt(self, action_input: EchoInput) -> EchoOutput:
        raise NotImplementedError

    def execute(self, action_input: EchoInput) -> EchoOutput:  # noqa: D102
        self.execute_calls += 1
        self.statuses_seen_from_independent_connection.append(
            _committed_step_status_over_independent_connection(self._run_id, self._step_index)
        )
        if self.execute_calls == 1:
            raise TransientAdapterError("transient failure, attempt 1")
        return self._second_attempt(action_input)


class RetryThenCrashProbeAdapter(_RetryDispatchProbeAdapter):
    """Fails transiently once, then "crashes" on the resumed attempt --
    leaving behind precisely the database state a real mid-`execute()`
    process death leaves, for the test to assert on.
    """

    adapter_id = "test.retry-then-crash-probe"

    def _second_attempt(self, action_input: EchoInput) -> EchoOutput:
        raise SimulatedProcessDeath("the worker process died inside execute()")


class RetryThenSucceedProbeAdapter(_RetryDispatchProbeAdapter):
    """Fails transiently once, then succeeds on the resumed attempt -- the
    non-crashing counterpart, guarding against a fix that double-dispatches
    (`execute_calls` must be exactly 2) while still proving both attempts
    were preceded by a committed `'dispatched'` row.
    """

    adapter_id = "test.retry-then-succeed-probe"

    def _second_attempt(self, action_input: EchoInput) -> EchoOutput:
        return EchoOutput(value=action_input.value)


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


# ---------------------------------------------------------------------------
# 5. Retry-resume commits 'dispatched' before execute(), so a crash inside
#    that call resolves to 'unknown' -- never a second execute().
# ---------------------------------------------------------------------------


def _expire_backoff_window(run_id: UUID) -> None:
    """Advance `next_attempt_at` into the past -- exactly the state a real
    clock reaching the backoff deadline produces, with no sleep (this
    module's `test_claim_next_run_does_not_reclaim_before_next_attempt_at_
    but_does_after` uses the identical mechanic).
    """
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE workflow_runs SET next_attempt_at = now() - interval '1 second' "
                "WHERE id = :id"
            ),
            {"id": run_id},
        )


def _expire_lease(run_id: UUID) -> None:
    """Push `leased_until` into the past, the way a crashed worker's lease
    simply stops being renewed (`test_automation_worker_postgres.py`'s
    `test_crash_recovery_reclaims_expired_leased_run` uses the same shape).
    """
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE workflow_runs SET leased_until = now() - interval '1 second' WHERE id = :id"
            ),
            {"id": run_id},
        )


def _single_step_row(workspace_id: UUID, run_id: UUID) -> Any:
    with SessionFactory() as session:
        steps = automation_worker.list_run_steps(session, workspace_id, run_id)
    assert len(steps) == 1
    return steps[0]


def test_retry_resume_commits_dispatched_before_execute_so_a_crash_becomes_unknown(
    retry_test_context: tuple[UUID, UUID],
) -> None:
    """The retry-path analogue of `test_automation_worker_postgres.py`'s
    `test_unknown_outcome_surfaces_without_retry`.

    A resumed attempt must get the identical crash-in-the-gap protection a
    first attempt already had: the row committed as `'dispatched'` *before*
    `execute()`, so a crash anywhere inside that call leaves
    `'dispatched'`+no-outcome for the next claim attempt to resolve to
    `'unknown'`/`needs_review`.

    Before the fix, the row stayed at `'retrying'` for the whole duration of
    `execute()`. A crash there left `'retrying'`, the next worker to claim
    the run read `'retrying'`, took the identical re-dispatch branch, and
    called `execute()` a *second* time on a side effect that may already
    have partially or fully occurred -- repeatable once per lease expiry,
    with no bound at all. Both halves are asserted directly: what the
    adapter saw committed while it was running, and that the second claim
    attempt never calls `execute()` again.
    """
    workspace_id, user_id = retry_test_context
    workflow_id = f"test.retry-resume-crash.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.retry-then-crash-probe"))
    _publish_workflow(workspace_id, user_id, workflow_id, graph)

    run = _enqueue_and_claim(workspace_id, user_id, workflow_id)
    adapter = RetryThenCrashProbeAdapter(run.id, 0)
    registry = _make_registry(adapter)

    # Attempt 1: transient failure -> the step row is 'retrying', the run is
    # released back to 'queued' behind a future next_attempt_at.
    with SessionFactory() as session:
        first_outcome = automation_worker.run_step(session, run, 0, registry)
    assert first_outcome.status == "retrying"
    assert adapter.execute_calls == 1
    assert _single_step_row(workspace_id, run.id).status == "retrying"

    # The backoff window elapses and a worker reclaims the run.
    _expire_backoff_window(run.id)
    with SessionFactory() as session:
        reclaimed = automation_worker.claim_next_run(session, "worker-b")
    assert reclaimed is not None
    assert reclaimed.id == run.id

    # Attempt 2 (the resume): the worker process dies inside execute().
    # SimulatedProcessDeath is not an `Exception`, so run_step records no
    # outcome and its session never commits again -- the aftermath is
    # exactly what a real crash at this instant leaves behind.
    with pytest.raises(SimulatedProcessDeath):  # noqa: PT012
        with SessionFactory() as session:
            automation_worker.run_step(session, reclaimed, 0, registry)
    assert adapter.execute_calls == 2

    # The whole point: the adapter, reading over a connection that cannot
    # see run_step's uncommitted work, saw a *committed* 'dispatched' row on
    # the resumed attempt -- not the 'retrying' it would have seen before
    # this fix. (Attempt 1's own observation is 'dispatched' too, from the
    # brand-new-row INSERT+commit that always worked.)
    assert adapter.statuses_seen_from_independent_connection == ["dispatched", "dispatched"]

    # And the durable aftermath of the crash is 'dispatched' with no
    # outcome, attempt_count carried forward untouched by the resume.
    crashed_step = _single_step_row(workspace_id, run.id)
    assert crashed_step.status == "dispatched"
    assert crashed_step.attempt_count == 1
    assert crashed_step.finished_at is None

    # The next worker to claim the run resolves that to 'unknown' without
    # ever calling execute() again -- "unknown external outcome moves to
    # review, never blind retry" (EXECUTION-CONTRACT.md), now holding on the
    # retry path exactly as it always did on the first-dispatch path.
    _expire_lease(run.id)
    with SessionFactory() as session:
        reclaimed_after_crash = automation_worker.claim_next_run(session, "worker-c")
    assert reclaimed_after_crash is not None
    assert reclaimed_after_crash.id == run.id
    with SessionFactory() as session:
        finished = automation_worker.process_claimed_run(
            session, reclaimed_after_crash, registry, "worker-c"
        )
    assert finished.status == "needs_review"
    assert finished.finished_at is None
    assert adapter.execute_calls == 2  # never a third call
    assert _single_step_row(workspace_id, run.id).status == "unknown"


def test_retry_resume_still_succeeds_with_exactly_one_execute_call_per_attempt(
    retry_test_context: tuple[UUID, UUID],
) -> None:
    """Regression guard for the fix itself: adding the pre-`execute()`
    `'dispatched'` commit to the retry-resume path must not double-dispatch.
    A normal (non-crashed) resume still succeeds, `execute()` is called
    exactly once per attempt (two calls total, never three), `attempt_count`
    stays at the value the single transient failure produced, and both
    attempts were preceded by a committed `'dispatched'` row.
    """
    workspace_id, user_id = retry_test_context
    workflow_id = f"test.retry-resume-succeed.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.retry-then-succeed-probe"))
    _publish_workflow(workspace_id, user_id, workflow_id, graph)

    run = _enqueue_and_claim(workspace_id, user_id, workflow_id)
    adapter = RetryThenSucceedProbeAdapter(run.id, 0)
    registry = _make_registry(adapter)

    with SessionFactory() as session:
        first_outcome = automation_worker.run_step(session, run, 0, registry)
    assert first_outcome.status == "retrying"
    assert adapter.execute_calls == 1

    _expire_backoff_window(run.id)
    with SessionFactory() as session:
        reclaimed = automation_worker.claim_next_run(session, "worker-b")
    assert reclaimed is not None

    with SessionFactory() as session:
        finished = automation_worker.process_claimed_run(session, reclaimed, registry, "worker-b")
    assert finished.status == "succeeded"
    assert adapter.execute_calls == 2
    assert adapter.statuses_seen_from_independent_connection == ["dispatched", "dispatched"]

    succeeded_step = _single_step_row(workspace_id, run.id)
    assert succeeded_step.status == "succeeded"
    assert succeeded_step.attempt_count == 1


# ---------------------------------------------------------------------------
# 6. Retry-resume re-checks policy usability.
# ---------------------------------------------------------------------------


def test_revoking_policy_between_two_retry_attempts_blocks_the_resumed_attempt(
    retry_test_context: tuple[UUID, UUID],
) -> None:
    """The same-step analogue of `test_automation_worker_postgres.py`'s
    `test_revoking_policy_mid_run_blocks_the_next_step_but_not_the_one_
    already_succeeded`, which revokes between two *different* steps. Here the
    revocation lands inside one step's own retry backoff window.

    `APPROVAL-POLICY.md`: "Revocation takes effect immediately for any
    not-yet-started step" -- a step waiting out a backoff window has not
    started its next attempt, so the resumed attempt must be blocked.
    Before the fix `_evaluate_dispatch_gate` ran only on first dispatch, so
    the retried `execute()` proceeded under authority that had been
    withdrawn.
    """
    workspace_id, user_id = retry_test_context
    workflow_id = f"test.retry-resume-revoked.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.always-transient"))
    active = _publish_workflow(workspace_id, user_id, workflow_id, graph)
    assert active.policy_ref is not None
    adapter = AlwaysTransientAdapter()
    registry = _make_registry(adapter)

    run = _enqueue_and_claim(workspace_id, user_id, workflow_id)

    # Attempt 1: transient failure -> 'retrying', run 'queued' behind a
    # future next_attempt_at.
    with SessionFactory() as session:
        first_outcome = automation_worker.run_step(session, run, 0, registry)
    assert first_outcome.status == "retrying"
    assert adapter.execute_calls == 1
    with SessionFactory() as session:
        requeued = automation_worker.get_run(session, workspace_id, run.id)
    assert requeued is not None
    assert requeued.status == "queued"
    assert requeued.next_attempt_at is not None
    assert requeued.next_attempt_at > datetime.now(UTC)
    assert _single_step_row(workspace_id, run.id).status == "retrying"

    # The authorizing policy is revoked *between* the two attempts.
    with SessionFactory() as session, session.begin():
        revoked = automation_policy.revoke_policy(session, workspace_id, user_id, active.policy_ref)
    assert isinstance(revoked, automation_policy.AutomationPolicy)

    # The backoff window elapses; the run is reclaimed as usual (revocation
    # is not a claim-time gate -- it is enforced at dispatch, like the
    # first-dispatch case).
    _expire_backoff_window(run.id)
    with SessionFactory() as session:
        reclaimed = automation_worker.claim_next_run(session, "worker-b")
    assert reclaimed is not None
    assert reclaimed.id == run.id

    # run_step returns the identical outcome value first dispatch returns
    # for a revoked policy -- not a StepOutcome, and no execute() call.
    with SessionFactory() as session:
        blocked = automation_worker.run_step(session, reclaimed, 0, registry)
    assert isinstance(blocked, automation_worker.StepBlockedByPolicy)
    assert blocked.step_index == 0
    assert blocked.reason == "policy_revoked"
    assert adapter.execute_calls == 1  # unchanged -- never re-executed

    # The step row is untouched by the blocked attempt: still 'retrying',
    # attempt_count still 1, deliberately never advanced to 'dispatched'
    # (nothing was dispatched).
    still_retrying = _single_step_row(workspace_id, run.id)
    assert still_retrying.status == "retrying"
    assert still_retrying.attempt_count == 1

    # Driven through process_claimed_run, StepBlockedByPolicy maps to
    # needs_review through the exact same branch a first-dispatch block
    # takes -- no retry-specific run state.
    _expire_lease(run.id)
    _expire_backoff_window(run.id)
    with SessionFactory() as session:
        reclaimed_again = automation_worker.claim_next_run(session, "worker-c")
    assert reclaimed_again is not None
    with SessionFactory() as session:
        finished = automation_worker.process_claimed_run(
            session, reclaimed_again, registry, "worker-c"
        )
    assert finished.status == "needs_review"
    assert finished.finished_at is None
    assert adapter.execute_calls == 1


def test_expiring_policy_between_two_retry_attempts_blocks_the_resumed_attempt(
    retry_test_context: tuple[UUID, UUID],
) -> None:
    """The expiry half of the same gap -- `is_policy_usable` covers revoked
    *and* expired, and `StepBlockedByPolicy.reason` distinguishes them, so
    the resumed attempt must report `'policy_expired'` here rather than
    `'policy_revoked'`.
    """
    workspace_id, user_id = retry_test_context
    workflow_id = f"test.retry-resume-expired.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.always-transient"))
    active = _publish_workflow(workspace_id, user_id, workflow_id, graph)
    assert active.policy_ref is not None
    adapter = AlwaysTransientAdapter()
    registry = _make_registry(adapter)

    run = _enqueue_and_claim(workspace_id, user_id, workflow_id)
    with SessionFactory() as session:
        first_outcome = automation_worker.run_step(session, run, 0, registry)
    assert first_outcome.status == "retrying"
    assert adapter.execute_calls == 1

    # The policy expires during the backoff window.
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE automation_policies SET expires_at = :past WHERE id = :id"),
            {"past": datetime.now(UTC) - timedelta(seconds=1), "id": active.policy_ref},
        )

    _expire_backoff_window(run.id)
    with SessionFactory() as session:
        reclaimed = automation_worker.claim_next_run(session, "worker-b")
    assert reclaimed is not None
    with SessionFactory() as session:
        blocked = automation_worker.run_step(session, reclaimed, 0, registry)
    assert isinstance(blocked, automation_worker.StepBlockedByPolicy)
    assert blocked.reason == "policy_expired"
    assert adapter.execute_calls == 1
    assert _single_step_row(workspace_id, run.id).status == "retrying"
