import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.knowledge import embeddings
from ecc.domains.knowledge.embeddings import (
    EMBEDDING_DIMENSIONS,
    MODEL_ID,
    queue_embedding,
    rebuild_embeddings,
)
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


class FakeEmbeddingProvider:
    """Deterministic stand-in for the real sentence-transformers model.
    Maps exact input strings to pre-registered vectors so a test can control
    cosine similarity precisely (1.0 for two texts registered to the same
    vector, 0.0 for orthogonal ones), rather than depending on the real
    model's actual semantic judgement. Every registered/default vector here
    is already unit-length so pgvector's cosine distance <=> needs no
    further normalization to reproduce the intended similarity exactly."""

    def __init__(self, vectors: dict[str, list[float]], default: list[float] | None = None) -> None:
        self._vectors = vectors
        self._default = default or ([0.0] * (EMBEDDING_DIMENSIONS - 1) + [1.0])

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vectors.get(text, self._default) for text in texts]


def _unit_vector(index: int) -> list[float]:
    vector = [0.0] * EMBEDDING_DIMENSIONS
    vector[index] = 1.0
    return vector


VECTOR_A = _unit_vector(0)
VECTOR_B = _unit_vector(1)


@pytest.fixture(autouse=True)
def _reset_embedding_provider() -> Iterator[None]:
    embeddings.set_provider_for_testing(None)
    yield
    embeddings.set_provider_for_testing(None)


@pytest.fixture
def embeddings_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO workspaces (id, name, created_at) VALUES (:id, :name, :created_at)"),
            {"id": workspace_id, "name": "Knowledge Embeddings Test", "created_at": now},
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
                "id": session_id,
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
                "event_outbox",
                "audit_events",
                "idempotency_records",
                "embedding_projections",
                "retrieval_documents",
                "entity_operations",
                "resolution_candidates",
                "timeline_entries",
                "knowledge_claims",
                "entity_aliases",
                "pkos_edges",
                "pkos_evidence",
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


def _headers(token: str, key: str) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    return {
        "Idempotency-Key": key,
        "X-CSRF-Token": csrf,
        "X-Correlation-ID": str(uuid4()),
    }


def _create_entity(
    client: TestClient, token: str, key: str, kind: str, name: str, summary: str | None = None
) -> UUID:
    payload = {"kind": kind, "canonical_name": name}
    if summary is not None:
        payload["summary"] = summary
    response = client.post("/api/v1/knowledge/entities", headers=_headers(token, key), json=payload)
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


def _retrieve(client: TestClient, token: str, key: str, **params: object) -> object:
    return client.get("/api/v1/knowledge/retrieve", headers=_headers(token, key), params=params)


# --- Degraded path first, per the implementation plan's explicit ordering
# requirement (RETRIEVAL-CONTRACT.md's degradation rule, chapter-04's
# "if embeddings fail, graph traversal continues" principle): these tests
# come before any happy-path test in this file.


def test_queue_embedding_skips_when_no_provider_available(
    embeddings_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """No provider injected (the autouse fixture resets to the default
    state) and Settings.embeddings_enabled is False in the test environment
    -- creating an entity must still succeed; it just writes no embedding."""
    client, workspace_id, _user_id, token = embeddings_test_context
    entity_id = _create_entity(client, token, "no-provider", "person", "Ada Lovelace")

    with engine.begin() as connection:
        count = connection.execute(
            text(
                "SELECT count(*) FROM embedding_projections e "
                "JOIN retrieval_documents d ON d.id = e.document_id "
                "WHERE d.workspace_id = :workspace_id AND d.entity_id = :entity_id"
            ),
            {"workspace_id": workspace_id, "entity_id": entity_id},
        ).scalar_one()
    assert count == 0


def test_hybrid_retrieval_degrades_when_provider_unavailable(
    embeddings_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = embeddings_test_context
    _create_entity(client, token, "degrade-entity", "person", "Ada Lovelace")

    response = _retrieve(client, token, "degrade-search", q="Ada Lovelace", mode="hybrid")
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "lexical"
    assert body["degraded"] is True
    assert body["degraded_reason"] == "embeddings_disabled"


def test_provider_load_failure_degrades_rather_than_errors(
    embeddings_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """A provider that raises on embed() (simulating a real model-load
    failure) must never turn into a 500 -- the request degrades instead."""

    class _BrokenProvider:
        def embed(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("simulated model failure")

    client, _workspace_id, _user_id, token = embeddings_test_context
    _create_entity(client, token, "broken-provider-entity", "person", "Ada Lovelace")

    embeddings.set_provider_for_testing(_BrokenProvider())
    response = _retrieve(client, token, "broken-search", q="Ada Lovelace", mode="hybrid")
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "lexical"
    assert body["degraded"] is True
    assert body["degraded_reason"] == "embedding_generation_failed"


# --- Happy path: a working (fake, deterministic) provider is injected.


def test_queue_embedding_writes_and_skips_unchanged_content(
    embeddings_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, _user_id, token = embeddings_test_context
    embeddings.set_provider_for_testing(FakeEmbeddingProvider({}))
    entity_id = _create_entity(client, token, "write-entity", "person", "Ada Lovelace")

    with engine.begin() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT model_id, dimensions FROM embedding_projections e "
                    "JOIN retrieval_documents d ON d.id = e.document_id "
                    "WHERE d.workspace_id = :workspace_id AND d.entity_id = :entity_id"
                ),
                {"workspace_id": workspace_id, "entity_id": entity_id},
            )
            .mappings()
            .one()
        )
    assert row["model_id"] == MODEL_ID
    assert row["dimensions"] == EMBEDDING_DIMENSIONS

    # Calling queue_embedding again with no change to the entity's
    # title/summary must skip re-embedding (content_hash unchanged), while a
    # genuine content change must re-embed.
    now = datetime.now(UTC)
    with SessionFactory() as session, session.begin():
        unchanged = queue_embedding(session, workspace_id, entity_id, now)
    assert unchanged.written is False
    assert unchanged.reason == "unchanged"

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE retrieval_documents SET body = 'a new body' "
                "WHERE workspace_id = :workspace_id AND entity_id = :entity_id"
            ),
            {"workspace_id": workspace_id, "entity_id": entity_id},
        )
    with SessionFactory() as session, session.begin():
        changed = queue_embedding(session, workspace_id, entity_id, now)
    assert changed.written is True


def test_rebuild_embeddings_reconstructs_after_manual_deletion(
    embeddings_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, _user_id, token = embeddings_test_context
    embeddings.set_provider_for_testing(FakeEmbeddingProvider({}))
    entity_id = _create_entity(client, token, "rebuild-entity", "person", "Ada Lovelace")

    with engine.begin() as connection:
        connection.execute(
            text("DELETE FROM embedding_projections WHERE workspace_id = :workspace_id"),
            {"workspace_id": workspace_id},
        )

    with SessionFactory() as session:
        report = rebuild_embeddings(session, workspace_id)
        session.commit()
    assert report.embedded >= 1

    with engine.begin() as connection:
        count = connection.execute(
            text(
                "SELECT count(*) FROM embedding_projections e "
                "JOIN retrieval_documents d ON d.id = e.document_id "
                "WHERE d.workspace_id = :workspace_id AND d.entity_id = :entity_id"
            ),
            {"workspace_id": workspace_id, "entity_id": entity_id},
        ).scalar_one()
    assert count == 1


def test_hybrid_retrieval_surfaces_a_semantic_only_match(
    embeddings_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """ "Org leadership contact" shares zero words/trigrams with "Chief
    Executive Officer", so lexical mode can never find it -- but both are
    registered to the same fake vector, so hybrid mode must surface it via
    semantic similarity alone, ranked below any lexical relevance band."""
    client, _workspace_id, _user_id, token = embeddings_test_context
    title = "Chief Executive Officer"
    summary = "Sets company direction"
    doc_key = f"{title}\n{summary}"
    query = "org leadership contact"
    embeddings.set_provider_for_testing(FakeEmbeddingProvider({doc_key: VECTOR_A, query: VECTOR_A}))
    entity_id = _create_entity(client, token, "semantic-entity", "person", title, summary=summary)

    lexical_response = _retrieve(client, token, "semantic-lexical-check", q=query)
    assert entity_id not in {UUID(item["entity_id"]) for item in lexical_response.json()["items"]}

    response = _retrieve(client, token, "semantic-search", q=query, mode="hybrid")
    assert response.status_code == 200
    body = response.json()
    assert body["mode"] == "hybrid"
    assert body["degraded"] is False
    items = {UUID(item["entity_id"]): item for item in body["items"]}
    assert entity_id in items
    match = items[entity_id]
    assert match["matching_mode"] == "semantic"
    assert match["factors"]["semantic"] == pytest.approx(1.0)
    assert 0 < match["score"] < 0.70  # strictly below lexical relevance's ceiling


def test_hybrid_retrieval_never_ranks_semantic_only_above_lexical_relevance(
    embeddings_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = embeddings_test_context
    # Deliberately NOT a prefix/exact match for the query below (it would
    # then rank in the much higher exact/prefix band, which this test isn't
    # about) -- this exercises the generic trigram/fulltext lexical-relevance
    # band specifically, per the docstring's "above lexical relevance" claim.
    lexical_title = "Directory of Org Leadership Contact"
    semantic_title = "Chief Executive Officer"
    semantic_summary = "Sets company direction"
    doc_key = f"{semantic_title}\n{semantic_summary}"
    query = "org leadership contact"
    embeddings.set_provider_for_testing(FakeEmbeddingProvider({doc_key: VECTOR_A, query: VECTOR_A}))
    lexical_id = _create_entity(client, token, "lexical-entity", "person", lexical_title)
    semantic_id = _create_entity(
        client, token, "semantic-only-entity", "person", semantic_title, summary=semantic_summary
    )

    response = _retrieve(client, token, "ranking-search", q=query, mode="hybrid")
    assert response.status_code == 200
    items = response.json()["items"]
    entity_ids = [UUID(item["entity_id"]) for item in items]
    assert entity_ids.index(lexical_id) < entity_ids.index(semantic_id)


def test_hybrid_bonus_ranks_above_equivalent_pure_lexical_match(
    embeddings_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """Two entities share the exact same lexically-matching title text (so
    their lexical scores are identical); only one also carries a semantic
    match with the query. The hybrid-boosted one must rank first."""
    client, _workspace_id, _user_id, token = embeddings_test_context
    # Same title on both entities => identical trigram_score for both
    # (trigram scoring is title-only, see retrieval.py). The query is a
    # near-but-not-exact match ("meridan" typo for "meridian") so neither
    # entity hits the exact-name/prefix bands, which don't leave room for a
    # semantic bonus to matter -- both land in the generic lexical-relevance
    # band with an identical baseline score, isolating the semantic bonus as
    # the only variable between them.
    title = "Meridian Project Status Update"
    query = "meridan project status update"
    boosted_summary = "quarterly summary alpha"
    plain_summary = "quarterly summary beta"
    boosted_doc_key = f"{title}\n{boosted_summary}"
    embeddings.set_provider_for_testing(
        FakeEmbeddingProvider({boosted_doc_key: VECTOR_A, query: VECTOR_A})
    )
    boosted_id = _create_entity(
        client, token, "boosted-entity", "project", title, summary=boosted_summary
    )
    plain_id = _create_entity(
        client, token, "plain-entity", "project", title, summary=plain_summary
    )

    response = _retrieve(client, token, "bonus-search", q=query, mode="hybrid")
    assert response.status_code == 200
    items = {UUID(item["entity_id"]): item for item in response.json()["items"]}
    assert items[boosted_id]["matching_mode"] == "hybrid"
    assert items[plain_id]["matching_mode"] == "lexical"
    assert items[boosted_id]["score"] > items[plain_id]["score"]


def test_embedding_workspace_isolation(
    embeddings_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = embeddings_test_context
    title = "Chief Executive Officer"
    summary = "Sets company direction"
    doc_key = f"{title}\n{summary}"
    query = "org leadership contact"
    embeddings.set_provider_for_testing(FakeEmbeddingProvider({doc_key: VECTOR_A, query: VECTOR_A}))
    _create_entity(client, token, "iso-entity", "person", title, summary=summary)

    other_workspace_id = uuid4()
    other_user_id = uuid4()
    other_token = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO workspaces (id, name, created_at) VALUES (:id, :name, :created_at)"),
            {"id": other_workspace_id, "name": "Other Embeddings Workspace", "created_at": now},
        )
        connection.execute(
            text(
                "INSERT INTO users (id, workspace_id, email, password_hash, created_at) "
                "VALUES (:id, :workspace_id, :email, :password_hash, :created_at)"
            ),
            {
                "id": other_user_id,
                "workspace_id": other_workspace_id,
                "email": f"{other_user_id}@example.test",
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
                "workspace_id": other_workspace_id,
                "user_id": other_user_id,
                "token_hash": sha256(other_token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "last_seen_at": now,
            },
        )
    other_client = TestClient(app)
    other_client.cookies.set("ecc_session", other_token)
    try:
        response = _retrieve(other_client, other_token, "iso-other", q=query, mode="hybrid")
        assert response.status_code == 200
        assert response.json()["items"] == []
    finally:
        other_client.close()
        with engine.begin() as connection:
            for table in ("sessions", "users"):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                    {"workspace_id": other_workspace_id},
                )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": other_workspace_id},
            )


@pytest.mark.skipif(
    os.environ.get("ECC_SKIP_REAL_MODEL_TESTS") == "1",
    reason="real-model integration test opted out via ECC_SKIP_REAL_MODEL_TESTS",
)
def test_real_model_ranks_semantically_related_text_higher(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one test in this suite that loads and runs the actual
    sentence-transformers model, proving the real integration -- not just
    the fake-provider plumbing -- produces sane semantic judgements. Skips
    gracefully rather than failing the build if the model genuinely can't be
    fetched (e.g. a transient Hugging Face Hub outage in CI); that's an
    infra flakiness concern, not a code-correctness one.
    """
    monkeypatch.setenv("ECC_EMBEDDINGS_ENABLED", "true")
    get_settings.cache_clear()
    embeddings.set_provider_for_testing(None)
    try:
        try:
            provider = embeddings.get_provider()
        except embeddings.EmbeddingUnavailable as exc:
            pytest.skip(f"real embedding model unavailable in this environment: {exc}")

        anchor, related, unrelated = provider.embed(
            [
                "chief executive officer",
                "CEO of the company",
                "vegetable soup recipe",
            ]
        )

        def cosine(a: list[float], b: list[float]) -> float:
            dot = sum(x * y for x, y in zip(a, b, strict=True))
            norm_a = sum(x * x for x in a) ** 0.5
            norm_b = sum(y * y for y in b) ** 0.5
            return dot / (norm_a * norm_b)

        related_similarity = cosine(anchor, related)
        unrelated_similarity = cosine(anchor, unrelated)
        assert related_similarity > unrelated_similarity
        assert related_similarity > 0.5
    finally:
        get_settings.cache_clear()
        embeddings.set_provider_for_testing(None)
