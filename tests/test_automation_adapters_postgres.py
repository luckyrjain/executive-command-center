"""Phase 5 Automation Task 5: connector action adapters and sandbox tests
(`docs/superpowers/specs/2026-07-25-phase-5-automation-design.md` Decision
8, `docs/phases/phase-005/TEST-PLAN.md`).

Unlike every prior automation test module (`test_automation_worker_
postgres.py`'s own module docstring: "Every fake adapter in this module is
test-only, not shipped as product code"), this module dispatches through
the **shared production `registry`** (`ecc.domains.automation.adapters.
registry`) and its three real, now-registered adapters (`local.create_note`,
`local.send_test_notification`, `fake.external_action`,
`ecc.domains.automation.local_adapters`) -- proving the real registration,
not a private stand-in for it. "Sandbox tests" (this task's own roadmap
label) means exactly `TEST-PLAN.md`'s own words: "run against the local/
fake action adapters ... no test in this activation requires a real
external system or network call" -- not literal OS-level network
sandboxing.

Covers, per this task's own required minimum:

1. The shared `registry` resolves all three registered adapters.
2. `local.create_note` dispatched end-to-end through the real worker path
   (`enqueue_run` -> `claim_next_run` -> `process_claimed_run`) produces a
   real `notes` row with correct `workspace_id`/content, and `workflow_run_
   steps.output` reflects it (deliberately never carrying the note's own
   `body` -- `local_adapters.py`'s own module docstring has the redaction
   reasoning).
3. `local.create_note.simulate()` produces zero rows changed in `notes`
   (`TEST-PLAN.md`'s specifically-required fault-injection property);
   `execute()` immediately after inserts exactly one row.
4. `local.send_test_notification` (`high_impact_categories={"person-
   directed"}`) pauses a run on `waiting_approval` and is never dispatched
   before approval; approving with the correct digest dispatches it
   exactly once.
5. `fake.external_action` (`high_impact_categories={"public"}`, this
   task's own documented judgment call) also requires approval before
   dispatch, and produces a deterministic, clearly-fake output once
   approved and dispatched.
6. `adapters.compensable`/`adapters.call_compensate` against a real
   `FakeExternalActionAdapter` instance -- proving the adapter itself
   correctly implements the optional `compensate()` shape (Decision 9),
   without wiring compensation into `worker.py`'s own dispatch loop (a
   later task's scope).
7. Workspace isolation: `local.create_note` writes only into the
   triggering run's own workspace, both in the ordinary (correctly-
   authored) case and when a deliberately mismatched `workspace_id` in a
   step's own `input_mapping` is rejected before `execute()` ever runs
   (`worker.WorkspaceScopeMismatch`, this task's own self-review addition
   -- see `worker.py`'s and `local_adapters.py`'s own module docstrings).
8. Security audit batch C: actor isolation. A step whose `input_mapping.
   actor_id` names a *different* user in the *same* workspace than the one
   who actually started the run is rejected before `execute()` ever runs
   (`worker.ActorScopeMismatch`) -- so a workflow author can neither hand
   another member ownership of a note they never created nor manufacture an
   `audit_events` row attributing their own automation to that member.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.automation import adapters as automation_adapters
from ecc.domains.automation import approvals as automation_approvals
from ecc.domains.automation import local_adapters
from ecc.domains.automation import policy as automation_policy
from ecc.domains.automation import worker as automation_worker
from ecc.domains.automation import workflows as automation_workflows

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


# ---------------------------------------------------------------------------
# Self-contained fixtures/helpers -- mirrors test_automation_worker_
# postgres.py's own conventions exactly (each test module owns its fixtures
# rather than sharing global mutable state).
# ---------------------------------------------------------------------------


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
    steps = list(steps)  # type: ignore[assignment]
    for index, step in enumerate(steps[:-1]):
        step["on_success"] = steps[index + 1]["step_id"]
    return {"steps": steps}


@pytest.fixture
def adapters_test_context() -> Iterator[tuple[UUID, UUID]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Automation Adapters Test', 'Asia/Kolkata', :now)"
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
            "notes",
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
) -> automation_policy.AutomationPolicy:
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
            rate_limit=None,
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
) -> automation_workflows.WorkflowVersion:
    """Identical shape to `test_automation_worker_postgres.py`'s own
    `_publish_workflow` -- a throwaway version=1 draft to satisfy
    `automation_policies`' FK to `workflow_definitions`, then the real
    policy-bound, activated version.
    """
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
        workspace_id, user_id, workflow_id, approval_mode=approval_mode, count_limit=count_limit
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


def _notes_count(workspace_id: UUID) -> int:
    with engine.connect() as connection:
        result = connection.execute(
            text("SELECT COUNT(*) FROM notes WHERE workspace_id = :workspace_id"),
            {"workspace_id": workspace_id},
        ).scalar()
    return int(result or 0)


def _note_created_audit_count(workspace_id: UUID, actor_id: UUID) -> int:
    """How many `note.created` audit rows name `actor_id` as the acting user
    in this workspace -- the forged-attribution artefact test 8 below must
    prove never gets written (`_write_note_audit_and_outbox` sets
    `authorization_result='allowed'`/`source='automation'` on every row it
    writes, so such a row is a positive, authoritative claim that this user
    authorized the write).
    """
    with engine.connect() as connection:
        result = connection.execute(
            text(
                "SELECT COUNT(*) FROM audit_events WHERE workspace_id = :workspace_id "
                "AND actor_id = :actor_id AND event_type = 'note.created'"
            ),
            {"workspace_id": workspace_id, "actor_id": actor_id},
        ).scalar()
    return int(result or 0)


def _seed_second_user(workspace_id: UUID) -> UUID:
    """A second real member of the *same* workspace -- test 8's "member B."
    Cleaned up by `_cleanup_workspace`'s existing per-workspace `users`
    delete, so this needs no teardown of its own.
    """
    user_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO users (id, workspace_id, email, password_hash, created_at) "
                "VALUES (:id, :workspace_id, :email, 'test-password-hash', :now)"
            ),
            {
                "id": user_id,
                "workspace_id": workspace_id,
                "email": f"{user_id}@example.test",
                "now": datetime.now(UTC),
            },
        )
    return user_id


# ---------------------------------------------------------------------------
# 1. The shared production registry resolves all three registered adapters.
# ---------------------------------------------------------------------------


def test_shared_registry_resolves_all_three_registered_adapters() -> None:
    for adapter_id in (
        "local.create_note",
        "local.send_test_notification",
        "fake.external_action",
    ):
        adapter = automation_adapters.registry.get(adapter_id)
        assert adapter is not None, f"{adapter_id} not registered in the shared registry"
        assert adapter.adapter_id == adapter_id
    assert "local.create_note" in automation_adapters.registry.adapter_ids()


# ---------------------------------------------------------------------------
# 2. local.create_note end-to-end through the real worker dispatch path.
# ---------------------------------------------------------------------------


def test_local_create_note_dispatches_end_to_end_and_creates_real_row(
    adapters_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = adapters_test_context
    workflow_id = f"test.adapters.create-note.{uuid4().hex}"
    graph = _chained_graph(
        _action_step(
            "s1",
            "local.create_note",
            input_mapping={
                "workspace_id": str(workspace_id),
                "actor_id": str(user_id),
                "title": "From automation",
                "body": "This note was created by a workflow run, not a human click.",
                "note_type": "general",
            },
        )
    )
    _publish_workflow(workspace_id, user_id, workflow_id, graph)

    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun)

    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
        assert claimed is not None
        finished = automation_worker.process_claimed_run(
            session, claimed, automation_adapters.registry, "worker-a"
        )
    assert finished.status == "succeeded"

    # A real row landed in `notes`, workspace-scoped correctly.
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT workspace_id, owner_id, title, body, note_type "
                    "FROM notes WHERE workspace_id = :workspace_id"
                ),
                {"workspace_id": workspace_id},
            )
            .mappings()
            .one()
        )
    assert row["workspace_id"] == workspace_id
    assert row["owner_id"] == user_id
    assert row["title"] == "From automation"
    assert row["body"] == "This note was created by a workflow run, not a human click."
    assert row["note_type"] == "general"

    # workflow_run_steps.output reflects the created note -- id/title/
    # note_type/created_at present, body deliberately never carried
    # (local_adapters.py's own module docstring: _redact_payload cannot
    # catch secret-shaped *values*, only secret-shaped *keys*, so this
    # adapter simply never emits `body` in its output at all).
    with SessionFactory() as session, session.begin():
        steps = automation_worker.list_run_steps(session, workspace_id, queued.id)
    assert len(steps) == 1
    assert steps[0].status == "succeeded"
    output = steps[0].output
    assert output is not None
    assert output["title"] == "From automation"
    assert output["note_type"] == "general"
    assert "id" in output  # the created note's own id
    assert "body" not in output


# ---------------------------------------------------------------------------
# 3. Required fault-injection test: simulate() produces zero rows changed.
# ---------------------------------------------------------------------------


def test_local_create_note_simulate_produces_zero_rows_changed_then_execute_inserts_one(
    adapters_test_context: tuple[UUID, UUID],
) -> None:
    """`TEST-PLAN.md`'s own specifically-required property: "per registered
    adapter, asserts `simulate()` produces zero rows changed in every
    domain table its `execute()` touches." `local.create_note` is the one
    registered adapter in this activation with a real domain-table side
    effect (`notes`) -- this test proves it directly against the live
    table, not merely by the absence of an exception.
    """
    workspace_id, user_id = adapters_test_context
    adapter = automation_adapters.registry.get("local.create_note")
    assert adapter is not None

    action_input = local_adapters.CreateNoteInput(
        workspace_id=workspace_id,
        actor_id=user_id,
        title="Simulated only",
        body="This body must never actually reach the notes table via simulate().",
        note_type="general",
    )

    before = _notes_count(workspace_id)
    preview = adapter.simulate(action_input)
    after_simulate = _notes_count(workspace_id)
    assert after_simulate == before  # zero rows changed by simulate()
    assert isinstance(preview, local_adapters.CreateNoteOutput)
    assert preview.title == "Simulated only"

    real = adapter.execute(action_input)
    after_execute = _notes_count(workspace_id)
    assert after_execute == before + 1  # exactly one row from execute()
    assert isinstance(real, local_adapters.CreateNoteOutput)
    assert real.id != preview.id  # simulate()'s id was never a real row


# ---------------------------------------------------------------------------
# 4. local.send_test_notification: person-directed approval gate.
# ---------------------------------------------------------------------------


def test_local_send_test_notification_requires_approval_then_dispatches_once(
    adapters_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = adapters_test_context
    workflow_id = f"test.adapters.notify.{uuid4().hex}"
    graph = _chained_graph(
        _action_step(
            "s1",
            "local.send_test_notification",
            input_mapping={
                "workspace_id": str(workspace_id),
                "recipient_user_id": str(user_id),
                "message": "This is a test notification from an automation run.",
            },
        )
    )
    _publish_workflow(workspace_id, user_id, workflow_id, graph)

    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun)

    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
        assert claimed is not None
        paused = automation_worker.process_claimed_run(
            session, claimed, automation_adapters.registry, "worker-a"
        )
    assert paused.status == "waiting_approval"
    assert paused.finished_at is None

    # Never dispatched while paused -- no workflow_run_steps row at all
    # (Task 3's own precedent: gating happens before the 'dispatched' row
    # is ever written).
    with SessionFactory() as session, session.begin():
        steps = automation_worker.list_run_steps(session, workspace_id, queued.id)
    assert steps == []

    with SessionFactory() as session, session.begin():
        pending = automation_approvals.get_pending_approval(session, workspace_id, queued.id, 0)
        assert pending is not None
        assert pending.high_impact_categories == ("person-directed",)
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

    # decide_approval's own _advance_run_after_decision already flipped the
    # run back to 'queued' -- the next claim/process cycle picks it up
    # exactly like any other queued run (worker.py's own "Resuming a
    # waiting_approval run" module-docstring note), matching
    # test_automation_worker_postgres.py's own resume precedent exactly.
    with SessionFactory() as session:
        reclaimed = automation_worker.claim_next_run(session, "worker-b")
    assert reclaimed is not None
    with SessionFactory() as session:
        finished = automation_worker.process_claimed_run(
            session, reclaimed, automation_adapters.registry, "worker-b"
        )
    assert finished.status == "succeeded"
    with SessionFactory() as session, session.begin():
        steps = automation_worker.list_run_steps(session, workspace_id, queued.id)
    assert len(steps) == 1
    assert steps[0].status == "succeeded"
    assert steps[0].output is not None
    assert steps[0].output["message"] == "This is a test notification from an automation run."
    assert "acknowledged_at" in steps[0].output


# ---------------------------------------------------------------------------
# 5. fake.external_action: public-category approval gate (this task's own
#    documented judgment call), deterministic fake output once dispatched.
# ---------------------------------------------------------------------------


def test_fake_external_action_requires_approval_then_dispatches_with_deterministic_output(
    adapters_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = adapters_test_context
    workflow_id = f"test.adapters.fake-external.{uuid4().hex}"
    graph = _chained_graph(
        _action_step(
            "s1",
            "fake.external_action",
            input_mapping={
                "workspace_id": str(workspace_id),
                "external_ref": "issue-123",
                "payload": {"title": "A fake ticket"},
            },
        )
    )
    _publish_workflow(workspace_id, user_id, workflow_id, graph)

    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun)

    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
        assert claimed is not None
        paused = automation_worker.process_claimed_run(
            session, claimed, automation_adapters.registry, "worker-a"
        )
    assert paused.status == "waiting_approval"  # this adapter's own {"public"} choice

    with SessionFactory() as session, session.begin():
        pending = automation_approvals.get_pending_approval(session, workspace_id, queued.id, 0)
        assert pending is not None
        assert pending.high_impact_categories == ("public",)
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
        finished = automation_worker.process_claimed_run(
            session, reclaimed, automation_adapters.registry, "worker-b"
        )
    assert finished.status == "succeeded"

    with SessionFactory() as session, session.begin():
        steps = automation_worker.list_run_steps(session, workspace_id, queued.id)
    assert len(steps) == 1
    assert steps[0].status == "succeeded"
    output = steps[0].output
    assert output is not None
    assert output["status"] == "created"
    assert output["external_ref"] == "issue-123"
    assert output["fake_external_id"].startswith("fake-")

    # No real network/DB access -- adapter_id is visibly, honestly fake.
    adapter = automation_adapters.registry.get("fake.external_action")
    assert adapter is not None
    assert adapter.adapter_id.startswith("fake.")


# ---------------------------------------------------------------------------
# 6. compensable()/call_compensate() against the real FakeExternalAction
#    adapter -- unit-level, no worker/DB dispatch involved (this task does
#    not wire compensation into worker.py's own dispatch loop).
# ---------------------------------------------------------------------------


def test_fake_external_action_is_compensable_and_compensate_returns_sensible_result() -> None:
    adapter = local_adapters.FakeExternalActionAdapter()
    assert automation_adapters.compensable(adapter) is True

    workspace_id = uuid4()
    action_input = local_adapters.FakeExternalActionInput(
        workspace_id=workspace_id, external_ref="issue-999", payload={}
    )
    executed = adapter.execute(action_input)
    assert isinstance(executed, local_adapters.FakeExternalActionOutput)
    assert executed.status == "created"

    compensated = automation_adapters.call_compensate(adapter, action_input)
    assert isinstance(compensated, local_adapters.FakeExternalActionCompensationOutput)
    assert compensated.status == "compensated"
    # Deterministic given the same input -- the compensating action names
    # the exact same fake external id the original execute() produced.
    assert compensated.fake_external_id == executed.fake_external_id
    assert compensated.workspace_id == workspace_id
    assert compensated.external_ref == "issue-999"


def test_local_create_note_and_send_test_notification_are_not_compensable() -> None:
    """Decision 9: `compensate()` is optional, present only for an adapter
    with a genuine compensating action -- neither of these two declares
    one (a created note is compensated via the existing Phase 1 archive
    path, not a Phase-5-adapter-level `compensate()`; a notification has no
    compensating action at all, `reversible=False`).
    """
    note_adapter = automation_adapters.registry.get("local.create_note")
    notify_adapter = automation_adapters.registry.get("local.send_test_notification")
    assert note_adapter is not None
    assert notify_adapter is not None
    assert automation_adapters.compensable(note_adapter) is False
    assert automation_adapters.compensable(notify_adapter) is False


# ---------------------------------------------------------------------------
# 7. Workspace isolation for local.create_note.
# ---------------------------------------------------------------------------


def test_local_create_note_writes_only_into_the_triggering_runs_workspace(
    adapters_test_context: tuple[UUID, UUID],
) -> None:
    workspace_a, user_a = adapters_test_context
    workflow_id = f"test.adapters.isolation.{uuid4().hex}"
    graph = _chained_graph(
        _action_step(
            "s1",
            "local.create_note",
            input_mapping={
                "workspace_id": str(workspace_a),
                "actor_id": str(user_a),
                "body": "Belongs only to workspace A.",
            },
        )
    )
    _publish_workflow(workspace_a, user_a, workflow_id, graph)

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
            queued = automation_worker.enqueue_run(
                session, workspace_a, user_a, workflow_id=workflow_id
            )
        assert isinstance(queued, automation_worker.WorkflowRun)

        with SessionFactory() as session:
            claimed = automation_worker.claim_next_run(session, "worker-a")
            assert claimed is not None
            finished = automation_worker.process_claimed_run(
                session, claimed, automation_adapters.registry, "worker-a"
            )
        assert finished.status == "succeeded"

        assert _notes_count(workspace_a) == 1
        assert _notes_count(workspace_b) == 0
    finally:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM workspaces WHERE id = :id"), {"id": workspace_b})


def test_local_create_note_rejects_mismatched_workspace_id_before_execute(
    adapters_test_context: tuple[UUID, UUID],
) -> None:
    """The adversarial case: a workflow in workspace A whose own step
    `input_mapping` names a *different* workspace's `workspace_id` (a
    buggy or malicious workflow author, not a runtime-injected value --
    `worker.py`'s own module docstring / `local_adapters.py`'s "provenance"
    section trace exactly this). `worker.WorkspaceScopeMismatch` (this
    task's own self-review addition) rejects it before `execute()` is ever
    called -- proven directly against the live `notes` table, not merely
    by the absence of an exception.
    """
    workspace_a, user_a = adapters_test_context
    workflow_id = f"test.adapters.mismatch.{uuid4().hex}"
    workspace_b = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Mismatch Peer', 'Asia/Kolkata', :now)"
            ),
            {"id": workspace_b, "now": now},
        )
    try:
        graph = _chained_graph(
            _action_step(
                "s1",
                "local.create_note",
                input_mapping={
                    # Deliberately wrong: this run belongs to workspace_a,
                    # but the step's own literal input_mapping names
                    # workspace_b.
                    "workspace_id": str(workspace_b),
                    "actor_id": str(user_a),
                    "body": "This must never actually be written anywhere.",
                },
            )
        )
        _publish_workflow(workspace_a, user_a, workflow_id, graph)

        with SessionFactory() as session, session.begin():
            queued = automation_worker.enqueue_run(
                session, workspace_a, user_a, workflow_id=workflow_id
            )
        assert isinstance(queued, automation_worker.WorkflowRun)

        with SessionFactory() as session:
            claimed = automation_worker.claim_next_run(session, "worker-a")
            assert claimed is not None
            finished = automation_worker.process_claimed_run(
                session, claimed, automation_adapters.registry, "worker-a"
            )
        assert finished.status == "failed"

        with SessionFactory() as session, session.begin():
            steps = automation_worker.list_run_steps(session, workspace_a, queued.id)
        assert len(steps) == 1
        assert steps[0].status == "failed"
        assert steps[0].error_class == "WorkspaceScopeMismatch"

        assert _notes_count(workspace_a) == 0
        assert _notes_count(workspace_b) == 0
    finally:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM workspaces WHERE id = :id"), {"id": workspace_b})


# ---------------------------------------------------------------------------
# 8. Actor isolation for local.create_note (security audit batch C).
# ---------------------------------------------------------------------------


def test_local_create_note_rejects_mismatched_actor_id_before_execute(
    adapters_test_context: tuple[UUID, UUID],
) -> None:
    """The forged-attribution case Task 5's own `workspace_id`-only check
    left open, and the one that needs no second workspace at all: member A
    (`run_creator`, the user who actually starts the run) publishes a
    `local.create_note` step whose `input_mapping.actor_id` names member B
    (`victim`), a different real member of the *same* workspace.

    Before `worker.ActorScopeMismatch`, this succeeded, and produced two
    forged artefacts: a `notes` row whose `owner_id`/`created_by`/
    `updated_by` were all B, and an `audit_events` row asserting
    `actor_id = B`, `authorization_result = 'allowed'`, `source =
    'automation'` -- i.e. the authoritative provenance record claimed B
    authorized a write B never touched. Both are asserted absent here, not
    merely "no exception leaked": zero `notes` rows in the workspace, and
    zero `note.created` audit rows naming *either* user.
    """
    workspace_id, run_creator = adapters_test_context
    victim = _seed_second_user(workspace_id)
    workflow_id = f"test.adapters.actor-mismatch.{uuid4().hex}"
    graph = _chained_graph(
        _action_step(
            "s1",
            "local.create_note",
            input_mapping={
                "workspace_id": str(workspace_id),
                # Deliberately wrong: the run below is started by
                # `run_creator`, but this step attributes the note and its
                # audit trail to `victim`.
                "actor_id": str(victim),
                "title": "Forged on behalf of another member",
                "body": "This must never be written, and never be attributed to anyone.",
            },
        )
    )
    _publish_workflow(workspace_id, run_creator, workflow_id, graph)

    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, workspace_id, run_creator, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun)
    # The value the new check compares against -- recorded server-side by
    # enqueue_run, never authored into the graph.
    assert queued.created_by == run_creator

    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
        assert claimed is not None
        finished = automation_worker.process_claimed_run(
            session, claimed, automation_adapters.registry, "worker-a"
        )
    assert finished.status == "failed"

    with SessionFactory() as session, session.begin():
        steps = automation_worker.list_run_steps(session, workspace_id, queued.id)
    assert len(steps) == 1
    assert steps[0].status == "failed"
    assert steps[0].error_class == "ActorScopeMismatch"

    # No note, under either user's name.
    assert _notes_count(workspace_id) == 0
    # And critically, no forged audit attribution for either user.
    assert _note_created_audit_count(workspace_id, victim) == 0
    assert _note_created_audit_count(workspace_id, run_creator) == 0


def test_local_create_note_still_dispatches_when_actor_id_matches_run_creator(
    adapters_test_context: tuple[UUID, UUID],
) -> None:
    """The no-regression half: a correctly-authored step (`actor_id` == the
    run's own `created_by`) is unaffected by `_enforce_actor_scope` and still
    produces a real, correctly-attributed note plus its `note.created` audit
    row. Asserted here explicitly against `audit_events.actor_id` -- test 2
    above already covers the `notes` row itself, but nothing previously
    pinned the audit row's own actor, which is exactly the field the new
    check exists to keep honest.
    """
    workspace_id, run_creator = adapters_test_context
    # A second member exists in this workspace but is named nowhere -- proof
    # the check keys off `created_by`, not merely "some user id is present."
    other_member = _seed_second_user(workspace_id)
    workflow_id = f"test.adapters.actor-match.{uuid4().hex}"
    graph = _chained_graph(
        _action_step(
            "s1",
            "local.create_note",
            input_mapping={
                "workspace_id": str(workspace_id),
                "actor_id": str(run_creator),
                "title": "Correctly attributed",
                "body": "Authored by, and attributed to, the user who started the run.",
            },
        )
    )
    _publish_workflow(workspace_id, run_creator, workflow_id, graph)

    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, workspace_id, run_creator, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun)

    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
        assert claimed is not None
        finished = automation_worker.process_claimed_run(
            session, claimed, automation_adapters.registry, "worker-a"
        )
    assert finished.status == "succeeded"

    assert _notes_count(workspace_id) == 1
    assert _note_created_audit_count(workspace_id, run_creator) == 1
    assert _note_created_audit_count(workspace_id, other_member) == 0
