"""Scope enforcement on the dispatch path (scope-enforcement design,
Decision 5): first dispatch, the `automation.step_blocked` event, the
ordinal data-class ceiling, retry-resume, compensation, `preview_only`, and
input validation inside the gate.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from automation_scope_support import (
    CompensatingFakeAdapter,
    FakeAdapter,
    World,
    action_step,
    approval_rows,
    compensation_step,
    make_world,
    publish,
    registry_of,
    resume,
    run_once,
    step_blocked_events,
    step_blocked_payloads,
    step_rows,
)
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.automation import worker as automation_worker

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


@pytest.fixture
def world() -> Iterator[World]:
    yield from make_world()


@pytest.mark.parametrize(
    ("adapter_kwargs", "reason"),
    [
        ({"action_type": "note.create"}, "action_type_not_authorized"),
        ({"data_class": "sensitive"}, "data_class_not_authorized"),
    ],
)
def test_out_of_scope_step_blocks_before_any_approval(
    world: World, adapter_kwargs: dict[str, str], reason: str
) -> None:
    # High-impact on purpose: without the scope gate it would ask a human.
    adapter = FakeAdapter("test.scoped", categories=frozenset({"public"}), **adapter_kwargs)
    workflow_id, _ = publish(
        world,
        {"steps": [action_step("s1", "test.scoped")]},
        action_types=["fake.external"],
        data_classes=["internal"],
    )
    finished = run_once(world, workflow_id, registry_of(adapter))

    assert finished.status == "needs_review"
    assert adapter.execute_calls == 0
    assert approval_rows(world, finished.id) == []
    assert step_rows(world, finished.id) == []
    events = step_blocked_events(world, finished.id)
    assert [e["actor_id"] for e in events] == [world.user_id]
    assert step_blocked_payloads(world, finished.id) == [
        {"run_id": str(finished.id), "step_index": 0, "reason": reason}
    ]


def test_existing_block_reasons_write_no_step_blocked_event(world: World) -> None:
    adapter = FakeAdapter("test.scoped")
    workflow_id, policy = publish(world, {"steps": [action_step("s1", "test.scoped")]})
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE automation_policies SET revoked_at = now() WHERE id = :id"),
            {"id": policy.id},
        )
    finished = run_once(world, workflow_id, registry_of(adapter))
    assert finished.status == "needs_review"
    assert step_blocked_events(world, finished.id) == []


@pytest.mark.parametrize("adapter_class", ["public", "internal", "sensitive"])
def test_data_class_at_or_below_the_ceiling_is_in_scope(world: World, adapter_class: str) -> None:
    adapter = FakeAdapter("test.scoped", data_class=adapter_class)
    workflow_id, _ = publish(
        world,
        {"steps": [action_step("s1", "test.scoped")]},
        action_types=["fake.external"],
        data_classes=["sensitive"],
    )
    finished = run_once(world, workflow_id, registry_of(adapter))
    assert finished.status == "succeeded"
    assert adapter.execute_calls == 1


def test_restricted_adapter_is_above_a_sensitive_ceiling(world: World) -> None:
    adapter = FakeAdapter("test.scoped", data_class="restricted")
    workflow_id, _ = publish(
        world,
        {"steps": [action_step("s1", "test.scoped")]},
        data_classes=["public", "sensitive"],
    )
    finished = run_once(world, workflow_id, registry_of(adapter))
    assert finished.status == "needs_review"


def test_retry_resume_rechecks_scope(world: World) -> None:
    """The adapter is reclassified (a deploy) during the backoff window."""
    first = FakeAdapter("test.flaky", mode="transient_once")
    workflow_id, _ = publish(world, {"steps": [action_step("s1", "test.flaky")]})
    paused = run_once(world, workflow_id, registry_of(first))
    assert paused.status == "queued"
    assert step_rows(world, paused.id)[0]["status"] == "retrying"

    with engine.begin() as connection:
        connection.execute(
            text("UPDATE workflow_runs SET next_attempt_at = :past WHERE id = :id"),
            {"past": datetime.now(UTC) - timedelta(minutes=1), "id": paused.id},
        )
    reclassified = FakeAdapter("test.flaky", action_type="note.create", mode="succeed")
    finished = resume(world, paused.id, registry_of(reclassified))

    assert finished.status == "needs_review"
    assert reclassified.execute_calls == 0
    assert step_blocked_payloads(world, finished.id)[0]["reason"] == "action_type_not_authorized"


def test_out_of_scope_compensation_execute_fails_compensation(world: World) -> None:
    done = FakeAdapter("test.done")  # no compensate(): falls back to c0's own adapter
    failing = FakeAdapter("test.failing", mode="fail")
    undo = FakeAdapter("test.undo", action_type="note.create")
    workflow_id, _ = publish(
        world,
        {
            "steps": [
                action_step("s0", "test.done", compensate_ref="c0"),
                action_step("s1", "test.failing"),
                compensation_step("c0", "test.undo"),
            ]
        },
    )
    finished = run_once(world, workflow_id, registry_of(done, failing, undo))

    assert finished.status == "compensation_failed"
    assert undo.execute_calls == 0
    with SessionFactory() as session:
        ledger = automation_worker.list_compensation_steps(session, world.workspace_id, finished.id)
    assert [entry.status for entry in ledger] == ["failed"]
    errors = {row["error_class"] for row in step_rows(world, finished.id)}
    assert "PolicyScopeViolationDuringCompensation" in errors


def test_original_adapters_own_compensate_is_not_scope_blocked(world: World) -> None:
    """compensate() undoes an action the gate already authorized."""
    done = CompensatingFakeAdapter("test.done")
    failing = FakeAdapter("test.failing", mode="fail")
    undo = FakeAdapter("test.undo", action_type="note.create")  # would be out of scope
    workflow_id, _ = publish(
        world,
        {
            "steps": [
                action_step("s0", "test.done", compensate_ref="c0"),
                action_step("s1", "test.failing"),
                compensation_step("c0", "test.undo"),
            ]
        },
    )
    finished = run_once(world, workflow_id, registry_of(done, failing, undo))
    assert finished.status == "compensated"
    assert done.compensate_calls == 1


def test_preview_only_reports_scope_violation_first(world: World) -> None:
    adapter = FakeAdapter("test.scoped", action_type="note.create")
    workflow_id, _ = publish(
        world, {"steps": [action_step("s1", "test.scoped")]}, approval_mode="preview_only"
    )
    finished = run_once(world, workflow_id, registry_of(adapter))
    assert finished.status == "needs_review"
    assert approval_rows(world, finished.id) == []


# --- input validation inside the gate --------------------------------------

_BAD_INPUT = {"unexpected": 1}


def test_invalid_input_fails_at_once_without_dispatch_or_approval(world: World) -> None:
    adapter = FakeAdapter("test.v", categories=frozenset({"public"}))
    workflow_id, _ = publish(
        world, {"steps": [action_step("s1", "test.v", input_mapping=_BAD_INPUT)]}
    )
    finished = run_once(world, workflow_id, registry_of(adapter))

    assert finished.status == "failed"
    assert adapter.execute_calls == 0
    assert approval_rows(world, finished.id) == []
    rows = step_rows(world, finished.id)
    assert [(r["status"], r["error_class"], r["dispatch_value"]) for r in rows] == [
        ("failed", "ValidationError", None)
    ]


def test_invalid_input_under_per_run_creates_no_approval(world: World) -> None:
    adapter = FakeAdapter("test.v")
    workflow_id, _ = publish(
        world,
        {"steps": [action_step("s1", "test.v", input_mapping=_BAD_INPUT)]},
        approval_mode="per_run",
    )
    finished = run_once(world, workflow_id, registry_of(adapter))
    assert finished.status == "failed"
    assert approval_rows(world, finished.id) == []


def test_invalid_input_with_a_revoked_policy_blocks_instead(world: World) -> None:
    adapter = FakeAdapter("test.v")
    workflow_id, policy = publish(
        world, {"steps": [action_step("s1", "test.v", input_mapping=_BAD_INPUT)]}
    )
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE automation_policies SET revoked_at = now() WHERE id = :id"),
            {"id": policy.id},
        )
    finished = run_once(world, workflow_id, registry_of(adapter))
    assert finished.status == "needs_review"
    assert step_rows(world, finished.id) == []


def test_invalid_input_on_an_out_of_scope_step_blocks_instead(world: World) -> None:
    adapter = FakeAdapter("test.v", action_type="note.create")
    workflow_id, _ = publish(
        world, {"steps": [action_step("s1", "test.v", input_mapping=_BAD_INPUT)]}
    )
    finished = run_once(world, workflow_id, registry_of(adapter))
    assert finished.status == "needs_review"
    assert step_rows(world, finished.id) == []
    assert len(step_blocked_events(world, finished.id)) == 1


def test_invalid_input_under_preview_only_is_the_preview_block(world: World) -> None:
    adapter = FakeAdapter("test.v")
    workflow_id, _ = publish(
        world,
        {"steps": [action_step("s1", "test.v", input_mapping=_BAD_INPUT)]},
        approval_mode="preview_only",
    )
    finished = run_once(world, workflow_id, registry_of(adapter))
    assert finished.status == "preview_blocked"
    assert step_rows(world, finished.id) == []
    assert approval_rows(world, finished.id) == []


def test_cleared_step_records_dispatch_value_and_unregistered_still_fails(world: World) -> None:
    adapter = FakeAdapter("test.v")
    workflow_id, _ = publish(
        world,
        {"steps": [action_step("s1", "test.v"), action_step("s2", "test.unregistered")]},
    )
    finished = run_once(world, workflow_id, registry_of(adapter))
    rows = step_rows(world, finished.id)
    assert (rows[0]["status"], rows[0]["dispatch_value"]) == ("succeeded", 0)
    assert (rows[1]["status"], rows[1]["error_class"]) == ("failed", "AdapterNotRegistered")
