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
def recommendation_count_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Recommendation Count Test', 'Asia/Kolkata', :created_at)"
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
                "recommendations", "recommendation_feedback", "tasks", "commitments",
                "risks", "sessions", "users", "workspaces",
            ):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE id = :workspace_id")
                    if table == "workspaces"
                    else text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),
                    {"workspace_id": workspace_id},
                )


def _insert_recommendation(workspace_id: UUID, user_id: UUID, status: str, now: datetime) -> None:
    # Matches backend/migrations/versions/0009_phase1_recommendations.py's
    # real NOT-NULL columns exactly (confirmed against that migration, not
    # guessed): recommendation_type, target_type, proposed_action (jsonb),
    # rationale, confidence, source, created_by, updated_by are all
    # required with no server default; version/pinned/evidence_ids all
    # have server defaults and are omitted here.
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO recommendations (
                    id, workspace_id, recommendation_type, target_type, proposed_action,
                    rationale, confidence, status, source, created_by, updated_by,
                    created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, 'test_type', 'task', '{}'::jsonb,
                    'test rationale', 0.9, :status, 'rule', :user_id, :user_id,
                    :now, :now
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "status": status,
                "user_id": user_id,
                "now": now,
            },
        )


def test_recommendations_count_only_counts_pending_statuses(
    recommendation_count_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, _token = recommendation_count_context
    now = datetime.now(UTC)
    _insert_recommendation(workspace_id, user_id, "proposed", now)
    _insert_recommendation(workspace_id, user_id, "pending_confirmation", now)
    _insert_recommendation(workspace_id, user_id, "accepted", now)

    counted = client.get("/api/v1/recommendations/count")
    assert counted.status_code == 200
    assert counted.json() == {"count": 2}
