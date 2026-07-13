from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
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
def task_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO workspaces (id, name, created_at)
                VALUES (:id, :name, :created_at)
                """
            ),
            {"id": workspace_id, "name": "Task Test", "created_at": now},
        )
        connection.execute(
            text(
                """
                INSERT INTO users (id, workspace_id, email, password_hash, created_at)
                VALUES (:id, :workspace_id, :email, :password_hash, :created_at)
                """
            ),
            {
                "id": user_id,
                "workspace_id": workspace_id,
                "email": f"{user_id}@example.test",
                "password_hash": "test-password-hash",
                "created_at": now,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO sessions (
                    id, workspace_id, user_id, token_hash,
                    expires_at, last_seen_at
                ) VALUES (
                    :id, :workspace_id, :user_id, :token_hash,
                    :expires_at, :last_seen_at
                )
                """
            ),
            {
                "id": session_id,
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
            connection.execute(
                text("DELETE FROM event_outbox WHERE workspace_id = :workspace_id"),
                {"workspace_id": workspace_id},
            )
            connection.execute(
                text("DELETE FROM audit_events WHERE workspace_id = :workspace_id"),
                {"workspace_id": workspace_id},
            )
            connection.execute(
                text("DELETE FROM idempotency_records WHERE workspace_id = :workspace_id"),
                {"workspace_id": workspace_id},
            )
            connection.execute(
                text("DELETE FROM tasks WHERE workspace_id = :workspace_id"),
                {"workspace_id": workspace_id},
            )
            connection.execute(
                text("DELETE FROM sessions WHERE workspace_id = :workspace_id"),
                {"workspace_id": workspace_id},
            )
            connection.execute(
                text("DELETE FROM users WHERE workspace_id = :workspace_id"),
                {"workspace_id": workspace_id},
            )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": workspace_id},
            )


def _headers(token: str, key: str) -> dict[str, str]:
    csrf = new(
        settings.session_secret.encode(),
        token.encode(),
        "sha256",
    ).hexdigest()
    return {
        "Idempotency-Key": key,
        "X-CSRF-Token": csrf,
        "X-Correlation-ID": str(uuid4()),
    }


def test_task_lifecycle_is_transactional_and_workspace_scoped(
    task_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = task_test_context

    create = client.post(
        "/api/v1/tasks",
        headers=_headers(token, "create-task"),
        json={"title": "Prepare operating review", "manual_priority": "high"},
    )
    assert create.status_code == 201
    created = create.json()
    task_id = created["id"]
    assert created["owner_id"] == str(user_id)
    assert created["version"] == 1

    replay = client.post(
        "/api/v1/tasks",
        headers=_headers(token, "create-task"),
        json={"title": "Prepare operating review", "manual_priority": "high"},
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == task_id

    update = client.patch(
        f"/api/v1/tasks/{task_id}",
        headers=_headers(token, "update-task"),
        json={"expected_version": 1, "pinned": True},
    )
    assert update.status_code == 200
    assert update.json()["version"] == 2
    assert update.json()["pinned"] is True

    conflict = client.patch(
        f"/api/v1/tasks/{task_id}",
        headers=_headers(token, "stale-update"),
        json={"expected_version": 1, "title": "Stale update"},
    )
    assert conflict.status_code == 409

    complete = client.post(
        f"/api/v1/tasks/{task_id}/complete",
        headers=_headers(token, "complete-task"),
        json={"expected_version": 2},
    )
    assert complete.status_code == 200
    assert complete.json()["status"] == "completed"

    archive = client.post(
        f"/api/v1/tasks/{task_id}/archive",
        headers=_headers(token, "archive-task"),
        json={"expected_version": 3},
    )
    assert archive.status_code == 200
    assert archive.json()["archived_at"] is not None

    restore = client.post(
        f"/api/v1/tasks/{task_id}/restore",
        headers=_headers(token, "restore-task"),
        json={"expected_version": 4},
    )
    assert restore.status_code == 200
    assert restore.json()["status"] == "completed"
    assert restore.json()["archived_at"] is None

    with engine.connect() as connection:
        audit_types = connection.execute(
            text(
                """
                SELECT event_type
                FROM audit_events
                WHERE workspace_id = :workspace_id
                  AND aggregate_id = :task_id
                ORDER BY occurred_at
                """
            ),
            {"workspace_id": workspace_id, "task_id": task_id},
        ).scalars().all()
        outbox_types = connection.execute(
            text(
                """
                SELECT event_type
                FROM event_outbox
                WHERE workspace_id = :workspace_id
                ORDER BY occurred_at
                """
            ),
            {"workspace_id": workspace_id},
        ).scalars().all()

    assert "task.created" in audit_types
    assert "task.updated" in audit_types
    assert "task.completed" in audit_types
    assert "task.archived" in audit_types
    assert "task.restored" in audit_types
    assert "task.mutation_rejected" in audit_types
    assert "task.created.v1" in outbox_types
    assert "task.updated.v1" in outbox_types
    assert "task.completed.v1" in outbox_types
    assert "task.archived.v1" in outbox_types
    assert "task.restored.v1" in outbox_types
