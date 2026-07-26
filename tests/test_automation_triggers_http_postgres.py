"""Phase 5 Automation Task 7: `GET /api/v1/automations/triggers`
(`ecc.domains.automation.triggers`'s own new router -- see that module's
docstring for the full "why this endpoint exists now" reasoning: a small,
disclosed, additive read surface closing a real "schedule controls" UI
gap, mirroring `kill_switches.py`'s Task 7a `GET .../kill_switch`
precedent exactly).

Covers, per this task's own required minimum: the endpoint returns real
schedule-relevant fields (`schedule_expression`/`timezone`/`skip_missed`/
`last_fired_at`), not just bare `trigger_refs` strings; the `workflow_id`
filter; workspace isolation; and that this is a pure, unauthenticated-
mutation-free `GET` (no `CsrfDep`/`Idempotency-Key` required).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.automation import triggers as automation_triggers
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


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
    return token


def _cleanup_workspace(workspace_id: UUID) -> None:
    with engine.begin() as connection:
        for table in (
            "triggers",
            "automation_policies",
            "workflow_versions",
            "workflow_definitions",
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


def _insert_workflow_family(workspace_id: UUID, user_id: UUID, workflow_id: str) -> None:
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workflow_definitions (id, workspace_id, workflow_id, created_by, "
                "created_at, updated_at) VALUES (:id, :workspace_id, :workflow_id, :created_by, "
                ":now, :now)"
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "workflow_id": workflow_id,
                "created_by": user_id,
                "now": now,
            },
        )


@pytest.fixture
def trigger_http_context() -> Iterator[tuple[TestClient, UUID, UUID]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = _seed_workspace(workspace_id, user_id, "Automation Trigger HTTP Test")

    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    try:
        yield client, workspace_id, user_id
    finally:
        client.close()
        _cleanup_workspace(workspace_id)


def test_list_triggers_returns_schedule_fields_not_just_a_reference_string(
    trigger_http_context: tuple[TestClient, UUID, UUID],
) -> None:
    client, workspace_id, user_id = trigger_http_context
    workflow_id = f"test.schedule-http.{uuid4().hex}"
    _insert_workflow_family(workspace_id, user_id, workflow_id)

    with SessionFactory() as session, session.begin():
        automation_triggers.create_trigger(
            session,
            workspace_id,
            user_id,
            workflow_id=workflow_id,
            trigger_type="schedule",
            schedule_expression="0 9 * * MON-FRI",
            timezone="America/New_York",
            skip_missed=True,
        )

    response = client.get("/api/v1/automations/triggers", params={"workflow_id": workflow_id})
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["triggers"]) == 1
    trigger = payload["triggers"][0]
    assert trigger["workflow_id"] == workflow_id
    assert trigger["trigger_type"] == "schedule"
    assert trigger["schedule_expression"] == "0 9 * * MON-FRI"
    assert trigger["timezone"] == "America/New_York"
    assert trigger["skip_missed"] is True
    assert trigger["last_fired_at"] is None


def test_list_triggers_filters_by_workflow_id_over_http(
    trigger_http_context: tuple[TestClient, UUID, UUID],
) -> None:
    client, workspace_id, user_id = trigger_http_context
    workflow_a = f"test.http-list-a.{uuid4().hex}"
    workflow_b = f"test.http-list-b.{uuid4().hex}"
    _insert_workflow_family(workspace_id, user_id, workflow_a)
    _insert_workflow_family(workspace_id, user_id, workflow_b)

    with SessionFactory() as session, session.begin():
        automation_triggers.create_trigger(
            session, workspace_id, user_id, workflow_id=workflow_a, trigger_type="manual"
        )
        automation_triggers.create_trigger(
            session, workspace_id, user_id, workflow_id=workflow_b, trigger_type="manual"
        )

    response = client.get("/api/v1/automations/triggers", params={"workflow_id": workflow_a})
    assert response.status_code == 200
    payload = response.json()
    assert [t["workflow_id"] for t in payload["triggers"]] == [workflow_a]


def test_list_triggers_with_no_workflow_id_filter_returns_every_workspace_trigger(
    trigger_http_context: tuple[TestClient, UUID, UUID],
) -> None:
    client, workspace_id, user_id = trigger_http_context
    workflow_a = f"test.http-all-a.{uuid4().hex}"
    workflow_b = f"test.http-all-b.{uuid4().hex}"
    _insert_workflow_family(workspace_id, user_id, workflow_a)
    _insert_workflow_family(workspace_id, user_id, workflow_b)

    with SessionFactory() as session, session.begin():
        automation_triggers.create_trigger(
            session, workspace_id, user_id, workflow_id=workflow_a, trigger_type="manual"
        )
        automation_triggers.create_trigger(
            session, workspace_id, user_id, workflow_id=workflow_b, trigger_type="manual"
        )

    response = client.get("/api/v1/automations/triggers")
    assert response.status_code == 200
    assert {t["workflow_id"] for t in response.json()["triggers"]} == {workflow_a, workflow_b}


def test_list_triggers_empty_workflow_is_not_an_error(
    trigger_http_context: tuple[TestClient, UUID, UUID],
) -> None:
    client, workspace_id, user_id = trigger_http_context
    workflow_id = f"test.http-empty.{uuid4().hex}"
    _insert_workflow_family(workspace_id, user_id, workflow_id)

    response = client.get("/api/v1/automations/triggers", params={"workflow_id": workflow_id})
    assert response.status_code == 200
    assert response.json()["triggers"] == []


def test_list_triggers_is_workspace_isolated_over_http(
    trigger_http_context: tuple[TestClient, UUID, UUID],
) -> None:
    client, workspace_id, user_id = trigger_http_context
    workflow_id = f"test.http-isolated.{uuid4().hex}"
    _insert_workflow_family(workspace_id, user_id, workflow_id)
    with SessionFactory() as session, session.begin():
        automation_triggers.create_trigger(
            session, workspace_id, user_id, workflow_id=workflow_id, trigger_type="manual"
        )

    other_workspace_id = uuid4()
    other_user_id = uuid4()
    other_token = _seed_workspace(other_workspace_id, other_user_id, "Other Trigger HTTP Workspace")
    other_client = TestClient(app)
    other_client.cookies.set("ecc_session", other_token)
    try:
        response = other_client.get(
            "/api/v1/automations/triggers", params={"workflow_id": workflow_id}
        )
        assert response.status_code == 200
        assert response.json()["triggers"] == []
    finally:
        other_client.close()
        _cleanup_workspace(other_workspace_id)


def test_list_triggers_requires_authentication(
    trigger_http_context: tuple[TestClient, UUID, UUID],
) -> None:
    _client, _workspace_id, _user_id = trigger_http_context
    anonymous_client = TestClient(app)
    try:
        response = anonymous_client.get("/api/v1/automations/triggers")
        assert response.status_code == 401
    finally:
        anonymous_client.close()
