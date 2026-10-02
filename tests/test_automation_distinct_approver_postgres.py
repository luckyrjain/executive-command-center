"""`workspaces.require_distinct_approver` (migration `0085_distinct_approver`):
an opt-in, owner-controlled separation-of-duties setting. When on, the member
who started a run (`workflow_runs.created_by`) may not approve that run's
approval request -- a second member must. Rejecting stays allowed. When off
(the default), today's self-approval keeps working, so single-member
workspaces are unaffected.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from hmac import new
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from identity_fixtures import create_identity
from lock_race_support import holder_backend_pid, wait_for_lock_waiter
from pydantic import BaseModel
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.automation import approvals as automation_approvals
from ecc.domains.automation import policy as automation_policy
from ecc.domains.automation import worker as automation_worker
from ecc.domains.automation import workflows as automation_workflows
from ecc.domains.automation.adapters import AdapterRegistry
from ecc.main import app
from ecc.platform.connector_security import membership_mutation_lock_key

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_ADAPTER_ID = "test.distinct-approver-high-impact"


@dataclass(frozen=True)
class _Member:
    user_id: UUID
    token: str


@dataclass(frozen=True)
class _World:
    workspace_id: UUID
    owner: _Member
    admin: _Member
    member: _Member


def _add_member(connection: Any, workspace_id: UUID, role: str, now: datetime) -> _Member:
    user_id = uuid4()
    token = f"session-{uuid4()}"
    create_identity(connection, workspace_id=workspace_id, user_id=user_id, now=now, role=role)
    connection.execute(
        text(
            "INSERT INTO sessions (id, workspace_id, user_id, token_hash, expires_at, "
            "last_seen_at) VALUES (:id, :ws, :uid, :hash, :expires_at, :now)"
        ),
        {
            "id": uuid4(),
            "ws": workspace_id,
            "uid": user_id,
            "hash": sha256(token.encode()).hexdigest(),
            "expires_at": now + timedelta(hours=1),
            "now": now,
        },
    )
    return _Member(user_id, token)


@pytest.fixture
def world() -> Iterator[_World]:
    workspace_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Distinct Approver', 'UTC', :now)"
            ),
            {"id": workspace_id, "now": now},
        )
        owner = _add_member(connection, workspace_id, "owner", now)
        admin = _add_member(connection, workspace_id, "admin", now)
        member = _add_member(connection, workspace_id, "member", now)
    try:
        yield _World(workspace_id, owner, admin, member)
    finally:
        with engine.begin() as connection:
            account_ids = list(
                connection.execute(
                    text("SELECT account_id FROM users WHERE workspace_id = :ws"),
                    {"ws": workspace_id},
                ).scalars()
            )
            for table in (
                "approval_requests",
                "workflow_run_steps",
                "workflow_runs",
                "automation_policies",
                "workflow_versions",
                "workflow_definitions",
                "event_outbox",
                "audit_events",
                "idempotency_records",
                "sessions",
                "workspace_memberships",
                "users",
            ):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :ws"),  # noqa: S608
                    {"ws": workspace_id},
                )
            connection.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": workspace_id})
            connection.execute(
                text("DELETE FROM accounts WHERE id = ANY(:ids)"), {"ids": account_ids}
            )


def _client(member: _Member) -> TestClient:
    client = TestClient(app)
    client.cookies.set("ecc_session", member.token)
    return client


def _headers(member: _Member, key: str | None = None) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), member.token.encode(), "sha256").hexdigest()
    headers = {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _set_distinct_approver(world: _World, value: bool) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE workspaces SET require_distinct_approver = :v WHERE id = :ws"),
            {"v": value, "ws": world.workspace_id},
        )


class _Input(BaseModel):
    value: str = ""


class _Output(BaseModel):
    value: str


class _HighImpactAdapter:
    adapter_id = _ADAPTER_ID
    input_schema: type[BaseModel] = _Input
    output_schema: type[BaseModel] = _Output
    reversible = True
    high_impact_categories: frozenset[str] = frozenset({"public"})

    def __init__(self) -> None:
        self.execute_calls = 0

    def simulate(self, action_input: BaseModel) -> BaseModel:  # noqa: D102
        return _Output(value="preview")

    def execute(self, action_input: BaseModel) -> BaseModel:  # noqa: D102
        self.execute_calls += 1
        return _Output(value="done")


def _pause_run(
    world: _World, starter: UUID
) -> tuple[automation_approvals.ApprovalRequest, _HighImpactAdapter, AdapterRegistry]:
    """Publish a one-step high-impact workflow, let `starter` start it, and
    run it to its `waiting_approval` pause."""
    workflow_id = f"test.distinct-approver.{uuid4().hex}"
    step = {
        "step_id": "s1",
        "step_type": "action",
        "action_ref": _ADAPTER_ID,
        "input_mapping": {},
        "on_success": "succeeded",
        "on_failure": "failed",
    }
    with SessionFactory() as session, session.begin():
        automation_workflows.create_workflow_draft(
            session,
            world.workspace_id,
            starter,
            workflow_id=workflow_id,
            graph={"steps": [step]},
            trigger_refs=[],
            policy_ref=None,
        )
    with SessionFactory() as session, session.begin():
        policy_row = automation_policy.create_policy(
            session,
            world.workspace_id,
            starter,
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
            world.workspace_id,
            starter,
            workflow_id=workflow_id,
            graph={"steps": [step]},
            trigger_refs=[],
            policy_ref=policy_row.id,
        )
        activated = automation_workflows.activate_workflow_version(
            session, world.workspace_id, draft.id
        )
    assert isinstance(activated, automation_workflows.WorkflowVersion)

    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, world.workspace_id, starter, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun)
    adapter = _HighImpactAdapter()
    registry = AdapterRegistry()
    registry.register(adapter)
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
        assert claimed is not None and claimed.id == queued.id
        paused = automation_worker.process_claimed_run(session, claimed, registry, "worker-a")
    assert paused.status == "waiting_approval"
    with SessionFactory() as session, session.begin():
        pending = automation_approvals.get_pending_approval(
            session, world.workspace_id, queued.id, 0
        )
    assert pending is not None
    return pending, adapter, registry


def _approve(world: _World, approver: _Member, pending: Any) -> Any:
    with _client(approver) as client:
        return client.post(
            f"/api/v1/automations/approvals/{pending.id}/approve",
            json={"action_digest": pending.action_digest},
            headers=_headers(approver, key=f"approve-{uuid4()}"),
        )


def _approval_status(world: _World, approval_id: UUID) -> str:
    with SessionFactory() as session, session.begin():
        approval = automation_approvals.get_approval(session, world.workspace_id, approval_id)
    assert approval is not None
    return approval.status


# --- the approval check ------------------------------------------------------


def test_self_approval_still_allowed_when_setting_is_off(world: _World) -> None:
    pending, _adapter, _registry = _pause_run(world, world.member.user_id)
    response = _approve(world, world.member, pending)
    assert response.status_code == 200, response.text
    assert response.json()["decided_by"] == str(world.member.user_id)


def test_self_approval_is_forbidden_when_setting_is_on(world: _World) -> None:
    _set_distinct_approver(world, True)
    pending, adapter, registry = _pause_run(world, world.member.user_id)

    response = _approve(world, world.member, pending)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "SELF_APPROVAL_FORBIDDEN"
    assert _approval_status(world, pending.id) == "pending"

    # The run stays paused: nothing for a worker to claim, nothing executed.
    with SessionFactory() as session:
        assert automation_worker.claim_next_run(session, "worker-b") is None
    assert adapter.execute_calls == 0


def test_owner_is_not_exempt_from_the_setting(world: _World) -> None:
    _set_distinct_approver(world, True)
    pending, _adapter, _registry = _pause_run(world, world.owner.user_id)
    response = _approve(world, world.owner, pending)
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "SELF_APPROVAL_FORBIDDEN"


def test_starter_may_still_reject_their_own_run_when_setting_is_on(world: _World) -> None:
    _set_distinct_approver(world, True)
    pending, adapter, _registry = _pause_run(world, world.member.user_id)
    with _client(world.member) as client:
        response = client.post(
            f"/api/v1/automations/approvals/{pending.id}/reject",
            headers=_headers(world.member, key=f"reject-{uuid4()}"),
        )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "rejected"
    assert adapter.execute_calls == 0


def test_a_different_member_can_approve_and_the_run_proceeds(world: _World) -> None:
    _set_distinct_approver(world, True)
    pending, adapter, registry = _pause_run(world, world.member.user_id)

    response = _approve(world, world.owner, pending)
    assert response.status_code == 200, response.text
    assert response.json()["decided_by"] == str(world.owner.user_id)

    with SessionFactory() as session:
        reclaimed = automation_worker.claim_next_run(session, "worker-b")
        assert reclaimed is not None
        finished = automation_worker.process_claimed_run(session, reclaimed, registry, "worker-b")
    assert finished.status == "succeeded"
    assert adapter.execute_calls == 1


def test_decide_approval_returns_self_approval_forbidden(world: _World) -> None:
    _set_distinct_approver(world, True)
    pending, _adapter, _registry = _pause_run(world, world.member.user_id)
    with SessionFactory() as session, session.begin():
        result = automation_approvals.decide_approval(
            session,
            world.workspace_id,
            world.member.user_id,
            pending.id,
            "approved",
            current_action_digest=pending.action_digest,
        )
    assert result == automation_approvals.ApprovalSelfApprovalForbidden(
        run_created_by=world.member.user_id
    )


def test_setting_enabled_during_the_decision_is_applied(world: _World) -> None:
    """The decision reads the setting `FOR SHARE`: an owner enabling it
    while the approval waits on the workspace row is seen once it commits."""
    pending, adapter, _registry = _pause_run(world, world.member.user_id)
    result: dict[str, Any] = {}

    def approve() -> None:
        result["response"] = _approve(world, world.member, pending)

    with engine.connect() as holder:
        transaction = holder.begin()
        holder_pid = holder_backend_pid(holder)
        holder.execute(
            text("UPDATE workspaces SET require_distinct_approver = true WHERE id = :ws"),
            {"ws": world.workspace_id},
        )
        worker = threading.Thread(target=approve)
        worker.start()
        wait_for_lock_waiter("workspaces", holder_pid=holder_pid)
        transaction.commit()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert result["response"].status_code == 403
    assert result["response"].json()["error"]["code"] == "SELF_APPROVAL_FORBIDDEN"
    assert _approval_status(world, pending.id) == "pending"
    assert adapter.execute_calls == 0


# --- the workspace setting ---------------------------------------------------


def test_workspace_response_reports_the_setting_defaulting_to_false(world: _World) -> None:
    with _client(world.member) as client:
        response = client.get(f"/api/v1/identity/workspaces/{world.workspace_id}")
    assert response.status_code == 200, response.text
    assert response.json()["require_distinct_approver"] is False


def test_owner_can_turn_the_setting_on_and_off(world: _World) -> None:
    with _client(world.owner) as client:
        for value in (True, False):
            response = client.patch(
                f"/api/v1/identity/workspaces/{world.workspace_id}",
                json={"require_distinct_approver": value},
                headers=_headers(world.owner),
            )
            assert response.status_code == 200, response.text
            assert response.json()["require_distinct_approver"] is value


@pytest.mark.parametrize("role", ["admin", "member"])
def test_only_an_owner_can_change_the_setting(world: _World, role: str) -> None:
    _set_distinct_approver(world, True)
    caller = world.admin if role == "admin" else world.member
    with _client(caller) as client:
        response = client.patch(
            f"/api/v1/identity/workspaces/{world.workspace_id}",
            json={"require_distinct_approver": False},
            headers=_headers(caller),
        )
    assert response.status_code == 403
    with engine.connect() as connection:
        value = connection.execute(
            text("SELECT require_distinct_approver FROM workspaces WHERE id = :ws"),
            {"ws": world.workspace_id},
        ).scalar_one()
    assert value is True


def test_admin_can_still_rename_the_workspace(world: _World) -> None:
    with _client(world.admin) as client:
        response = client.patch(
            f"/api/v1/identity/workspaces/{world.workspace_id}",
            json={"name": "Renamed by admin"},
            headers=_headers(world.admin),
        )
    assert response.status_code == 200, response.text
    assert response.json()["name"] == "Renamed by admin"


def test_turning_the_setting_on_needs_a_second_possible_approver(world: _World) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE workspace_memberships SET role = 'viewer' "
                "WHERE workspace_id = :ws AND users_id IN (:a, :m)"
            ),
            {"ws": world.workspace_id, "a": world.admin.user_id, "m": world.member.user_id},
        )
    with _client(world.owner) as client:
        response = client.patch(
            f"/api/v1/identity/workspaces/{world.workspace_id}",
            json={"require_distinct_approver": True},
            headers=_headers(world.owner),
        )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "DISTINCT_APPROVER_REQUIRES_SECOND_MEMBER"


def _wait_for_advisory_waiter(holder_pid: int) -> None:
    deadline = time.monotonic() + 10
    while True:
        with engine.connect() as probe:
            waiting = probe.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                    "AND wait_event = 'advisory' "
                    "AND pg_blocking_pids(pid) @> ARRAY[CAST(:holder AS integer)]"
                ),
                {"holder": holder_pid},
            ).scalar_one()
        if waiting:
            return
        if time.monotonic() > deadline:
            raise AssertionError("the PATCH never waited on the membership lock")
        time.sleep(0.05)


def test_owner_demoted_while_patch_waits_cannot_change_the_setting(world: _World) -> None:
    """The PATCH takes the membership lock first: a demotion holding the
    exclusive side commits before the PATCH reads the caller's role."""
    _set_distinct_approver(world, True)
    result: dict[str, Any] = {}

    def patch() -> None:
        with _client(world.owner) as client:
            result["response"] = client.patch(
                f"/api/v1/identity/workspaces/{world.workspace_id}",
                json={"require_distinct_approver": False},
                headers=_headers(world.owner),
            )

    with engine.connect() as holder:
        transaction = holder.begin()
        holder_pid = holder_backend_pid(holder)
        holder.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": membership_mutation_lock_key(world.workspace_id)},
        )
        holder.execute(
            text(
                "UPDATE workspace_memberships SET role = 'admin' "
                "WHERE workspace_id = :ws AND users_id = :uid"
            ),
            {"ws": world.workspace_id, "uid": world.owner.user_id},
        )
        worker = threading.Thread(target=patch)
        worker.start()
        _wait_for_advisory_waiter(holder_pid)
        transaction.commit()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert result["response"].status_code == 403
    with engine.connect() as connection:
        value = connection.execute(
            text("SELECT require_distinct_approver FROM workspaces WHERE id = :ws"),
            {"ws": world.workspace_id},
        ).scalar_one()
    assert value is True
