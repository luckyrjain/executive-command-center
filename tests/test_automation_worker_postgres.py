"""Phase 5 Automation Task 2: the durable local worker and crash recovery
(`docs/superpowers/specs/2026-07-25-phase-5-automation-design.md` Decision
3, `docs/phases/phase-005/EXECUTION-CONTRACT.md`,
`docs/runbooks/PHASE-5-RECOVERY.md`).

Every fake adapter in this module is test-only, not shipped as product
code (this task registers zero real adapters -- `ecc.domains.automation.
adapters`'s own module docstring). Covers, per this task's own required
minimum:

1. Two workers racing the same `workflow_runs` row under real concurrent
   database connections -- exactly one claims it.
2. Crash recovery: a run stuck in `leased` (and, separately, `running`)
   with an expired `leased_until` is reclaimed and resumes from its last
   durable checkpoint, not restarted from scratch.
3. At-most-one-effect: a step already `succeeded` under its digest is
   never re-dispatched.
4. Unknown outcome: a digest persisted with no recorded outcome (the
   crash-in-the-gap case) surfaces as `unknown`/`needs_review`, never a
   blind retry.
5. Cancellation blocks the next not-yet-dispatched step without claiming
   to interrupt an already-dispatched one.
6. Full run success path: enqueue -> claim -> run every step -> succeeded.
7. An adapter raising marks the step `failed` and the run `failed`.
8. Workspace isolation.
9. Redaction of secret-shaped keys in stored `input`/`output`.
10. The heartbeat (`renew_lease`) as an independently testable function.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
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
from ecc.domains.automation.adapters import (
    AdapterAlreadyRegistered,
    AdapterCategoryInvalid,
    AdapterRegistry,
)

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


# ---------------------------------------------------------------------------
# Test-only fake adapters (design doc Decision 8's contract shape, no
# product registration -- see this module's own docstring).
# ---------------------------------------------------------------------------


class EchoInput(BaseModel):
    value: str = ""
    secret_token: str | None = None


class EchoOutput(BaseModel):
    value: str
    echoed_secret: str | None = None


class EchoAdapter:
    """Deterministic no-op/echo adapter -- counts `execute()` calls so
    tests can assert at-most-one-effect directly.
    """

    adapter_id = "test.echo"
    input_schema = EchoInput
    output_schema = EchoOutput
    reversible = True
    high_impact_categories: frozenset[str] = frozenset()

    def __init__(self) -> None:
        self.execute_calls = 0
        self.simulate_calls = 0

    def simulate(self, action_input: EchoInput) -> EchoOutput:  # noqa: D102
        self.simulate_calls += 1
        return EchoOutput(value=action_input.value, echoed_secret=action_input.secret_token)

    def execute(self, action_input: EchoInput) -> EchoOutput:  # noqa: D102
        self.execute_calls += 1
        return EchoOutput(value=action_input.value, echoed_secret=action_input.secret_token)


class FailingAdapter:
    """Adapter whose `execute()` always raises -- exercises the
    classified-failure (not ambiguous) path.
    """

    adapter_id = "test.failing"
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
        raise RuntimeError("adapter always fails")


class HighImpactAdapter:
    """`person-directed` (Decision 5) -- always requires per-run approval
    regardless of `approval_mode` (Task 3's gate). Counts `execute()`
    calls exactly like `EchoAdapter`, so a test can assert it stays `0`
    while a run sits in `waiting_approval`.
    """

    adapter_id = "test.high-impact"
    input_schema = EchoInput
    output_schema = EchoOutput
    reversible = False
    high_impact_categories: frozenset[str] = frozenset({"person-directed"})

    def __init__(self) -> None:
        self.execute_calls = 0
        self.simulate_calls = 0

    def simulate(self, action_input: EchoInput) -> EchoOutput:  # noqa: D102
        self.simulate_calls += 1
        return EchoOutput(value=action_input.value)

    def execute(self, action_input: EchoInput) -> EchoOutput:  # noqa: D102
        self.execute_calls += 1
        return EchoOutput(value=action_input.value)


class DigestVisibilityProbeAdapter:
    """Proves `run_step`'s core durability guarantee for real, rather than
    merely asserting no exception was raised: its `execute()` opens a
    brand-new connection (never the caller's own `session`, so it cannot
    see the caller's own uncommitted work through the same transaction)
    and reads `workflow_run_steps` directly. If `run_step` only `flush()`ed
    the 'dispatched'+digest row instead of durably committing it before
    calling `execute()` (the exact bug this task's own review found and
    fixed -- worker.py's module docstring, commit-placement note), this
    independent connection would see nothing, because an uncommitted write
    is invisible outside its own transaction under this database's default
    (READ COMMITTED) isolation. Records what it saw so the test can assert
    on it directly, rather than merely on the absence of an error.
    """

    adapter_id = "test.digest-visibility-probe"
    input_schema = EchoInput
    output_schema = EchoOutput
    reversible = True
    high_impact_categories: frozenset[str] = frozenset()

    def __init__(self, run_id: UUID, step_index: int) -> None:
        self._run_id = run_id
        self._step_index = step_index
        self.saw_dispatched_row_from_independent_connection = False
        self.execute_calls = 0

    def simulate(self, action_input: EchoInput) -> EchoOutput:  # noqa: D102
        return EchoOutput(value=action_input.value)

    def execute(self, action_input: EchoInput) -> EchoOutput:  # noqa: D102
        self.execute_calls += 1
        with engine.connect() as independent_connection:
            row = (
                independent_connection.execute(
                    text(
                        "SELECT status, action_digest FROM workflow_run_steps "
                        "WHERE run_id = :run_id AND step_index = :step_index"
                    ),
                    {"run_id": self._run_id, "step_index": self._step_index},
                )
                .mappings()
                .one_or_none()
            )
        if row is not None and row["status"] == "dispatched" and row["action_digest"]:
            self.saw_dispatched_row_from_independent_connection = True
        return EchoOutput(value=action_input.value)


def _make_registry(*adapters: Any) -> AdapterRegistry:
    registry = AdapterRegistry()
    for adapter in adapters:
        registry.register(adapter)
    return registry


def _action_step(
    step_id: str, action_ref: str, *, input_mapping: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "step_type": "action",
        "action_ref": action_ref,
        "input_mapping": input_mapping or {},
        "on_success": "succeeded",
        "on_failure": "failed",
    }


def _chained_graph(*steps: dict[str, Any]) -> dict[str, Any]:
    """Wires each step's `on_success` to the next step's `step_id`
    (the last step's `on_success` stays `succeeded`) -- a small, strictly
    sequential, acyclic graph (design doc Decision 2).
    """
    steps = list(steps)  # type: ignore[assignment]
    for index, step in enumerate(steps[:-1]):
        step["on_success"] = steps[index + 1]["step_id"]
    return {"steps": steps}


# ---------------------------------------------------------------------------
# Shared workspace/user fixture -- mirrors test_automation_workflows_
# postgres.py / test_automation_policy_postgres.py exactly, minus the
# HTTP TestClient this module's own worker has no router to exercise.
# ---------------------------------------------------------------------------


@pytest.fixture
def worker_test_context() -> Iterator[tuple[UUID, UUID]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Automation Worker Test', 'Asia/Kolkata', :now)"
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


def _create_policy(
    workspace_id: UUID,
    user_id: UUID,
    workflow_id: str,
    *,
    approval_mode: automation_policy.ApprovalMode = "bounded_recurring",
    count_limit: int = 1000,
    rate_limit: dict[str, Any] | None = None,
) -> automation_policy.AutomationPolicy:
    """Task 3's own test helper. `create_policy` requires `workflow_id` to
    already name a real `workflow_definitions` row (`policy.py`'s own FK),
    so this is always called *after* `automation_workflows.create_workflow_
    draft` has created that row for the same `workflow_id` -- `_publish_
    workflow` below sequences the two calls accordingly.

    `rate_limit=None` keeps `create_policy`'s own documented fallback
    (`{"runs_per_workflow_per_hour": 10}`, `APPROVAL-POLICY.md`'s system
    default), which is what every pre-existing test in this module gets --
    harmless for all of them, since each creates its own fresh `workflow_id`
    and none enqueues ten runs against one workflow. The rate-limit tests
    below pass an explicit value.
    """
    with SessionFactory() as session, session.begin():
        return automation_policy.create_policy(
            session,
            workspace_id,
            user_id,
            workflow_id=workflow_id,
            action_types=[],
            data_classes=[],
            value_limit=Decimal("1000000"),
            count_limit=count_limit,
            rate_limit=rate_limit,
            schedule=None,
            approval_mode=approval_mode,
        )


def _publish_workflow(
    workspace_id: UUID,
    user_id: UUID,
    workflow_id: str,
    graph: dict[str, Any],
    *,
    approval_mode: automation_policy.ApprovalMode = "bounded_recurring",
    count_limit: int = 1000,
    rate_limit: dict[str, Any] | None = None,
) -> automation_workflows.WorkflowVersion:
    """Task 3 extends this in place (this task's own instruction): every
    run in this test module now resolves against a real, *usable*
    `automation_policies` row -- Task 3's dispatch gate fail-closes on a
    run with no usable policy at all (`worker.py`'s own module docstring,
    "Task 3's approval/policy gate" section), which would otherwise
    silently break every one of Task 2's own pre-existing tests below
    (none of which are testing approval/policy semantics themselves). A
    `bounded_recurring` policy with a generous default `count_limit`
    reproduces Task 2's original "everything just dispatches" behavior
    exactly, since every fake adapter in this module declares
    `high_impact_categories = frozenset()` (`bounded`, module docstring).
    Tests that specifically exercise the approval gate or policy-
    revocation/expiry blocking pass `approval_mode`/`count_limit`
    explicitly, or revoke/expire the returned workflow's policy directly
    via `automation_policy.revoke_policy`/a direct `UPDATE`.
    """
    # A throwaway version=1 draft exists solely to create the
    # workflow_definitions family row automation_policies' own FK
    # requires (policy.py's `workflow_id` FK) -- never activated,
    # immediately superseded by the real, policy-bound draft below.
    # create_workflow_draft's own "insert a new version" path (Decision 2:
    # "editing a workflow always inserts a new row") makes this safe:
    # nothing about the eventual active version's own content depends on
    # this throwaway row, and no test in this module asserts a specific
    # `workflow_version` number for `_publish_workflow`'s own return value
    # (`active.version`, read back from whatever `activate_workflow_
    # version` actually activated).
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
    policy_row = _create_policy(
        workspace_id,
        user_id,
        workflow_id,
        approval_mode=approval_mode,
        count_limit=count_limit,
        rate_limit=rate_limit,
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
# 1. enqueue_run
# ---------------------------------------------------------------------------


def test_enqueue_run_rejects_workflow_with_no_active_version(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.no-active.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.echo"))
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
        result = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    assert isinstance(result, automation_worker.WorkflowNotActive)
    assert result.workflow_id == workflow_id


def test_enqueue_run_pins_active_version_and_is_queued(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.enqueue.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.echo"))
    active = _publish_workflow(workspace_id, user_id, workflow_id, graph)

    with SessionFactory() as session, session.begin():
        run = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id, trigger_ref="manual:test"
        )
    assert isinstance(run, automation_worker.WorkflowRun)
    assert run.status == "queued"
    assert run.workflow_version == active.version
    assert run.current_step_index == 0
    assert run.trigger_ref == "manual:test"
    assert run.leased_by is None


# ---------------------------------------------------------------------------
# 1b. enqueue_run's rate limit (`APPROVAL-POLICY.md`'s "Runs per workflow per
# hour | 10 (policy default, overridable per policy) | Next run past the
# limit rejected at enqueue with `rate_limited`"; `TEST-PLAN.md`'s own named
# "Rate-limit boundary tests" scenario, which had no implementation and no
# test before this change).
# ---------------------------------------------------------------------------


def test_eleventh_run_within_an_hour_under_a_ten_per_hour_policy_is_rate_limited(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    """`TEST-PLAN.md`'s scenario, verbatim: "the 11th run within an hour
    under a 10/hour policy is rejected `rate_limited`, not silently queued."
    Asserts both halves of "not silently queued": the rejection is a real
    `RunRateLimited` outcome carrying the limit it hit, and the eleventh row
    genuinely does not exist (the count stays at ten).
    """
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.rate-limit.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.echo"))
    _publish_workflow(
        workspace_id,
        user_id,
        workflow_id,
        graph,
        rate_limit={"runs_per_workflow_per_hour": 10},
    )

    for index in range(10):
        with SessionFactory() as session, session.begin():
            accepted = automation_worker.enqueue_run(
                session, workspace_id, user_id, workflow_id=workflow_id
            )
        assert isinstance(accepted, automation_worker.WorkflowRun), (
            f"run {index + 1} of 10 should be within the limit"
        )

    with SessionFactory() as session, session.begin():
        eleventh = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    assert isinstance(eleventh, automation_worker.RunRateLimited)
    assert eleventh.workflow_id == workflow_id
    assert eleventh.limit == 10
    assert eleventh.runs_in_window == 10

    with SessionFactory() as session, session.begin():
        runs = automation_worker.list_runs(session, workspace_id)
    assert len([run for run in runs if run.workflow_id == workflow_id]) == 10


def test_rate_limit_ignores_runs_queued_outside_the_trailing_hour(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    """The window is genuinely trailing, not a lifetime cap: ten runs
    back-dated past the hour boundary do not consume the current window's
    allowance. Back-dated by direct `UPDATE` (the only way to age a
    `queued_at` without waiting an hour), the same technique this module
    already uses to age a lease for the crash-recovery tests.
    """
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.rate-limit-window.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.echo"))
    _publish_workflow(
        workspace_id, user_id, workflow_id, graph, rate_limit={"runs_per_workflow_per_hour": 2}
    )

    for _ in range(2):
        with SessionFactory() as session, session.begin():
            automation_worker.enqueue_run(session, workspace_id, user_id, workflow_id=workflow_id)
    with SessionFactory() as session, session.begin():
        blocked = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    assert isinstance(blocked, automation_worker.RunRateLimited)

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE workflow_runs SET queued_at = queued_at - interval '2 hours' "
                "WHERE workspace_id = :workspace_id AND workflow_id = :workflow_id"
            ),
            {"workspace_id": workspace_id, "workflow_id": workflow_id},
        )

    with SessionFactory() as session, session.begin():
        allowed_again = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    assert isinstance(allowed_again, automation_worker.WorkflowRun)


def test_rate_limit_is_scoped_per_workflow_not_per_workspace(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    """ "Runs per *workflow* per hour" -- one workflow exhausting its own
    allowance never blocks a different workflow in the same workspace.
    """
    workspace_id, user_id = worker_test_context
    graph = _chained_graph(_action_step("s1", "test.echo"))
    exhausted_id = f"test.rate-limit-a.{uuid4().hex}"
    other_id = f"test.rate-limit-b.{uuid4().hex}"
    _publish_workflow(
        workspace_id, user_id, exhausted_id, graph, rate_limit={"runs_per_workflow_per_hour": 1}
    )
    _publish_workflow(
        workspace_id, user_id, other_id, graph, rate_limit={"runs_per_workflow_per_hour": 1}
    )

    with SessionFactory() as session, session.begin():
        automation_worker.enqueue_run(session, workspace_id, user_id, workflow_id=exhausted_id)
    with SessionFactory() as session, session.begin():
        blocked = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=exhausted_id
        )
    assert isinstance(blocked, automation_worker.RunRateLimited)

    with SessionFactory() as session, session.begin():
        unaffected = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=other_id
        )
    assert isinstance(unaffected, automation_worker.WorkflowRun)


def test_rate_limit_absent_from_policy_means_no_ceiling(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    """`_configured_runs_per_hour`'s one deliberate non-fail-closed
    judgment, pinned by a test so it can never drift silently: a policy
    whose `rate_limit` blob names no usable `runs_per_workflow_per_hour`
    value at all is treated as having no runs-per-hour ceiling, not a
    ceiling of zero (which would permanently reject every run of that
    workflow with no reachable remedy -- `automation_policies` has no update
    endpoint). See that function's own docstring for the full trade-off.
    """
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.rate-limit-unset.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.echo"))
    _publish_workflow(workspace_id, user_id, workflow_id, graph, rate_limit={})

    for _ in range(12):
        with SessionFactory() as session, session.begin():
            accepted = automation_worker.enqueue_run(
                session, workspace_id, user_id, workflow_id=workflow_id
            )
        assert isinstance(accepted, automation_worker.WorkflowRun)


# ---------------------------------------------------------------------------
# 2. claim_next_run -- single-claim and real concurrent racing.
# ---------------------------------------------------------------------------


def test_claim_next_run_claims_queued_row_and_sets_lease(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.claim.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.echo"))
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun)

    with SessionFactory() as session, session.begin():
        claimed = automation_worker.claim_next_run(session, "worker-a")
    assert claimed is not None
    assert claimed.id == queued.id
    assert claimed.status == "leased"
    assert claimed.leased_by == "worker-a"
    assert claimed.leased_until is not None
    assert claimed.leased_until > datetime.now(UTC)


def test_claim_next_run_returns_none_when_nothing_claimable(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, _user_id = worker_test_context
    with SessionFactory() as session, session.begin():
        result = automation_worker.claim_next_run(session, "worker-a")
    assert result is None


def test_two_workers_racing_the_same_run_exactly_one_claims_it(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    """Real concurrent database connections (not mocked) -- mirrors this
    task's own required minimum and Decision 3's own emphasis on this
    property.
    """
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.race.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.echo"))
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun)

    def _claim(worker_id: str) -> automation_worker.WorkflowRun | None:
        with SessionFactory() as session, session.begin():
            return automation_worker.claim_next_run(session, worker_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(_claim, "worker-a")
        future_b = pool.submit(_claim, "worker-b")
        result_a = future_a.result()
        result_b = future_b.result()

    claims = [r for r in (result_a, result_b) if r is not None]
    assert len(claims) == 1
    assert claims[0].id == queued.id
    assert claims[0].leased_by in ("worker-a", "worker-b")


# ---------------------------------------------------------------------------
# 3. renew_lease -- the heartbeat, independently testable.
# ---------------------------------------------------------------------------


def test_renew_lease_updates_leased_until_and_heartbeat(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.renew.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.echo"))
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    with SessionFactory() as session, session.begin():
        automation_worker.enqueue_run(session, workspace_id, user_id, workflow_id=workflow_id)
    with SessionFactory() as session, session.begin():
        claimed = automation_worker.claim_next_run(session, "worker-a")
    assert claimed is not None
    original_leased_until = claimed.leased_until

    # Force the lease into the near past so the renewal is unambiguously
    # observable (a real 30s renewal from "now" would still be strictly
    # later, but this makes the assertion robust against clock jitter).
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE workflow_runs SET leased_until = :past WHERE id = :id"),
            {"past": datetime.now(UTC) - timedelta(seconds=5), "id": claimed.id},
        )

    with SessionFactory() as session, session.begin():
        renewed = automation_worker.renew_lease(session, workspace_id, claimed.id, "worker-a")
    assert renewed is not None
    assert renewed.leased_until is not None
    assert renewed.leased_until > datetime.now(UTC)
    assert original_leased_until is not None
    assert renewed.lease_heartbeat_at is not None


def test_renew_lease_returns_none_for_wrong_worker_id(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.renew-wrong.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.echo"))
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    with SessionFactory() as session, session.begin():
        automation_worker.enqueue_run(session, workspace_id, user_id, workflow_id=workflow_id)
    with SessionFactory() as session, session.begin():
        claimed = automation_worker.claim_next_run(session, "worker-a")
    assert claimed is not None

    with SessionFactory() as session, session.begin():
        result = automation_worker.renew_lease(session, workspace_id, claimed.id, "worker-b")
    assert result is None


# ---------------------------------------------------------------------------
# 4. Full run success path.
# ---------------------------------------------------------------------------


def test_full_run_success_path_enqueue_claim_run_every_step(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.full-success.{uuid4().hex}"
    graph = _chained_graph(
        _action_step("s1", "test.echo", input_mapping={"value": "first"}),
        _action_step("s2", "test.echo", input_mapping={"value": "second"}),
        _action_step("s3", "test.echo", input_mapping={"value": "third"}),
    )
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    echo = EchoAdapter()
    registry = _make_registry(echo)

    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun)

    # Bare session, no `.begin()`: claim_next_run/process_claimed_run
    # commit internally (module docstring's commit-placement note) and
    # cannot be called from inside a caller-held `with session.begin():`
    # block.
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
        assert claimed is not None
        finished = automation_worker.process_claimed_run(session, claimed, registry, "worker-a")

    assert finished.status == "succeeded"
    assert finished.current_step_index == 3
    assert finished.finished_at is not None
    assert echo.execute_calls == 3
    assert echo.simulate_calls == 0  # execute-only path, simulate() never reached

    with SessionFactory() as session, session.begin():
        steps = automation_worker.list_run_steps(session, workspace_id, queued.id)
    assert [s.status for s in steps] == ["succeeded", "succeeded", "succeeded"]
    # `echoed_secret` matches the redaction marker "secret" regardless of
    # its underlying value (even `None`) -- fail-closed by key name, not
    # by value shape, matching this module's own documented redaction
    # rule (`worker.py:_redact_payload`).
    assert steps[0].output == {"value": "first", "echoed_secret": "[REDACTED]"}
    assert all(s.action_digest is not None for s in steps)


# ---------------------------------------------------------------------------
# 5. Adapter failure.
# ---------------------------------------------------------------------------


def test_adapter_failure_marks_step_and_run_failed(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.failure.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.failing"))
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    failing = FailingAdapter()
    registry = _make_registry(failing)

    with SessionFactory() as session, session.begin():
        automation_worker.enqueue_run(session, workspace_id, user_id, workflow_id=workflow_id)
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
        assert claimed is not None
        finished = automation_worker.process_claimed_run(session, claimed, registry, "worker-a")

    assert finished.status == "failed"
    assert failing.execute_calls == 1
    with SessionFactory() as session, session.begin():
        steps = automation_worker.list_run_steps(session, workspace_id, finished.id)
    assert steps[0].status == "failed"
    assert steps[0].error_class == "RuntimeError"


# ---------------------------------------------------------------------------
# 6. At-most-one-effect: a succeeded digest is never re-dispatched.
# ---------------------------------------------------------------------------


def test_at_most_one_effect_succeeded_step_never_redispatched(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.at-most-one.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.echo"))
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    echo = EchoAdapter()
    registry = _make_registry(echo)

    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun)

    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
        assert claimed is not None
        first_outcome = automation_worker.run_step(session, claimed, 0, registry)
    assert first_outcome.status == "succeeded"
    assert echo.execute_calls == 1

    # Simulate the worker examining this same step again -- a resumed run
    # after a crash would call run_step for an index whose row already
    # exists exactly this way.
    with SessionFactory() as session:
        run = automation_worker.get_run(session, workspace_id, queued.id)
        assert run is not None
        second_outcome = automation_worker.run_step(session, run, 0, registry)
    assert second_outcome.status == "succeeded"
    assert echo.execute_calls == 1  # never re-dispatched


def test_dispatched_digest_is_durably_committed_before_execute_is_called(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    """The real property Decision 3 depends on, proven directly rather than
    inferred from the absence of an exception: at the moment `execute()`
    runs, the 'dispatched'+digest row it is about to (maybe) confirm is
    already visible to a database connection that has nothing to do with
    the caller's own session/transaction. This is exactly the guarantee
    that failed silently before this task's review found it -- `run_step`
    originally called `session.flush()` (visible only within the same
    still-open transaction) instead of `session.commit()` (durable, visible
    to any other connection) at this exact point, which this test would
    have failed to catch. `DigestVisibilityProbeAdapter.execute()` opens
    its own independent connection specifically to make that distinction
    observable.
    """
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.digest-visibility.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.digest-visibility-probe"))
    _publish_workflow(workspace_id, user_id, workflow_id, graph)

    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun)

    probe = DigestVisibilityProbeAdapter(queued.id, 0)
    registry = _make_registry(probe)
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
        assert claimed is not None
        outcome = automation_worker.run_step(session, claimed, 0, registry)
    assert outcome.status == "succeeded"
    assert probe.execute_calls == 1
    assert probe.saw_dispatched_row_from_independent_connection is True


# ---------------------------------------------------------------------------
# 7. Unknown outcome: digest persisted, no outcome recorded (crash gap).
# ---------------------------------------------------------------------------


def test_unknown_outcome_surfaces_without_retry(worker_test_context: tuple[UUID, UUID]) -> None:
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.unknown.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.echo"))
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    echo = EchoAdapter()
    registry = _make_registry(echo)

    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun)
    with SessionFactory() as session, session.begin():
        claimed = automation_worker.claim_next_run(session, "worker-a")
    assert claimed is not None

    # Simulate a crash landing exactly between "digest persisted" and
    # "adapter confirms completion": insert the row run_step's own INSERT
    # would have written, at status='dispatched', without ever calling
    # execute() -- the process died right there.
    digest = automation_worker.compute_action_digest(
        workflow_id=claimed.workflow_id,
        workflow_version=claimed.workflow_version,
        step_id="s1",
        resolved_input={},
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO workflow_run_steps (
                    id, workspace_id, run_id, step_index, step_type, status,
                    action_digest, input, started_at, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, :run_id, 0, 'action', 'dispatched',
                    :digest, '{}'::jsonb, :now, :now, :now
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "run_id": claimed.id,
                "digest": digest,
                "now": datetime.now(UTC),
            },
        )

    # A later claim attempt examines this same step -- never calls
    # execute() (the crash-in-the-gap outcome is inherently ambiguous).
    with SessionFactory() as session:
        run = automation_worker.get_run(session, workspace_id, claimed.id)
        assert run is not None
        outcome = automation_worker.run_step(session, run, 0, registry)
    assert outcome.status == "unknown"
    assert echo.execute_calls == 0

    # Driven through the full loop, this moves the run to needs_review,
    # not a blind retry, per EXECUTION-CONTRACT.md verbatim.
    with SessionFactory() as session:
        run = automation_worker.get_run(session, workspace_id, claimed.id)
        assert run is not None
        finished = automation_worker.process_claimed_run(session, run, registry, "worker-a")
    assert finished.status == "needs_review"
    assert echo.execute_calls == 0
    assert finished.finished_at is None


# ---------------------------------------------------------------------------
# 8. Crash recovery: expired lease is reclaimed and resumes correctly.
# ---------------------------------------------------------------------------


def test_crash_recovery_reclaims_expired_leased_run(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    """Directly matches `docs/runbooks/PHASE-5-RECOVERY.md`'s own
    description: a run left in `leased` status with an expired
    `leased_until` (simulating a worker crash) is reclaimed by any live
    worker's next `claim_next_run` call.
    """
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.recovery.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.echo"))
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    with SessionFactory() as session, session.begin():
        automation_worker.enqueue_run(session, workspace_id, user_id, workflow_id=workflow_id)
    with SessionFactory() as session, session.begin():
        claimed = automation_worker.claim_next_run(session, "crashed-worker")
    assert claimed is not None

    # Simulate the crash: the lease simply expires (no renewal ever
    # happens again) -- set leased_until into the past directly, exactly
    # as the runbook's own recovery-confirmation query describes.
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE workflow_runs SET leased_until = :past WHERE id = :id"),
            {"past": datetime.now(UTC) - timedelta(seconds=1), "id": claimed.id},
        )

    with SessionFactory() as session, session.begin():
        reclaimed = automation_worker.claim_next_run(session, "live-worker")
    assert reclaimed is not None
    assert reclaimed.id == claimed.id
    assert reclaimed.leased_by == "live-worker"
    assert reclaimed.status == "leased"


def test_crash_recovery_mid_run_resumes_from_checkpoint_not_from_scratch(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    """The stronger version of the recovery claim: a run that already
    completed its first step (`current_step_index` advanced, a `succeeded`
    `workflow_run_steps` row exists) and then "crashes" (status left
    `running`, lease expired) resumes from the correct checkpoint -- the
    already-succeeded first step is never re-dispatched, proving this is a
    genuine resume, not a restart from step 0. Also proves this
    implementation's documented generalization of Decision 3's claim
    predicate (`status IN ('leased', 'running')`, not `'leased'` alone,
    see `worker.py`'s own docstring) actually reclaims a `running` row.
    """
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.recovery-mid-run.{uuid4().hex}"
    graph = _chained_graph(
        _action_step("s1", "test.echo", input_mapping={"value": "one"}),
        _action_step("s2", "test.echo", input_mapping={"value": "two"}),
    )
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    echo = EchoAdapter()
    registry = _make_registry(echo)

    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun)
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "crashed-worker")
        assert claimed is not None
        # Dispatch only the first step directly (not the full driving
        # loop) so the run is left mid-flight, exactly like a worker that
        # crashed after step 1 succeeded but before step 2 was attempted.
        outcome = automation_worker.run_step(session, claimed, 0, registry)
        assert outcome.status == "succeeded"
        session.execute(
            text(
                "UPDATE workflow_runs SET status = 'running', current_step_index = 1 WHERE id = :id"
            ),
            {"id": claimed.id},
        )
        session.commit()
    assert echo.execute_calls == 1

    # Simulate the crash: lease expires while status is 'running' (not
    # 'leased') -- the exact timing the module docstring's point 2 exists
    # to cover.
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE workflow_runs SET leased_until = :past WHERE id = :id"),
            {"past": datetime.now(UTC) - timedelta(seconds=1), "id": claimed.id},
        )

    with SessionFactory() as session, session.begin():
        reclaimed = automation_worker.claim_next_run(session, "live-worker")
    assert reclaimed is not None
    assert reclaimed.id == claimed.id
    assert reclaimed.current_step_index == 1  # resumes at step 2, not step 0

    with SessionFactory() as session:
        run = automation_worker.get_run(session, workspace_id, queued.id)
        assert run is not None
        finished = automation_worker.process_claimed_run(session, run, registry, "live-worker")

    assert finished.status == "succeeded"
    assert echo.execute_calls == 2  # step 1 never re-dispatched, only step 2 ran
    with SessionFactory() as session, session.begin():
        steps = automation_worker.list_run_steps(session, workspace_id, queued.id)
    assert [s.status for s in steps] == ["succeeded", "succeeded"]


# ---------------------------------------------------------------------------
# 9. Cancellation.
# ---------------------------------------------------------------------------


def test_cancel_queued_run_is_immediate(worker_test_context: tuple[UUID, UUID]) -> None:
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.cancel-queued.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.echo"))
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun)

    with SessionFactory() as session, session.begin():
        result = automation_worker.cancel_run(session, workspace_id, queued.id)
    assert isinstance(result, automation_worker.WorkflowRun)
    assert result.status == "cancelled"
    assert result.cancel_requested_at is not None


def test_cancel_blocks_next_not_yet_dispatched_step_but_not_in_flight_one(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.cancel-mid-run.{uuid4().hex}"
    graph = _chained_graph(
        _action_step("s1", "test.echo"),
        _action_step("s2", "test.echo"),
    )
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    echo = EchoAdapter()
    registry = _make_registry(echo)

    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun)
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
        assert claimed is not None
        outcome = automation_worker.run_step(session, claimed, 0, registry)
        assert outcome.status == "succeeded"
    assert echo.execute_calls == 1

    with SessionFactory() as session, session.begin():
        cancelled = automation_worker.cancel_run(session, workspace_id, queued.id)
    assert isinstance(cancelled, automation_worker.WorkflowRun)
    assert cancelled.status not in ("cancelled",)  # not queued -- not immediate
    assert cancelled.cancel_requested_at is not None

    with SessionFactory() as session, session.begin():
        run = automation_worker.get_run(session, workspace_id, queued.id)
        assert run is not None
        session.execute(
            text(
                "UPDATE workflow_runs SET status = 'leased', current_step_index = 1 WHERE id = :id"
            ),
            {"id": run.id},
        )
    with SessionFactory() as session:
        run = automation_worker.get_run(session, workspace_id, queued.id)
        assert run is not None
        finished = automation_worker.process_claimed_run(session, run, registry, "worker-a")

    assert finished.status == "cancelled"
    assert echo.execute_calls == 1  # step 2 was never dispatched
    with SessionFactory() as session, session.begin():
        steps = automation_worker.list_run_steps(session, workspace_id, queued.id)
    assert [s.status for s in steps] == ["succeeded"]  # only step 1 has a row at all


def test_cancel_unknown_run_returns_not_found(worker_test_context: tuple[UUID, UUID]) -> None:
    workspace_id, _user_id = worker_test_context
    with SessionFactory() as session, session.begin():
        result = automation_worker.cancel_run(session, workspace_id, uuid4())
    assert isinstance(result, automation_worker.WorkflowRunNotFound)


# ---------------------------------------------------------------------------
# 10. Redaction.
# ---------------------------------------------------------------------------


def test_secret_shaped_keys_are_redacted_in_stored_input_and_output(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.redact.{uuid4().hex}"
    graph = _chained_graph(
        _action_step("s1", "test.echo", input_mapping={"value": "x", "secret_token": "sh-hh"})
    )
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    echo = EchoAdapter()
    registry = _make_registry(echo)

    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun)
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
        assert claimed is not None
        outcome = automation_worker.run_step(session, claimed, 0, registry)
    assert outcome.status == "succeeded"
    # The adapter itself received the real secret value (redaction only
    # applies to what is persisted, never to what execute() is called
    # with) -- proven by the adapter echoing it back unredacted in its
    # own return value before this module's own storage layer redacts it.
    assert outcome.output is not None
    assert outcome.output["echoed_secret"] == "sh-hh"

    with SessionFactory() as session, session.begin():
        steps = automation_worker.list_run_steps(session, workspace_id, queued.id)
    assert steps[0].input["secret_token"] == "[REDACTED]"
    assert steps[0].input["value"] == "x"
    assert steps[0].output is not None
    assert steps[0].output["echoed_secret"] == "[REDACTED]"


def test_redact_payload_handles_nested_structures() -> None:
    payload = {
        "outer": {"api_key": "abc123", "note": "keep"},
        "list": [{"password": "hunter2"}, {"note": "keep"}],
        "Authorization": "Bearer xyz",
    }
    redacted = automation_worker._redact_payload(payload)  # noqa: SLF001
    assert redacted["outer"]["api_key"] == "[REDACTED]"
    assert redacted["outer"]["note"] == "keep"
    assert redacted["list"][0]["password"] == "[REDACTED]"
    assert redacted["list"][1]["note"] == "keep"
    assert redacted["Authorization"] == "[REDACTED]"


# ---------------------------------------------------------------------------
# 11. Workspace isolation.
# ---------------------------------------------------------------------------


def test_workspace_isolation_run_not_visible_from_other_workspace(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    workspace_a, user_a = worker_test_context
    workflow_id = f"test.isolation.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.echo"))
    _publish_workflow(workspace_a, user_a, workflow_id, graph)
    with SessionFactory() as session, session.begin():
        run = automation_worker.enqueue_run(session, workspace_a, user_a, workflow_id=workflow_id)
    assert isinstance(run, automation_worker.WorkflowRun)

    workspace_b = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Isolation Peer', 'Asia/Kolkata', :now)"
            ),
            {"id": workspace_b, "now": now},
        )
    try:
        with SessionFactory() as session, session.begin():
            assert automation_worker.get_run(session, workspace_b, run.id) is None
            assert automation_worker.list_run_steps(session, workspace_b, run.id) == []
            cancel_result = automation_worker.cancel_run(session, workspace_b, run.id)
        assert isinstance(cancel_result, automation_worker.WorkflowRunNotFound)

        # The run is still perfectly visible/cancellable from its own
        # workspace -- proves this is workspace isolation, not a general
        # lookup bug.
        with SessionFactory() as session, session.begin():
            assert automation_worker.get_run(session, workspace_a, run.id) is not None
    finally:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM workspaces WHERE id = :id"), {"id": workspace_b})


# ---------------------------------------------------------------------------
# 12. compute_action_digest -- payload substitution / replay backstop.
# ---------------------------------------------------------------------------


def test_compute_action_digest_changes_with_resolved_input() -> None:
    base = automation_worker.compute_action_digest(
        workflow_id="wf", workflow_version=1, step_id="s1", resolved_input={"value": "a"}
    )
    changed_input = automation_worker.compute_action_digest(
        workflow_id="wf", workflow_version=1, step_id="s1", resolved_input={"value": "b"}
    )
    changed_version = automation_worker.compute_action_digest(
        workflow_id="wf", workflow_version=2, step_id="s1", resolved_input={"value": "a"}
    )
    same = automation_worker.compute_action_digest(
        workflow_id="wf", workflow_version=1, step_id="s1", resolved_input={"value": "a"}
    )
    assert base == same
    assert base != changed_input
    assert base != changed_version


# ---------------------------------------------------------------------------
# 13. Unsupported step type is a loud, documented scope guard.
# ---------------------------------------------------------------------------


def test_run_step_rejects_non_action_step_type(worker_test_context: tuple[UUID, UUID]) -> None:
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.unsupported.{uuid4().hex}"
    graph = {
        "steps": [
            {
                "step_id": "gate",
                "step_type": "approval_gate",
                "input_mapping": {},
                "on_success": "succeeded",
                "on_failure": "failed",
            }
        ]
    }
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun)
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
        assert claimed is not None
        with pytest.raises(automation_worker.UnsupportedStepType):
            automation_worker.run_step(session, claimed, 0, AdapterRegistry())


# ---------------------------------------------------------------------------
# 14. AdapterRegistry itself.
# ---------------------------------------------------------------------------


def test_adapter_registry_rejects_duplicate_registration() -> None:
    registry = AdapterRegistry()
    registry.register(EchoAdapter())
    with pytest.raises(AdapterAlreadyRegistered):
        registry.register(EchoAdapter())


def test_adapter_registry_rejects_unknown_high_impact_category() -> None:
    class BadAdapter:
        adapter_id = "test.bad"
        input_schema = EchoInput
        output_schema = EchoOutput
        reversible = True
        high_impact_categories = frozenset({"not-a-real-category"})

        def simulate(self, action_input: EchoInput) -> EchoOutput:  # noqa: D102
            return EchoOutput(value=action_input.value)

        def execute(self, action_input: EchoInput) -> EchoOutput:  # noqa: D102
            return EchoOutput(value=action_input.value)

    registry = AdapterRegistry()
    with pytest.raises(AdapterCategoryInvalid):
        registry.register(BadAdapter())


def test_adapter_registry_get_returns_none_for_unknown_id() -> None:
    registry = AdapterRegistry()
    assert registry.get("nope") is None


# ---------------------------------------------------------------------------
# 15. Task 3: the approval/policy dispatch gate wired into run_step.
# ---------------------------------------------------------------------------


def test_high_impact_step_pauses_run_without_ever_calling_execute(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    """The required minimum this task's own instructions name first: a
    high-impact-category step is never dispatched without an approved,
    digest-matching request -- `execute_calls` stays `0`.
    """
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.high-impact-pause.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.high-impact"))
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    with SessionFactory() as session, session.begin():
        automation_worker.enqueue_run(session, workspace_id, user_id, workflow_id=workflow_id)

    adapter = HighImpactAdapter()
    registry = _make_registry(adapter)
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
    assert claimed is not None

    with SessionFactory() as session:
        finished = automation_worker.process_claimed_run(session, claimed, registry, "worker-a")
    assert finished.status == "waiting_approval"
    assert finished.current_step_index == 0
    assert adapter.execute_calls == 0
    # `waiting_approval` is not terminal (excluded from
    # `_TERMINAL_RUN_STATUSES`) -- `finished_at` must stay unset while
    # paused, or a later resume back to 'queued' would leave a stale,
    # misleading timestamp on a run that is still actively in flight
    # (`_pause_run`, as distinct from `_finish_run`).
    assert finished.finished_at is None

    # No workflow_run_steps row is written while paused -- the whole point
    # of gating *before* the 'dispatched' INSERT (worker.py's own module
    # docstring): a paused step must never look like Task 2's own
    # crash-in-the-gap 'unknown' case to a later run_step call.
    with SessionFactory() as session, session.begin():
        steps = automation_worker.list_run_steps(session, workspace_id, claimed.id)
    assert steps == []

    with SessionFactory() as session, session.begin():
        pending = automation_approvals.get_pending_approval(session, workspace_id, claimed.id, 0)
    assert pending is not None
    assert pending.status == "pending"
    assert pending.high_impact_categories == ("person-directed",)


def test_approving_correct_digest_resumes_run_and_dispatches_exactly_once(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    """The full path this task's own instructions require: request
    created, approved with the correct digest, run resumes and the step
    actually executes exactly once.
    """
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.high-impact-resume.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.high-impact", input_mapping={"value": "x"}))
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    with SessionFactory() as session, session.begin():
        automation_worker.enqueue_run(session, workspace_id, user_id, workflow_id=workflow_id)

    adapter = HighImpactAdapter()
    registry = _make_registry(adapter)
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
    assert claimed is not None
    with SessionFactory() as session:
        paused = automation_worker.process_claimed_run(session, claimed, registry, "worker-a")
    assert paused.status == "waiting_approval"
    assert adapter.execute_calls == 0
    assert paused.finished_at is None

    with SessionFactory() as session, session.begin():
        pending = automation_approvals.get_pending_approval(session, workspace_id, claimed.id, 0)
        assert pending is not None
        decided = automation_approvals.decide_approval(
            session,
            workspace_id,
            user_id,
            pending.id,
            "approved",
            current_action_digest=pending.action_digest,
        )
    assert isinstance(decided, automation_approvals.ApprovalRequest)
    assert decided.status == "approved"

    # decide_approval's own internal _advance_run_after_decision already
    # flipped the run back to 'queued' -- the next claim/process cycle
    # picks it up exactly like any other queued run (worker.py's own
    # "Resuming a waiting_approval run" module-docstring note), no
    # separate resume-specific call required.
    with SessionFactory() as session, session.begin():
        resumed = automation_worker.get_run(session, workspace_id, claimed.id)
    assert resumed is not None
    assert resumed.status == "queued"
    assert resumed.current_step_index == 0
    # Regression check: an approved run flipped back to 'queued' must not
    # carry a stale `finished_at` from its earlier pause -- a 'queued'
    # (still actively progressing) run showing a non-null `finished_at`
    # would be a real, observable inconsistency (see `_pause_run`'s
    # docstring in worker.py for the full reasoning).
    assert resumed.finished_at is None

    with SessionFactory() as session:
        reclaimed = automation_worker.claim_next_run(session, "worker-b")
    assert reclaimed is not None
    with SessionFactory() as session:
        finished = automation_worker.process_claimed_run(session, reclaimed, registry, "worker-b")
    assert finished.status == "succeeded"
    assert adapter.execute_calls == 1
    assert finished.finished_at is not None

    with SessionFactory() as session, session.begin():
        steps = automation_worker.list_run_steps(session, workspace_id, claimed.id)
    assert len(steps) == 1
    assert steps[0].status == "succeeded"


def test_bounded_step_dispatches_without_any_approval_under_usable_policy(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    """A `bounded` (non-high-impact) step under a usable `bounded_recurring`
    policy dispatches without needing any approval at all.
    """
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.bounded-no-approval.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.echo"))
    _publish_workflow(workspace_id, user_id, workflow_id, graph, approval_mode="bounded_recurring")
    with SessionFactory() as session, session.begin():
        automation_worker.enqueue_run(session, workspace_id, user_id, workflow_id=workflow_id)

    adapter = EchoAdapter()
    registry = _make_registry(adapter)
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
    assert claimed is not None
    with SessionFactory() as session:
        finished = automation_worker.process_claimed_run(session, claimed, registry, "worker-a")
    assert finished.status == "succeeded"
    assert adapter.execute_calls == 1

    with SessionFactory() as session, session.begin():
        approvals = automation_approvals.list_approvals(session, workspace_id)
    assert approvals == []


def test_count_limit_exceeded_requires_approval_for_otherwise_bounded_step(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    """`policy-limit-exceeding` (Decision 5): a `bounded_recurring` policy
    whose `count_limit` this run's own already-dispatched action-step
    count has reached still gates the *next* step, even though the
    adapter itself declares no high-impact category.
    """
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.count-limit.{uuid4().hex}"
    graph = _chained_graph(
        _action_step("s1", "test.echo", input_mapping={"value": "first"}),
        _action_step("s2", "test.echo", input_mapping={"value": "second"}),
    )
    _publish_workflow(
        workspace_id, user_id, workflow_id, graph, approval_mode="bounded_recurring", count_limit=1
    )
    with SessionFactory() as session, session.begin():
        automation_worker.enqueue_run(session, workspace_id, user_id, workflow_id=workflow_id)

    adapter = EchoAdapter()
    registry = _make_registry(adapter)
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
    assert claimed is not None
    with SessionFactory() as session:
        finished = automation_worker.process_claimed_run(session, claimed, registry, "worker-a")
    # s1 dispatches (count_so_far=0 < count_limit=1); s2 would bring the
    # count to 2 > 1, so it requires approval instead of dispatching.
    assert finished.status == "waiting_approval"
    assert finished.current_step_index == 1
    assert adapter.execute_calls == 1


@pytest.mark.parametrize("approval_mode", ["preview_only", "per_run"])
def test_preview_only_and_per_run_modes_require_approval_for_bounded_step(
    worker_test_context: tuple[UUID, UUID], approval_mode: automation_policy.ApprovalMode
) -> None:
    """Neither `preview_only` nor `per_run` lets a `bounded` step dispatch
    unattended -- both gate every action step, high-impact or not
    (`approvals.evaluate_approval_requirement`). The two modes diverge only
    *after* an approval is granted: `per_run` then dispatches for real,
    `preview_only` never does (see `test_preview_only_never_dispatches_even_
    after_an_approved_digest` below) -- so this test's assertions hold
    identically for both and are deliberately left unchanged by the
    `preview_only` fix.
    """
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.mode-gate.{approval_mode}.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.echo"))
    _publish_workflow(workspace_id, user_id, workflow_id, graph, approval_mode=approval_mode)
    with SessionFactory() as session, session.begin():
        automation_worker.enqueue_run(session, workspace_id, user_id, workflow_id=workflow_id)

    adapter = EchoAdapter()
    registry = _make_registry(adapter)
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
    assert claimed is not None
    with SessionFactory() as session:
        finished = automation_worker.process_claimed_run(session, claimed, registry, "worker-a")
    assert finished.status == "waiting_approval"
    assert adapter.execute_calls == 0


# ---------------------------------------------------------------------------
# `preview_only` mechanically prevents real dispatch (the docs-vs-code fix:
# `APPROVAL-POLICY.md`/`docs/runbooks/PHASE-5-DOGFOOD.md` both promise
# "zero real side effects" for this mode; before this fix an *approved*
# preview_only step called the adapter's real execute()).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action_ref", ["test.echo", "test.high-impact"])
def test_preview_only_never_dispatches_even_after_an_approved_digest(
    worker_test_context: tuple[UUID, UUID], action_ref: str
) -> None:
    """The central guarantee, asserted on the adapter's own call counter --
    not on a status string. A `preview_only` policy pauses the step for
    approval exactly like `per_run`; the approval is granted with the
    correct live digest and the run is requeued by `approvals._advance_run_
    after_decision` exactly like any approved run; the next claim/process
    cycle then terminates the run in `preview_blocked` **without** ever
    calling `execute()`. Parametrized over a `bounded` adapter and a
    high-impact one, since the two reach the dispatch gate through different
    branches of `evaluate_approval_requirement` and both must be blocked.
    """
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.preview-only.{action_ref}.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", action_ref, input_mapping={"value": "x"}))
    _publish_workflow(workspace_id, user_id, workflow_id, graph, approval_mode="preview_only")
    with SessionFactory() as session, session.begin():
        automation_worker.enqueue_run(session, workspace_id, user_id, workflow_id=workflow_id)

    adapter: Any = EchoAdapter() if action_ref == "test.echo" else HighImpactAdapter()
    registry = _make_registry(adapter)
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
    assert claimed is not None
    with SessionFactory() as session:
        paused = automation_worker.process_claimed_run(session, claimed, registry, "worker-a")

    # Phase 1: the approval flow is fully real under this mode -- rehearsing
    # it is the whole point (PHASE-5-DOGFOOD.md's Stage 1).
    assert paused.status == "waiting_approval"
    assert paused.finished_at is None
    assert adapter.execute_calls == 0
    with SessionFactory() as session, session.begin():
        pending = automation_approvals.get_pending_approval(session, workspace_id, claimed.id, 0)
    assert pending is not None

    # Phase 2: a real human decision, digest-echoed, accepted.
    with SessionFactory() as session, session.begin():
        decided = automation_approvals.decide_approval(
            session,
            workspace_id,
            user_id,
            pending.id,
            "approved",
            current_action_digest=pending.action_digest,
        )
    assert isinstance(decided, automation_approvals.ApprovalRequest)
    assert decided.status == "approved"
    with SessionFactory() as session, session.begin():
        requeued = automation_worker.get_run(session, workspace_id, claimed.id)
    assert requeued is not None
    assert requeued.status == "queued"

    # Phase 3: the approved step is re-evaluated and still never dispatches.
    with SessionFactory() as session:
        reclaimed = automation_worker.claim_next_run(session, "worker-b")
    assert reclaimed is not None
    with SessionFactory() as session:
        finished = automation_worker.process_claimed_run(session, reclaimed, registry, "worker-b")

    assert finished.status == "preview_blocked"
    # The property that actually matters, stated against the adapter itself:
    # no real side effect happened, approval or no approval.
    assert adapter.execute_calls == 0
    # Terminal, honestly: `finished_at` stamped, lease released, and no
    # longer claimable by any subsequent poll cycle.
    assert finished.finished_at is not None
    assert finished.leased_by is None
    with SessionFactory() as session:
        assert automation_worker.claim_next_run(session, "worker-c") is None

    # No workflow_run_steps row is written for a blocked step, matching
    # StepBlockedByPolicy/StepAwaitingApproval's identical discipline.
    with SessionFactory() as session, session.begin():
        steps = automation_worker.list_run_steps(session, workspace_id, claimed.id)
    assert steps == []

    # The approval itself survives untouched and stays fully inspectable --
    # an operator's own record of the decision they practised.
    with SessionFactory() as session, session.begin():
        approvals_after = automation_approvals.list_approvals(session, workspace_id)
    assert len(approvals_after) == 1
    assert approvals_after[0].status == "approved"
    assert approvals_after[0].decided_by == user_id


def test_preview_only_rejected_approval_still_fails_the_run_unchanged(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    """The other half of "the approval can still be decided": rejecting a
    `preview_only` step's request behaves exactly as it does under any other
    mode (`approvals._advance_run_after_decision` -> `failed`), and is
    deliberately *not* rerouted to `preview_blocked` -- a human explicitly
    declined this action, which is a different, more informative fact about
    the run than "this mode never dispatches," and it is the outcome an
    operator rehearsing a rejection expects to see.
    """
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.preview-only-reject.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.echo"))
    _publish_workflow(workspace_id, user_id, workflow_id, graph, approval_mode="preview_only")
    with SessionFactory() as session, session.begin():
        automation_worker.enqueue_run(session, workspace_id, user_id, workflow_id=workflow_id)

    adapter = EchoAdapter()
    registry = _make_registry(adapter)
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
    assert claimed is not None
    with SessionFactory() as session:
        automation_worker.process_claimed_run(session, claimed, registry, "worker-a")

    with SessionFactory() as session, session.begin():
        pending = automation_approvals.get_pending_approval(session, workspace_id, claimed.id, 0)
        assert pending is not None
        decided = automation_approvals.decide_approval(
            session, workspace_id, user_id, pending.id, "rejected", current_action_digest=None
        )
    assert isinstance(decided, automation_approvals.ApprovalRequest)
    assert decided.status == "rejected"

    with SessionFactory() as session, session.begin():
        run_after = automation_worker.get_run(session, workspace_id, claimed.id)
    assert run_after is not None
    assert run_after.status == "failed"
    assert adapter.execute_calls == 0


def test_preview_only_unregistered_adapter_is_preview_blocked_never_failed(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    """Closes the one path from `preview_only` to a real side effect that a
    naive placement of the check would have left open. `run_step`'s
    `AdapterNotRegistered` branch writes a `'failed'` step row, and a
    `'failed'` step is exactly what triggers `_qualifying_compensations`/
    `_run_compensation_sequence`, which *does* call real `execute()`/
    `compensate()` on compensation adapters. `_evaluate_dispatch_gate`
    therefore blocks the unregistered-adapter case under `preview_only` too,
    which makes `'failed'` unreachable for any action step under this mode
    and the compensation sequence unreachable with it -- without this test
    module needing to touch compensation dispatch at all.
    """
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.preview-only-unregistered.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.not-registered-anywhere"))
    _publish_workflow(workspace_id, user_id, workflow_id, graph, approval_mode="preview_only")
    with SessionFactory() as session, session.begin():
        automation_worker.enqueue_run(session, workspace_id, user_id, workflow_id=workflow_id)

    # A registry that deliberately does not contain this step's action_ref.
    registry = _make_registry(EchoAdapter())
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
    assert claimed is not None
    with SessionFactory() as session:
        finished = automation_worker.process_claimed_run(session, claimed, registry, "worker-a")

    assert finished.status == "preview_blocked"
    with SessionFactory() as session, session.begin():
        steps = automation_worker.list_run_steps(session, workspace_id, claimed.id)
    # Crucially: no 'failed' row exists, so nothing downstream can interpret
    # this run as a failure with compensations to dispatch.
    assert steps == []
    with SessionFactory() as session, session.begin():
        compensations = automation_worker.list_compensation_steps(session, workspace_id, claimed.id)
    assert compensations == []


def test_per_run_mode_still_dispatches_for_real_once_approved(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    """The explicit no-regression counterpart to the `preview_only` fix:
    `per_run` must keep behaving exactly as it did -- gate the step, then
    dispatch for real once a human approves. If the `preview_only` check
    were ever placed one branch too high in `_evaluate_dispatch_gate` (or
    keyed off the wrong field), this test is what fails.
    """
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.per-run-unaffected.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.echo", input_mapping={"value": "x"}))
    _publish_workflow(workspace_id, user_id, workflow_id, graph, approval_mode="per_run")
    with SessionFactory() as session, session.begin():
        automation_worker.enqueue_run(session, workspace_id, user_id, workflow_id=workflow_id)

    adapter = EchoAdapter()
    registry = _make_registry(adapter)
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
    assert claimed is not None
    with SessionFactory() as session:
        paused = automation_worker.process_claimed_run(session, claimed, registry, "worker-a")
    assert paused.status == "waiting_approval"
    assert adapter.execute_calls == 0

    with SessionFactory() as session, session.begin():
        pending = automation_approvals.get_pending_approval(session, workspace_id, claimed.id, 0)
        assert pending is not None
        automation_approvals.decide_approval(
            session,
            workspace_id,
            user_id,
            pending.id,
            "approved",
            current_action_digest=pending.action_digest,
        )

    with SessionFactory() as session:
        reclaimed = automation_worker.claim_next_run(session, "worker-b")
    assert reclaimed is not None
    with SessionFactory() as session:
        finished = automation_worker.process_claimed_run(session, reclaimed, registry, "worker-b")
    assert finished.status == "succeeded"
    assert adapter.execute_calls == 1


def test_bounded_recurring_mode_is_unaffected_by_the_preview_only_block(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    """The second no-regression counterpart: a `bounded_recurring` policy
    still dispatches a `bounded` step with no approval at all, across a
    multi-step graph, and never reaches `preview_blocked`.
    """
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.bounded-unaffected.{uuid4().hex}"
    graph = _chained_graph(
        _action_step("s1", "test.echo", input_mapping={"value": "first"}),
        _action_step("s2", "test.echo", input_mapping={"value": "second"}),
    )
    _publish_workflow(workspace_id, user_id, workflow_id, graph, approval_mode="bounded_recurring")
    with SessionFactory() as session, session.begin():
        automation_worker.enqueue_run(session, workspace_id, user_id, workflow_id=workflow_id)

    adapter = EchoAdapter()
    registry = _make_registry(adapter)
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
    assert claimed is not None
    with SessionFactory() as session:
        finished = automation_worker.process_claimed_run(session, claimed, registry, "worker-a")
    assert finished.status == "succeeded"
    assert adapter.execute_calls == 2
    with SessionFactory() as session, session.begin():
        assert automation_approvals.list_approvals(session, workspace_id) == []


def test_no_policy_blocks_dispatch_as_needs_review(worker_test_context: tuple[UUID, UUID]) -> None:
    """Fail-closed: a run whose workflow was published with no policy at
    all (`policy_ref=None`) never dispatches its first step -- "no policy
    means no authority," this task's own instruction.
    """
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.no-policy.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.echo"))
    with SessionFactory() as session, session.begin():
        draft = automation_workflows.create_workflow_draft(
            session,
            workspace_id,
            user_id,
            workflow_id=workflow_id,
            graph=graph,
            trigger_refs=[],
            policy_ref=None,
        )
        automation_workflows.activate_workflow_version(session, workspace_id, draft.id)
    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun)
    assert queued.policy_id is None

    adapter = EchoAdapter()
    registry = _make_registry(adapter)
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
    assert claimed is not None
    with SessionFactory() as session:
        finished = automation_worker.process_claimed_run(session, claimed, registry, "worker-a")
    assert finished.status == "needs_review"
    assert adapter.execute_calls == 0
    # `needs_review` is not terminal either (`_TERMINAL_RUN_STATUSES`
    # excludes it alongside `waiting_approval`) -- same `_pause_run`
    # reasoning applies.
    assert finished.finished_at is None
    with SessionFactory() as session, session.begin():
        steps = automation_worker.list_run_steps(session, workspace_id, claimed.id)
    assert steps == []


def test_revoked_policy_blocks_the_next_not_yet_started_step(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.revoked-policy.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.echo"))
    active = _publish_workflow(workspace_id, user_id, workflow_id, graph)
    assert active.policy_ref is not None
    with SessionFactory() as session, session.begin():
        revoked = automation_policy.revoke_policy(session, workspace_id, user_id, active.policy_ref)
    assert isinstance(revoked, automation_policy.AutomationPolicy)

    with SessionFactory() as session, session.begin():
        automation_worker.enqueue_run(session, workspace_id, user_id, workflow_id=workflow_id)

    adapter = EchoAdapter()
    registry = _make_registry(adapter)
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
    assert claimed is not None
    with SessionFactory() as session:
        finished = automation_worker.process_claimed_run(session, claimed, registry, "worker-a")
    assert finished.status == "needs_review"
    assert adapter.execute_calls == 0


def test_expired_policy_blocks_the_next_not_yet_started_step(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.expired-policy.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.echo"))
    active = _publish_workflow(workspace_id, user_id, workflow_id, graph)
    assert active.policy_ref is not None
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE automation_policies SET expires_at = :past WHERE id = :id"),
            {"past": datetime.now(UTC) - timedelta(seconds=1), "id": active.policy_ref},
        )

    with SessionFactory() as session, session.begin():
        automation_worker.enqueue_run(session, workspace_id, user_id, workflow_id=workflow_id)

    adapter = EchoAdapter()
    registry = _make_registry(adapter)
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
    assert claimed is not None
    with SessionFactory() as session:
        finished = automation_worker.process_claimed_run(session, claimed, registry, "worker-a")
    assert finished.status == "needs_review"
    assert adapter.execute_calls == 0


def test_revoking_policy_mid_run_blocks_the_next_step_but_not_the_one_already_succeeded(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    """Decision 6 verbatim: revocation blocks the *next not-yet-started*
    step; a step that already succeeded before revocation stays succeeded.
    """
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.revoked-mid-run.{uuid4().hex}"
    graph = _chained_graph(
        _action_step("s1", "test.echo", input_mapping={"value": "first"}),
        _action_step("s2", "test.echo", input_mapping={"value": "second"}),
    )
    active = _publish_workflow(workspace_id, user_id, workflow_id, graph)
    assert active.policy_ref is not None
    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun)

    adapter = EchoAdapter()
    registry = _make_registry(adapter)
    # Dispatch only s1 directly (run_step, not process_claimed_run), then
    # revoke the policy before s2 is ever attempted.
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
    assert claimed is not None
    with SessionFactory() as session:
        outcome = automation_worker.run_step(session, claimed, 0, registry)
    assert isinstance(outcome, automation_worker.StepOutcome)
    assert outcome.status == "succeeded"

    with SessionFactory() as session, session.begin():
        revoked = automation_policy.revoke_policy(session, workspace_id, user_id, active.policy_ref)
    assert isinstance(revoked, automation_policy.AutomationPolicy)

    with SessionFactory() as session:
        run = automation_worker.get_run(session, workspace_id, claimed.id)
        assert run is not None
        finished = automation_worker.process_claimed_run(session, run, registry, "worker-a")
    assert finished.status == "needs_review"
    assert adapter.execute_calls == 1  # only s1 -- s2 never dispatched

    with SessionFactory() as session, session.begin():
        steps = automation_worker.list_run_steps(session, workspace_id, claimed.id)
    assert len(steps) == 1
    assert steps[0].status == "succeeded"


def test_rejected_approval_fails_the_run_without_dispatching(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.rejected.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.high-impact"))
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    with SessionFactory() as session, session.begin():
        automation_worker.enqueue_run(session, workspace_id, user_id, workflow_id=workflow_id)

    adapter = HighImpactAdapter()
    registry = _make_registry(adapter)
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
    assert claimed is not None
    with SessionFactory() as session:
        paused = automation_worker.process_claimed_run(session, claimed, registry, "worker-a")
    assert paused.status == "waiting_approval"

    with SessionFactory() as session, session.begin():
        pending = automation_approvals.get_pending_approval(session, workspace_id, claimed.id, 0)
        assert pending is not None
        decided = automation_approvals.decide_approval(
            session, workspace_id, user_id, pending.id, "rejected", current_action_digest=None
        )
    assert isinstance(decided, automation_approvals.ApprovalRequest)
    assert decided.status == "rejected"

    with SessionFactory() as session, session.begin():
        run = automation_worker.get_run(session, workspace_id, claimed.id)
    assert run is not None
    assert run.status == "failed"
    assert adapter.execute_calls == 0


def test_approving_with_mismatched_digest_is_rejected_and_never_dispatches(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.digest-mismatch.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.high-impact"))
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    with SessionFactory() as session, session.begin():
        automation_worker.enqueue_run(session, workspace_id, user_id, workflow_id=workflow_id)

    adapter = HighImpactAdapter()
    registry = _make_registry(adapter)
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
    assert claimed is not None
    with SessionFactory() as session:
        automation_worker.process_claimed_run(session, claimed, registry, "worker-a")

    with SessionFactory() as session, session.begin():
        pending = automation_approvals.get_pending_approval(session, workspace_id, claimed.id, 0)
        assert pending is not None
        decided = automation_approvals.decide_approval(
            session,
            workspace_id,
            user_id,
            pending.id,
            "approved",
            current_action_digest="0" * 64,
        )
    assert isinstance(decided, automation_approvals.ApprovalDigestMismatch)
    assert decided.expected_digest == pending.action_digest

    with SessionFactory() as session, session.begin():
        run = automation_worker.get_run(session, workspace_id, claimed.id)
        still_pending = automation_approvals.get_pending_approval(
            session, workspace_id, claimed.id, 0
        )
    assert run is not None
    assert run.status == "waiting_approval"  # never resumed
    assert still_pending is not None  # the request itself is still open
    assert adapter.execute_calls == 0


def test_an_expired_approval_request_cannot_be_approved(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.approval-expired.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.high-impact"))
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    with SessionFactory() as session, session.begin():
        automation_worker.enqueue_run(session, workspace_id, user_id, workflow_id=workflow_id)

    adapter = HighImpactAdapter()
    registry = _make_registry(adapter)
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
    assert claimed is not None
    with SessionFactory() as session:
        automation_worker.process_claimed_run(session, claimed, registry, "worker-a")

    with SessionFactory() as session, session.begin():
        pending = automation_approvals.get_pending_approval(session, workspace_id, claimed.id, 0)
    assert pending is not None
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE approval_requests SET expires_at = :past WHERE id = :id"),
            {"past": datetime.now(UTC) - timedelta(seconds=1), "id": pending.id},
        )

    with SessionFactory() as session, session.begin():
        decided = automation_approvals.decide_approval(
            session,
            workspace_id,
            user_id,
            pending.id,
            "approved",
            current_action_digest=pending.action_digest,
        )
    assert isinstance(decided, automation_approvals.ApprovalExpired)
    assert adapter.execute_calls == 0


def test_workspace_isolation_for_approval_requests(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    workspace_a, user_a = worker_test_context
    workflow_id = f"test.approval-isolation.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.high-impact"))
    _publish_workflow(workspace_a, user_a, workflow_id, graph)
    with SessionFactory() as session, session.begin():
        automation_worker.enqueue_run(session, workspace_a, user_a, workflow_id=workflow_id)

    registry = _make_registry(HighImpactAdapter())
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
    assert claimed is not None
    with SessionFactory() as session:
        automation_worker.process_claimed_run(session, claimed, registry, "worker-a")
    with SessionFactory() as session, session.begin():
        pending = automation_approvals.get_pending_approval(session, workspace_a, claimed.id, 0)
    assert pending is not None

    workspace_b = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Approval Isolation Peer', 'Asia/Kolkata', :now)"
            ),
            {"id": workspace_b, "now": now},
        )
    try:
        with SessionFactory() as session, session.begin():
            assert automation_approvals.get_approval(session, workspace_b, pending.id) is None
            assert automation_approvals.list_approvals(session, workspace_b) == []
            wrong_workspace_decision = automation_approvals.decide_approval(
                session,
                workspace_b,
                user_a,
                pending.id,
                "approved",
                current_action_digest=pending.action_digest,
            )
        assert isinstance(wrong_workspace_decision, automation_approvals.ApprovalNotFound)

        with SessionFactory() as session, session.begin():
            assert automation_approvals.get_approval(session, workspace_a, pending.id) is not None
    finally:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM workspaces WHERE id = :id"), {"id": workspace_b})


# ---------------------------------------------------------------------------
# 16. Task 4: user-initiated pause_run/resume_run.
# ---------------------------------------------------------------------------


def test_pause_queued_run_is_immediate(worker_test_context: tuple[UUID, UUID]) -> None:
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.pause-queued.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.echo"))
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun)

    with SessionFactory() as session, session.begin():
        paused = automation_worker.pause_run(session, workspace_id, queued.id)
    assert isinstance(paused, automation_worker.WorkflowRun)
    assert paused.status == "paused"
    assert paused.pause_requested_at is not None
    assert paused.finished_at is None  # 'paused' is not terminal


def test_pause_blocks_next_not_yet_dispatched_step_but_not_in_flight_one(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    """Mirrors `test_cancel_blocks_next_not_yet_dispatched_step_but_not_
    in_flight_one` exactly, but the run ends `'paused'`, not `'cancelled'`,
    and `finished_at` stays `None` -- the one property that distinguishes
    pause from cancel (module docstring's own "Task 4" section).
    """
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.pause-mid-run.{uuid4().hex}"
    graph = _chained_graph(
        _action_step("s1", "test.echo"),
        _action_step("s2", "test.echo"),
    )
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    echo = EchoAdapter()
    registry = _make_registry(echo)

    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun)
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
        assert claimed is not None
        outcome = automation_worker.run_step(session, claimed, 0, registry)
        assert outcome.status == "succeeded"
    assert echo.execute_calls == 1

    with SessionFactory() as session, session.begin():
        paused = automation_worker.pause_run(session, workspace_id, queued.id)
    assert isinstance(paused, automation_worker.WorkflowRun)
    assert paused.status not in ("paused",)  # not queued -- not immediate
    assert paused.pause_requested_at is not None

    with SessionFactory() as session, session.begin():
        run = automation_worker.get_run(session, workspace_id, queued.id)
        assert run is not None
        session.execute(
            text(
                "UPDATE workflow_runs SET status = 'leased', current_step_index = 1 WHERE id = :id"
            ),
            {"id": run.id},
        )
    with SessionFactory() as session:
        run = automation_worker.get_run(session, workspace_id, queued.id)
        assert run is not None
        finished = automation_worker.process_claimed_run(session, run, registry, "worker-a")

    assert finished.status == "paused"
    assert finished.finished_at is None  # never stamped for a non-terminal pause
    assert echo.execute_calls == 1  # step 2 was never dispatched
    with SessionFactory() as session, session.begin():
        steps = automation_worker.list_run_steps(session, workspace_id, queued.id)
    assert [s.status for s in steps] == ["succeeded"]  # only step 1 has a row at all


def test_resume_paused_run_flips_to_queued_and_completes_normally(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.resume.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.echo"))
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    echo = EchoAdapter()
    registry = _make_registry(echo)

    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun)
    with SessionFactory() as session, session.begin():
        paused = automation_worker.pause_run(session, workspace_id, queued.id)
    assert isinstance(paused, automation_worker.WorkflowRun)
    assert paused.status == "paused"

    with SessionFactory() as session, session.begin():
        resumed = automation_worker.resume_run(session, workspace_id, queued.id)
    assert isinstance(resumed, automation_worker.WorkflowRun)
    assert resumed.status == "queued"
    assert resumed.pause_requested_at is None  # cleared -- see worker.py's own note
    assert resumed.finished_at is None

    # Regression check for the exact bug this task's own review was asked
    # to design around upfront: if resume_run failed to clear
    # pause_requested_at, this claim/process would immediately re-pause
    # the run instead of completing it.
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
        assert claimed is not None
        finished = automation_worker.process_claimed_run(session, claimed, registry, "worker-a")
    assert finished.status == "succeeded"
    assert echo.execute_calls == 1
    assert finished.finished_at is not None


def test_resume_non_paused_run_returns_not_paused(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.resume-not-paused.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.echo"))
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun)

    with SessionFactory() as session, session.begin():
        result = automation_worker.resume_run(session, workspace_id, queued.id)
    assert isinstance(result, automation_worker.WorkflowRunNotPaused)
    assert result.status == "queued"


def test_pause_unknown_run_returns_not_found(worker_test_context: tuple[UUID, UUID]) -> None:
    workspace_id, _user_id = worker_test_context
    with SessionFactory() as session, session.begin():
        result = automation_worker.pause_run(session, workspace_id, uuid4())
    assert isinstance(result, automation_worker.WorkflowRunNotFound)


def test_resume_unknown_run_returns_not_found(worker_test_context: tuple[UUID, UUID]) -> None:
    workspace_id, _user_id = worker_test_context
    with SessionFactory() as session, session.begin():
        result = automation_worker.resume_run(session, workspace_id, uuid4())
    assert isinstance(result, automation_worker.WorkflowRunNotFound)


def test_pause_already_finished_run_is_a_noop(worker_test_context: tuple[UUID, UUID]) -> None:
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.pause-finished.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.echo"))
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    echo = EchoAdapter()
    registry = _make_registry(echo)
    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun)
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
        assert claimed is not None
        finished = automation_worker.process_claimed_run(session, claimed, registry, "worker-a")
    assert finished.status == "succeeded"

    with SessionFactory() as session, session.begin():
        result = automation_worker.pause_run(session, workspace_id, queued.id)
    assert isinstance(result, automation_worker.WorkflowRun)
    assert result.status == "succeeded"  # unchanged, not overwritten to 'paused'
    assert result.pause_requested_at is None


def test_cancel_an_already_paused_run_becomes_cancelled_not_stuck(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    """Regression test for a real bug found during this task's own
    self-review (`worker.cancel_run`'s own docstring has the full
    reasoning): `'paused'` is deliberately excluded from `_CLAIMABLE_
    PREDICATE`, so a `'paused'` run is never revisited by `process_
    claimed_run` on its own. Before the fix, cancelling an already-
    `'paused'` run only set `cancel_requested_at` without changing
    `status` (mirroring every other non-`'queued'` branch), leaving the
    run permanently stuck in `'paused'` -- a flag nothing would ever
    consult. This proves the fix directly: `status` becomes `'cancelled'`
    immediately, exactly like cancelling a never-claimed `'queued'` run.
    """
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.cancel-already-paused.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.echo"))
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun)

    with SessionFactory() as session, session.begin():
        paused = automation_worker.pause_run(session, workspace_id, queued.id)
    assert isinstance(paused, automation_worker.WorkflowRun)
    assert paused.status == "paused"

    with SessionFactory() as session, session.begin():
        cancelled = automation_worker.cancel_run(session, workspace_id, queued.id)
    assert isinstance(cancelled, automation_worker.WorkflowRun)
    assert cancelled.status == "cancelled"  # not stuck in 'paused'
    assert cancelled.finished_at is not None

    # Confirms it is genuinely terminal, not merely relabeled: a later
    # resume_run attempt correctly reports it is no longer 'paused'.
    with SessionFactory() as session, session.begin():
        resume_attempt = automation_worker.resume_run(session, workspace_id, queued.id)
    assert isinstance(resume_attempt, automation_worker.WorkflowRunNotPaused)
    assert resume_attempt.status == "cancelled"


def test_cancel_takes_priority_over_a_pending_pause_request(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    """When both flags happen to be set on a claimed run, `process_claimed_
    run` checks `cancel_requested_at` first (worker.py's own module
    docstring: "cancellation is the strictly stronger, more final request
    of the two") -- the run ends `'cancelled'`, not `'paused'`.
    """
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.cancel-over-pause.{uuid4().hex}"
    graph = _chained_graph(
        _action_step("s1", "test.echo"),
        _action_step("s2", "test.echo"),
    )
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    echo = EchoAdapter()
    registry = _make_registry(echo)

    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun)
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
        assert claimed is not None
        outcome = automation_worker.run_step(session, claimed, 0, registry)
        assert outcome.status == "succeeded"

    with SessionFactory() as session, session.begin():
        automation_worker.pause_run(session, workspace_id, queued.id)
        automation_worker.cancel_run(session, workspace_id, queued.id)

    with SessionFactory() as session, session.begin():
        run = automation_worker.get_run(session, workspace_id, queued.id)
        assert run is not None
        session.execute(
            text(
                "UPDATE workflow_runs SET status = 'leased', current_step_index = 1 WHERE id = :id"
            ),
            {"id": run.id},
        )
    with SessionFactory() as session:
        run = automation_worker.get_run(session, workspace_id, queued.id)
        assert run is not None
        finished = automation_worker.process_claimed_run(session, run, registry, "worker-a")

    assert finished.status == "cancelled"
    assert finished.finished_at is not None


def test_pausing_a_waiting_approval_run_takes_effect_on_the_approval_resume(
    worker_test_context: tuple[UUID, UUID],
) -> None:
    """Cross-feature interaction between Task 3's approval gate and this
    task's user-initiated pause, not covered by either task's own test
    suite in isolation -- worth proving directly rather than assumed
    correct by inspection. Pausing a run that is currently sitting in
    `'waiting_approval'` sets `pause_requested_at` but leaves `status`
    unchanged (identical to pausing any other non-`'queued'`,
    non-immediately-claimable state -- `pause_run`'s own documented
    "claimed run's own process_claimed_run loop observes the flag" shape;
    a `'waiting_approval'` run simply is not being actively polled by
    anything until a human decides it). The pending pause request is not
    lost: `decide_approval`'s `'approved'` branch flips the run straight
    back to `'queued'` exactly as it would with no pending pause (Task 3's
    own resume mechanic, unaware of and unaffected by pause), and the very
    next claim/poll cycle's `process_claimed_run` call observes `pause_
    requested_at` at the top of its loop -- *before* dispatching the
    now-approved step -- and re-pauses the run instead of ever calling
    `execute()`. The pause request "wins," taking effect at the next
    available checkpoint, exactly the same way a pause requested against
    an actively-dispatching run takes effect at its own next checkpoint.
    """
    workspace_id, user_id = worker_test_context
    workflow_id = f"test.pause-waiting-approval.{uuid4().hex}"
    graph = _chained_graph(_action_step("s1", "test.high-impact"))
    _publish_workflow(workspace_id, user_id, workflow_id, graph)
    with SessionFactory() as session, session.begin():
        automation_worker.enqueue_run(session, workspace_id, user_id, workflow_id=workflow_id)

    adapter = HighImpactAdapter()
    registry = _make_registry(adapter)
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
    assert claimed is not None
    with SessionFactory() as session:
        paused_for_approval = automation_worker.process_claimed_run(
            session, claimed, registry, "worker-a"
        )
    assert paused_for_approval.status == "waiting_approval"

    with SessionFactory() as session, session.begin():
        pause_result = automation_worker.pause_run(session, workspace_id, claimed.id)
    assert isinstance(pause_result, automation_worker.WorkflowRun)
    assert pause_result.status == "waiting_approval"  # unchanged -- not immediately claimable
    assert pause_result.pause_requested_at is not None

    with SessionFactory() as session, session.begin():
        pending = automation_approvals.get_pending_approval(session, workspace_id, claimed.id, 0)
        assert pending is not None
        decided = automation_approvals.decide_approval(
            session,
            workspace_id,
            user_id,
            pending.id,
            "approved",
            current_action_digest=pending.action_digest,
        )
    assert isinstance(decided, automation_approvals.ApprovalRequest)

    with SessionFactory() as session, session.begin():
        resumed_to_queued = automation_worker.get_run(session, workspace_id, claimed.id)
    assert resumed_to_queued is not None
    assert resumed_to_queued.status == "queued"  # decide_approval's own resume, unaware of pause

    with SessionFactory() as session:
        reclaimed = automation_worker.claim_next_run(session, "worker-b")
    assert reclaimed is not None
    with SessionFactory() as session:
        final = automation_worker.process_claimed_run(session, reclaimed, registry, "worker-b")
    assert final.status == "paused"  # the pending pause request took effect here
    assert adapter.execute_calls == 0  # never dispatched -- paused before the step ran
    assert final.finished_at is None

    # Confirms it is genuinely resumable, not accidentally re-blocked by a
    # leftover approval-gate artifact: an ordinary resume_run + reclaim now
    # completes normally.
    with SessionFactory() as session, session.begin():
        resumed = automation_worker.resume_run(session, workspace_id, claimed.id)
    assert isinstance(resumed, automation_worker.WorkflowRun)
    assert resumed.status == "queued"
    with SessionFactory() as session:
        reclaimed_again = automation_worker.claim_next_run(session, "worker-c")
    assert reclaimed_again is not None
    with SessionFactory() as session:
        completed = automation_worker.process_claimed_run(
            session, reclaimed_again, registry, "worker-c"
        )
    assert completed.status == "succeeded"
    assert adapter.execute_calls == 1
