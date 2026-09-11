from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from identity_fixtures import create_identity
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import engine
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


@pytest.fixture
def tasks_count_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Tasks Count Test', 'Asia/Kolkata', :created_at)"
            ),
            {"id": workspace_id, "created_at": now},
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
                "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :last_seen_at)"
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "user_id": user_id,
                "token_hash": sha256(token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "last_seen_at": now,
            },
        )

    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    try:
        yield client, workspace_id, user_id, token
    finally:
        client.close()
        with engine.begin() as connection:
            for table in (
                "tasks",
                "event_outbox",
                "audit_events",
                "idempotency_records",
                "sessions",
                "users",
            ):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),
                    {"workspace_id": workspace_id},
                )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": workspace_id},
            )


def _headers(token: str, key: str) -> dict[str, str]:
    # Mirrors tests/test_knowledge_resolution_postgres.py's own `_headers` --
    # every mutating endpoint in this app requires both an Idempotency-Key
    # and an HMAC CSRF token derived from the session token.
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    return {"Idempotency-Key": key, "X-CSRF-Token": csrf}


def _create_task(client: TestClient, token: str, title: str) -> str:
    response = client.post(
        "/api/v1/tasks",
        json={"title": title},
        headers=_headers(token, f"count-test-{uuid4()}"),
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def test_tasks_count_excludes_completed_and_archived(
    tasks_count_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = tasks_count_context
    _create_task(client, token, "open task one")
    _create_task(client, token, "open task two")
    completed_id = _create_task(client, token, "will be completed")
    archived_id = _create_task(client, token, "will be archived")

    # Complete one task
    complete_response = client.post(
        f"/api/v1/tasks/{completed_id}/complete",
        json={"expected_version": 1},
        headers=_headers(token, f"count-test-{uuid4()}"),
    )
    assert complete_response.status_code == 200, complete_response.text

    # Archive another task
    archive_response = client.post(
        f"/api/v1/tasks/{archived_id}/archive",
        json={"expected_version": 1},
        headers=_headers(token, f"count-test-{uuid4()}"),
    )
    assert archive_response.status_code == 200, archive_response.text

    # Count should only include the 2 open tasks
    counted = client.get("/api/v1/tasks/count")
    assert counted.status_code == 200
    assert counted.json()["count"] == 2
