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
