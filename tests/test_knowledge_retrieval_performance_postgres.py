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
from ecc.domains.knowledge import embeddings
from ecc.domains.knowledge.embeddings import EMBEDDING_DIMENSIONS, MODEL_ID, MODEL_VERSION
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

# PHASE-002-knowledge-platform.md's non-functional requirements: lexical
# retrieval p95 under 500 ms locally / 800 ms in CI, hybrid retrieval p95
# under 800 ms locally / 1280 ms in CI (same 1.6x local->CI multiplier this
# suite already uses for test_search_performance_postgres.py and
# test_risks_attention_postgres.py -- CI runners are shared/slower hardware,
# not a different budget).
_IN_CI = os.getenv("CI") is not None
RETRIEVAL_BUDGET_SECONDS = 0.8 if _IN_CI else 0.5
HYBRID_BUDGET_SECONDS = 1.28 if _IN_CI else 0.8
_DOCUMENT_COUNT = 10_000
SAMPLE_SIZE = 20


def _p95(samples: list[float]) -> float:
    """Nearest-rank 95th percentile: the smallest value at or above 95% of samples."""
    ordered = sorted(samples)
    index = min(len(ordered) - 1, -(-(95 * len(ordered)) // 100) - 1)
    return ordered[index]


class _FixedVectorProvider:
    """Deterministic, near-zero-cost stand-in for the real sentence-
    transformers model -- a hybrid-retrieval latency test measures the
    pgvector HNSW query path at representative scale, not the real model's
    own inference latency, which depends on hardware/model choice and is
    explicitly outside RETRIEVAL-CONTRACT.md's degradation-guarded scope
    (see retrieval.py's retrieve()). Same fake-provider convention as
    test_knowledge_embeddings_postgres.py's FakeEmbeddingProvider."""

    def __init__(self, vector: list[float]) -> None:
        self._vector = vector

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector for _ in texts]


def _unit_vector(index: int) -> list[float]:
    vector = [0.0] * EMBEDDING_DIMENSIONS
    vector[index] = 1.0
    return vector


_BACKGROUND_VECTOR = _unit_vector(0)


@pytest.fixture
def retrieval_performance_context() -> Iterator[tuple[TestClient, UUID]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        # Bulk-inserting 10,000 embedding_projections rows into an
        # HNSW-indexed column (below) costs more than the connection's 5s
        # application-request statement_timeout -- same relaxation as this
        # fixture's own teardown block, scoped to just this setup
        # transaction via SET LOCAL.
        connection.execute(text("SET LOCAL statement_timeout = '60s'"))
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
        # Give every retrieval_documents row its own random embedding,
        # generated SQL-side per row rather than one shared literal --
        # 10,000 rows all sharing one exact vector puts pgvector's HNSW
        # index into a degenerate all-tied-at-distance-zero search that is
        # dramatically slower than real, diverse production embeddings ever
        # are, which would make this fixture measure a pathological case
        # instead of the representative one the budget is about.
        connection.execute(
            text(
                """
                INSERT INTO embedding_projections (
                    id, workspace_id, document_id, model_id, model_version,
                    dimensions, embedding, content_hash, created_at, updated_at
                )
                SELECT
                    gen_random_uuid(), workspace_id, id, :model_id, :model_version,
                    :dimensions,
                    (SELECT ('[' || string_agg(random()::text, ',') || ']')::vector
                     FROM generate_series(1, :dimensions)),
                    'fixture', :now, :now
                FROM retrieval_documents
                WHERE workspace_id = :workspace_id
                """
            ),
            {
                "workspace_id": workspace_id,
                "model_id": MODEL_ID,
                "model_version": MODEL_VERSION,
                "dimensions": EMBEDDING_DIMENSIONS,
                "now": now,
            },
        )

    # VACUUM cannot run inside a transaction block, so this always opens its
    # own autocommit connection rather than reusing engine.begin() above.
    # Without it, the planner has no fresh statistics for the just-built
    # HNSW index and the just-bulk-inserted rows, and can pick a cold,
    # dramatically slower plan for the first live query -- exactly the kind
    # of one-off spike an untimed warm-up call is supposed to absorb, not
    # something that should still be capable of blowing the 5s
    # application-request statement_timeout on its own. Same reasoning as
    # test_risks_attention_postgres.py's identical _vacuum_analyze step.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text("VACUUM (ANALYZE) pkos_nodes, retrieval_documents"))
        connection.execute(text("VACUUM (ANALYZE) embedding_projections"))

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
            for table in (
                "embedding_projections",
                "retrieval_documents",
                "pkos_nodes",
                "sessions",
                "users",
            ):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                    {"workspace_id": workspace_id},
                )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": workspace_id},
            )


def test_retrieve_10000_document_lexical_p95_under_budget(
    retrieval_performance_context: tuple[TestClient, UUID],
) -> None:
    client, _workspace_id = retrieval_performance_context

    # One untimed correctness check, kept separate from the timed sample
    # loop below so a cold connection-pool checkout or an unprimed query
    # plan cache doesn't inflate the first timed sample.
    warmup = client.get(
        "/api/v1/knowledge/retrieve", params={"q": "Needle Quarterly Contact", "limit": 20}
    )
    assert warmup.status_code == 200
    assert warmup.json()["items"][0]["title"] == "Needle Quarterly Contact"

    samples: list[float] = []
    for _ in range(SAMPLE_SIZE):
        started = perf_counter()
        response = client.get(
            "/api/v1/knowledge/retrieve", params={"q": "Needle Quarterly Contact", "limit": 20}
        )
        samples.append(perf_counter() - started)
        assert response.status_code == 200

    p95 = _p95(samples)
    assert p95 < RETRIEVAL_BUDGET_SECONDS, (
        f"lexical retrieval p95 {p95 * 1000:.1f} ms exceeded "
        f"{RETRIEVAL_BUDGET_SECONDS * 1000:.0f} ms budget (in_ci={_IN_CI}); samples(ms)="
        f"{[round(s * 1000, 1) for s in samples]}"
    )


def test_retrieve_10000_document_hybrid_p95_under_budget(
    retrieval_performance_context: tuple[TestClient, UUID],
) -> None:
    client, _workspace_id = retrieval_performance_context
    embeddings.set_provider_for_testing(_FixedVectorProvider(_BACKGROUND_VECTOR))
    try:
        warmup = client.get(
            "/api/v1/knowledge/retrieve",
            params={"q": "Needle Quarterly Contact", "limit": 20, "mode": "hybrid"},
        )
        assert warmup.status_code == 200
        assert warmup.json()["degraded"] is False

        samples: list[float] = []
        for _ in range(SAMPLE_SIZE):
            started = perf_counter()
            response = client.get(
                "/api/v1/knowledge/retrieve",
                params={"q": "Needle Quarterly Contact", "limit": 20, "mode": "hybrid"},
            )
            samples.append(perf_counter() - started)
            assert response.status_code == 200
            assert response.json()["degraded"] is False

        p95 = _p95(samples)
        assert p95 < HYBRID_BUDGET_SECONDS, (
            f"hybrid retrieval p95 {p95 * 1000:.1f} ms exceeded "
            f"{HYBRID_BUDGET_SECONDS * 1000:.0f} ms budget (in_ci={_IN_CI}); samples(ms)="
            f"{[round(s * 1000, 1) for s in samples]}"
        )
    finally:
        embeddings.set_provider_for_testing(None)
