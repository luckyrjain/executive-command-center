"""Phase 5 Automation Task 3: the approval inbox and policy-driven
revocation enforcement (`docs/superpowers/specs/2026-07-25-phase-5-
automation-design.md` Decision 5/6/Threat model,
`docs/phases/phase-005/APPROVAL-POLICY.md`,
`docs/phases/phase-005/API-SCHEMAS.md`).

Covers `ecc.domains.automation.approvals` directly (`evaluate_approval_
requirement`'s pure logic, `create_approval_request`'s 24h default expiry,
`approval_lifecycle_status`'s computed-not-stored expiry, `decide_approval`'s
five outcomes) and the `/api/v1/automations/approvals` HTTP surface
(list/approve/reject, CSRF, idempotency, workspace isolation, error codes).
Dispatch-path wiring (the gate actually inserted into `worker.run_step`,
including "an adapter's `execute()` is never called while a step awaits
approval") is covered in `tests/test_automation_worker_postgres.py`
instead, alongside the rest of that module's own worker-level fixtures.

Plus one gap closed by the test-coverage audit: **cross-workspace isolation
for `POST /approvals/{id}/reject`**. Only `/approve` had that test, and
`/approve` is the half that carries a second, independent defence (the
echoed `action_digest`); `/reject` takes an empty body and terminally fails
the victim's run. See `test_reject_is_rejected_across_workspaces`' own
docstring for the full reasoning.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from hmac import new
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.automation import approvals as automation_approvals
from ecc.domains.automation import policy as automation_policy
from ecc.domains.automation import worker as automation_worker
from ecc.domains.automation import workflows as automation_workflows
from ecc.domains.automation.adapters import ActionAdapter, AdapterRegistry
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


# ---------------------------------------------------------------------------
# Fixtures / helpers -- self-contained, mirroring test_automation_policy_
# postgres.py's own fixture shape (this file's closest precedent for an
# HTTP-surface test module in this package).
# ---------------------------------------------------------------------------


@pytest.fixture
def approval_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Automation Approvals Test', 'Asia/Kolkata', :now)"
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
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    headers = {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


class _Input(BaseModel):
    value: str = ""


class _Output(BaseModel):
    value: str


class _HighImpactAdapter:
    adapter_id = "test.approvals-high-impact"
    input_schema = _Input
    output_schema = _Output
    reversible = False
    high_impact_categories: frozenset[str] = frozenset({"person-directed"})

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


def _publish_high_impact_workflow(
    workspace_id: UUID, user_id: UUID, workflow_id: str
) -> automation_workflows.WorkflowVersion:
    """A one-step, high-impact-category workflow, published with a usable
    `bounded_recurring` policy attached -- every request an approval-
    endpoint test needs to decide on comes from actually running this
    workflow up to its `waiting_approval` pause, exactly like a real
    request would be created (not hand-inserted), so these tests exercise
    the real `create_approval_request` code path too.
    """
    with SessionFactory() as session, session.begin():
        automation_workflows.create_workflow_draft(
            session,
            workspace_id,
            user_id,
            workflow_id=workflow_id,
            graph={"steps": [_action_step("s1", "test.approvals-high-impact")]},
            trigger_refs=[],
            policy_ref=None,
        )
    policy_row = _create_policy(workspace_id, user_id, workflow_id)
    with SessionFactory() as session, session.begin():
        draft = automation_workflows.create_workflow_draft(
            session,
            workspace_id,
            user_id,
            workflow_id=workflow_id,
            graph={"steps": [_action_step("s1", "test.approvals-high-impact")]},
            trigger_refs=[],
            policy_ref=policy_row.id,
        )
        activated = automation_workflows.activate_workflow_version(session, workspace_id, draft.id)
    assert isinstance(activated, automation_workflows.WorkflowVersion)
    return activated


def _pause_run_awaiting_approval(
    workspace_id: UUID, user_id: UUID, workflow_id: str
) -> tuple[automation_worker.WorkflowRun, automation_approvals.ApprovalRequest, _HighImpactAdapter]:
    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun)

    registry = AdapterRegistry()
    adapter = _HighImpactAdapter()
    registry.register(adapter)  # type: ignore[arg-type]  -- structural Protocol, see adapters.py

    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
        assert claimed is not None
        paused = automation_worker.process_claimed_run(session, claimed, registry, "worker-a")
    assert paused.status == "waiting_approval"

    with SessionFactory() as session, session.begin():
        pending = automation_approvals.get_pending_approval(session, workspace_id, claimed.id, 0)
    assert pending is not None
    return paused, pending, adapter


# ---------------------------------------------------------------------------
# 1. evaluate_approval_requirement -- pure, no session.
# ---------------------------------------------------------------------------


def _fake_adapter(categories: frozenset[str]) -> ActionAdapter:
    class _Adapter:
        adapter_id = "test.fake"
        input_schema = _Input
        output_schema = _Output
        reversible = True
        high_impact_categories = categories

        def simulate(self, action_input: _Input) -> _Output:  # noqa: D102
            return _Output(value=action_input.value)

        def execute(self, action_input: _Input) -> _Output:  # noqa: D102
            return _Output(value=action_input.value)

    return _Adapter()  # type: ignore[return-value]


def _fake_policy(
    *, approval_mode: automation_policy.ApprovalMode, count_limit: int = 1000
) -> automation_policy.AutomationPolicy:
    now = datetime.now(UTC)
    return automation_policy.AutomationPolicy(
        id=uuid4(),
        workspace_id=uuid4(),
        workflow_id="test.workflow",
        action_types=(),
        data_classes=(),
        value_limit=Decimal("0"),
        count_limit=count_limit,
        rate_limit={"runs_per_workflow_per_hour": 10},
        schedule=None,
        approval_mode=approval_mode,
        expires_at=now + timedelta(days=1),
        revoked_at=None,
        version=1,
        created_by=uuid4(),
        updated_by=uuid4(),
        created_at=now,
        updated_at=now,
    )


@pytest.mark.parametrize("approval_mode", ["preview_only", "per_run", "bounded_recurring"])
def test_high_impact_always_requires_approval_regardless_of_mode(
    approval_mode: automation_policy.ApprovalMode,
) -> None:
    adapter = _fake_adapter(frozenset({"person-directed"}))
    policy = _fake_policy(approval_mode=approval_mode)
    assert (
        automation_approvals.evaluate_approval_requirement(
            adapter, policy, action_step_count_so_far=0
        )
        is True
    )


def test_bounded_step_needs_no_approval_under_bounded_recurring_within_count_limit() -> None:
    adapter = _fake_adapter(frozenset())
    policy = _fake_policy(approval_mode="bounded_recurring", count_limit=10)
    assert (
        automation_approvals.evaluate_approval_requirement(
            adapter, policy, action_step_count_so_far=0
        )
        is False
    )


@pytest.mark.parametrize("approval_mode", ["preview_only", "per_run"])
def test_bounded_step_still_requires_approval_under_preview_only_and_per_run(
    approval_mode: automation_policy.ApprovalMode,
) -> None:
    adapter = _fake_adapter(frozenset())
    policy = _fake_policy(approval_mode=approval_mode)
    assert (
        automation_approvals.evaluate_approval_requirement(
            adapter, policy, action_step_count_so_far=0
        )
        is True
    )


def test_bounded_step_requires_approval_once_count_limit_reached() -> None:
    adapter = _fake_adapter(frozenset())
    policy = _fake_policy(approval_mode="bounded_recurring", count_limit=2)
    assert (
        automation_approvals.evaluate_approval_requirement(
            adapter, policy, action_step_count_so_far=1
        )
        is False
    )
    assert (
        automation_approvals.evaluate_approval_requirement(
            adapter, policy, action_step_count_so_far=2
        )
        is True
    )


# ---------------------------------------------------------------------------
# 2. approval_lifecycle_status -- computed, never stored (mirrors
#    policy.policy_status's identical convention).
# ---------------------------------------------------------------------------


def test_approval_lifecycle_status_expired_is_computed_not_written_to_db(
    approval_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    _client, workspace_id, user_id, _token = approval_test_context
    workflow_id = f"test.lifecycle-expired.{uuid4().hex}"
    _publish_high_impact_workflow(workspace_id, user_id, workflow_id)
    _run, pending, _adapter = _pause_run_awaiting_approval(workspace_id, user_id, workflow_id)

    with engine.begin() as connection:
        connection.execute(
            text("UPDATE approval_requests SET expires_at = :past WHERE id = :id"),
            {"past": datetime.now(UTC) - timedelta(seconds=1), "id": pending.id},
        )

    with SessionFactory() as session, session.begin():
        refreshed = automation_approvals.get_approval(session, workspace_id, pending.id)
    assert refreshed is not None
    assert refreshed.status == "pending"  # never written to the DB as 'expired'
    assert automation_approvals.approval_lifecycle_status(refreshed) == "expired"


def test_create_approval_request_defaults_expires_at_to_24_hours(
    approval_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    _client, workspace_id, user_id, _token = approval_test_context
    workflow_id = f"test.default-expiry.{uuid4().hex}"
    _publish_high_impact_workflow(workspace_id, user_id, workflow_id)
    _run, pending, _adapter = _pause_run_awaiting_approval(workspace_id, user_id, workflow_id)

    delta = pending.expires_at - pending.requested_at
    assert timedelta(hours=23, minutes=59) < delta <= timedelta(hours=24, minutes=1)


# ---------------------------------------------------------------------------
# 3. decide_approval -- direct-function-level outcomes.
# ---------------------------------------------------------------------------


def test_decide_approval_rejects_digest_mismatch_on_approve(
    approval_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    _client, workspace_id, user_id, _token = approval_test_context
    workflow_id = f"test.decide-mismatch.{uuid4().hex}"
    _publish_high_impact_workflow(workspace_id, user_id, workflow_id)
    _run, pending, adapter = _pause_run_awaiting_approval(workspace_id, user_id, workflow_id)

    with SessionFactory() as session, session.begin():
        result = automation_approvals.decide_approval(
            session,
            workspace_id,
            user_id,
            pending.id,
            "approved",
            current_action_digest="stale" * 16,
        )
    assert isinstance(result, automation_approvals.ApprovalDigestMismatch)
    assert result.expected_digest == pending.action_digest
    assert adapter.execute_calls == 0


def test_decide_approval_approve_requires_a_digest_at_all(
    approval_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """Omitting `current_action_digest` entirely on an `approved` decision
    is treated identically to an explicit mismatch -- fail closed, never
    an implicit approval (module docstring).
    """
    _client, workspace_id, user_id, _token = approval_test_context
    workflow_id = f"test.decide-no-digest.{uuid4().hex}"
    _publish_high_impact_workflow(workspace_id, user_id, workflow_id)
    _run, pending, _adapter = _pause_run_awaiting_approval(workspace_id, user_id, workflow_id)

    with SessionFactory() as session, session.begin():
        result = automation_approvals.decide_approval(
            session, workspace_id, user_id, pending.id, "approved", current_action_digest=None
        )
    assert isinstance(result, automation_approvals.ApprovalDigestMismatch)
    assert result.provided_digest is None


def test_decide_approval_reject_does_not_require_a_digest(
    approval_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    _client, workspace_id, user_id, _token = approval_test_context
    workflow_id = f"test.decide-reject-no-digest.{uuid4().hex}"
    _publish_high_impact_workflow(workspace_id, user_id, workflow_id)
    _run, pending, _adapter = _pause_run_awaiting_approval(workspace_id, user_id, workflow_id)

    with SessionFactory() as session, session.begin():
        result = automation_approvals.decide_approval(
            session, workspace_id, user_id, pending.id, "rejected", current_action_digest=None
        )
    assert isinstance(result, automation_approvals.ApprovalRequest)
    assert result.status == "rejected"


def test_decide_approval_rejects_already_decided(
    approval_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    _client, workspace_id, user_id, _token = approval_test_context
    workflow_id = f"test.decide-twice.{uuid4().hex}"
    _publish_high_impact_workflow(workspace_id, user_id, workflow_id)
    _run, pending, _adapter = _pause_run_awaiting_approval(workspace_id, user_id, workflow_id)

    with SessionFactory() as session, session.begin():
        first = automation_approvals.decide_approval(
            session,
            workspace_id,
            user_id,
            pending.id,
            "approved",
            current_action_digest=pending.action_digest,
        )
    assert isinstance(first, automation_approvals.ApprovalRequest)

    with SessionFactory() as session, session.begin():
        second = automation_approvals.decide_approval(
            session,
            workspace_id,
            user_id,
            pending.id,
            "approved",
            current_action_digest=pending.action_digest,
        )
    assert isinstance(second, automation_approvals.ApprovalAlreadyDecided)
    assert second.decision == "approved"


def test_decide_approval_unknown_id_is_not_found(
    approval_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    _client, workspace_id, user_id, _token = approval_test_context
    with SessionFactory() as session, session.begin():
        result = automation_approvals.decide_approval(
            session, workspace_id, user_id, uuid4(), "approved", current_action_digest="x" * 64
        )
    assert isinstance(result, automation_approvals.ApprovalNotFound)


# ---------------------------------------------------------------------------
# 4. HTTP surface -- GET /approvals, POST .../approve|reject.
# ---------------------------------------------------------------------------


def test_list_approvals_endpoint_filters_by_status(
    approval_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, _token = approval_test_context
    workflow_id = f"test.http-list.{uuid4().hex}"
    _publish_high_impact_workflow(workspace_id, user_id, workflow_id)
    _run, pending, _adapter = _pause_run_awaiting_approval(workspace_id, user_id, workflow_id)

    all_response = client.get("/api/v1/automations/approvals")
    assert all_response.status_code == 200
    ids = {a["id"] for a in all_response.json()["approvals"]}
    assert str(pending.id) in ids

    pending_response = client.get("/api/v1/automations/approvals", params={"status": "pending"})
    assert pending_response.status_code == 200
    assert str(pending.id) in {a["id"] for a in pending_response.json()["approvals"]}

    approved_response = client.get("/api/v1/automations/approvals", params={"status": "approved"})
    assert approved_response.status_code == 200
    assert str(pending.id) not in {a["id"] for a in approved_response.json()["approvals"]}


def test_approve_endpoint_success_returns_approved_and_resumes_run(
    approval_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = approval_test_context
    workflow_id = f"test.http-approve.{uuid4().hex}"
    _publish_high_impact_workflow(workspace_id, user_id, workflow_id)
    run, pending, adapter = _pause_run_awaiting_approval(workspace_id, user_id, workflow_id)

    response = client.post(
        f"/api/v1/automations/approvals/{pending.id}/approve",
        json={"action_digest": pending.action_digest},
        headers=_headers(token, key="approve-1"),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "approved"
    assert body["decided_by"] == str(user_id)

    with SessionFactory() as session, session.begin():
        resumed = automation_worker.get_run(session, workspace_id, run.id)
    assert resumed is not None
    assert resumed.status == "queued"

    registry = AdapterRegistry()
    registry.register(adapter)  # type: ignore[arg-type]
    with SessionFactory() as session:
        reclaimed = automation_worker.claim_next_run(session, "worker-b")
        assert reclaimed is not None
        finished = automation_worker.process_claimed_run(session, reclaimed, registry, "worker-b")
    assert finished.status == "succeeded"
    assert adapter.execute_calls == 1


def test_approve_endpoint_digest_mismatch_is_409(
    approval_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = approval_test_context
    workflow_id = f"test.http-mismatch.{uuid4().hex}"
    _publish_high_impact_workflow(workspace_id, user_id, workflow_id)
    _run, pending, adapter = _pause_run_awaiting_approval(workspace_id, user_id, workflow_id)

    response = client.post(
        f"/api/v1/automations/approvals/{pending.id}/approve",
        json={"action_digest": "0" * 64},
        headers=_headers(token, key="approve-mismatch"),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DIGEST_MISMATCH"
    assert adapter.execute_calls == 0


def test_reject_endpoint_success_fails_the_run(
    approval_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """The HTTP-surface half of the rejection-path change (`approvals.
    _advance_run_after_decision`'s own docstring has the reasoning): a
    rejection records the rejected step as a real `'failed'`
    `workflow_run_steps` row and re-queues the run rather than writing
    `'failed'` onto the run inline, so `worker.process_claimed_run`'s own
    `'failed'` branch -- the same seam an adapter exception goes through --
    decides whether compensation applies. This one-step workflow declares
    no `compensate_ref`, so the run still ends `'failed'`, one poll cycle
    later, with `execute()` never called.
    """
    client, workspace_id, user_id, token = approval_test_context
    workflow_id = f"test.http-reject.{uuid4().hex}"
    _publish_high_impact_workflow(workspace_id, user_id, workflow_id)
    run, pending, adapter = _pause_run_awaiting_approval(workspace_id, user_id, workflow_id)

    response = client.post(
        f"/api/v1/automations/approvals/{pending.id}/reject",
        headers=_headers(token, key="reject-1"),
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rejected"

    with SessionFactory() as session, session.begin():
        requeued = automation_worker.get_run(session, workspace_id, run.id)
        steps = automation_worker.list_run_steps(session, workspace_id, run.id)
    assert requeued is not None
    assert requeued.status == "queued"
    assert [(step.status, step.error_class) for step in steps] == [("failed", "ApprovalRejected")]

    registry = AdapterRegistry()
    registry.register(adapter)  # type: ignore[arg-type]
    with SessionFactory() as session:
        reclaimed = automation_worker.claim_next_run(session, "worker-b")
        assert reclaimed is not None
        finished = automation_worker.process_claimed_run(session, reclaimed, registry, "worker-b")
    assert finished.status == "failed"
    assert adapter.execute_calls == 0


def test_approve_endpoint_unknown_id_is_404(
    approval_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = approval_test_context
    response = client.post(
        f"/api/v1/automations/approvals/{uuid4()}/approve",
        json={"action_digest": "0" * 64},
        headers=_headers(token, key="approve-404"),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "APPROVAL_NOT_FOUND"


def test_approve_endpoint_requires_csrf(
    approval_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = approval_test_context
    workflow_id = f"test.http-no-csrf.{uuid4().hex}"
    _publish_high_impact_workflow(workspace_id, user_id, workflow_id)
    _run, pending, _adapter = _pause_run_awaiting_approval(workspace_id, user_id, workflow_id)

    response = client.post(
        f"/api/v1/automations/approvals/{pending.id}/approve",
        json={"action_digest": pending.action_digest},
        headers={"Idempotency-Key": "no-csrf-1", "X-Correlation-ID": str(uuid4())},
    )
    assert response.status_code == 403


def test_approve_endpoint_requires_authentication() -> None:
    client = TestClient(app)
    response = client.post(
        f"/api/v1/automations/approvals/{uuid4()}/approve",
        json={"action_digest": "0" * 64},
        headers={"Idempotency-Key": "no-auth-1", "X-Correlation-ID": str(uuid4())},
    )
    assert response.status_code == 401


def test_approve_endpoint_idempotency_key_replays_cached_response(
    approval_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = approval_test_context
    workflow_id = f"test.http-idempotent.{uuid4().hex}"
    _publish_high_impact_workflow(workspace_id, user_id, workflow_id)
    _run, pending, adapter = _pause_run_awaiting_approval(workspace_id, user_id, workflow_id)

    headers = _headers(token, key="approve-idempotent")
    first = client.post(
        f"/api/v1/automations/approvals/{pending.id}/approve",
        json={"action_digest": pending.action_digest},
        headers=headers,
    )
    second = client.post(
        f"/api/v1/automations/approvals/{pending.id}/approve",
        json={"action_digest": pending.action_digest},
        headers=headers,
    )
    assert first.status_code == 200
    assert second.status_code == 200
    ignored = {"request_id", "correlation_id"}
    first_body = {k: v for k, v in first.json().items() if k not in ignored}
    second_body = {k: v for k, v in second.json().items() if k not in ignored}
    assert first_body == second_body
    # The replayed response is a cache hit, not a second decision -- a
    # genuine re-decision of an already-approved request would have been
    # ApprovalAlreadyDecided/409, not a clean 200 matching the first call.
    assert adapter.execute_calls == 0  # dispatch hasn't happened yet either way


# ---------------------------------------------------------------------------
# 5. Workspace isolation.
# ---------------------------------------------------------------------------


def test_approvals_are_isolated_across_workspaces(
    approval_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_a, user_a, _token_a = approval_test_context
    workflow_id = f"test.isolation.{uuid4().hex}"
    _publish_high_impact_workflow(workspace_a, user_a, workflow_id)
    _run, pending, _adapter = _pause_run_awaiting_approval(workspace_a, user_a, workflow_id)

    workspace_b = uuid4()
    user_b = uuid4()
    token_b = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Approval Isolation Peer', 'Asia/Kolkata', :now)"
            ),
            {"id": workspace_b, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO users (id, workspace_id, email, password_hash, created_at) "
                "VALUES (:id, :workspace_id, :email, 'test-password-hash', :now)"
            ),
            {
                "id": user_b,
                "workspace_id": workspace_b,
                "email": f"{user_b}@example.test",
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
                "workspace_id": workspace_b,
                "user_id": user_b,
                "token_hash": sha256(token_b.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "now": now,
            },
        )

    other_client = TestClient(app)
    other_client.cookies.set("ecc_session", token_b)
    try:
        list_response = other_client.get("/api/v1/automations/approvals")
        assert list_response.status_code == 200
        assert list_response.json()["approvals"] == []

        approve_response = other_client.post(
            f"/api/v1/automations/approvals/{pending.id}/approve",
            json={"action_digest": pending.action_digest},
            headers=_headers(token_b, key="cross-workspace-approve"),
        )
        assert approve_response.status_code == 404
        assert approve_response.json()["error"]["code"] == "APPROVAL_NOT_FOUND"

        # Still fully visible/decidable from its own workspace.
        own_response = client.get("/api/v1/automations/approvals")
        assert str(pending.id) in {a["id"] for a in own_response.json()["approvals"]}
    finally:
        other_client.close()
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM sessions WHERE workspace_id = :id"), {"id": workspace_b}
            )
            connection.execute(
                text("DELETE FROM users WHERE workspace_id = :id"), {"id": workspace_b}
            )
            connection.execute(text("DELETE FROM workspaces WHERE id = :id"), {"id": workspace_b})


def test_reject_is_rejected_across_workspaces(
    approval_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """The cross-workspace half of `POST /approvals/{id}/reject`, which had
    none before this test -- `test_approvals_are_isolated_across_workspaces`
    above covers the list route and `/approve`, but not `/reject`.

    The asymmetry mattered more than it looks. `/approve` carries a second,
    independent defence: the caller must echo the request's own
    `action_digest`, so even a workspace-scoping regression would leave a
    cross-workspace approve blocked by `DIGEST_MISMATCH` for anyone who could
    not also read the victim's digest. `/reject` takes an **empty body** by
    design (`reject_endpoint`'s own `_EmptyBody`) -- there is nothing to echo
    and nothing to guess. A regression that dropped `auth.workspace_id` from
    `decide_approval`'s lookup would therefore be caught by the existing
    approve test only incidentally, and not at all for reject.

    The consequence is terminal, not merely noisy. A rejection is not a
    no-op: `approvals._advance_run_after_decision` writes the rejected step
    as a real `'failed'` `workflow_run_steps` row and re-queues the run, so
    the next poll cycle drives the victim's run to `'failed'` (or through
    compensation), with `execute()` never called. One unauthenticated-to-this-
    workspace request would permanently kill someone else's in-flight run.

    404 `APPROVAL_NOT_FOUND` -- the same response an id that never existed
    gets -- so the route never confirms the approval is real, matching the
    `/approve` assertion's own shape and this codebase's isolation
    convention.
    """
    client, workspace_a, user_a, token_a = approval_test_context
    workflow_id = f"test.isolation-reject.{uuid4().hex}"
    _publish_high_impact_workflow(workspace_a, user_a, workflow_id)
    run, pending, adapter = _pause_run_awaiting_approval(workspace_a, user_a, workflow_id)

    workspace_b = uuid4()
    user_b = uuid4()
    token_b = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Approval Reject Isolation Peer', 'Asia/Kolkata', :now)"
            ),
            {"id": workspace_b, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO users (id, workspace_id, email, password_hash, created_at) "
                "VALUES (:id, :workspace_id, :email, 'test-password-hash', :now)"
            ),
            {
                "id": user_b,
                "workspace_id": workspace_b,
                "email": f"{user_b}@example.test",
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
                "workspace_id": workspace_b,
                "user_id": user_b,
                "token_hash": sha256(token_b.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "now": now,
            },
        )

    other_client = TestClient(app)
    other_client.cookies.set("ecc_session", token_b)
    try:
        reject_response = other_client.post(
            f"/api/v1/automations/approvals/{pending.id}/reject",
            headers=_headers(token_b, key="cross-workspace-reject"),
        )
        assert reject_response.status_code == 404
        assert reject_response.json()["error"]["code"] == "APPROVAL_NOT_FOUND"
    finally:
        other_client.close()
        _cleanup_workspace(workspace_b)

    # The refused call had no effect of any kind on the victim: the request is
    # still undecided and the run is still parked awaiting a decision from its
    # own workspace, not re-queued toward 'failed'. Asserting the *run* too,
    # not only the approval row, is what makes this a test about the
    # consequence rather than about a status column -- a partial write that
    # left the approval pending but re-queued the run would still be a
    # cross-workspace kill.
    with SessionFactory() as session:
        still_pending = automation_approvals.get_pending_approval(
            session, workspace_a, run.id, pending.step_index
        )
        victim_run = automation_worker.get_run(session, workspace_a, run.id)
        victim_steps = automation_worker.list_run_steps(session, workspace_a, run.id)
    assert still_pending is not None
    assert still_pending.id == pending.id
    assert still_pending.status == "pending"
    assert victim_run is not None
    assert victim_run.status == "waiting_approval"
    # No `'failed'`/`ApprovalRejected` step row was written -- the specific
    # trace a landed cross-workspace rejection would have left behind.
    assert [step for step in victim_steps if step.status == "failed"] == []
    assert adapter.execute_calls == 0

    # Still decidable by its real owner, so the 404 above was about
    # authorization and not about the request having become undecidable.
    own_reject = client.post(
        f"/api/v1/automations/approvals/{pending.id}/reject",
        headers=_headers(token_a, key="own-workspace-reject"),
    )
    assert own_reject.status_code == 200, own_reject.text
    assert own_reject.json()["status"] == "rejected"
