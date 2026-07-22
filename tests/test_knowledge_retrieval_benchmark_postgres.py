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
from fastapi.testclient import TestClient
from fixtures.phase2_retrieval_embedding_dataset import DATASET_VERSION, build_dataset
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
# these floors are deliberately not 1.0: MiniLM is a small, fast, CPU-friendly
# model (the local-first tradeoff ADR-0011 accepts), not a large one, and
# this dataset's queries are deliberately worded to share no literal words
# with their target document, which is the hardest case for any embedding
# model. hit@5/MRR/false-positive-rate@5 are not literal contract text but
# are standard IR complements that make a floor-only pass/fail result
# actionable: MRR shows how *high* a found document ranks (not just whether
# it cleared the cutoff), and false-positive-rate@5 shows how much
# irrelevant noise fills the slots a real result didn't. A future
# ranking-formula or model change compares its printed numbers against
# these recorded floors and this run's own printed output, per the
# contract's "ranking changes require before/after benchmark results" rule
# -- there is deliberately no separate baseline-snapshot file for a
# benchmark this small; the floors below *are* the recorded baseline, and
# raising one is how a real improvement gets locked in.
MIN_HIT_AT_5_RATE = 0.7
# precision@5 and recall@10's floors are derived, not independently chosen:
# with 7 queries, hit@5 >= 0.7 requires at least 5 queries to hit (4/7 =
# 0.571 fails), which puts a mathematical floor under precision@5
# (>= 5/35 = 0.1429) and recall@10 (>= 5/7 = 0.714, since top-10 is a
# superset of top-5). Both floors below sit just under that derived
# minimum, so they can never fail on a run that already passes the
# established hit@5 floor -- they tighten the gate without becoming a new,
# separately-uncalibrated one.
MIN_PRECISION_AT_5 = 0.14
MIN_RECALL_AT_10 = 0.7
# false-positive-rate@5's ceiling is derived the same way: the worst case
# consistent with hit@5 >= 0.7 (exactly 5 of 7 queries hitting) is
# (35 - 5) / 35 = 0.8571, so 0.86 can never fail a run that already passes
# the hit@5 floor.
MAX_FALSE_POSITIVE_RATE_AT_5 = 0.86
# MRR has no floor: unlike the three metrics above, no value derived from
# the hit@5 floor alone is tight enough to be meaningful (the worst case
# consistent with hit@5 >= 0.7 is MRR >= 0.143, barely above zero) and this
# benchmark has never been run against the real model to record an actual
# achieved value to gate on -- see this module's docstring on why a
# baseline-snapshot file isn't used here. Printed every run instead, so a
# future change to the ranking formula has real before/after numbers to
# compare, and a real floor can be set once a genuine baseline exists.
_TOP_K = 10


def _retrieval_metrics(
    ranked_results: list[list[str]], expected_ids: list[str]
) -> dict[str, float]:
    """Pure metric computation over already-fetched, already-ranked top-_TOP_K
    id lists -- kept separate from the HTTP/DB/model-dependent fetch so it is
    unit-testable on synthetic data (see test_retrieval_metrics_computation
    in test_resolution_scoring.py-style isolation) without needing the real
    embedding model. Every query in this dataset has exactly one relevant
    document (LabelledQuery.relevant_document_index is singular), so
    precision@5/recall@10 reduce to hit-rate arithmetic rather than needing
    a multi-relevant-document accumulator.
    """
    if not ranked_results:
        return {
            "hit_at_5": 0.0,
            "precision_at_5": 0.0,
            "recall_at_10": 0.0,
            "mrr": 0.0,
            "false_positive_rate_at_5": 0.0,
        }
    hits_at_5 = 0
    hits_at_10 = 0
    reciprocal_ranks: list[float] = []
    top5_slots_filled = 0
    top5_correct_slots = 0
    for results, expected_id in zip(ranked_results, expected_ids, strict=True):
        top5 = results[:5]
        top5_slots_filled += len(top5)
        if expected_id in top5:
            hits_at_5 += 1
            top5_correct_slots += 1
        if expected_id in results[:10]:
            hits_at_10 += 1
        if expected_id in results:
            reciprocal_ranks.append(1.0 / (results.index(expected_id) + 1))
        else:
            reciprocal_ranks.append(0.0)
    count = len(ranked_results)
    return {
        "hit_at_5": hits_at_5 / count,
        "precision_at_5": hits_at_5 / (count * 5),
        "recall_at_10": hits_at_10 / count,
        "mrr": sum(reciprocal_ranks) / count,
        "false_positive_rate_at_5": (
            (top5_slots_filled - top5_correct_slots) / top5_slots_filled
            if top5_slots_filled
            else 0.0
        ),
    }


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

        ranked_results: list[list[str]] = []
        expected_ids: list[str] = []
        for query_index, labelled_query in enumerate(queries):
            response = client.get(
                "/api/v1/knowledge/retrieve",
                headers=_headers(token, f"benchmark-query-{query_index}"),
                params={"q": labelled_query.query, "mode": "hybrid", "limit": _TOP_K},
            )
            assert response.status_code == 200, response.text
            body = response.json()
            reason = body.get("degraded_reason")
            assert body["degraded"] is False, (
                f"query {labelled_query.query!r} unexpectedly degraded: {reason}"
            )
            ranked_results.append([item["entity_id"] for item in body["items"]])
            expected_ids.append(str(document_ids[labelled_query.relevant_document_index]))

        metrics = _retrieval_metrics(ranked_results, expected_ids)
        print(
            f"\n[retrieval benchmark] hit@5={metrics['hit_at_5']:.3f} "
            f"precision@5={metrics['precision_at_5']:.3f} "
            f"recall@10={metrics['recall_at_10']:.3f} mrr={metrics['mrr']:.3f} "
            f"false_positive_rate@5={metrics['false_positive_rate_at_5']:.3f} "
            f"over {len(queries)} queries"
        )
        assert metrics["hit_at_5"] >= MIN_HIT_AT_5_RATE, (
            f"hit@5={metrics['hit_at_5']} below floor {MIN_HIT_AT_5_RATE}"
        )
        assert metrics["precision_at_5"] >= MIN_PRECISION_AT_5, (
            f"precision@5={metrics['precision_at_5']} below floor {MIN_PRECISION_AT_5}"
        )
        assert metrics["recall_at_10"] >= MIN_RECALL_AT_10, (
            f"recall@10={metrics['recall_at_10']} below floor {MIN_RECALL_AT_10}"
        )
        assert metrics["false_positive_rate_at_5"] <= MAX_FALSE_POSITIVE_RATE_AT_5, (
            f"false_positive_rate@5={metrics['false_positive_rate_at_5']} above ceiling "
            f"{MAX_FALSE_POSITIVE_RATE_AT_5}"
        )
    finally:
        get_settings.cache_clear()
        embeddings.set_provider_for_testing(None)


def test_retrieval_metrics_computation_on_synthetic_ranked_lists() -> None:
    """Unit-tests _retrieval_metrics's pure arithmetic against hand-computed
    values on synthetic data, independent of the real embedding model (which
    this environment may not have network access to load) -- so the metric
    formulas themselves are verified even when
    test_hybrid_retrieval_semantic_recall_meets_benchmark_floor above skips."""
    ranked_results = [
        ["a", "x", "x", "x", "x", "x", "x", "x", "x", "x"],  # found at rank 1
        ["x", "x", "a", "x", "x", "x", "x", "x", "x", "x"],  # found at rank 3
        ["x", "x", "x", "x", "x", "x", "a", "x", "x", "x"],  # found at rank 7 (outside top 5)
        ["x", "x", "x", "x", "x", "x", "x", "x", "x", "x"],  # never found
    ]
    expected_ids = ["a", "a", "a", "a"]

    metrics = _retrieval_metrics(ranked_results, expected_ids)

    assert metrics["hit_at_5"] == 2 / 4
    assert metrics["precision_at_5"] == 2 / 20
    assert metrics["recall_at_10"] == 3 / 4
    assert metrics["mrr"] == pytest.approx((1.0 + 1 / 3 + 1 / 7 + 0.0) / 4)
    assert metrics["false_positive_rate_at_5"] == pytest.approx(18 / 20)


def test_retrieval_metrics_computation_on_empty_input() -> None:
    metrics = _retrieval_metrics([], [])
    assert metrics == {
        "hit_at_5": 0.0,
        "precision_at_5": 0.0,
        "recall_at_10": 0.0,
        "mrr": 0.0,
        "false_positive_rate_at_5": 0.0,
    }
