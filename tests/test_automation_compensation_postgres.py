"""Phase 5 Automation Task 6: compensation dispatch
(`docs/superpowers/specs/2026-07-25-phase-5-automation-design.md` Decision
9, `docs/phases/phase-005/DATA-MODEL.md`'s `compensation_steps`).

A new, dedicated test module for this task's own new mechanic. Covers,
per this task's own required minimum:

1. A regression proving `process_claimed_run`'s main walk no longer
   crashes (`UnsupportedStepType`, unhandled) when it passes over a
   `step_type='compensation'` step placed inline in the flat `steps`
   list -- the real bug this task's own instructions named explicitly.
2. A workflow whose step 1 succeeds (with `compensate_ref` set) and step 2
   fails compensates step 1 and ends `compensated`.
3. Two qualifying compensations dispatch in descending (most-recently-
   succeeded-first) order.
4. A compensation that itself raises ends the run `compensation_failed`,
   and the ledger (`compensation_steps`) records the failure.
5. A failed step whose earlier steps declare no `compensate_ref` behaves
   exactly as today (`failed`, unchanged regression coverage).
6. The two-shapes adapter-resolution judgment call: the original step's
   own `compensate()` is preferred when the adapter declares one, called
   with the *original* step's own resolved input (not the compensation
   step's); a distinct compensation-step adapter is used otherwise.
7. The dispatch-time half of the compensation approval-gate-bypass fix
   (`worker._compensation_policy_usable`): a policy revoked mid-run blocks
   compensation dispatch entirely -- the compensation adapter's
   `execute()`/`compensate()` is never called, and both the step row and the
   `compensation_steps` ledger row record `'failed'` /
   `'PolicyUnusableDuringCompensation'`. The complementary publish-time half
   (`workflows.high_impact_compensation_action_refs`, which rejects a
   compensation step naming a high-impact adapter outright) is covered in
   `test_automation_workflows_postgres.py`, since it never dispatches a run.

Plus two regressions added by the run-state audit, both about how this
mechanic joins up with the rest of the run-state machine:

8. A step failing because **a human rejected its approval** compensates
   exactly like the identical step failing because its adapter raised --
   the Task 3 x Task 6 seam `approvals._advance_run_after_decision` used to
   bypass entirely by writing `'failed'` straight onto the run.
9. A lease lost inside one step's `execute()` never strands the run in
   `'compensating'` -- `_mark_compensating` is lease-guarded, and
   `'compensating'` with a `NULL` lease is a state no claim predicate ever
   reclaims (`worker.py`'s "Lease-ownership guard" section).

Plus one added by the test-coverage audit:

10. **A genuinely mixed ledger** -- one compensation succeeds and a
    *different* one fails inside a single sequence, and the persisted
    `compensation_steps` ledger records both outcomes. Every test above
    exercises a uniform ledger (all-succeeded, all-blocked, or a single row),
    none of which can distinguish `_run_compensation_sequence`'s "never stop
    early" guarantee from an early exit after the first failure. See that
    test's own docstring for the case-by-case reasoning.
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
from ecc.domains.automation import approvals as automation_approvals
from ecc.domains.automation import policy as automation_policy
from ecc.domains.automation import worker as automation_worker
from ecc.domains.automation import workflows as automation_workflows
from ecc.domains.automation.adapters import AdapterRegistry

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


class SucceedingAdapter:
    """Plain, always-succeeds action adapter -- no `compensate()`."""

    def __init__(self, adapter_id: str) -> None:
        self.adapter_id = adapter_id
        self.input_schema = EchoInput
        self.output_schema = EchoOutput
        self.reversible = True
        self.high_impact_categories: frozenset[str] = frozenset()
        self.execute_calls = 0

    def simulate(self, action_input: EchoInput) -> EchoOutput:  # noqa: D102
        return EchoOutput(value=action_input.value)

    def execute(self, action_input: EchoInput) -> EchoOutput:  # noqa: D102
        self.execute_calls += 1
        return EchoOutput(value=action_input.value)


class FailingAdapter:
    """Always raises -- the step that triggers compensation."""

    def __init__(self, adapter_id: str) -> None:
        self.adapter_id = adapter_id
        self.input_schema = EchoInput
        self.output_schema = EchoOutput
        self.reversible = True
        self.high_impact_categories: frozenset[str] = frozenset()
        self.execute_calls = 0

    def simulate(self, action_input: EchoInput) -> EchoOutput:  # noqa: D102
        return EchoOutput(value=action_input.value)

    def execute(self, action_input: EchoInput) -> EchoOutput:  # noqa: D102
        self.execute_calls += 1
        raise RuntimeError("this step always fails")


class CompensatableAdapter:
    """Declares `compensate()` -- the preferred dispatch shape (module
    docstring's two-shapes judgment call). Records the exact `value` it
    was compensated with, so a test can assert it was the *original*
    step's own resolved input, not the compensation step's own `input_
    mapping`.
    """

    def __init__(self, adapter_id: str, *, dispatch_log: list[str] | None = None) -> None:
        self.adapter_id = adapter_id
        self.input_schema = EchoInput
        self.output_schema = EchoOutput
        self.reversible = True
        self.high_impact_categories: frozenset[str] = frozenset()
        self.execute_calls = 0
        self.compensate_calls = 0
        self.last_compensated_value: str | None = None
        self._dispatch_log = dispatch_log

    def simulate(self, action_input: EchoInput) -> EchoOutput:  # noqa: D102
        return EchoOutput(value=action_input.value)

    def execute(self, action_input: EchoInput) -> EchoOutput:  # noqa: D102
        self.execute_calls += 1
        return EchoOutput(value=action_input.value)

    def compensate(self, action_input: EchoInput) -> EchoOutput:  # noqa: D102
        self.compensate_calls += 1
        self.last_compensated_value = action_input.value
        if self._dispatch_log is not None:
            self._dispatch_log.append(self.adapter_id)
        return EchoOutput(value=f"compensated:{action_input.value}")


class FailingCompensationAdapter:
    """`compensate()` itself raises -- proves `compensation_failed`."""

    def __init__(self, adapter_id: str) -> None:
        self.adapter_id = adapter_id
        self.input_schema = EchoInput
        self.output_schema = EchoOutput
        self.reversible = True
        self.high_impact_categories: frozenset[str] = frozenset()
        self.compensate_calls = 0

    def simulate(self, action_input: EchoInput) -> EchoOutput:  # noqa: D102
        return EchoOutput(value=action_input.value)

    def execute(self, action_input: EchoInput) -> EchoOutput:  # noqa: D102
        return EchoOutput(value=action_input.value)

    def compensate(self, action_input: EchoInput) -> EchoOutput:  # noqa: D102
        self.compensate_calls += 1
        raise RuntimeError("compensation blew up")


class PolicyRevokingFailingAdapter:
    """Fails the step that triggers compensation, and -- immediately before
    raising, from its own separate session/transaction -- revokes the run's
    authorizing policy. This is how "the policy is revoked *mid-run*, after
    every ordinary step's own `_evaluate_dispatch_gate` already passed, but
    before compensation dispatches" is expressed deterministically in a
    single-threaded test: `process_claimed_run` drives a whole run to a
    terminal state inside one call, so the only place to interpose a
    concurrent policy change is inside an adapter the worker itself invokes.

    Uses a fresh `SessionFactory()` session deliberately, not the worker's:
    the revocation must be *committed by someone else* for the worker's own
    later `get_policy` read to see it, which is exactly the real-world shape
    (an operator hitting `POST /policies/{id}/revoke` while a run is in
    flight). Safe against lock contention because the worker's session has
    no open transaction at this moment -- `run_step` committed the step's
    `'dispatched'` row before calling `execute()` (this module's
    digest-before-execute discipline) -- and because `get_policy` never takes
    `FOR UPDATE`.
    """

    def __init__(
        self, adapter_id: str, *, workspace_id: UUID, policy_id: UUID, actor_id: UUID
    ) -> None:
        self.adapter_id = adapter_id
        self.input_schema = EchoInput
        self.output_schema = EchoOutput
        self.reversible = True
        self.high_impact_categories: frozenset[str] = frozenset()
        self.execute_calls = 0
        self._workspace_id = workspace_id
        self._policy_id = policy_id
        self._actor_id = actor_id

    def simulate(self, action_input: EchoInput) -> EchoOutput:  # noqa: D102
        return EchoOutput(value=action_input.value)

    def execute(self, action_input: EchoInput) -> EchoOutput:  # noqa: D102
        self.execute_calls += 1
        with SessionFactory() as session, session.begin():
            revoked = automation_policy.revoke_policy(
                session, self._workspace_id, self._actor_id, self._policy_id
            )
        assert isinstance(revoked, automation_policy.AutomationPolicy)
        assert revoked.revoked_at is not None
        raise RuntimeError("this step always fails, and revoked the policy on its way out")


class HighImpactUndoAdapter:
    """A compensation-step adapter that declares a non-empty
    `high_impact_categories` -- the shape every other fake in this module
    deliberately does not have (they all declare `frozenset()`), and the
    shape the compensation approval-gate-bypass bug turned into an
    unapproved real execution.

    Registered under the *compensation* step's own `action_ref` and
    declaring no `compensate()`, so `_dispatch_compensation_step` resolves
    the fallback shape and would call this adapter's ordinary `execute()`.
    `execute_calls` staying at 0 is the assertion that matters: it is direct
    evidence no side effect occurred, not merely that a status column says
    so.

    A graph naming this adapter from a compensation step could no longer be
    published through `publish_workflow_endpoint` (that is fix 1's
    publish-time rejection, covered in `test_automation_workflows_
    postgres.py`). This module's `_publish_workflow` calls
    `activate_workflow_version` *without* an `adapter_registry`, which is
    exactly how an already-published, pre-fix workflow behaves -- so this is
    a faithful stand-in for the existing rows the dispatch-time check has to
    protect, not a contrived bypass.
    """

    def __init__(self, adapter_id: str) -> None:
        self.adapter_id = adapter_id
        self.input_schema = EchoInput
        self.output_schema = EchoOutput
        self.reversible = False
        self.high_impact_categories: frozenset[str] = frozenset({"person-directed"})
        self.execute_calls = 0

    def simulate(self, action_input: EchoInput) -> EchoOutput:  # noqa: D102
        return EchoOutput(value=action_input.value)

    def execute(self, action_input: EchoInput) -> EchoOutput:  # noqa: D102
        self.execute_calls += 1
        return EchoOutput(value=f"undone:{action_input.value}")


class DedicatedUndoAdapter:
    """A *distinct* adapter, registered under the compensation step's own
    `action_ref`, dispatched via ordinary `execute()` -- the fallback shape
    when the original step's own adapter does not declare `compensate()`.
    """

    def __init__(self, adapter_id: str) -> None:
        self.adapter_id = adapter_id
        self.input_schema = EchoInput
        self.output_schema = EchoOutput
        self.reversible = True
        self.high_impact_categories: frozenset[str] = frozenset()
        self.execute_calls = 0
        self.last_executed_value: str | None = None

    def simulate(self, action_input: EchoInput) -> EchoOutput:  # noqa: D102
        return EchoOutput(value=action_input.value)

    def execute(self, action_input: EchoInput) -> EchoOutput:  # noqa: D102
        self.execute_calls += 1
        self.last_executed_value = action_input.value
        return EchoOutput(value=f"undone:{action_input.value}")


class HighImpactAdapter:
    """`person-directed` (Decision 5) -- always requires a fresh per-run
    human approval regardless of `approval_mode`, so a step using it pauses
    the run in `waiting_approval` instead of dispatching. Used by this
    module's rejection-path test to make a *human's* "no" the thing that
    fails a step, rather than an adapter exception.
    """

    def __init__(self, adapter_id: str) -> None:
        self.adapter_id = adapter_id
        self.input_schema = EchoInput
        self.output_schema = EchoOutput
        self.reversible = False
        self.high_impact_categories: frozenset[str] = frozenset({"person-directed"})
        self.execute_calls = 0

    def simulate(self, action_input: EchoInput) -> EchoOutput:  # noqa: D102
        return EchoOutput(value=action_input.value)

    def execute(self, action_input: EchoInput) -> EchoOutput:  # noqa: D102
        self.execute_calls += 1
        return EchoOutput(value=action_input.value)


class LeaseHandoverFailingAdapter:
    """Raises (so the compensation path is taken) *after* simulating a
    complete lease handover on an independent connection: a second worker
    reclaiming this run and parking it in `needs_review` with `leased_by =
    NULL`, exactly as `worker._pause_run` would. No crash or `sleep` is
    needed -- a step slower than `LEASE_DURATION_SECONDS` plus one other
    worker's ordinary poll cycle produces this same state, which is why the
    hole this reproduces was rated HIGH rather than theoretical.
    """

    def __init__(self, adapter_id: str, run_id: UUID) -> None:
        self.adapter_id = adapter_id
        self.input_schema = EchoInput
        self.output_schema = EchoOutput
        self.reversible = True
        self.high_impact_categories: frozenset[str] = frozenset()
        self._run_id = run_id
        self.execute_calls = 0

    def simulate(self, action_input: EchoInput) -> EchoOutput:  # noqa: D102
        return EchoOutput(value=action_input.value)

    def execute(self, action_input: EchoInput) -> EchoOutput:  # noqa: D102
        self.execute_calls += 1
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE workflow_runs SET status = 'needs_review', leased_by = NULL, "
                    "leased_until = NULL, updated_at = now() WHERE id = :id"
                ),
                {"id": self._run_id},
            )
        raise RuntimeError("failed after losing the lease")


def _make_registry(*adapters: Any) -> AdapterRegistry:
    registry = AdapterRegistry()
    for adapter in adapters:
        registry.register(adapter)
    return registry


def _action_step(
    step_id: str,
    action_ref: str,
    *,
    compensate_ref: str | None = None,
    input_mapping: dict[str, Any] | None = None,
) -> dict[str, Any]:
    step: dict[str, Any] = {
        "step_id": step_id,
        "step_type": "action",
        "action_ref": action_ref,
        "input_mapping": input_mapping or {},
        "on_success": "succeeded",
        "on_failure": "failed",
    }
    if compensate_ref is not None:
        step["compensate_ref"] = compensate_ref
    return step


def _compensation_step(
    step_id: str, action_ref: str, *, input_mapping: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "step_type": "compensation",
        "action_ref": action_ref,
        "input_mapping": input_mapping or {},
    }


def _linear_graph(*steps: dict[str, Any]) -> dict[str, Any]:
    """Steps in the exact order given, `step_index` == list position --
    `process_claimed_run`'s main walk advances by plain index increment,
    never by following `on_success`/`on_failure` (a real, already-disclosed
    property of this codebase, not something this task's own graphs need
    to route around).
    """
    return {"steps": list(steps)}


@pytest.fixture
def compensation_test_context() -> Iterator[tuple[UUID, UUID]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Automation Compensation Test', 'Asia/Kolkata', :now)"
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


def _run_to_completion(
    workspace_id: UUID, user_id: UUID, workflow_id: str, registry: AdapterRegistry
) -> automation_worker.WorkflowRun:
    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun)
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
        assert claimed is not None
        finished = automation_worker.process_claimed_run(session, claimed, registry, "worker-a")
    return finished


# ---------------------------------------------------------------------------
# 1. Regression: an inline compensation step never crashes the main walk.
# ---------------------------------------------------------------------------


def test_inline_compensation_step_never_crashes_the_main_walk(
    compensation_test_context: tuple[UUID, UUID],
) -> None:
    """`c1` sits at index 1, between `s1` (index 0) and `s2` (index 2), and
    is never a target of any `on_success`/`on_failure` edge -- proves the
    real bug this task's own instructions named: before the fix, `process_
    claimed_run`'s linear walk would pass `c1` straight into `run_step`,
    which raises `UnsupportedStepType` unhandled. With the fix, `c1` is
    skipped (no `workflow_run_steps` row written for it at all) and the
    run completes normally.
    """
    workspace_id, user_id = compensation_test_context
    workflow_id = f"test.inline-compensation.{uuid4().hex}"
    s1 = SucceedingAdapter("test.s1")
    c1 = CompensatableAdapter("test.c1")
    s2 = SucceedingAdapter("test.s2")
    graph = _linear_graph(
        _action_step("s1", "test.s1", compensate_ref="c1"),
        _compensation_step("c1", "test.c1"),
        _action_step("s2", "test.s2"),
    )
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    registry = _make_registry(s1, c1, s2)

    finished = _run_to_completion(workspace_id, user_id, workflow_id, registry)

    assert finished.status == "succeeded"
    assert s1.execute_calls == 1
    assert s2.execute_calls == 1
    assert c1.compensate_calls == 0  # never invoked -- nothing failed

    with SessionFactory() as session:
        steps = automation_worker.list_run_steps(session, workspace_id, finished.id)
    # Exactly two rows -- step_index 0 (s1) and 2 (s2). No row for index 1
    # (the skipped compensation step).
    assert {step.step_index for step in steps} == {0, 2}


# ---------------------------------------------------------------------------
# 2. A qualifying compensation dispatches and the run ends 'compensated'.
# ---------------------------------------------------------------------------


def test_step_1_succeeds_step_2_fails_compensates_step_1_ends_compensated(
    compensation_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = compensation_test_context
    workflow_id = f"test.compensate-one.{uuid4().hex}"
    s1 = CompensatableAdapter("test.s1", dispatch_log=[])
    s2 = FailingAdapter("test.s2")
    graph = _linear_graph(
        _action_step("s1", "test.s1", compensate_ref="c1", input_mapping={"value": "original"}),
        _action_step("s2", "test.s2"),
        _compensation_step("c1", "test.s1", input_mapping={"value": "ignored-by-compensate-shape"}),
    )
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    registry = _make_registry(s1, s2)

    finished = _run_to_completion(workspace_id, user_id, workflow_id, registry)

    assert finished.status == "compensated"
    assert s1.execute_calls == 1
    assert s2.execute_calls == 1
    assert s1.compensate_calls == 1
    # compensate() was called with the *original* step's own resolved
    # input, not the compensation step's own input_mapping.
    assert s1.last_compensated_value == "original"

    with SessionFactory() as session:
        steps = automation_worker.list_run_steps(session, workspace_id, finished.id)
        ledger = automation_worker.list_compensation_steps(session, workspace_id, finished.id)
    comp_step = next(step for step in steps if step.step_type == "compensation")
    assert comp_step.step_index == 2
    assert comp_step.status == "succeeded"
    assert len(ledger) == 1
    assert ledger[0].compensates_step_index == 0
    assert ledger[0].status == "succeeded"


# ---------------------------------------------------------------------------
# 3. Two qualifying compensations dispatch in descending order.
# ---------------------------------------------------------------------------


def test_two_qualifying_compensations_dispatch_in_descending_order(
    compensation_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = compensation_test_context
    workflow_id = f"test.compensate-two.{uuid4().hex}"
    dispatch_log: list[str] = []
    s1 = CompensatableAdapter("test.s1", dispatch_log=dispatch_log)
    s2 = CompensatableAdapter("test.s2", dispatch_log=dispatch_log)
    s3 = FailingAdapter("test.s3")
    graph = _linear_graph(
        _action_step("s1", "test.s1", compensate_ref="c1"),
        _action_step("s2", "test.s2", compensate_ref="c2"),
        _action_step("s3", "test.s3"),
        _compensation_step("c1", "test.s1"),
        _compensation_step("c2", "test.s2"),
    )
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    registry = _make_registry(s1, s2, s3)

    finished = _run_to_completion(workspace_id, user_id, workflow_id, registry)

    assert finished.status == "compensated"
    # s2 (the more recently succeeded original step) is compensated first.
    assert dispatch_log == ["test.s2", "test.s1"]


# ---------------------------------------------------------------------------
# 4. A compensation that itself fails -> compensation_failed, ledger records it.
# ---------------------------------------------------------------------------


def test_compensation_that_itself_fails_ends_compensation_failed(
    compensation_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = compensation_test_context
    workflow_id = f"test.compensation-fails.{uuid4().hex}"
    s1 = FailingCompensationAdapter("test.s1")
    s2 = FailingAdapter("test.s2")
    graph = _linear_graph(
        _action_step("s1", "test.s1", compensate_ref="c1"),
        _action_step("s2", "test.s2"),
        _compensation_step("c1", "test.s1"),
    )
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    registry = _make_registry(s1, s2)

    finished = _run_to_completion(workspace_id, user_id, workflow_id, registry)

    assert finished.status == "compensation_failed"
    assert s1.compensate_calls == 1

    with SessionFactory() as session:
        steps = automation_worker.list_run_steps(session, workspace_id, finished.id)
        ledger = automation_worker.list_compensation_steps(session, workspace_id, finished.id)
    comp_step = next(step for step in steps if step.step_type == "compensation")
    assert comp_step.status == "failed"
    assert comp_step.error_class == "RuntimeError"
    assert len(ledger) == 1
    assert ledger[0].status == "failed"
    assert ledger[0].error_class == "RuntimeError"


# ---------------------------------------------------------------------------
# 5. No compensate_ref declared -- unchanged 'failed' regression coverage.
# ---------------------------------------------------------------------------


def test_no_compensate_ref_declared_behaves_exactly_as_before(
    compensation_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = compensation_test_context
    workflow_id = f"test.no-compensation.{uuid4().hex}"
    s1 = SucceedingAdapter("test.s1")
    s2 = FailingAdapter("test.s2")
    graph = _linear_graph(
        _action_step("s1", "test.s1"),
        _action_step("s2", "test.s2"),
    )
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    registry = _make_registry(s1, s2)

    finished = _run_to_completion(workspace_id, user_id, workflow_id, registry)

    assert finished.status == "failed"
    assert s1.execute_calls == 1
    assert s2.execute_calls == 1

    with SessionFactory() as session:
        ledger = automation_worker.list_compensation_steps(session, workspace_id, finished.id)
    assert ledger == []


# ---------------------------------------------------------------------------
# 6. Fallback shape: original adapter has no compensate() -> the
#    compensation step's own distinct adapter is executed instead.
# ---------------------------------------------------------------------------


def test_original_adapter_without_compensate_falls_back_to_dedicated_undo_adapter(
    compensation_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = compensation_test_context
    workflow_id = f"test.dedicated-undo.{uuid4().hex}"
    s1 = SucceedingAdapter("test.s1")  # no compensate()
    s2 = FailingAdapter("test.s2")
    undo = DedicatedUndoAdapter("test.undo-s1")
    graph = _linear_graph(
        _action_step(
            "s1", "test.s1", compensate_ref="c1", input_mapping={"value": "from-comp-step"}
        ),
        _action_step("s2", "test.s2"),
        _compensation_step("c1", "test.undo-s1", input_mapping={"value": "from-comp-step"}),
    )
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    registry = _make_registry(s1, s2, undo)

    finished = _run_to_completion(workspace_id, user_id, workflow_id, registry)

    assert finished.status == "compensated"
    assert undo.execute_calls == 1
    assert undo.last_executed_value == "from-comp-step"


# ---------------------------------------------------------------------------
# 7. A policy revoked mid-run blocks compensation dispatch entirely
#    (`worker._compensation_policy_usable`).
#
#    The bug: `_dispatch_compensation_step` called `execute()`/`compensate()`
#    with no policy check of any kind, so a policy revoked after the last
#    ordinary step was gated still produced a real, un-reauthorized side
#    effect -- contradicting `APPROVAL-POLICY.md`'s "Revocation takes effect
#    immediately for any not-yet-started step" and `EXECUTION-CONTRACT.md`'s
#    "compensation executes only declared, *authorized* steps." Worse, the
#    compensation step's `action_ref` was free to name a high-impact adapter,
#    so what ran unapproved could be exactly the class of action that
#    "always requires per-run approval regardless of policy mode."
# ---------------------------------------------------------------------------


def test_policy_revoked_mid_run_blocks_compensation_and_never_calls_the_adapter(
    compensation_test_context: tuple[UUID, UUID],
) -> None:
    """`s1` (low-impact, no `compensate()`) succeeds with `compensate_ref=
    'c1'`; `s2` revokes the run's policy and then fails, triggering
    compensation; `c1` names a *high-impact* adapter -- precisely the
    gate-bypass scenario. The compensation must not execute: the adapter is
    never called, and both the `workflow_run_steps` row and the
    `compensation_steps` ledger row record `'failed'` /
    `'PolicyUnusableDuringCompensation'`, with the run ending
    `'compensation_failed'` for human review rather than `'compensated'`.
    """
    workspace_id, user_id = compensation_test_context
    workflow_id = f"test.revoked-mid-run.{uuid4().hex}"
    graph = _linear_graph(
        _action_step("s1", "test.s1", compensate_ref="c1", input_mapping={"value": "original"}),
        _action_step("s2", "test.s2"),
        _compensation_step("c1", "test.undo-s1", input_mapping={"value": "undo-me"}),
    )
    version = _publish_workflow(workspace_id, user_id, workflow_id, graph)
    assert version.policy_ref is not None

    s1 = SucceedingAdapter("test.s1")  # no compensate() -> fallback shape
    s2 = PolicyRevokingFailingAdapter(
        "test.s2", workspace_id=workspace_id, policy_id=version.policy_ref, actor_id=user_id
    )
    undo = HighImpactUndoAdapter("test.undo-s1")
    registry = _make_registry(s1, s2, undo)

    finished = _run_to_completion(workspace_id, user_id, workflow_id, registry)

    # Both ordinary steps really did dispatch (their own gate passed while
    # the policy was still usable) -- this is a mid-run revocation, not a
    # run that was never authorized at all.
    assert s1.execute_calls == 1
    assert s2.execute_calls == 1

    # The compensation adapter was never invoked. This, not a status column,
    # is the real proof no unapproved side effect occurred.
    assert undo.execute_calls == 0

    assert finished.status == "compensation_failed"

    with SessionFactory() as session:
        steps = automation_worker.list_run_steps(session, workspace_id, finished.id)
        ledger = automation_worker.list_compensation_steps(session, workspace_id, finished.id)

    comp_step = next(step for step in steps if step.step_type == "compensation")
    assert comp_step.step_index == 2
    assert comp_step.status == "failed"
    assert comp_step.error_class == "PolicyUnusableDuringCompensation"
    assert comp_step.output is None

    # The ledger a human reads for partial-recovery status agrees with the
    # step row -- the two must never diverge.
    assert len(ledger) == 1
    assert ledger[0].compensates_step_index == 0
    assert ledger[0].status == "failed"
    assert ledger[0].error_class == "PolicyUnusableDuringCompensation"


def test_policy_revoked_mid_run_blocks_every_qualifying_compensation_not_just_the_first(
    compensation_test_context: tuple[UUID, UUID],
) -> None:
    """`_run_compensation_sequence`'s "never stop early, keep attempting
    every qualifying compensation" guarantee must survive the new blocked
    outcome: two qualifying compensations both get their own attempt, so a
    human sees one blocked ledger row per compensation that was supposed to
    run rather than one blocked row and then silence.
    """
    workspace_id, user_id = compensation_test_context
    workflow_id = f"test.revoked-mid-run-two.{uuid4().hex}"
    graph = _linear_graph(
        _action_step("s1", "test.s1", compensate_ref="c1"),
        _action_step("s2", "test.s2", compensate_ref="c2"),
        _action_step("s3", "test.s3"),
        _compensation_step("c1", "test.undo-s1"),
        _compensation_step("c2", "test.undo-s2"),
    )
    version = _publish_workflow(workspace_id, user_id, workflow_id, graph)
    assert version.policy_ref is not None

    s1 = SucceedingAdapter("test.s1")
    s2 = SucceedingAdapter("test.s2")
    s3 = PolicyRevokingFailingAdapter(
        "test.s3", workspace_id=workspace_id, policy_id=version.policy_ref, actor_id=user_id
    )
    undo1 = HighImpactUndoAdapter("test.undo-s1")
    undo2 = HighImpactUndoAdapter("test.undo-s2")
    registry = _make_registry(s1, s2, s3, undo1, undo2)

    finished = _run_to_completion(workspace_id, user_id, workflow_id, registry)

    assert finished.status == "compensation_failed"
    assert undo1.execute_calls == 0
    assert undo2.execute_calls == 0

    with SessionFactory() as session:
        ledger = automation_worker.list_compensation_steps(session, workspace_id, finished.id)
    assert [(row.compensates_step_index, row.status, row.error_class) for row in ledger] == [
        (0, "failed", "PolicyUnusableDuringCompensation"),
        (1, "failed", "PolicyUnusableDuringCompensation"),
    ]


def test_usable_policy_still_compensates_normally(
    compensation_test_context: tuple[UUID, UUID],
) -> None:
    """Negative control for the new check: the same graph shape as the
    revocation test, with the policy left untouched, still compensates and
    ends `'compensated'` -- so a failing revocation test is evidence about
    the policy check specifically, not about compensation dispatch being
    broken in general.
    """
    workspace_id, user_id = compensation_test_context
    workflow_id = f"test.policy-usable-comp.{uuid4().hex}"
    graph = _linear_graph(
        _action_step("s1", "test.s1", compensate_ref="c1"),
        _action_step("s2", "test.s2"),
        _compensation_step("c1", "test.undo-s1", input_mapping={"value": "undo-me"}),
    )
    _publish_workflow(workspace_id, user_id, workflow_id, graph)

    s1 = SucceedingAdapter("test.s1")
    s2 = FailingAdapter("test.s2")
    undo = DedicatedUndoAdapter("test.undo-s1")
    registry = _make_registry(s1, s2, undo)

    finished = _run_to_completion(workspace_id, user_id, workflow_id, registry)

    assert finished.status == "compensated"
    assert undo.execute_calls == 1


# ---------------------------------------------------------------------------
# 8. A *human rejection* triggers the same compensation as an adapter
#    exception -- the Task 3 x Task 6 seam an adversarial review found open.
# ---------------------------------------------------------------------------


def test_rejecting_an_approval_compensates_earlier_steps_like_any_other_failure(
    compensation_test_context: tuple[UUID, UUID],
) -> None:
    """Regression test for a real inconsistency at the approval/compensation
    seam (`approvals._advance_run_after_decision`'s own docstring has the
    full write-up). Task 3's rejection path wrote `status = 'failed'`
    straight onto the run, justified in its own docstring as "matching how
    an adapter's own raised exception also produces `failed`" -- a
    justification Task 6 invalidated when it routed adapter-exception
    failures through `_qualifying_compensations`/`_mark_compensating`/
    `_run_compensation_sequence`. The result was that this exact workflow
    compensated `s1` if `s2` failed by accident, but did **not** if a human
    explicitly declined `s2` -- the more consequential case getting the
    worse handling, and a real side effect left un-rolled-back.

    Structurally identical to test 2 above (`s1` succeeds with a
    `compensate_ref`, `s2` fails, `c1` compensates `s1`, run ends
    `'compensated'`), with exactly one thing swapped: `s2` fails because a
    human rejected it rather than because its adapter raised.
    """
    workspace_id, user_id = compensation_test_context
    workflow_id = f"test.reject-compensates.{uuid4().hex}"
    s1 = CompensatableAdapter("test.s1")
    s2 = HighImpactAdapter("test.s2")
    graph = _linear_graph(
        _action_step("s1", "test.s1", compensate_ref="c1", input_mapping={"value": "original"}),
        _action_step("s2", "test.s2"),
        _compensation_step("c1", "test.s1"),
    )
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    registry = _make_registry(s1, s2)

    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun)
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
        assert claimed is not None
        paused = automation_worker.process_claimed_run(session, claimed, registry, "worker-a")
    assert paused.status == "waiting_approval"
    assert s1.execute_calls == 1  # the step that will need undoing really ran
    assert s2.execute_calls == 0

    with SessionFactory() as session, session.begin():
        pending = automation_approvals.get_pending_approval(session, workspace_id, queued.id, 1)
        assert pending is not None
        decided = automation_approvals.decide_approval(
            session, workspace_id, user_id, pending.id, "rejected", current_action_digest=None
        )
    assert isinstance(decided, automation_approvals.ApprovalRequest)
    assert decided.status == "rejected"

    # The rejection records the rejected step as an ordinary 'failed' step
    # and hands the run back to the poll loop -- it does not decide the
    # run's own fate inline any more.
    with SessionFactory() as session:
        requeued = automation_worker.get_run(session, workspace_id, queued.id)
    assert requeued is not None
    assert requeued.status == "queued"

    with SessionFactory() as session:
        reclaimed = automation_worker.claim_next_run(session, "worker-b")
        assert reclaimed is not None
        finished = automation_worker.process_claimed_run(session, reclaimed, registry, "worker-b")

    # The whole point: identical outcome to test 2's adapter-exception case.
    assert finished.status == "compensated"
    assert s1.compensate_calls == 1
    assert s1.last_compensated_value == "original"
    assert s2.execute_calls == 0  # the rejected step is never dispatched

    with SessionFactory() as session:
        steps = automation_worker.list_run_steps(session, workspace_id, queued.id)
        ledger = automation_worker.list_compensation_steps(session, workspace_id, queued.id)
    by_index = {step.step_index: step for step in steps}
    assert by_index[0].status == "succeeded"
    # The rejected step ends in exactly the state an ordinary failed action
    # step would, with a classified error_class naming why.
    assert by_index[1].status == "failed"
    assert by_index[1].error_class == "ApprovalRejected"
    assert by_index[1].step_type == "action"
    assert by_index[1].action_digest == pending.action_digest
    assert by_index[2].step_type == "compensation"
    assert by_index[2].status == "succeeded"
    assert [(row.compensates_step_index, row.status) for row in ledger] == [(0, "succeeded")]


# ---------------------------------------------------------------------------
# 9. A lease lost mid-execute never strands the run in 'compensating'.
# ---------------------------------------------------------------------------


def test_a_lease_lost_mid_execute_never_strands_the_run_in_compensating(
    compensation_test_context: tuple[UUID, UUID],
) -> None:
    """Regression test for the concrete HIGH failure the lease-ownership
    review named (`worker.py`'s "Lease-ownership guard" docstring section).
    `renew_lease` is called once per step, so the whole of one `execute()`
    is a window in which a second worker can reclaim the run -- and
    `_mark_compensating` used to write `status = 'compensating'`
    unconditionally, `WHERE id = :id`. Because `'compensating'` is
    deliberately excluded from `_CLAIMABLE_PREDICATE` and *nothing anywhere*
    reclaims a `'compensating'` run whose `leased_by` is `NULL` (that
    module's own disclosed scope-boundary note), the pre-fix behaviour left
    the run stranded permanently -- no crash required, an ordinary lease
    handover was enough.

    Here `s2`'s `execute()` performs that handover itself and then raises,
    so the failing worker reaches `_mark_compensating` owning nothing.
    Post-fix it writes nothing, dispatches no compensation under a lease it
    does not hold, and reports the run's real persisted state.
    """
    workspace_id, user_id = compensation_test_context
    workflow_id = f"test.lease-lost-compensating.{uuid4().hex}"
    s1 = CompensatableAdapter("test.s1")
    graph = _linear_graph(
        _action_step("s1", "test.s1", compensate_ref="c1", input_mapping={"value": "original"}),
        _action_step("s2", "test.s2"),
        _compensation_step("c1", "test.s1"),
    )
    _publish_workflow(workspace_id, user_id, workflow_id, graph)

    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun)
    s2 = LeaseHandoverFailingAdapter("test.s2", queued.id)
    registry = _make_registry(s1, s2)

    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
        assert claimed is not None
        finished = automation_worker.process_claimed_run(session, claimed, registry, "worker-a")

    assert s2.execute_calls == 1  # the step really did run and really did fail
    # The stranding bug: pre-fix this was 'compensating' with leased_by NULL,
    # which no claim predicate ever picks up again.
    assert finished.status != "compensating"
    assert finished.status == "needs_review"  # the new owner's own state
    assert finished.leased_by is None
    # No compensation was dispatched under a lease this worker did not hold.
    assert s1.compensate_calls == 0

    with SessionFactory() as session:
        persisted = automation_worker.get_run(session, workspace_id, queued.id)
        ledger = automation_worker.list_compensation_steps(session, workspace_id, queued.id)
        steps = automation_worker.list_run_steps(session, workspace_id, queued.id)
    assert persisted is not None
    assert persisted.status == "needs_review"
    assert ledger == []
    # No compensation step row either -- only the two action steps.
    assert {step.step_index for step in steps} == {0, 1}
    # And the run is not stranded: it sits in a state an operator can now
    # actually act on (`cancel_run`'s needs_review escape hatch).
    with SessionFactory() as session, session.begin():
        cancelled = automation_worker.cancel_run(session, workspace_id, queued.id)
    assert isinstance(cancelled, automation_worker.WorkflowRun)
    assert cancelled.status == "cancelled"


# ---------------------------------------------------------------------------
# 10. A genuinely MIXED compensation ledger -- one succeeded row and one
#     failed row from a single compensation sequence.
# ---------------------------------------------------------------------------


def test_a_mixed_compensation_sequence_records_both_outcomes_in_the_ledger(
    compensation_test_context: tuple[UUID, UUID],
) -> None:
    """The direct assertion for `_run_compensation_sequence`'s "never stop
    early, keep attempting every qualifying compensation" guarantee, which no
    existing test in this module actually pinned against a *mixed* ledger.

    What was already covered, and why none of it catches the regression:

    - Test 2/6 (`compensated`): every compensation succeeds -- an early-exit
      bug is unreachable because there is no failure to exit on.
    - Test 4 (`compensation_failed`): exactly *one* qualifying compensation
      exists, so "stopped after the first failure" and "attempted all of
      them" produce byte-identical ledgers.
    - Test 3 (descending order): two qualifying compensations, but both
      succeed, and the assertion is a `dispatch_log` list built by the fake
      adapters themselves -- it proves call order, not that the persisted
      ledger a human reads records each outcome.
    - Test 7's second case (both blocked by a revoked policy): two rows, but
      both `'failed'` with the *same* `error_class`, so it cannot distinguish
      "each compensation was independently evaluated" from "the first
      failure's outcome was copied onto the rest."

    This test closes that: two qualifying compensations where the **first one
    attempted fails and the second succeeds**. Dispatch order is descending
    by original step index (`_qualifying_compensations`), so `s2`'s
    compensation -- the one whose adapter raises -- runs first, and `s1`'s --
    the one that succeeds -- runs second. An early-exit regression therefore
    loses a compensation that *would have worked*: the ledger drops to a
    single failed row, `s1.compensate_calls` stays 0, and a real side effect
    stays un-rolled-back with nothing in the ledger to tell a human it was
    ever supposed to be undone. That is the concrete harm the "so partial
    compensation is visible" guarantee exists to prevent, and this is the
    only ordering in which a stop-early bug is observable at all.

    The same test also pins the reverse mistake in `all_succeeded`'s
    bookkeeping: the *last* compensation attempted here succeeds, so a
    regression that recomputed the flag per-iteration rather than latching it
    would report the run `'compensated'` -- claiming a complete rollback while
    the ledger holds a failed row. Both the run status and the ledger are
    asserted for exactly that reason; neither alone is sufficient.
    """
    workspace_id, user_id = compensation_test_context
    workflow_id = f"test.mixed-compensation.{uuid4().hex}"
    # s1's compensation succeeds; s2's raises. Descending dispatch order means
    # s2's failure is encountered *before* s1's compensation is attempted.
    s1 = CompensatableAdapter("test.s1")
    s2 = FailingCompensationAdapter("test.s2")
    s3 = FailingAdapter("test.s3")
    graph = _linear_graph(
        _action_step("s1", "test.s1", compensate_ref="c1", input_mapping={"value": "first"}),
        _action_step("s2", "test.s2", compensate_ref="c2", input_mapping={"value": "second"}),
        _action_step("s3", "test.s3"),
        _compensation_step("c1", "test.s1"),
        _compensation_step("c2", "test.s2"),
    )
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    registry = _make_registry(s1, s2, s3)

    finished = _run_to_completion(workspace_id, user_id, workflow_id, registry)

    # Both compensations were genuinely attempted -- the second one only runs
    # at all if the sequence did not stop on the first one's exception.
    assert s2.compensate_calls == 1
    assert s1.compensate_calls == 1
    assert s1.last_compensated_value == "first"

    # One failure anywhere in the sequence is enough for compensation_failed,
    # even though the chronologically last compensation succeeded.
    assert finished.status == "compensation_failed"

    with SessionFactory() as session:
        steps = automation_worker.list_run_steps(session, workspace_id, finished.id)
        ledger = automation_worker.list_compensation_steps(session, workspace_id, finished.id)

    # The ledger -- the artefact a human reads to decide what still needs
    # manual rollback -- holds exactly one succeeded row and one failed row,
    # each attributed to the original step it was supposed to undo.
    assert [(row.compensates_step_index, row.status) for row in ledger] == [
        (0, "succeeded"),
        (1, "failed"),
    ]
    assert sum(1 for row in ledger if row.status == "succeeded") == 1
    assert sum(1 for row in ledger if row.status == "failed") == 1
    failed_row = next(row for row in ledger if row.status == "failed")
    assert failed_row.error_class == "RuntimeError"
    succeeded_row = next(row for row in ledger if row.status == "succeeded")
    assert succeeded_row.error_class is None

    # The `workflow_run_steps` rows agree with the ledger row-for-row -- the
    # two tables must never diverge, and a mixed sequence is the case where a
    # copy-the-last-outcome bug in either writer would show up.
    comp_steps = {step.step_index: step for step in steps if step.step_type == "compensation"}
    assert comp_steps[3].status == "succeeded"  # c1, compensating s1
    assert comp_steps[4].status == "failed"  # c2, compensating s2
    assert comp_steps[4].error_class == "RuntimeError"
