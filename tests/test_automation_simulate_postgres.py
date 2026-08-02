"""Phase 5 Automation Task 7a: `/simulate`, the kill-switch status endpoint,
and the compensation ledger in run detail
(`docs/superpowers/specs/2026-07-25-phase-5-automation-design.md` Decision
4, `docs/phases/phase-005/API-SCHEMAS.md`, `docs/phases/phase-005/
UX-STATES.md`'s "Wiring notes, Task 6" paragraph).

Covers, per this task's own required minimum:

1. `POST /automations/workflows/{id}/simulate` never writes any row across
   `workflow_runs`/`workflow_run_steps`/`compensation_steps`/`approval_
   requests`/`audit_events`/`event_outbox` (plus `notes`, since `local.
   create_note` is exercised) -- the endpoint-level extension of `TEST-
   PLAN.md`'s per-adapter fault-injection requirement (Decision 4).
1b. Security audit batch C: `/simulate` now requires a valid CSRF token
   (`CsrfDep`), like every other mutating-method `/api/v1` route -- a
   missing or wrong `X-CSRF-Token` is a 403, the correct one still a 200.
   It still requires no `Idempotency-Key` (it has no state change to
   replay-protect), so every successful-call test below sends CSRF only.
2. `local.create_note`/`local.send_test_notification`/`fake.external_
   action`'s own declared `simulate()` output is surfaced faithfully.
3. An approval-requiring step is correctly flagged (`dispatch_gate ==
   "requires_approval"`) without ever creating an `approval_requests` row.
4. A version naming an unregistered `action_ref` (hand-constructed to
   bypass this task's own new publish-time check, since `activate_workflow_
   version`'s registry check is opt-in) degrades gracefully -- a structured
   per-step error entry, never a 500, and `simulate()` is never called for
   that step.
5. The new publish-time `action_ref`-must-resolve check: rejects an
   unpublishable graph (`422 ACTION_REF_NOT_REGISTERED`), and a workflow
   using only registered adapters still publishes fine (no regression).
6. `GET /automations/workflows/{workflow_id}/kill_switch` -- current
   effective state (global/per-workflow) plus history, workspace-isolated.
7. `GET /automations/runs/{id}` now embeds `compensation_steps` -- the
   ledger for a run that actually compensated, an empty list for a run that
   never needed to, and workspace isolation.
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
from identity_fixtures import create_identity
from pydantic import BaseModel
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.automation import kill_switches as automation_kill_switches
from ecc.domains.automation import policy as automation_policy
from ecc.domains.automation import worker as automation_worker
from ecc.domains.automation import workflows as automation_workflows
from ecc.domains.automation.adapters import AdapterRegistry
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_ZERO_ROWS_TABLES = (
    "workflow_runs",
    "workflow_run_steps",
    "compensation_steps",
    "approval_requests",
    "audit_events",
    "event_outbox",
    "notes",
)


# ---------------------------------------------------------------------------
# Fixtures / helpers -- mirrors test_automation_kill_switches_postgres.py's
# own conventions (each test module owns its fixtures).
# ---------------------------------------------------------------------------


def _seed_workspace(workspace_id: UUID, user_id: UUID, name: str) -> str:
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, :name, 'Asia/Kolkata', :now)"
            ),
            {"id": workspace_id, "name": name, "now": now},
        )
        create_identity(
            connection,
            workspace_id=workspace_id,
            user_id=user_id,
            email=f"{user_id}@example.test",
            now=now,
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
    return token


def _cleanup_workspace(workspace_id: UUID) -> None:
    with engine.begin() as connection:
        for table in (
            "compensation_steps",
            "automation_kill_switches",
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


@pytest.fixture
def simulate_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = _seed_workspace(workspace_id, user_id, "Automation Simulate Test")

    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    try:
        yield client, workspace_id, user_id, token
    finally:
        client.close()
        _cleanup_workspace(workspace_id)


def _headers(token: str, key: str | None = None) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    headers = {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _action_step(
    step_id: str,
    action_ref: str,
    *,
    input_mapping: dict[str, Any] | None = None,
    compensate_ref: str | None = None,
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
    return {"steps": list(steps)}


def _count(workspace_id: UUID, table: str) -> int:
    with engine.begin() as connection:
        result = connection.execute(
            text(f"SELECT COUNT(*) FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
            {"workspace_id": workspace_id},
        ).scalar()
    return int(result or 0)


def _snapshot(workspace_id: UUID) -> dict[str, int]:
    return {table: _count(workspace_id, table) for table in _ZERO_ROWS_TABLES}


def _publish_workflow_direct(
    workspace_id: UUID,
    user_id: UUID,
    workflow_id: str,
    graph: dict[str, Any],
    *,
    approval_mode: automation_policy.ApprovalMode = "bounded_recurring",
    count_limit: int = 1000,
) -> automation_workflows.WorkflowVersion:
    """Direct (non-HTTP) setup, mirroring every other Phase 5 test module's
    own `_publish_workflow` helper -- used for the simulate-behavior tests
    below, which exercise the endpoint itself, not the publish HTTP surface
    (covered separately by tests 5a/5b, which deliberately do go through
    HTTP). Passes no `adapter_registry` to `activate_workflow_version`,
    matching every other pre-existing Phase 5 test's own call shape
    (`activate_workflow_version`'s own docstring: the check is opt-in,
    off by default, precisely so calls like this one are unaffected).
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
    with SessionFactory() as session, session.begin():
        policy_row = automation_policy.create_policy(
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
# 1. simulate never writes any row.
# ---------------------------------------------------------------------------


def test_simulate_never_writes_any_row(
    simulate_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = simulate_test_context
    workflow_id = f"test.sim-zero-rows.{uuid4().hex}"
    graph = _linear_graph(
        _action_step(
            "s1",
            "local.create_note",
            input_mapping={
                "workspace_id": str(workspace_id),
                "actor_id": str(user_id),
                "title": "Simulated note",
                "body": "This note must never actually be created.",
            },
        )
    )
    version = _publish_workflow_direct(workspace_id, user_id, workflow_id, graph)

    before = _snapshot(workspace_id)
    response = client.post(
        f"/api/v1/automations/workflows/{version.id}/simulate", headers=_headers(token)
    )
    assert response.status_code == 200
    after = _snapshot(workspace_id)

    assert before == after
    assert all(count == 0 for count in before.values())


# ---------------------------------------------------------------------------
# 1b. CSRF protection (security audit batch C).
# ---------------------------------------------------------------------------


def test_simulate_requires_csrf(
    simulate_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """`/simulate` was the only mutating-method `/api/v1` route in the
    codebase without `CsrfDep` (security audit batch C -- see `workflows.
    simulate_workflow_endpoint`'s own docstring for why "writes no row" is
    not a reason to omit CSRF). Structured exactly like `test_automation_
    workflows_postgres.py`'s `test_create_workflow_requires_csrf` and `test_
    automation_runs_postgres.py`'s `test_create_run_endpoint_requires_csrf`:
    an otherwise-valid, fully-authenticated request (the session cookie is
    still on the client) with no `X-CSRF-Token` header must be rejected.

    No `Idempotency-Key` appears in either the rejected or the accepted case
    -- this route deliberately does not require one (nothing to
    replay-protect), so the two protections are exercised independently here,
    unlike the `create_workflow`/`create_run` tests above whose routes
    require both.
    """
    client, workspace_id, user_id, token = simulate_test_context
    workflow_id = f"test.sim-no-csrf.{uuid4().hex}"
    graph = _linear_graph(_action_step("s1", "local.send_test_notification"))
    version = _publish_workflow_direct(workspace_id, user_id, workflow_id, graph)

    missing = client.post(
        f"/api/v1/automations/workflows/{version.id}/simulate",
        headers={"X-Correlation-ID": str(uuid4())},
    )
    assert missing.status_code == 403

    # A present-but-wrong token is rejected too, not merely a missing one
    # (require_csrf's own compare_digest branch, distinct from its
    # "header absent" branch).
    wrong = client.post(
        f"/api/v1/automations/workflows/{version.id}/simulate",
        headers={"X-CSRF-Token": "not-the-real-token", "X-Correlation-ID": str(uuid4())},
    )
    assert wrong.status_code == 403

    # And the same request with the correct token still succeeds -- proving
    # the 403s above are the CSRF check, not an unrelated regression.
    allowed = client.post(
        f"/api/v1/automations/workflows/{version.id}/simulate", headers=_headers(token)
    )
    assert allowed.status_code == 200


# ---------------------------------------------------------------------------
# 2. Each adapter's own declared simulate() output is surfaced faithfully.
# ---------------------------------------------------------------------------


def test_simulate_surfaces_local_create_note_declared_output(
    simulate_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = simulate_test_context
    workflow_id = f"test.sim-create-note.{uuid4().hex}"
    graph = _linear_graph(
        _action_step(
            "s1",
            "local.create_note",
            input_mapping={
                "workspace_id": str(workspace_id),
                "actor_id": str(user_id),
                "title": "Preview title",
                "body": "Preview body -- never persisted.",
            },
        )
    )
    version = _publish_workflow_direct(workspace_id, user_id, workflow_id, graph)

    response = client.post(
        f"/api/v1/automations/workflows/{version.id}/simulate", headers=_headers(token)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["workflow_id"] == workflow_id
    [step] = body["steps"]
    assert step["step_type"] == "action"
    assert step["action_ref"] == "local.create_note"
    assert step["error"] is None
    assert step["reversible"] is True
    assert step["high_impact_categories"] == []
    assert step["preview"]["title"] == "Preview title"
    assert "body" not in step["preview"]  # CreateNoteOutput never carries body
    assert step["dispatch_gate"] == "clear"  # bounded, count_limit not reached
    assert _count(workspace_id, "notes") == 0


def test_simulate_surfaces_local_send_test_notification_declared_output(
    simulate_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = simulate_test_context
    workflow_id = f"test.sim-send-notification.{uuid4().hex}"
    graph = _linear_graph(
        _action_step(
            "s1",
            "local.send_test_notification",
            input_mapping={
                "workspace_id": str(workspace_id),
                "recipient_user_id": str(user_id),
                "message": "Hello from a simulation.",
            },
        )
    )
    version = _publish_workflow_direct(workspace_id, user_id, workflow_id, graph)

    response = client.post(
        f"/api/v1/automations/workflows/{version.id}/simulate", headers=_headers(token)
    )
    assert response.status_code == 200
    [step] = response.json()["steps"]
    assert step["preview"]["message"] == "Hello from a simulation."
    assert step["reversible"] is False
    assert step["high_impact_categories"] == ["person-directed"]
    # person-directed always requires approval, regardless of policy mode.
    assert step["dispatch_gate"] == "requires_approval"


def test_simulate_surfaces_fake_external_action_declared_output(
    simulate_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = simulate_test_context
    workflow_id = f"test.sim-fake-external.{uuid4().hex}"
    graph = _linear_graph(
        _action_step(
            "s1",
            "fake.external_action",
            input_mapping={
                "workspace_id": str(workspace_id),
                "external_ref": "issue-123",
                "payload": {"title": "Simulated"},
            },
        )
    )
    version = _publish_workflow_direct(workspace_id, user_id, workflow_id, graph)

    response = client.post(
        f"/api/v1/automations/workflows/{version.id}/simulate", headers=_headers(token)
    )
    assert response.status_code == 200
    [step] = response.json()["steps"]
    assert step["preview"]["external_ref"] == "issue-123"
    assert step["preview"]["status"] == "would_create"
    assert step["reversible"] is True
    assert step["high_impact_categories"] == ["public"]
    assert step["dispatch_gate"] == "requires_approval"


# ---------------------------------------------------------------------------
# 3. Approval-requiring step flagged without creating an approval_requests
#    row.
# ---------------------------------------------------------------------------


def test_simulate_flags_approval_without_creating_approval_request(
    simulate_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = simulate_test_context
    workflow_id = f"test.sim-approval-flag.{uuid4().hex}"
    graph = _linear_graph(
        _action_step(
            "s1",
            "local.send_test_notification",
            input_mapping={
                "workspace_id": str(workspace_id),
                "recipient_user_id": str(user_id),
                "message": "Needs approval.",
            },
        )
    )
    version = _publish_workflow_direct(workspace_id, user_id, workflow_id, graph)

    before = _count(workspace_id, "approval_requests")
    response = client.post(
        f"/api/v1/automations/workflows/{version.id}/simulate", headers=_headers(token)
    )
    assert response.status_code == 200
    [step] = response.json()["steps"]
    assert step["dispatch_gate"] == "requires_approval"
    after = _count(workspace_id, "approval_requests")
    assert before == after == 0


def test_simulate_rejects_workspace_scope_mismatch_before_calling_simulate(
    simulate_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """The one confused-deputy check `/simulate` adds
    (`SimulateWorkspaceScopeMismatch`) had zero test coverage before this
    review pass -- verified only by reading the code that the mismatch is
    caught before `adapter.simulate()` is ever called. Proven directly here:
    a step whose resolved `input_mapping.workspace_id` names a foreign
    workspace must surface as an error, never a leaked preview.
    """
    client, workspace_id, user_id, token = simulate_test_context
    foreign_workspace_id = uuid4()
    workflow_id = f"test.sim-scope-mismatch.{uuid4().hex}"
    graph = _linear_graph(
        _action_step(
            "s1",
            "local.create_note",
            input_mapping={
                "workspace_id": str(foreign_workspace_id),
                "actor_id": str(user_id),
                "body": "cross-workspace scope mismatch probe",
            },
        )
    )
    version = _publish_workflow_direct(workspace_id, user_id, workflow_id, graph)

    response = client.post(
        f"/api/v1/automations/workflows/{version.id}/simulate", headers=_headers(token)
    )
    assert response.status_code == 200
    [step] = response.json()["steps"]
    assert step["error"] == "SimulateWorkspaceScopeMismatch"
    assert step["preview"] is None


def test_simulate_redact_payload_replaces_marker_keys() -> None:
    """None of the three registered adapters' own `simulate()` output
    schemas happen to contain a redaction-marker-matching key, so no
    end-to-end test above ever exercises `_simulate_redact_payload`'s
    actual replacement branch (only its pass-through branch). Proven
    directly at the unit level instead.
    """
    payload = {
        "note": "visible",
        "api_key": "should-not-appear",
        "nested": {"password": "also-hidden", "ok": "fine"},
        "items": [{"credential": "gone"}, {"visible": "kept"}],
    }
    redacted = automation_workflows._simulate_redact_payload(payload)
    assert redacted["note"] == "visible"
    assert redacted["api_key"] == "[REDACTED]"
    assert redacted["nested"]["password"] == "[REDACTED]"
    assert redacted["nested"]["ok"] == "fine"
    assert redacted["items"][0]["credential"] == "[REDACTED]"
    assert redacted["items"][1]["visible"] == "kept"


# ---------------------------------------------------------------------------
# 4. Unregistered action_ref degrades gracefully.
# ---------------------------------------------------------------------------


def test_simulate_degrades_gracefully_for_unregistered_action_ref(
    simulate_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """Hand-constructs a version naming an action_ref the shared production
    registry does not resolve -- exactly the scenario a version drafted (or
    published) before this task's own publish-time check existed could
    still reach. `activate_workflow_version` is called with no `adapter_
    registry` (its own default), deliberately bypassing the new check to
    reproduce that pre-existing-version case.
    """
    client, workspace_id, user_id, token = simulate_test_context
    workflow_id = f"test.sim-unregistered.{uuid4().hex}"
    graph = _linear_graph(_action_step("s1", "test.definitely-not-a-real-adapter"))
    version = _publish_workflow_direct(workspace_id, user_id, workflow_id, graph)

    response = client.post(
        f"/api/v1/automations/workflows/{version.id}/simulate", headers=_headers(token)
    )
    assert response.status_code == 200
    [step] = response.json()["steps"]
    assert step["dispatch_gate"] == "adapter_not_registered"
    assert step["error"] == "AdapterNotRegistered"
    assert step["preview"] is None


def test_simulate_policy_blocked_takes_precedence_over_adapter_not_registered(
    simulate_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """Found during review: `_simulate_steps` originally checked adapter
    registration before policy usability, so a step whose policy was
    unusable *and* whose action_ref was unregistered wrongly reported
    `adapter_not_registered` -- a real run on the identical graph would
    report `policy_blocked` first via `worker._evaluate_dispatch_gate`
    (policy checked before the adapter is even resolved) and never reach
    the adapter-registration question at all. This is exactly the
    "identical graph-walk ... not a separate, potentially-drifting guess"
    property Decision 4 requires. Uses a version with no policy at all
    (`policy_ref=None`, a valid, unattached-policy draft state -- `DATA-
    MODEL.md`: "a workflow can be drafted before or after its intended
    policy") naming an unregistered action_ref, so both conditions are
    true simultaneously and only the ordering distinguishes the two
    possible (wrong vs. correct) outcomes.
    """
    client, workspace_id, user_id, token = simulate_test_context
    workflow_id = f"test.sim-policy-precedence.{uuid4().hex}"
    graph = _linear_graph(_action_step("s1", "test.definitely-not-a-real-adapter"))
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

    response = client.post(
        f"/api/v1/automations/workflows/{activated.id}/simulate", headers=_headers(token)
    )
    assert response.status_code == 200
    [step] = response.json()["steps"]
    assert step["dispatch_gate"] == "policy_blocked"
    assert step["policy_block_reason"] == "no_policy"
    assert step["error"] is None
    assert step["preview"] is None


def test_simulate_workspace_isolation(
    simulate_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = simulate_test_context
    other_workspace_id = uuid4()
    other_user_id = uuid4()
    other_token = _seed_workspace(other_workspace_id, other_user_id, "Simulate Isolation Peer")
    try:
        workflow_id = f"test.sim-isolation.{uuid4().hex}"
        graph = _linear_graph(_action_step("s1", "local.send_test_notification"))
        version = _publish_workflow_direct(workspace_id, user_id, workflow_id, graph)

        other_client = TestClient(app)
        other_client.cookies.set("ecc_session", other_token)
        try:
            response = other_client.post(
                f"/api/v1/automations/workflows/{version.id}/simulate",
                headers=_headers(other_token),
            )
            assert response.status_code == 404
        finally:
            other_client.close()
    finally:
        _cleanup_workspace(other_workspace_id)


# ---------------------------------------------------------------------------
# 5. Publish-time action_ref-must-resolve check.
# ---------------------------------------------------------------------------


def test_publish_rejects_unregistered_action_ref(
    simulate_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = simulate_test_context
    graph = {
        "steps": [
            {
                "step_id": "s1",
                "step_type": "action",
                "action_ref": "test.definitely-not-a-real-adapter",
                "input_mapping": {},
                "on_success": "succeeded",
                "on_failure": "failed",
            }
        ]
    }
    created = client.post(
        "/api/v1/automations/workflows",
        json={"workflow_id": f"test.publish-reject.{uuid4().hex}", "graph": graph},
        headers=_headers(token, key="publish-reject-create"),
    ).json()

    publish = client.post(
        f"/api/v1/automations/workflows/{created['id']}/publish",
        headers=_headers(token, key="unregistered-adapter"),
    )
    assert publish.status_code == 422
    assert publish.json()["error"]["code"] == "ACTION_REF_NOT_REGISTERED"

    # Confirm the version genuinely never activated -- still draft.
    get_response = client.get(f"/api/v1/automations/workflows/{created['id']}")
    assert get_response.json()["status"] == "draft"


def test_publish_succeeds_with_only_registered_adapters(
    simulate_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = simulate_test_context
    graph = {
        "steps": [
            {
                "step_id": "s1",
                "step_type": "action",
                "action_ref": "local.send_test_notification",
                "input_mapping": {},
                "on_success": "succeeded",
                "on_failure": "failed",
                "compensate_ref": "c1",
            },
            {
                # `local.create_note`, not `fake.external_action`: this test
                # asserts only that publishing succeeds when every
                # `action_ref` resolves, and a compensation step's own
                # `action_ref` must additionally name a *non*-high-impact
                # adapter (`workflows.high_impact_compensation_action_refs`
                # -- compensation dispatch never creates the per-run approval
                # request a high-impact adapter requires, so such a graph is
                # now rejected at publish). `fake.external_action` declares
                # `high_impact_categories={'public'}`; `local.create_note`
                # declares none, so it keeps this test testing exactly what
                # it was written to test.
                "step_id": "c1",
                "step_type": "compensation",
                "action_ref": "local.create_note",
                "input_mapping": {},
            },
        ]
    }
    created = client.post(
        "/api/v1/automations/workflows",
        json={"workflow_id": f"test.publish-ok.{uuid4().hex}", "graph": graph},
        headers=_headers(token, key="publish-ok-create"),
    ).json()
    assert "id" in created, created

    publish = client.post(
        f"/api/v1/automations/workflows/{created['id']}/publish",
        headers=_headers(token, key="publish-ok-1"),
    )
    assert publish.status_code == 200
    assert publish.json()["status"] == "active"


# ---------------------------------------------------------------------------
# 6. GET /automations/workflows/{workflow_id}/kill_switch.
# ---------------------------------------------------------------------------


def test_kill_switch_status_reflects_current_effective_state(
    simulate_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = simulate_test_context
    workflow_id = f"test.kill-status.{uuid4().hex}"
    other_workflow_id = f"test.kill-status-other.{uuid4().hex}"

    initial = client.get(f"/api/v1/automations/workflows/{workflow_id}/kill_switch")
    assert initial.status_code == 200
    # request_observability_middleware injects `request_id`/`correlation_id`
    # into every successful JSON response body globally (backend/ecc/
    # main.py) -- ignored here, matching test_automation_workflows_
    # postgres.py's own `ignored = {"request_id", "correlation_id"}`
    # precedent for the identical reason.
    initial_body = {
        k: v for k, v in initial.json().items() if k not in ("request_id", "correlation_id")
    }
    assert initial_body == {
        "workflow_id": workflow_id,
        "killed": False,
        "active_global": None,
        "active_workflow": None,
        "history": [],
    }

    client.post(
        f"/api/v1/automations/workflows/{workflow_id}/kill_switch",
        json={"active": True, "reason": "per-workflow incident"},
        headers=_headers(token, key="kill-status-activate-1"),
    )
    after_workflow_kill = client.get(f"/api/v1/automations/workflows/{workflow_id}/kill_switch")
    body = after_workflow_kill.json()
    assert body["killed"] is True
    assert body["active_workflow"]["workflow_id"] == workflow_id
    assert body["active_global"] is None
    assert len(body["history"]) == 1

    # A global switch shows up for *every* workflow, including one never
    # itself killed directly.
    client.post(
        "/api/v1/automations/kill_switch",
        json={"active": True, "reason": "global incident"},
        headers=_headers(token, key="kill-status-activate-global"),
    )
    other_status = client.get(f"/api/v1/automations/workflows/{other_workflow_id}/kill_switch")
    other_body = other_status.json()
    assert other_body["killed"] is True
    assert other_body["active_global"] is not None
    assert other_body["active_workflow"] is None
    assert other_body["history"] == []  # no per-workflow row for this workflow_id

    # Deactivating the per-workflow switch leaves it out of `killed`
    # (global still active) but keeps it in history.
    client.post(
        f"/api/v1/automations/workflows/{workflow_id}/kill_switch",
        json={"active": False},
        headers=_headers(token, key="kill-status-deactivate-1"),
    )
    final = client.get(f"/api/v1/automations/workflows/{workflow_id}/kill_switch").json()
    assert final["active_workflow"] is None
    assert final["killed"] is True  # global switch still active
    assert len(final["history"]) == 1


def test_kill_switch_status_workspace_isolation(
    simulate_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = simulate_test_context
    other_workspace_id = uuid4()
    other_user_id = uuid4()
    other_token = _seed_workspace(other_workspace_id, other_user_id, "Kill Switch Status Peer")
    try:
        shared_workflow_id = f"test.kill-status-shared.{uuid4().hex}"
        client.post(
            f"/api/v1/automations/workflows/{shared_workflow_id}/kill_switch",
            json={"active": True, "reason": "workspace A only"},
            headers=_headers(token, key="kill-status-isolation-1"),
        )

        other_client = TestClient(app)
        other_client.cookies.set("ecc_session", other_token)
        try:
            response = other_client.get(
                f"/api/v1/automations/workflows/{shared_workflow_id}/kill_switch"
            )
            assert response.status_code == 200
            body = response.json()
            assert body["killed"] is False
            assert body["active_workflow"] is None
            assert body["history"] == []
        finally:
            other_client.close()

        with SessionFactory() as session:
            assert (
                automation_kill_switches.is_workflow_killed(
                    session, workspace_id, shared_workflow_id
                )
                is True
            )
    finally:
        _cleanup_workspace(other_workspace_id)


# ---------------------------------------------------------------------------
# 7. Compensation ledger in run detail.
# ---------------------------------------------------------------------------


class _EchoInput(BaseModel):
    value: str = ""


class _EchoOutput(BaseModel):
    value: str


class _SucceedingAdapter:
    def __init__(self, adapter_id: str) -> None:
        self.adapter_id = adapter_id
        self.input_schema = _EchoInput
        self.output_schema = _EchoOutput
        self.reversible = True
        self.high_impact_categories: frozenset[str] = frozenset()

    def simulate(self, action_input: _EchoInput) -> _EchoOutput:  # noqa: D102
        return _EchoOutput(value=action_input.value)

    def execute(self, action_input: _EchoInput) -> _EchoOutput:  # noqa: D102
        return _EchoOutput(value=action_input.value)


class _FailingAdapter:
    def __init__(self, adapter_id: str) -> None:
        self.adapter_id = adapter_id
        self.input_schema = _EchoInput
        self.output_schema = _EchoOutput
        self.reversible = True
        self.high_impact_categories: frozenset[str] = frozenset()

    def simulate(self, action_input: _EchoInput) -> _EchoOutput:  # noqa: D102
        return _EchoOutput(value=action_input.value)

    def execute(self, action_input: _EchoInput) -> _EchoOutput:  # noqa: D102
        raise RuntimeError("this step always fails")


class _CompensatableAdapter:
    def __init__(self, adapter_id: str) -> None:
        self.adapter_id = adapter_id
        self.input_schema = _EchoInput
        self.output_schema = _EchoOutput
        self.reversible = True
        self.high_impact_categories: frozenset[str] = frozenset()

    def simulate(self, action_input: _EchoInput) -> _EchoOutput:  # noqa: D102
        return _EchoOutput(value=action_input.value)

    def execute(self, action_input: _EchoInput) -> _EchoOutput:  # noqa: D102
        return _EchoOutput(value=action_input.value)

    def compensate(self, action_input: _EchoInput) -> _EchoOutput:  # noqa: D102
        return _EchoOutput(value=f"compensated:{action_input.value}")


def _make_registry(*adapters: Any) -> AdapterRegistry:
    registry = AdapterRegistry()
    for adapter in adapters:
        registry.register(adapter)
    return registry


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


def test_run_detail_compensation_ledger_populated_for_a_compensated_run(
    simulate_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = simulate_test_context
    workflow_id = f"test.run-detail-compensated.{uuid4().hex}"
    graph = _linear_graph(
        _action_step("s1", "test.rd-s1", compensate_ref="c1"),
        _compensation_step("c1", "test.rd-c1"),
        _action_step("s2", "test.rd-s2"),
    )
    _publish_workflow_direct(workspace_id, user_id, workflow_id, graph)
    registry = _make_registry(
        _SucceedingAdapter("test.rd-s1"),
        _CompensatableAdapter("test.rd-c1"),
        _FailingAdapter("test.rd-s2"),
    )
    finished = _run_to_completion(workspace_id, user_id, workflow_id, registry)
    assert finished.status == "compensated"

    response = client.get(f"/api/v1/automations/runs/{finished.id}")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "compensated"
    [ledger_entry] = body["compensation_steps"]
    assert ledger_entry["compensates_step_index"] == 0
    assert ledger_entry["status"] == "succeeded"
    assert ledger_entry["action_digest"] is not None


def test_run_detail_compensation_ledger_empty_for_an_ordinary_run(
    simulate_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = simulate_test_context
    workflow_id = f"test.run-detail-ordinary.{uuid4().hex}"
    graph = _linear_graph(_action_step("s1", "test.rd-ordinary-s1"))
    _publish_workflow_direct(workspace_id, user_id, workflow_id, graph)
    registry = _make_registry(_SucceedingAdapter("test.rd-ordinary-s1"))
    finished = _run_to_completion(workspace_id, user_id, workflow_id, registry)
    assert finished.status == "succeeded"

    response = client.get(f"/api/v1/automations/runs/{finished.id}")
    assert response.status_code == 200
    assert response.json()["compensation_steps"] == []


def test_run_detail_compensation_ledger_workspace_isolation(
    simulate_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = simulate_test_context
    workflow_id = f"test.run-detail-isolation.{uuid4().hex}"
    graph = _linear_graph(_action_step("s1", "test.rd-iso-s1"))
    _publish_workflow_direct(workspace_id, user_id, workflow_id, graph)
    registry = _make_registry(_SucceedingAdapter("test.rd-iso-s1"))
    finished = _run_to_completion(workspace_id, user_id, workflow_id, registry)

    other_workspace_id = uuid4()
    other_user_id = uuid4()
    other_token = _seed_workspace(other_workspace_id, other_user_id, "Run Detail Isolation Peer")
    try:
        other_client = TestClient(app)
        other_client.cookies.set("ecc_session", other_token)
        try:
            response = other_client.get(f"/api/v1/automations/runs/{finished.id}")
            assert response.status_code == 404
        finally:
            other_client.close()
    finally:
        _cleanup_workspace(other_workspace_id)
