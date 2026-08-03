"""Phase 5 Automation Task 4: the `/api/v1/automations/runs` HTTP surface
(`docs/phases/phase-005/API-SCHEMAS.md`, `ecc.domains.automation.runs`).

Covers, per this task's own required minimum, mirroring `tests/
test_automation_approvals_postgres.py`'s exact fixture shape:

1. Manual trigger via `POST /automations/runs` creates a run and it
   processes normally through the ordinary claim/poll path.
2. `GET /automations/runs`/`GET /automations/runs/{id}` return correct,
   workspace-scoped data, the latter including step detail.
3. `POST .../cancel`, `POST .../pause`, `POST .../resume` all work
   end-to-end via HTTP, including pause-then-resume completing normally.
4. CSRF, auth, idempotency-key replay, workspace isolation.
5. No endpoint accepts a caller-supplied `policy_id` -- `extra="forbid"`
   rejects it with a `422`, never silently ignoring or honoring it.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from identity_fixtures import create_identity
from pydantic import BaseModel
from sqlalchemy import text

from ecc.auth import AuthContext
from ecc.config import get_settings
from ecc.database import SessionFactory, engine
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


# ---------------------------------------------------------------------------
# Test-only fake adapter -- this task registers zero real adapters (module
# docstrings throughout this package); mirrors test_automation_approvals_
# postgres.py's own `_HighImpactAdapter`/`_Input`/`_Output` shape.
# ---------------------------------------------------------------------------


class _Input(BaseModel):
    value: str = ""


class _Output(BaseModel):
    value: str


class _EchoAdapter:
    adapter_id = "test.runs-echo"
    input_schema = _Input
    output_schema = _Output
    reversible = True
    high_impact_categories: frozenset[str] = frozenset()

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


def _chained_graph(*steps: dict[str, Any]) -> dict[str, Any]:
    steps = list(steps)  # type: ignore[assignment]
    for index, step in enumerate(steps[:-1]):
        step["on_success"] = steps[index + 1]["step_id"]
    return {"steps": steps}


# ---------------------------------------------------------------------------
# Fixtures / helpers -- self-contained, mirroring test_automation_approvals_
# postgres.py's own fixture shape exactly.
# ---------------------------------------------------------------------------


@pytest.fixture
def runs_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Automation Runs Test', 'Asia/Kolkata', :now)"
            ),
            {"id": workspace_id, "now": now},
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
    from hmac import new

    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    headers = {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _publish_workflow(
    workspace_id: UUID,
    user_id: UUID,
    workflow_id: str,
    *,
    steps: int = 1,
    rate_limit: dict[str, Any] | None = None,
) -> automation_workflows.WorkflowVersion:
    """An active workflow with a usable, generous `bounded_recurring`
    policy attached -- Task 3's dispatch gate fail-closes on a run with
    no usable policy at all (`worker.py`'s own module docstring), so
    every test here that goes on to actually process a run (`worker.
    claim_next_run`/`process_claimed_run`) needs one, exactly like
    `test_automation_worker_postgres.py`'s own `_publish_workflow`
    helper. A throwaway version=1 draft exists solely to create the
    `workflow_definitions` family row `automation_policies`' own FK
    requires, immediately superseded by the real, policy-bound draft.

    `rate_limit=None` keeps `create_policy`'s own documented system default
    (`{"runs_per_workflow_per_hour": 10}`) -- harmless for every test here,
    each of which uses a fresh `workflow_id` and starts well under ten runs.
    `test_create_run_endpoint_rate_limited_is_409` passes an explicit value.
    """
    graph = _chained_graph(*[_action_step(f"s{i + 1}", "test.runs-echo") for i in range(steps)])
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
            rate_limit=rate_limit,
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


# ---------------------------------------------------------------------------
# 1. POST /automations/runs -- manual trigger.
# ---------------------------------------------------------------------------


def test_create_run_endpoint_enqueues_and_processes_normally(
    runs_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = runs_test_context
    workflow_id = f"test.http-create.{uuid4().hex}"
    _publish_workflow(workspace_id, user_id, workflow_id)

    response = client.post(
        "/api/v1/automations/runs",
        json={"workflow_id": workflow_id},
        headers=_headers(token, key="create-run-1"),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["workflow_id"] == workflow_id
    assert body["status"] == "queued"
    assert body["trigger_ref"] == f"manual:{user_id}"
    assert body["policy_id"] is not None  # server-resolved from the active version

    adapter = _EchoAdapter()
    registry = AdapterRegistry()
    registry.register(adapter)  # type: ignore[arg-type] -- structural Protocol, see adapters.py
    run_id = UUID(body["id"])
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
        assert claimed is not None
        assert claimed.id == run_id
        finished = automation_worker.process_claimed_run(session, claimed, registry, "worker-a")
    assert finished.status == "succeeded"
    assert adapter.execute_calls == 1


def test_create_run_endpoint_rejects_caller_supplied_policy_id(
    runs_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """Confused-deputy protection (`API-SCHEMAS.md`): a `policy_id` field
    in the request body, if present at all, is rejected (`422`, Pydantic's
    `extra="forbid"`), never silently ignored or honored -- proven by
    confirming the workspace-scoped run count stays zero after the
    rejected attempt (i.e. this is a real validation rejection, not a
    200 that merely dropped the field).
    """
    client, workspace_id, user_id, token = runs_test_context
    workflow_id = f"test.http-confused-deputy.{uuid4().hex}"
    _publish_workflow(workspace_id, user_id, workflow_id)

    response = client.post(
        "/api/v1/automations/runs",
        json={"workflow_id": workflow_id, "policy_id": str(uuid4())},
        headers=_headers(token, key="create-run-policy-id"),
    )
    assert response.status_code == 422

    auth = AuthContext(workspace_id=workspace_id, user_id=user_id, timezone="UTC")
    with SessionFactory() as session, session.begin():
        runs = automation_worker.list_runs(session, auth, workspace_id)
    assert runs == []


def test_create_run_endpoint_workflow_not_active_is_409(
    runs_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = runs_test_context
    response = client.post(
        "/api/v1/automations/runs",
        json={"workflow_id": f"test.never-existed.{uuid4().hex}"},
        headers=_headers(token, key="create-run-inactive"),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "WORKFLOW_NOT_ACTIVE"


def test_create_run_endpoint_rate_limited_is_409(
    runs_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """`API-SCHEMAS.md`'s required `rate_limited` error code, surfaced by
    the endpoint for the first time. `worker.enqueue_run` rejects the run
    past the policy's own `runs_per_workflow_per_hour` ceiling before any
    `workflow_runs` row is written (`APPROVAL-POLICY.md`: "rejected at
    enqueue with `rate_limited`"), and this endpoint maps that to a 409
    naming the limit -- the same shape `WORKFLOW_NOT_ACTIVE`/
    `KILL_SWITCH_ACTIVE` already use.
    """
    client, workspace_id, user_id, token = runs_test_context
    workflow_id = f"test.http-rate-limit.{uuid4().hex}"
    _publish_workflow(
        workspace_id, user_id, workflow_id, rate_limit={"runs_per_workflow_per_hour": 1}
    )

    first = client.post(
        "/api/v1/automations/runs",
        json={"workflow_id": workflow_id},
        headers=_headers(token, key=f"rate-limit-1-{uuid4().hex}"),
    )
    assert first.status_code == 201

    second = client.post(
        "/api/v1/automations/runs",
        json={"workflow_id": workflow_id},
        headers=_headers(token, key=f"rate-limit-2-{uuid4().hex}"),
    )
    assert second.status_code == 409
    error = second.json()["error"]
    assert error["code"] == "RATE_LIMITED"
    assert error["details"]["workflow_id"] == workflow_id
    assert error["details"]["limit"] == 1

    # Rejected *before* any row was written -- exactly one run exists.
    auth = AuthContext(workspace_id=workspace_id, user_id=user_id, timezone="UTC")
    with SessionFactory() as session, session.begin():
        runs = automation_worker.list_runs(session, auth, workspace_id)
    assert len([run for run in runs if run.workflow_id == workflow_id]) == 1


def test_create_run_endpoint_requires_csrf(
    runs_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = runs_test_context
    workflow_id = f"test.http-no-csrf.{uuid4().hex}"
    _publish_workflow(workspace_id, user_id, workflow_id)

    response = client.post(
        "/api/v1/automations/runs",
        json={"workflow_id": workflow_id},
        headers={"Idempotency-Key": "no-csrf-1", "X-Correlation-ID": str(uuid4())},
    )
    assert response.status_code == 403


def test_create_run_endpoint_requires_authentication() -> None:
    client = TestClient(app)
    response = client.post(
        "/api/v1/automations/runs",
        json={"workflow_id": "test.no-auth"},
        headers={"Idempotency-Key": "no-auth-1", "X-Correlation-ID": str(uuid4())},
    )
    assert response.status_code == 401


def test_create_run_endpoint_idempotency_key_replays_cached_response(
    runs_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = runs_test_context
    workflow_id = f"test.http-idempotent.{uuid4().hex}"
    _publish_workflow(workspace_id, user_id, workflow_id)

    headers = _headers(token, key="create-run-idempotent")
    first = client.post(
        "/api/v1/automations/runs", json={"workflow_id": workflow_id}, headers=headers
    )
    second = client.post(
        "/api/v1/automations/runs", json={"workflow_id": workflow_id}, headers=headers
    )
    assert first.status_code == 201
    assert second.status_code == 201
    ignored = {"request_id", "correlation_id"}
    first_body = {k: v for k, v in first.json().items() if k not in ignored}
    second_body = {k: v for k, v in second.json().items() if k not in ignored}
    assert first_body == second_body

    auth = AuthContext(workspace_id=workspace_id, user_id=user_id, timezone="UTC")
    with SessionFactory() as session, session.begin():
        runs = automation_worker.list_runs(session, auth, workspace_id)
    assert len(runs) == 1  # only one run was ever actually enqueued


# ---------------------------------------------------------------------------
# 2. GET /automations/runs, GET /automations/runs/{id}.
# ---------------------------------------------------------------------------


def test_list_runs_endpoint_workspace_scoped_with_status_filter(
    runs_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = runs_test_context
    workflow_id = f"test.http-list.{uuid4().hex}"
    _publish_workflow(workspace_id, user_id, workflow_id)

    for key in ("list-run-1", "list-run-2"):
        response = client.post(
            "/api/v1/automations/runs",
            json={"workflow_id": workflow_id},
            headers=_headers(token, key=key),
        )
        assert response.status_code == 201

    all_response = client.get("/api/v1/automations/runs")
    assert all_response.status_code == 200
    assert len(all_response.json()["runs"]) == 2

    filtered_response = client.get("/api/v1/automations/runs", params={"status": "queued"})
    assert filtered_response.status_code == 200
    assert len(filtered_response.json()["runs"]) == 2

    filtered_empty = client.get("/api/v1/automations/runs", params={"status": "failed"})
    assert filtered_empty.status_code == 200
    assert filtered_empty.json()["runs"] == []


def test_get_run_endpoint_returns_detail_with_step_state(
    runs_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = runs_test_context
    workflow_id = f"test.http-detail.{uuid4().hex}"
    _publish_workflow(workspace_id, user_id, workflow_id)

    create_response = client.post(
        "/api/v1/automations/runs",
        json={"workflow_id": workflow_id},
        headers=_headers(token, key="detail-run-1"),
    )
    assert create_response.status_code == 201
    run_id = create_response.json()["id"]

    detail_before = client.get(f"/api/v1/automations/runs/{run_id}")
    assert detail_before.status_code == 200
    assert detail_before.json()["steps"] == []  # not yet dispatched

    adapter = _EchoAdapter()
    registry = AdapterRegistry()
    registry.register(adapter)  # type: ignore[arg-type]
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
        assert claimed is not None
        automation_worker.process_claimed_run(session, claimed, registry, "worker-a")

    detail_after = client.get(f"/api/v1/automations/runs/{run_id}")
    assert detail_after.status_code == 200
    body = detail_after.json()
    assert body["status"] == "succeeded"
    assert len(body["steps"]) == 1
    assert body["steps"][0]["status"] == "succeeded"
    assert body["steps"][0]["output"] == {"value": ""}
    # Task 7: attempt_count (workflow_run_steps, Task 6) was never wired
    # into this response before now -- a real gap found while building the
    # "retrying" run-detail UI, closed as a pure response-shape addition.
    assert body["steps"][0]["attempt_count"] == 0
    # Found during my own independent review of Task 7's PR: action_ref was
    # never surfaced at all, so an approver had no way to see the actual
    # target (which adapter/action) a step invokes -- resolved at read time
    # from the run's own pinned (workflow_id, workflow_version) graph.
    assert body["steps"][0]["action_ref"] == adapter.adapter_id


def test_get_run_endpoint_unknown_id_is_404(
    runs_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, _token = runs_test_context
    response = client.get(f"/api/v1/automations/runs/{uuid4()}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "RUN_NOT_FOUND"


# ---------------------------------------------------------------------------
# 3. POST .../cancel, .../pause, .../resume -- end to end via HTTP.
# ---------------------------------------------------------------------------


def test_cancel_run_endpoint_end_to_end(
    runs_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = runs_test_context
    workflow_id = f"test.http-cancel.{uuid4().hex}"
    _publish_workflow(workspace_id, user_id, workflow_id)
    create_response = client.post(
        "/api/v1/automations/runs",
        json={"workflow_id": workflow_id},
        headers=_headers(token, key="cancel-run-1"),
    )
    run_id = create_response.json()["id"]

    response = client.post(
        f"/api/v1/automations/runs/{run_id}/cancel", headers=_headers(token, key="cancel-run-do-1")
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "cancelled"

    with SessionFactory() as session, session.begin():
        run = automation_worker.get_run(session, workspace_id, UUID(run_id))
    assert run is not None
    assert run.status == "cancelled"


def test_pause_and_resume_run_endpoints_end_to_end_completes_normally(
    runs_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = runs_test_context
    workflow_id = f"test.http-pause-resume.{uuid4().hex}"
    _publish_workflow(workspace_id, user_id, workflow_id)
    create_response = client.post(
        "/api/v1/automations/runs",
        json={"workflow_id": workflow_id},
        headers=_headers(token, key="pause-resume-run-1"),
    )
    run_id = create_response.json()["id"]

    pause_response = client.post(
        f"/api/v1/automations/runs/{run_id}/pause", headers=_headers(token, key="pause-run-do-1")
    )
    assert pause_response.status_code == 200, pause_response.text
    assert pause_response.json()["status"] == "paused"
    assert pause_response.json()["finished_at"] is None

    resume_response = client.post(
        f"/api/v1/automations/runs/{run_id}/resume", headers=_headers(token, key="resume-run-do-1")
    )
    assert resume_response.status_code == 200, resume_response.text
    assert resume_response.json()["status"] == "queued"

    adapter = _EchoAdapter()
    registry = AdapterRegistry()
    registry.register(adapter)  # type: ignore[arg-type]
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
        assert claimed is not None
        finished = automation_worker.process_claimed_run(session, claimed, registry, "worker-a")
    assert finished.status == "succeeded"
    assert adapter.execute_calls == 1


def test_resume_run_endpoint_not_paused_is_409(
    runs_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = runs_test_context
    workflow_id = f"test.http-resume-not-paused.{uuid4().hex}"
    _publish_workflow(workspace_id, user_id, workflow_id)
    create_response = client.post(
        "/api/v1/automations/runs",
        json={"workflow_id": workflow_id},
        headers=_headers(token, key="resume-not-paused-1"),
    )
    run_id = create_response.json()["id"]

    response = client.post(
        f"/api/v1/automations/runs/{run_id}/resume",
        headers=_headers(token, key="resume-not-paused-do-1"),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RUN_NOT_PAUSED"


def test_cancel_pause_resume_endpoints_unknown_id_is_404(
    runs_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = runs_test_context
    for action, key in (("cancel", "k1"), ("pause", "k2"), ("resume", "k3")):
        response = client.post(
            f"/api/v1/automations/runs/{uuid4()}/{action}", headers=_headers(token, key=key)
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "RUN_NOT_FOUND"


def test_mutating_run_endpoints_require_csrf(
    runs_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = runs_test_context
    workflow_id = f"test.http-mutate-no-csrf.{uuid4().hex}"
    _publish_workflow(workspace_id, user_id, workflow_id)
    create_response = client.post(
        "/api/v1/automations/runs",
        json={"workflow_id": workflow_id},
        headers=_headers(token, key="no-csrf-guard"),
    )
    run_id = create_response.json()["id"]

    for action in ("cancel", "pause", "resume"):
        response = client.post(
            f"/api/v1/automations/runs/{run_id}/{action}",
            headers={"Idempotency-Key": f"no-csrf-{action}", "X-Correlation-ID": str(uuid4())},
        )
        assert response.status_code == 403


def test_mutating_run_endpoints_require_authentication() -> None:
    client = TestClient(app)
    for action in ("cancel", "pause", "resume"):
        response = client.post(
            f"/api/v1/automations/runs/{uuid4()}/{action}",
            headers={"Idempotency-Key": f"no-auth-{action}", "X-Correlation-ID": str(uuid4())},
        )
        assert response.status_code == 401


def test_pause_run_endpoint_idempotency_key_replays_cached_response(
    runs_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = runs_test_context
    workflow_id = f"test.http-pause-idempotent.{uuid4().hex}"
    _publish_workflow(workspace_id, user_id, workflow_id)
    create_response = client.post(
        "/api/v1/automations/runs",
        json={"workflow_id": workflow_id},
        headers=_headers(token, key="pause-idempotent-create"),
    )
    run_id = create_response.json()["id"]

    headers = _headers(token, key="pause-idempotent-1")
    first = client.post(f"/api/v1/automations/runs/{run_id}/pause", headers=headers)
    second = client.post(f"/api/v1/automations/runs/{run_id}/pause", headers=headers)
    assert first.status_code == 200
    assert second.status_code == 200
    ignored = {"request_id", "correlation_id"}
    first_body = {k: v for k, v in first.json().items() if k not in ignored}
    second_body = {k: v for k, v in second.json().items() if k not in ignored}
    assert first_body == second_body


# ---------------------------------------------------------------------------
# 4. Workspace isolation.
# ---------------------------------------------------------------------------


def test_runs_are_isolated_across_workspaces(
    runs_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_a, user_a, _token_a = runs_test_context
    workflow_id = f"test.http-isolation.{uuid4().hex}"
    _publish_workflow(workspace_a, user_a, workflow_id)
    create_response = client.post(
        "/api/v1/automations/runs",
        json={"workflow_id": workflow_id},
        headers=_headers(_token_a, key="isolation-create-1"),
    )
    run_id = create_response.json()["id"]

    workspace_b = uuid4()
    user_b = uuid4()
    token_b = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Runs Isolation Peer', 'Asia/Kolkata', :now)"
            ),
            {"id": workspace_b, "now": now},
        )
        create_identity(
            connection,
            workspace_id=workspace_b,
            user_id=user_b,
            email=f"{user_b}@example.test",
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
        list_response = other_client.get("/api/v1/automations/runs")
        assert list_response.status_code == 200
        assert list_response.json()["runs"] == []

        get_response = other_client.get(f"/api/v1/automations/runs/{run_id}")
        assert get_response.status_code == 404

        for action in ("cancel", "pause", "resume"):
            mutate_response = other_client.post(
                f"/api/v1/automations/runs/{run_id}/{action}",
                headers=_headers(token_b, key=f"cross-workspace-{action}"),
            )
            assert mutate_response.status_code == 404
            assert mutate_response.json()["error"]["code"] == "RUN_NOT_FOUND"

        # Still fully visible/actionable from its own workspace.
        own_get = client.get(f"/api/v1/automations/runs/{run_id}")
        assert own_get.status_code == 200
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
