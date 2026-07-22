import os
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

# TEST-PLAN.md's non-functional requirement: lexical retrieval p95 under
# 500 ms at acceptance dataset size, following the same local/CI budget
# split as test_search_performance_postgres.py -- GitHub Actions sets `CI`
# for every job, the standard platform-provided signal already used
# elsewhere in this test suite.
_IN_CI = os.getenv("CI") is not None
RETRIEVAL_BUDGET_SECONDS = 0.8 if _IN_CI else 0.5
_DOCUMENT_COUNT = 10_000


@pytest.fixture
def retrieval_performance_context() -> Iterator[tuple[TestClient, UUID]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO workspaces (id, name, created_at) VALUES (:id, :name, :created_at)"),
            {"id": workspace_id, "name": "Retrieval Performance", "created_at": now},
        )
        connection.execute(
            text(
                "INSERT INTO users (id, workspace_id, email, password_hash, created_at) "
                "VALUES (:id, :workspace_id, :email, :password_hash, :created_at)"
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
                "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                "expires_at, last_seen_at) VALUES (:id, :workspace_id, :user_id, "
                ":token_hash, :expires_at, :last_seen_at)"
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
        # Bulk-seed representative-scale pkos_nodes + retrieval_documents
        # directly (not via 10,000 individual entity-creation HTTP calls,
        # which would make the fixture itself the bottleneck) -- mirrors
        # phase1_dataset.py's generate_series bulk-insert convention.
        connection.execute(
            text(
                """
                INSERT INTO pkos_nodes (
                    id, workspace_id, node_type, canonical_name, attributes,
                    status, confidence, version, created_at, updated_at
                )
                SELECT
                    gen_random_uuid(), :workspace_id, 'person',
                    'Representative synthetic person ' || series::text,
                    '{}'::jsonb, 'active', 1.00, 1, :now, :now
                FROM generate_series(1, :count) AS series
                """
            ),
            {"workspace_id": workspace_id, "now": now, "count": _DOCUMENT_COUNT},
        )
        connection.execute(
            text(
                """
                INSERT INTO retrieval_documents (
                    id, workspace_id, entity_type, entity_id, title, body,
                    source_version, updated_at
                )
                SELECT
                    gen_random_uuid(), workspace_id, node_type, id, canonical_name,
                    'Representative synthetic biography and role summary text',
                    version, updated_at
                FROM pkos_nodes
                WHERE workspace_id = :workspace_id
                """
            ),
            {"workspace_id": workspace_id},
        )
        # One distinguishing, easy-to-match row layered on top of the bulk
        # fixture so the test can assert a specific, deterministic result.
        needle_id = uuid4()
        connection.execute(
            text(
                """
                INSERT INTO pkos_nodes (
                    id, workspace_id, node_type, canonical_name, attributes,
                    status, confidence, version, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, 'person', 'Needle Quarterly Contact',
                    '{}'::jsonb, 'active', 1.00, 1, :now, :now
                )
                """
            ),
            {"id": needle_id, "workspace_id": workspace_id, "now": now},
        )
        connection.execute(
            text(
                """
                INSERT INTO retrieval_documents (
                    id, workspace_id, entity_type, entity_id, title, body,
                    source_version, updated_at
                ) VALUES (
                    gen_random_uuid(), :workspace_id, 'person', :entity_id,
                    'Needle Quarterly Contact', 'Representative searchable body', 1, :now
                )
                """
            ),
            {"workspace_id": workspace_id, "entity_id": needle_id, "now": now},
        )

    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    try:
        yield client, workspace_id
    finally:
        client.close()
        with engine.begin() as connection:
            # pkos_nodes is now referenced by more Phase 2 tables (pkos_edges,
            # resolution_candidates, claims, ...) than when this fixture's
            # teardown budget was last checked, and this workspace's 10,000
            # pkos_nodes rows make the DELETE's FK-integrity re-checks slow
            # enough on CI-runner hardware to exceed the connection's 5s
            # statement_timeout (STATEMENT_TIMEOUT_MS in ecc/database.py) --
            # an approved *application-request* SLA that was never meant to
            # bound test cleanup. SET LOCAL scopes the relaxed budget to only
            # this teardown transaction; every other connection through this
            # engine (including the test's own request above) still enforces
            # the real 5s budget.
            connection.execute(text("SET LOCAL statement_timeout = '60s'"))
            for table in ("retrieval_documents", "pkos_nodes", "sessions", "users"):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                    {"workspace_id": workspace_id},
                )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": workspace_id},
            )


def test_retrieve_10000_document_local_and_ci_budget(
    retrieval_performance_context: tuple[TestClient, UUID],
) -> None:
    client, _workspace_id = retrieval_performance_context

    started = perf_counter()
    response = client.get(
        "/api/v1/knowledge/retrieve", params={"q": "Needle Quarterly Contact", "limit": 20}
    )
    elapsed_ms = (perf_counter() - started) * 1000

    assert response.status_code == 200
    assert response.json()["items"][0]["title"] == "Needle Quarterly Contact"
    assert elapsed_ms < RETRIEVAL_BUDGET_SECONDS * 1000, (
        f"retrieval took {elapsed_ms:.1f} ms "
        f"(budget {RETRIEVAL_BUDGET_SECONDS * 1000:.0f} ms, in_ci={_IN_CI})"
    )
