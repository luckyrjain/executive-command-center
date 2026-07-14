from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
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
def search_audit_context() -> Iterator[tuple[TestClient, UUID, UUID, UUID]]:
    workspace_id = uuid4()
    other_workspace_id = uuid4()
    user_id = uuid4()
    other_user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        for current_workspace, name in (
            (workspace_id, "Search Audit Test"),
            (other_workspace_id, "Other Workspace"),
        ):
            connection.execute(
                text(
                    """
                    INSERT INTO workspaces (id, name, timezone, created_at)
                    VALUES (:id, :name, 'Asia/Kolkata', :created_at)
                    """
                ),
                {"id": current_workspace, "name": name, "created_at": now},
            )
        for current_user, current_workspace in (
            (user_id, workspace_id),
            (other_user_id, other_workspace_id),
        ):
            connection.execute(
                text(
                    """
                    INSERT INTO users (id, workspace_id, email, password_hash, created_at)
                    VALUES (:id, :workspace_id, :email, 'test-password-hash', :created_at)
                    """
                ),
                {
                    "id": current_user,
                    "workspace_id": current_workspace,
                    "email": f"{current_user}@example.test",
                    "created_at": now,
                },
            )
        connection.execute(
            text(
                """
                INSERT INTO sessions (
                    id, workspace_id, user_id, token_hash, expires_at, last_seen_at
                ) VALUES (
                    :id, :workspace_id, :user_id, :token_hash, :expires_at, :last_seen_at
                )
                """
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
        yield client, workspace_id, other_workspace_id, user_id
    finally:
        client.close()
        with engine.begin() as connection:
            for current_workspace in (workspace_id, other_workspace_id):
                for table in (
                    "event_outbox",
                    "audit_events",
                    "idempotency_records",
                    "meetings",
                    "calendar_events",
                    "notes",
                    "risks",
                    "commitments",
                    "tasks",
                    "sessions",
                    "users",
                ):
                    connection.execute(
                        text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),
                        {"workspace_id": current_workspace},
                    )
                connection.execute(
                    text("DELETE FROM workspaces WHERE id = :workspace_id"),
                    {"workspace_id": current_workspace},
                )


def test_search_ranking_sanitization_pagination_and_isolation(
    search_audit_context: tuple[TestClient, UUID, UUID, UUID],
) -> None:
    client, workspace_id, other_workspace_id, user_id = search_audit_context
    now = datetime.now(UTC)
    exact_id = uuid4()
    prefix_id = uuid4()
    note_id = uuid4()
    other_id = uuid4()

    with engine.begin() as connection:
        for entity_id, title, description, current_workspace, updated_at in (
            (exact_id, "Quarterly plan", "<script>alert(1)</script>", workspace_id, now),
            (
                prefix_id,
                "Quarterly planning follow-up",
                "Review operating plan assumptions",
                workspace_id,
                now - timedelta(days=1),
            ),
            (other_id, "Quarterly plan", "private", other_workspace_id, now),
        ):
            actor_id = user_id
            if current_workspace == other_workspace_id:
                actor_id = connection.execute(
                    text("SELECT id FROM users WHERE workspace_id = :workspace_id"),
                    {"workspace_id": other_workspace_id},
                ).scalar_one()
            connection.execute(
                text(
                    """
                    INSERT INTO tasks (
                        id, workspace_id, owner_id, title, description, status,
                        manual_priority, pinned, source_type, created_by, updated_by,
                        created_at, updated_at, version
                    ) VALUES (
                        :id, :workspace_id, :owner_id, :title, :description, 'planned',
                        'medium', false, 'local', :actor_id, :actor_id,
                        :created_at, :updated_at, 1
                    )
                    """
                ),
                {
                    "id": entity_id,
                    "workspace_id": current_workspace,
                    "owner_id": actor_id,
                    "title": title,
                    "description": description,
                    "actor_id": actor_id,
                    "created_at": updated_at,
                    "updated_at": updated_at,
                },
            )
        connection.execute(
            text(
                """
                INSERT INTO notes (
                    id, workspace_id, owner_id, title, body, note_type, source_type,
                    created_by, updated_by, created_at, updated_at, version
                ) VALUES (
                    :id, :workspace_id, :owner_id, 'Planning notes',
                    'Quarterly plan details and decisions', 'general', 'local',
                    :actor_id, :actor_id, :created_at, :updated_at, 1
                )
                """
            ),
            {
                "id": note_id,
                "workspace_id": workspace_id,
                "owner_id": user_id,
                "actor_id": user_id,
                "created_at": now,
                "updated_at": now,
            },
        )

    first = client.get("/api/v1/search", params={"q": "quarterly plan", "limit": 1})
    assert first.status_code == 200
    body = first.json()
    assert body["items"][0]["entity_id"] == str(exact_id)
    assert body["items"][0]["score"] == 1.0
    assert "&lt;script&gt;" in body["items"][0]["snippet"]
    assert "<script>" not in body["items"][0]["snippet"]
    assert body["next_cursor"] is not None

    second = client.get(
        "/api/v1/search",
        params={"q": "quarterly plan", "limit": 10, "cursor": body["next_cursor"]},
    )
    assert second.status_code == 200
    second_ids = {item["entity_id"] for item in second.json()["items"]}
    assert str(exact_id) not in second_ids
    assert str(other_id) not in second_ids
    assert str(prefix_id) in second_ids or str(note_id) in second_ids

    malformed = client.get("/api/v1/search", params={"q": "plan", "cursor": "invalid"})
    assert malformed.status_code == 400
    assert malformed.json()["error"]["code"] == "INVALID_CURSOR"


def test_audit_filters_pagination_redaction_and_isolation(
    search_audit_context: tuple[TestClient, UUID, UUID, UUID],
) -> None:
    client, workspace_id, other_workspace_id, user_id = search_audit_context
    now = datetime.now(UTC)
    aggregate_id = uuid4()
    other_aggregate_id = uuid4()

    with engine.begin() as connection:
        for offset, event_type in enumerate(("task.created", "task.updated", "note.updated")):
            connection.execute(
                text(
                    """
                    INSERT INTO audit_events (
                        id, workspace_id, event_type, aggregate_type, aggregate_id,
                        aggregate_version, actor_id, request_id, correlation_id,
                        before, after, changed_fields, authorization_result, source,
                        metadata, occurred_at
                    ) VALUES (
                        :id, :workspace_id, :event_type, :aggregate_type, :aggregate_id,
                        :version, :actor_id, :request_id, :correlation_id,
                        CAST(:before AS jsonb), CAST(:after AS jsonb), :changed_fields,
                        'allowed', 'user', CAST(:metadata AS jsonb), :occurred_at
                    )
                    """
                ),
                {
                    "id": uuid4(),
                    "workspace_id": workspace_id,
                    "event_type": event_type,
                    "aggregate_type": "note" if event_type.startswith("note") else "task",
                    "aggregate_id": aggregate_id,
                    "version": offset + 1,
                    "actor_id": user_id,
                    "request_id": uuid4(),
                    "correlation_id": uuid4(),
                    "before": '{}',
                    "after": '{"body_checksum":"abc123","body_length":42}',
                    "changed_fields": ["body_checksum"],
                    "metadata": '{}',
                    "occurred_at": now - timedelta(minutes=offset),
                },
            )
        other_user_id = connection.execute(
            text("SELECT id FROM users WHERE workspace_id = :workspace_id"),
            {"workspace_id": other_workspace_id},
        ).scalar_one()
        connection.execute(
            text(
                """
                INSERT INTO audit_events (
                    id, workspace_id, event_type, aggregate_type, aggregate_id,
                    aggregate_version, actor_id, request_id, correlation_id,
                    changed_fields, authorization_result, source, metadata, occurred_at
                ) VALUES (
                    :id, :workspace_id, 'task.created', 'task', :aggregate_id,
                    1, :actor_id, :request_id, :correlation_id,
                    '{}', 'allowed', 'user', '{}'::jsonb, :occurred_at
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": other_workspace_id,
                "aggregate_id": other_aggregate_id,
                "actor_id": other_user_id,
                "request_id": uuid4(),
                "correlation_id": uuid4(),
                "occurred_at": now,
            },
        )

    first = client.get("/api/v1/audit", params={"aggregate_id": str(aggregate_id), "limit": 1})
    assert first.status_code == 200
    body = first.json()
    assert len(body["items"]) == 1
    assert body["items"][0]["aggregate_id"] == str(aggregate_id)
    assert body["next_cursor"] is not None
    serialized = str(body["items"][0])
    assert "raw note body" not in serialized

    second = client.get(
        "/api/v1/audit",
        params={"aggregate_id": str(aggregate_id), "limit": 10, "cursor": body["next_cursor"]},
    )
    assert second.status_code == 200
    assert len(second.json()["items"]) == 2

    isolated = client.get("/api/v1/audit", params={"aggregate_id": str(other_aggregate_id)})
    assert isolated.status_code == 200
    assert isolated.json()["items"] == []

    malformed = client.get("/api/v1/audit", params={"cursor": "invalid"})
    assert malformed.status_code == 400
    assert malformed.json()["error"]["code"] == "INVALID_CURSOR"
