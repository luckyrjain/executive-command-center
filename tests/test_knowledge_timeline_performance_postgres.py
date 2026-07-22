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

# PHASE-002-knowledge-platform.md's non-functional requirements: a
# 10,000-entry timeline p95 under 500 ms locally / 800 ms in CI (same 1.6x
# local->CI multiplier this suite already uses elsewhere -- CI runners are
# shared/slower hardware, not a different budget).
_IN_CI = os.getenv("CI") is not None
TIMELINE_BUDGET_SECONDS = 0.8 if _IN_CI else 0.5
_ENTRY_COUNT = 10_000
SAMPLE_SIZE = 20


def _p95(samples: list[float]) -> float:
    """Nearest-rank 95th percentile: the smallest value at or above 95% of samples."""
    ordered = sorted(samples)
    index = min(len(ordered) - 1, -(-(95 * len(ordered)) // 100) - 1)
    return ordered[index]


@pytest.fixture
def timeline_performance_context() -> Iterator[tuple[TestClient, UUID, UUID]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)
    entity_id = uuid4()

    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO workspaces (id, name, created_at) VALUES (:id, :name, :created_at)"),
            {"id": workspace_id, "name": "Timeline Performance", "created_at": now},
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
        connection.execute(
            text(
                """
                INSERT INTO pkos_nodes (
                    id, workspace_id, node_type, canonical_name, attributes,
                    status, confidence, version, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, 'person', 'Timeline Subject',
                    '{}'::jsonb, 'active', 1.00, 1, :now, :now
                )
                """
            ),
            {"id": entity_id, "workspace_id": workspace_id, "now": now},
        )
        # 10,000 timeline_entries for the one entity -- "a 10,000-entry
        # timeline" in the contract means one entity's history at that
        # depth, not 10,000 distinct entities. effective_at is staggered by
        # series index (descending as series grows) so the entries have a
        # real, non-degenerate ORDER BY effective_at DESC, recorded_at DESC,
        # id DESC sort to perform, matching how a real long-lived entity's
        # timeline accumulates over time rather than all sharing one instant.
        connection.execute(
            text(
                """
                INSERT INTO timeline_entries (
                    id, workspace_id, entity_id, effective_at, recorded_at,
                    event_type, source_id, summary
                )
                SELECT
                    gen_random_uuid(), :workspace_id, :entity_id,
                    :now - (series * interval '1 minute'),
                    :now - (series * interval '1 minute'),
                    'knowledge_entity.claim_recorded', NULL,
                    'Representative synthetic timeline entry ' || series::text
                FROM generate_series(1, :count) AS series
                """
            ),
            {
                "workspace_id": workspace_id,
                "entity_id": entity_id,
                "now": now,
                "count": _ENTRY_COUNT,
            },
        )

    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text("VACUUM (ANALYZE) timeline_entries"))

    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    try:
        yield client, workspace_id, entity_id
    finally:
        client.close()
        with engine.begin() as connection:
            # See test_knowledge_retrieval_performance_postgres.py's
            # teardown for why this workspace's 10,000-row DELETE needs a
            # relaxed statement_timeout beyond the 5s application-request
            # budget.
            connection.execute(text("SET LOCAL statement_timeout = '60s'"))
            for table in ("timeline_entries", "pkos_nodes", "sessions", "users"):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                    {"workspace_id": workspace_id},
                )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": workspace_id},
            )


def test_timeline_10000_entry_p95_under_budget(
    timeline_performance_context: tuple[TestClient, UUID, UUID],
) -> None:
    client, _workspace_id, entity_id = timeline_performance_context

    warmup = client.get(f"/api/v1/knowledge/entities/{entity_id}/timeline", params={"limit": 100})
    assert warmup.status_code == 200
    assert len(warmup.json()["items"]) == 100

    samples: list[float] = []
    for _ in range(SAMPLE_SIZE):
        started = perf_counter()
        response = client.get(
            f"/api/v1/knowledge/entities/{entity_id}/timeline", params={"limit": 100}
        )
        samples.append(perf_counter() - started)
        assert response.status_code == 200

    p95 = _p95(samples)
    assert p95 < TIMELINE_BUDGET_SECONDS, (
        f"timeline p95 {p95 * 1000:.1f} ms exceeded "
        f"{TIMELINE_BUDGET_SECONDS * 1000:.0f} ms budget (in_ci={_IN_CI}); samples(ms)="
        f"{[round(s * 1000, 1) for s in samples]}"
    )
