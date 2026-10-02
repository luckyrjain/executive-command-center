"""Policy scope at creation (scope-enforcement design, Decision 3): new
policies must name a closed, non-empty scope and are written enforced;
legacy rows (migration 0086's default) stay unenforced; the DB CHECK backs
the rule for enforced rows only.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from automation_scope_support import (
    World,
    action_step,
    client_for,
    headers,
    make_legacy,
    make_world,
    publish,
)
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.automation import policy as automation_policy
from ecc.domains.automation import workflows as automation_workflows
from ecc.domains.automation.adapter_contract import DATA_CLASSES

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_MIGRATION = Path(__file__).parents[1] / "backend/migrations/versions/0086_policy_scope_enforced.py"


@pytest.fixture
def world() -> Iterator[World]:
    yield from make_world()


def _workflow(world: World) -> str:
    workflow_id = f"test.scope-create.{uuid4().hex}"
    with SessionFactory() as session, session.begin():
        automation_workflows.create_workflow_draft(
            session,
            world.workspace_id,
            world.user_id,
            workflow_id=workflow_id,
            graph={"steps": [action_step("s1", "local.create_note")]},
            trigger_refs=[],
            policy_ref=None,
        )
    return workflow_id


def _create(world: World, workflow_id: str, **scope: Any) -> Any:
    with SessionFactory() as session, session.begin():
        return automation_policy.create_policy(
            session,
            world.workspace_id,
            world.user_id,
            workflow_id=workflow_id,
            value_limit=Decimal("0"),
            count_limit=1,
            rate_limit=None,
            schedule=None,
            approval_mode="per_run",
            **scope,
        )


@pytest.mark.parametrize(
    ("scope", "code", "field"),
    [
        ({"action_types": [], "data_classes": ["internal"]}, "POLICY_SCOPE_EMPTY", "action_types"),
        (
            {"action_types": ["note.create"], "data_classes": []},
            "POLICY_SCOPE_EMPTY",
            "data_classes",
        ),
        (
            {"action_types": ["local.create_note"], "data_classes": ["internal"]},
            "POLICY_SCOPE_UNKNOWN_VALUE",
            "action_types",
        ),
        (
            {"action_types": ["note.create"], "data_classes": ["secret"]},
            "POLICY_SCOPE_UNKNOWN_VALUE",
            "data_classes",
        ),
    ],
)
def test_create_refuses_empty_or_unknown_scope(
    world: World, scope: dict[str, list[str]], code: str, field: str
) -> None:
    workflow_id = _workflow(world)
    result = _create(world, workflow_id, **scope)
    assert isinstance(result, automation_policy.PolicyScopeInvalid)
    assert (result.code, result.field) == (code, field)

    with client_for(world) as client:
        response = client.post(
            "/api/v1/automations/policies",
            json={
                "workflow_id": workflow_id,
                **scope,
                "value_limit": "0",
                "count_limit": 1,
                "approval_mode": "per_run",
            },
            headers=headers(world, key=f"scope-{uuid4()}"),
        )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == code
    assert error["details"]["field"] == field
    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM automation_policies WHERE workflow_id = :wf"),
            {"wf": workflow_id},
        ).scalar_one()
    assert count == 0


def test_omitted_scope_in_the_api_is_refused_as_empty(world: World) -> None:
    workflow_id = _workflow(world)
    with client_for(world) as client:
        response = client.post(
            "/api/v1/automations/policies",
            json={
                "workflow_id": workflow_id,
                "value_limit": "0",
                "count_limit": 1,
                "approval_mode": "per_run",
            },
            headers=headers(world, key=f"scope-{uuid4()}"),
        )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "POLICY_SCOPE_EMPTY"


def test_new_policy_is_enforced_and_legacy_row_is_not(world: World) -> None:
    workflow_id = _workflow(world)
    created = _create(world, workflow_id, action_types=["note.create"], data_classes=["sensitive"])
    assert isinstance(created, automation_policy.AutomationPolicy)
    assert created.scope_enforced is True

    with client_for(world) as client:
        listed = client.get("/api/v1/automations/policies", params={"workflow_id": workflow_id})
        assert listed.status_code == 200
        assert [p["scope_enforced"] for p in listed.json()["policies"]] == [True]
        make_legacy(created.id)
        listed = client.get("/api/v1/automations/policies", params={"workflow_id": workflow_id})
    assert [p["scope_enforced"] for p in listed.json()["policies"]] == [False]


def _insert_policy(world: World, workflow_id: str, **columns: Any) -> None:
    now = datetime.now(UTC)
    names = ", ".join(columns)
    values = ", ".join(f":{name}" for name in columns)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO automation_policies (id, workspace_id, workflow_id, value_limit, "
                "count_limit, rate_limit, approval_mode, expires_at, version, created_by, "
                f"updated_by, created_at, updated_at, {names}) VALUES (:id, :ws, :wf, 0, 1, "
                "'{}'::jsonb, 'per_run', :expires_at, 1, :uid, :uid, :now, :now, "
                f"{values})"
            ),
            {
                "id": uuid4(),
                "ws": world.workspace_id,
                "wf": workflow_id,
                "expires_at": now + timedelta(days=1),
                "uid": world.user_id,
                "now": now,
                **columns,
            },
        )


@pytest.mark.parametrize(
    ("action_types", "data_classes"),
    [([], ["internal"]), (["note.create"], []), (["note.create"], ["secret"])],
)
def test_check_constraint_refuses_bad_scope_on_enforced_rows_only(
    world: World, action_types: list[str], data_classes: list[str]
) -> None:
    workflow_id = _workflow(world)
    with pytest.raises(IntegrityError, match="ck_automation_policies_enforced_scope"):
        _insert_policy(
            world,
            workflow_id,
            action_types=action_types,
            data_classes=data_classes,
            scope_enforced=True,
        )
    _insert_policy(
        world,
        workflow_id,
        action_types=action_types,
        data_classes=data_classes,
        scope_enforced=False,
    )


def test_a_direct_insert_omitting_the_flag_is_legacy(world: World) -> None:
    """No default flip: only `create_policy` writes enforced rows, so an
    old app instance during a rolling deploy keeps writing legacy rows."""
    workflow_id = _workflow(world)
    _insert_policy(world, workflow_id, action_types=[], data_classes=[])
    with engine.connect() as connection:
        enforced = connection.execute(
            text("SELECT scope_enforced FROM automation_policies WHERE workflow_id = :wf"),
            {"wf": workflow_id},
        ).scalar_one()
    assert enforced is False


def test_check_constraint_literal_matches_data_classes() -> None:
    literal = re.search(r"ARRAY\[([^\]]+)\]::text\[\]", _MIGRATION.read_text())
    assert literal is not None
    assert tuple(v.strip().strip("'") for v in literal.group(1).split(",")) == DATA_CLASSES


def test_a_legacy_policy_dispatches_exactly_as_before(world: World) -> None:
    """Free-text, out-of-vocabulary scope on a legacy row is not checked."""
    from automation_scope_support import FakeAdapter, registry_of, run_once

    adapter = FakeAdapter("test.legacy", action_type="note.create", data_class="restricted")
    workflow_id, _policy = publish(
        world, {"steps": [action_step("s1", "test.legacy")]}, legacy=True
    )
    finished = run_once(world, workflow_id, registry_of(adapter))
    assert finished.status == "succeeded"
    assert adapter.execute_calls == 1
