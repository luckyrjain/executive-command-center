from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
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
def attention_count_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Attention Count Test', 'Asia/Kolkata', :created_at)"
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
                "attention_items",
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


def _insert_attention_item(workspace_id: UUID, owner_id: UUID, now: datetime) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO attention_items (
                    id, workspace_id, entity_type, entity_id, source_entity_version,
                    score, confidence, factors, explanation, generated_at, expires_at,
                    pinned, policy_version, owner_id, visibility
                ) VALUES (
                    :id, :workspace_id, 'task', :entity_id, 1,
                    50, 0.9, '[]'::jsonb, 'test item', :now, :expires_at,
                    false, 1, :owner_id, 'private'
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "entity_id": uuid4(),
                "now": now,
                "expires_at": now + timedelta(days=1),
                "owner_id": owner_id,
            },
        )


def test_attention_count_matches_list_length(
    attention_count_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, _token = attention_count_context
    now = datetime.now(UTC)
    _insert_attention_item(workspace_id, user_id, now)
    _insert_attention_item(workspace_id, user_id, now)

    listed = client.get("/api/v1/attention")
    assert listed.status_code == 200
    assert len(listed.json()["items"]) == 2

    counted = client.get("/api/v1/attention/count")
    assert counted.status_code == 200
    assert counted.json()["count"] == 2


def test_attention_count_is_zero_for_an_empty_workspace(
    attention_count_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, _token = attention_count_context
    counted = client.get("/api/v1/attention/count")
    assert counted.status_code == 200
    assert counted.json()["count"] == 0
