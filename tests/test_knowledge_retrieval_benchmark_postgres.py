"""RFC-005's "Retrieval benchmark" activation requirement for pgvector
(see ADR-0011): measures hybrid retrieval's semantic recall against the
versioned labelled dataset in fixtures/phase2_retrieval_embedding_dataset.py,
using the real sentence-transformers model (not a fake provider -- unlike
test_knowledge_embeddings_postgres.py's ranking-formula tests, the point
here is to measure genuine semantic quality). Skips gracefully rather than
failing the build if the real model can't be loaded in this environment
(e.g. a sandboxed dev environment with no Hugging Face Hub access); that's
an infra concern, not a code-correctness one -- CI has full network access
and is this benchmark's real gate.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from uuid import UUID, uuid4

import pytest
from fixtures.phase2_retrieval_embedding_dataset import DATASET_VERSION, build_dataset
from fastapi.testclient import TestClient
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import engine
from ecc.domains.knowledge import embeddings
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

# RETRIEVAL-CONTRACT.md's Evaluation section names precision@5/recall@10 as
# release gates without prescribing exact numbers for a first activation --
# this floor is deliberately not 1.0: MiniLM is a small, fast, CPU-friendly
# model (the local-first tradeoff ADR-0011 accepts), not a large one, and
# this dataset's queries are deliberately worded to share no literal words
# with their target document, which is the hardest case for any embedding
# model. A future ranking-formula or model change compares against this
# recorded floor, per the contract's "ranking changes require before/after
# benchmark results" rule.
MIN_HIT_AT_5_RATE = 0.7


def _headers(token: str, key: str) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    return {
        "Idempotency-Key": key,
        "X-CSRF-Token": csrf,
        "X-Correlation-ID": str(uuid4()),
    }


@pytest.fixture
def benchmark_context() -> Iterator[tuple[TestClient, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO workspaces (id, name, created_at) VALUES (:id, :name, :created_at)"),
            {"id": workspace_id, "name": "Retrieval Benchmark", "created_at": now},
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

    client = TestClient(app)
    client.cookies.set("ecc_session", token)

    try:
        yield client, workspace_id, token
    finally:
        client.close()
        with engine.begin() as connection:
            for table in (
                "event_outbox",
                "audit_events",
                "idempotency_records",
                "embedding_projections",
                "retrieval_documents",
                "knowledge_claims",
                "entity_aliases",
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


def test_dataset_version_is_pinned() -> None:
    assert DATASET_VERSION == "1.0.0"


def test_hybrid_retrieval_semantic_recall_meets_benchmark_floor(
    benchmark_context: tuple[TestClient, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _workspace_id, token = benchmark_context
    documents, queries = build_dataset()
    assert len(documents) >= 5
    assert len(queries) >= 5

    monkeypatch.setenv("ECC_EMBEDDINGS_ENABLED", "true")
    get_settings.cache_clear()
    embeddings.set_provider_for_testing(None)
    try:
        try:
            embeddings.get_provider()
        except embeddings.EmbeddingUnavailable as exc:
            pytest.skip(f"real embedding model unavailable in this environment: {exc}")

        document_ids: list[UUID] = []
        for index, document in enumerate(documents):
            response = client.post(
                "/api/v1/knowledge/entities",
                headers=_headers(token, f"benchmark-doc-{index}"),
                json={
                    "kind": document.kind,
                    "canonical_name": document.canonical_name,
                    "summary": document.summary,
                },
            )
            assert response.status_code == 201, response.text
            document_ids.append(UUID(response.json()["id"]))

        hits = 0
        for query_index, labelled_query in enumerate(queries):
            response = client.get(
                "/api/v1/knowledge/retrieve",
                headers=_headers(token, f"benchmark-query-{query_index}"),
                params={"q": labelled_query.query, "mode": "hybrid", "limit": 5},
            )
            assert response.status_code == 200, response.text
            body = response.json()
            reason = body.get("degraded_reason")
            assert body["degraded"] is False, (
                f"query {labelled_query.query!r} unexpectedly degraded: {reason}"
            )
            top_5_ids = {item["entity_id"] for item in body["items"][:5]}
            expected_id = str(document_ids[labelled_query.relevant_document_index])
            if expected_id in top_5_ids:
                hits += 1

        hit_at_5_rate = hits / len(queries)
        assert hit_at_5_rate >= MIN_HIT_AT_5_RATE, (
            f"hit@5 rate {hit_at_5_rate} below floor {MIN_HIT_AT_5_RATE} "
            f"({hits}/{len(queries)} queries found their labelled document in the top 5)"
        )
    finally:
        get_settings.cache_clear()
        embeddings.set_provider_for_testing(None)
