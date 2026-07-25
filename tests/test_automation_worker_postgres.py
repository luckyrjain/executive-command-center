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
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import SessionFactory, engine
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
        draft = automation_workflows.create_workflow_draft(
            session,
            workspace_id,
            user_id,
            workflow_id=workflow_id,
            graph=graph,
            trigger_refs=[],
            policy_ref=None,
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
