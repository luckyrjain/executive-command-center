import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from time import perf_counter
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from phase1_dataset import seed_phase1_dataset
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import engine
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

# The design doc's budget is genuinely split: search p95 below 500 ms locally,
# 800 ms in CI. GitHub Actions sets the `CI` environment variable to "true"
# for every job (https://docs.github.com/actions/learn-github-actions/variables)
# -- this is the standard, platform-provided signal, not a repo convention we
# had to invent; no local/CI detection mechanism already existed anywhere in
# this repo's tests or workflows (checked before adding this).
_IN_CI = os.getenv("CI") is not None
SEARCH_BUDGET_SECONDS = 0.8 if _IN_CI else 0.5


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
        # Full representative fixture (10,000 tasks/commitments/risks/calendar
        # events, 50,000 notes, 100,000 audit rows) -- search's candidate CTE
        # unions every searchable table regardless of the requested
        # entity_types filter (see backend/ecc/search.py), so a tasks-only
        # bulk insert understates real representative-scale search cost.
        seed_phase1_dataset(connection, workspace_id=workspace_id, owner_id=user_id)
        # One distinguishing, easy-to-match row layered on top of the bulk
        # fixture so the test can assert a specific, deterministic result.
        connection.execute(
            text(
                """
                INSERT INTO tasks (
                    id, workspace_id, owner_id, title, description, status,
                    manual_priority, pinned, source_type, created_by, updated_by,
                    created_at, updated_at, version
                ) VALUES (
                    gen_random_uuid(), :workspace_id, :user_id,
                    'Needle quarterly plan', 'Representative searchable task body',
                    'planned', 'medium', false, 'local', :user_id, :user_id,
                    :created_at, :updated_at, 1
                )
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
            for table in (
                "audit_events",
                "calendar_events",
                "risks",
                "commitments",
                "notes",
                "tasks",
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


def test_search_10000_entity_local_and_ci_budget(
    search_performance_context: tuple[TestClient, UUID],
) -> None:
    client, _ = search_performance_context

    started = perf_counter()
    response = client.get(
        "/api/v1/search",
        params={"q": "needle quarterly", "types[]": "task", "limit": 20},
    )
    elapsed_ms = (perf_counter() - started) * 1000

    assert response.status_code == 200
    assert response.json()["items"][0]["title"] == "Needle quarterly plan"
    assert elapsed_ms < SEARCH_BUDGET_SECONDS * 1000, (
        f"search took {elapsed_ms:.1f} ms "
        f"(budget {SEARCH_BUDGET_SECONDS * 1000:.0f} ms, in_ci={_IN_CI})"
    )
