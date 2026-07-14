from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from time import perf_counter
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
def search_performance_context() -> Iterator[tuple[TestClient, UUID]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO workspaces (id, name, timezone, created_at)
                VALUES (:id, 'Search Performance', 'Asia/Kolkata', :created_at)
                """
            ),
            {"id": workspace_id, "created_at": now},
        )
        connection.execute(
            text(
                """
                INSERT INTO users (id, workspace_id, email, password_hash, created_at)
                VALUES (:id, :workspace_id, :email, 'test-password-hash', :created_at)
                """
            ),
            {
                "id": user_id,
                "workspace_id": workspace_id,
                "email": f"{user_id}@example.test",
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
        connection.execute(
            text(
                """
                INSERT INTO tasks (
                    id, workspace_id, owner_id, title, description, status,
                    manual_priority, pinned, source_type, created_by, updated_by,
                    created_at, updated_at, version
                )
                SELECT gen_random_uuid(), :workspace_id, :user_id,
                       CASE WHEN series = 9999 THEN 'Needle quarterly plan'
                            ELSE 'Task ' || series::text END,
                       'Representative searchable task body ' || series::text,
                       'planned', 'medium', false, 'local', :user_id, :user_id,
                       :created_at, :updated_at, 1
                FROM generate_series(1, 10000) AS series
                """
            ),
            {
                "workspace_id": workspace_id,
                "user_id": user_id,
                "created_at": now,
                "updated_at": now,
            },
        )

    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    try:
        yield client, workspace_id
    finally:
        client.close()
        with engine.begin() as connection:
            for table in ("tasks", "sessions", "users"):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),
                    {"workspace_id": workspace_id},
                )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": workspace_id},
            )


def test_search_10000_entity_ci_budget(
    search_performance_context: tuple[TestClient, UUID],
) -> None:
    client, _ = search_performance_context

    started = perf_counter()
    response = client.get(
        "/api/v1/search",
        params={"q": "needle quarterly", "entity_type[]": "task", "limit": 20},
    )
    elapsed_ms = (perf_counter() - started) * 1000

    assert response.status_code == 200
    assert response.json()["items"][0]["title"] == "Needle quarterly plan"
    assert elapsed_ms < 800, f"search took {elapsed_ms:.1f} ms"
