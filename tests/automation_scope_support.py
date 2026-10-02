"""Shared scaffolding for the policy scope enforcement tests
(`docs/superpowers/specs/2026-10-01-automation-policy-scope-enforcement-
design.md`): a workspace world, a configurable fake adapter, and helpers to
publish a workflow under a chosen policy scope and drive a run.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from hmac import new
from typing import Any
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from identity_fixtures import create_identity
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.automation import policy as automation_policy
from ecc.domains.automation import worker as automation_worker
from ecc.domains.automation import workflows as automation_workflows
from ecc.domains.automation.adapter_contract import TransientAdapterError
from ecc.domains.automation.adapters import AdapterRegistry
from ecc.main import app

settings = get_settings()

_CLEANUP_TABLES = (
    "approval_requests",
    "compensation_steps",
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
    "workspace_memberships",
    "users",
)


@dataclass(frozen=True)
class World:
    workspace_id: UUID
    user_id: UUID
    token: str


def make_world() -> Iterator[World]:
    """Fixture body: one workspace, one owner with a live session."""
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Scope Enforcement', 'UTC', :now)"
            ),
            {"id": workspace_id, "now": now},
        )
        create_identity(connection, workspace_id=workspace_id, user_id=user_id, now=now)
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
    try:
        yield World(workspace_id, user_id, token)
    finally:
        with engine.begin() as connection:
            account_ids = list(
                connection.execute(
                    text("SELECT account_id FROM users WHERE workspace_id = :ws"),
                    {"ws": workspace_id},
                ).scalars()
            )
            for table in _CLEANUP_TABLES:
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :ws"),  # noqa: S608
                    {"ws": workspace_id},
                )
            connection.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": workspace_id})
            connection.execute(
                text("DELETE FROM accounts WHERE id = ANY(:ids)"), {"ids": account_ids}
            )


def client_for(world: World) -> TestClient:
    client = TestClient(app)
    client.cookies.set("ecc_session", world.token)
    return client


def headers(world: World, key: str | None = None) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), world.token.encode(), "sha256").hexdigest()
    result = {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}
    if key is not None:
        result["Idempotency-Key"] = key
    return result


class FakeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: str = ""
    amount: Decimal = Decimal("0")


class FakeOutput(BaseModel):
    value: str


class FakeAdapter:
    """A configurable fake: scope declarations, high-impact categories,
    and behaviour (`mode`: "succeed", "fail", "transient_once")."""

    input_schema: type[BaseModel] = FakeInput
    output_schema: type[BaseModel] = FakeOutput
    reversible = True

    def __init__(
        self,
        adapter_id: str,
        *,
        action_type: str = "fake.external",
        data_class: str = "internal",
        categories: frozenset[str] = frozenset(),
        mode: str = "succeed",
    ) -> None:
        self.adapter_id = adapter_id
        self.action_type = action_type
        self.data_class = data_class
        self.high_impact_categories = categories
        self.mode = mode
        self.execute_calls = 0

    def simulate(self, action_input: BaseModel) -> BaseModel:
        return FakeOutput(value="preview")

    def execute(self, action_input: BaseModel) -> BaseModel:
        self.execute_calls += 1
        if self.mode == "fail":
            raise RuntimeError("fake failure")
        if self.mode == "transient_once" and self.execute_calls == 1:
            raise TransientAdapterError("fake transient")
        return FakeOutput(value="done")


class FinancialFakeAdapter(FakeAdapter):
    """`financial`, with a `dispatch_value` read from the input's `amount`."""

    def __init__(self, adapter_id: str) -> None:
        super().__init__(adapter_id, categories=frozenset({"financial"}))

    def dispatch_value(self, action_input: BaseModel) -> Decimal:
        assert isinstance(action_input, FakeInput)
        return action_input.amount


class CompensatingFakeAdapter(FakeAdapter):
    def __init__(self, adapter_id: str, **kwargs: Any) -> None:
        super().__init__(adapter_id, **kwargs)
        self.compensate_calls = 0

    def compensate(self, action_input: BaseModel) -> BaseModel:
        self.compensate_calls += 1
        return FakeOutput(value="undone")


def registry_of(*adapters: Any) -> AdapterRegistry:
    registry = AdapterRegistry()
    for adapter in adapters:
        registry.register(adapter)
    return registry


def action_step(
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


def compensation_step(step_id: str, action_ref: str) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "step_type": "compensation",
        "action_ref": action_ref,
        "input_mapping": {},
    }


def create_policy(
    world: World,
    workflow_id: str,
    *,
    action_types: list[str],
    data_classes: list[str],
    approval_mode: automation_policy.ApprovalMode = "bounded_recurring",
    value_limit: Decimal = Decimal("1000"),
    count_limit: int = 1000,
) -> automation_policy.AutomationPolicy:
    with SessionFactory() as session, session.begin():
        policy = automation_policy.create_policy(
            session,
            world.workspace_id,
            world.user_id,
            workflow_id=workflow_id,
            action_types=action_types,
            data_classes=data_classes,
            value_limit=value_limit,
            count_limit=count_limit,
            rate_limit=None,
            schedule=None,
            approval_mode=approval_mode,
        )
    assert isinstance(policy, automation_policy.AutomationPolicy), policy
    return policy


def make_legacy(policy_id: UUID) -> None:
    """Turn a policy into a pre-0086 legacy row with free-text scope."""
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE automation_policies SET scope_enforced = false, "
                "action_types = ARRAY['bounded'], data_classes = '{}' WHERE id = :id"
            ),
            {"id": policy_id},
        )


def publish(
    world: World,
    graph: dict[str, Any],
    *,
    action_types: list[str] | None = None,
    data_classes: list[str] | None = None,
    approval_mode: automation_policy.ApprovalMode = "bounded_recurring",
    value_limit: Decimal = Decimal("1000"),
    count_limit: int = 1000,
    legacy: bool = False,
) -> tuple[str, automation_policy.AutomationPolicy]:
    """Draft, attach a policy with the given scope, activate (no registry,
    so no publish-time checks -- tests of dispatch need a graph publish
    would refuse)."""
    workflow_id = f"test.scope.{uuid4().hex}"
    with SessionFactory() as session, session.begin():
        automation_workflows.create_workflow_draft(
            session,
            world.workspace_id,
            world.user_id,
            workflow_id=workflow_id,
            graph=graph,
            trigger_refs=[],
            policy_ref=None,
        )
    policy = create_policy(
        world,
        workflow_id,
        action_types=action_types if action_types is not None else ["fake.external"],
        data_classes=data_classes if data_classes is not None else ["internal"],
        approval_mode=approval_mode,
        value_limit=value_limit,
        count_limit=count_limit,
    )
    if legacy:
        make_legacy(policy.id)
    with SessionFactory() as session, session.begin():
        draft = automation_workflows.create_workflow_draft(
            session,
            world.workspace_id,
            world.user_id,
            workflow_id=workflow_id,
            graph=graph,
            trigger_refs=[],
            policy_ref=policy.id,
        )
        activated = automation_workflows.activate_workflow_version(
            session, world.workspace_id, draft.id
        )
    assert isinstance(activated, automation_workflows.WorkflowVersion), activated
    return workflow_id, policy


def run_once(
    world: World, workflow_id: str, registry: AdapterRegistry
) -> automation_worker.WorkflowRun:
    """Enqueue, claim and process one run until it stops."""
    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, world.workspace_id, world.user_id, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun), queued
    return resume(world, queued.id, registry)


def resume(world: World, run_id: UUID, registry: AdapterRegistry) -> automation_worker.WorkflowRun:
    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-scope")
        assert claimed is not None and claimed.id == run_id
        return automation_worker.process_claimed_run(session, claimed, registry, "worker-scope")


def step_rows(world: World, run_id: UUID) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                text(
                    "SELECT step_index, status, error_class, dispatch_value "
                    "FROM workflow_run_steps WHERE workspace_id = :ws AND run_id = :run "
                    "ORDER BY step_index"
                ),
                {"ws": world.workspace_id, "run": run_id},
            ).mappings()
        ]


def approval_rows(world: World, run_id: UUID) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                text(
                    "SELECT step_index, high_impact_categories, status FROM approval_requests "
                    "WHERE workspace_id = :ws AND run_id = :run ORDER BY step_index"
                ),
                {"ws": world.workspace_id, "run": run_id},
            ).mappings()
        ]


def step_blocked_events(world: World, run_id: UUID) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        return [
            dict(row)
            for row in connection.execute(
                text(
                    "SELECT actor_id, aggregate_id FROM audit_events "
                    "WHERE workspace_id = :ws AND event_type = 'automation.step_blocked' "
                    "AND aggregate_id = :run"
                ),
                {"ws": world.workspace_id, "run": run_id},
            ).mappings()
        ]


def step_blocked_payloads(world: World, run_id: UUID) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        return [
            row[0]
            for row in connection.execute(
                text(
                    "SELECT payload FROM event_outbox WHERE workspace_id = :ws "
                    "AND event_type = 'automation.step_blocked.v1' AND payload->>'run_id' = :run"
                ),
                {"ws": world.workspace_id, "run": str(run_id)},
            )
        ]
