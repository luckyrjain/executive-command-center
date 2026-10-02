"""`value_limit` (scope-enforcement design, Decision 4) and the approval
category set (Owner decision 5): value is summed per run over every step
row, any status; a tripped count or value limit is recorded on the approval
row as `policy-limit-exceeding`; `evaluate_approval_requirement`'s empty
set still means "approval required".
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from uuid import UUID

import pytest
from automation_scope_support import (
    FakeAdapter,
    FinancialFakeAdapter,
    World,
    action_step,
    approval_rows,
    make_world,
    publish,
    registry_of,
    resume,
    run_once,
    step_rows,
)

from ecc.config import get_settings
from ecc.database import SessionFactory
from ecc.domains.automation import approvals as automation_approvals
from ecc.domains.automation import worker as automation_worker
from ecc.domains.automation import workflows as automation_workflows
from ecc.domains.automation.adapters import AdapterRegistry

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


@pytest.fixture
def world() -> Iterator[World]:
    yield from make_world()


def _approve(world: World, run_id: UUID, step_index: int) -> None:
    with SessionFactory() as session, session.begin():
        pending = automation_approvals.get_pending_approval(
            session, world.workspace_id, run_id, step_index
        )
        assert pending is not None
        decided = automation_approvals.decide_approval(
            session,
            world.workspace_id,
            world.user_id,
            pending.id,
            "approved",
            current_action_digest=pending.action_digest,
        )
    assert isinstance(decided, automation_approvals.ApprovalRequest)


def _drive(world: World, run_id: UUID, registry: AdapterRegistry) -> str:
    return resume(world, run_id, registry).status


def test_value_below_the_limit_records_only_financial(world: World) -> None:
    money = FinancialFakeAdapter("test.pay")
    workflow_id, _ = publish(
        world,
        {"steps": [action_step("s1", "test.pay", input_mapping={"amount": "40"})]},
        value_limit=Decimal("100"),
    )
    paused = run_once(world, workflow_id, registry_of(money))
    assert paused.status == "waiting_approval"
    assert [set(r["high_impact_categories"]) for r in approval_rows(world, paused.id)] == [
        {"financial"}
    ]


def test_crossing_the_value_limit_records_policy_limit_exceeding(world: World) -> None:
    money = FinancialFakeAdapter("test.pay")
    registry = registry_of(money)
    workflow_id, _ = publish(
        world,
        {
            "steps": [
                action_step("s1", "test.pay", input_mapping={"amount": "60"}),
                action_step("s2", "test.pay", input_mapping={"amount": "60", "value": "b"}),
            ]
        },
        value_limit=Decimal("100"),
    )
    paused = run_once(world, workflow_id, registry)
    _approve(world, paused.id, 0)
    assert _drive(world, paused.id, registry) == "waiting_approval"

    approvals = approval_rows(world, paused.id)
    assert set(approvals[0]["high_impact_categories"]) == {"financial"}
    assert set(approvals[1]["high_impact_categories"]) == {"financial", "policy-limit-exceeding"}
    assert step_rows(world, paused.id)[0]["dispatch_value"] == Decimal("60")


def test_a_prior_failed_steps_value_still_counts(world: World) -> None:
    class FailingPay(FinancialFakeAdapter):
        def execute(self, action_input: object) -> object:  # type: ignore[override]
            self.execute_calls += 1
            raise RuntimeError("declined")

    failing = FailingPay("test.pay-fail")
    money = FinancialFakeAdapter("test.pay")
    registry = registry_of(failing, money)
    workflow_id, _ = publish(
        world,
        {
            "steps": [
                action_step("s1", "test.pay-fail", input_mapping={"amount": "80"}),
                action_step("s2", "test.pay", input_mapping={"amount": "30"}),
            ]
        },
        value_limit=Decimal("100"),
    )
    paused = run_once(world, workflow_id, registry)
    _approve(world, paused.id, 0)
    assert _drive(world, paused.id, registry) == "failed"
    rows = step_rows(world, paused.id)
    assert (rows[0]["status"], rows[0]["dispatch_value"]) == ("failed", Decimal("80"))
    # The run stops at the failure; the totals the next step would be judged
    # against still include the failed step's value.
    with SessionFactory() as session:
        count, value = automation_worker._run_dispatch_totals(
            session, world.workspace_id, paused.id
        )
    assert (count, value) == (1, Decimal("80"))
    assert money.execute_calls == 0


def test_count_triggered_approval_records_policy_limit_exceeding(world: World) -> None:
    bounded = FakeAdapter("test.bounded")
    registry = registry_of(bounded)
    workflow_id, _ = publish(
        world,
        {
            "steps": [
                action_step("s1", "test.bounded"),
                action_step("s2", "test.bounded", input_mapping={"value": "2"}),
            ]
        },
        count_limit=1,
    )
    paused = run_once(world, workflow_id, registry)
    assert paused.status == "waiting_approval"
    assert [
        (r["step_index"], r["high_impact_categories"]) for r in approval_rows(world, paused.id)
    ] == [(1, ["policy-limit-exceeding"])]


def test_per_run_bounded_step_still_pauses_and_simulates_as_requiring_approval(
    world: World,
) -> None:
    """The empty-set-is-falsy guard, at gate level and in /simulate."""
    bounded = FakeAdapter("test.bounded")
    registry = registry_of(bounded)
    workflow_id, policy = publish(
        world, {"steps": [action_step("s1", "test.bounded")]}, approval_mode="per_run"
    )
    paused = run_once(world, workflow_id, registry)
    assert paused.status == "waiting_approval"
    assert approval_rows(world, paused.id)[0]["high_impact_categories"] == []
    assert bounded.execute_calls == 0

    with SessionFactory() as session:
        version = automation_workflows.get_active_workflow_version(
            session, world.workspace_id, workflow_id
        )
        assert version is not None
        results = automation_workflows._simulate_steps(session, version, registry)
    assert [r.dispatch_gate for r in results] == ["requires_approval"]
    assert policy.scope_enforced


def test_value_zero_never_trips_and_returns_none(world: World) -> None:
    bounded = FakeAdapter("test.bounded")
    workflow_id, policy = publish(
        world, {"steps": [action_step("s1", "test.bounded")]}, value_limit=Decimal("0")
    )
    assert (
        automation_approvals.evaluate_approval_requirement(
            bounded,
            policy,
            action_step_count_so_far=0,
            run_value_so_far=Decimal("0"),
            step_value=Decimal("0"),
        )
        is None
    )
    finished = run_once(world, workflow_id, registry_of(bounded))
    assert finished.status == "succeeded"
