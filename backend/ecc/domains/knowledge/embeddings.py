"""Phase 2 optional embeddings: a derived, replaceable semantic projection
layer over retrieval_documents (ADR-0003: "embeddings are replaceable
projections and never overwrite source evidence").

Gated by Settings.embeddings_enabled (see config.py) -- off by default, since
loading the local sentence-transformers model costs a multi-second first-call
delay and a Hugging Face Hub download before it's cached. Every entry point
here is best-effort: a disabled feature or a failed model load is reported
back to the caller (queue_embedding) or degrades the caller to lexical-only
(retrieval.py), never raised as a request-failing error, per
RETRIEVAL-CONTRACT.md's degradation rule.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from typing import Protocol
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.config import get_settings

MODEL_ID = "sentence-transformers/all-MiniLM-L6-v2"
# Bump when the model, its normalization, or the content fed to it changes in
# a way that makes previously-stored vectors no longer comparable to freshly
# generated ones -- existing rows keep their own model_version, so a rebuild
# is required (not automatic) to bring a workspace onto a new version.
MODEL_VERSION = "1"
EMBEDDING_DIMENSIONS = 384


class EmbeddingProvider(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class EmbeddingUnavailable(RuntimeError):
    """Embeddings cannot be generated right now (disabled, or the model
    failed to load). Always caught by callers in this module and in
    retrieval.py -- never expected to cross an API boundary."""


class _SentenceTransformerProvider:
    def __init__(self) -> None:
        # Imported lazily: sentence-transformers/torch are heavy (multi-second
        # import, large dependency footprint) and this constructor only runs
        # the first time embeddings are actually requested in a process that
        # has ECC_EMBEDDINGS_ENABLED=true, not on every app/module import.
        from sentence_transformers import SentenceTransformer

        self._model = SentenceTransformer(MODEL_ID)

    def embed(self, texts: list[str]) -> list[list[float]]:
        vectors = self._model.encode(texts, normalize_embeddings=True)
        return [vector.tolist() for vector in vectors]


_provider: EmbeddingProvider | None = None
_provider_failed = False


def get_provider() -> EmbeddingProvider:
    """Lazily load and cache the embedding provider once per process.

    A provider injected via set_provider_for_testing always wins, bypassing
    both the enabled-flag check and real model loading -- tests exercise the
    real queue_embedding/hybrid-retrieval code paths against a fast
    deterministic fake without downloading or running the real model.
    """
    global _provider, _provider_failed
    if _provider is not None:
        return _provider
    if not get_settings().embeddings_enabled:
        raise EmbeddingUnavailable("embeddings_disabled")
    if _provider_failed:
        # Do not retry a failed load on every call within the same process --
        # a missing/corrupt model cache or unreachable Hugging Face Hub fails
        # the same way every time until the process restarts.
        raise EmbeddingUnavailable("model_load_previously_failed")
    try:
        _provider = _SentenceTransformerProvider()
    except Exception as exc:
        _provider_failed = True
        raise EmbeddingUnavailable("model_load_failed") from exc
    return _provider


def set_provider_for_testing(provider: EmbeddingProvider | None) -> None:
    """Test-only hook. Pass a fake EmbeddingProvider to make get_provider()
    return it immediately (real settings/model-loading skipped entirely), or
    None to reset to the default lazy-real-provider state."""
    global _provider, _provider_failed
    _provider = provider
    _provider_failed = False


def _content_hash(title: str, body: str) -> str:
    return sha256(f"{title}\n{body}".encode()).hexdigest()


def vector_literal(values: list[float]) -> str:
    """Serializes to pgvector's text input format ("[v1,v2,...]"), bound as a
    plain string and CAST(:x AS vector) in SQL -- this codebase's raw text()
    queries don't route through pgvector.sqlalchemy's Core bind-parameter
    processing (that only applies to Core Table/Column-built statements), and
    the same explicit-cast-a-string-literal pattern is already used for jsonb
    columns elsewhere in this domain (see entities.py's CAST(:attributes AS
    jsonb)), so this keeps the same convention rather than introducing a
    separate psycopg-level type adapter registration path.
    """
    return "[" + ",".join(repr(float(value)) for value in values) + "]"


@dataclass(frozen=True)
class EmbeddingWriteResult:
    written: bool
    reason: str | None = None


def queue_embedding(
    session: Session, workspace_id: UUID, entity_id: UUID, now: datetime
) -> EmbeddingWriteResult:
    """Best-effort (re)compute of one entity's embedding_projections row from
    its current retrieval_documents row, in the caller's own transaction --
    same reasoning as timeline.py's queue_timeline_entry and retrieval.py's
    queue_retrieval_document: a rolled-back mutation rolls this write back
    with it, so no deferred-until-commit machinery is needed.

    Never raises. A disabled feature or failed model load is not a reason to
    fail the entity/claim mutation that triggered this call -- only a reason
    to skip writing an embedding this time. Skips (without calling the
    provider at all) when the retrieval_documents content hasn't changed
    since the last successful embed, so an unrelated field edit that doesn't
    touch title/body/claims doesn't re-pay the embedding cost.
    """
    row = (
        session.execute(
            text(
                "SELECT id, title, body FROM retrieval_documents "
                "WHERE workspace_id = :workspace_id AND entity_id = :entity_id"
            ),
            {"workspace_id": workspace_id, "entity_id": entity_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return EmbeddingWriteResult(written=False, reason="no_retrieval_document")

    content_hash = _content_hash(row["title"], row["body"])
    existing_hash = session.execute(
        text(
            "SELECT content_hash FROM embedding_projections "
            "WHERE workspace_id = :workspace_id AND document_id = :document_id "
            "AND model_id = :model_id"
        ),
        {"workspace_id": workspace_id, "document_id": row["id"], "model_id": MODEL_ID},
    ).scalar_one_or_none()
    if existing_hash == content_hash:
        return EmbeddingWriteResult(written=False, reason="unchanged")

    try:
        provider = get_provider()
        [vector] = provider.embed([f"{row['title']}\n{row['body']}"])
    except EmbeddingUnavailable as exc:
        return EmbeddingWriteResult(written=False, reason=str(exc))
    except Exception:
        # Broad by design, matching retrieve()'s same reasoning: the real
        # provider wraps a third-party ML library that can fail in ways this
        # module cannot enumerate, and a mutation (creating an entity,
        # recording a claim) must never fail because the embedding side
        # effect did.
        return EmbeddingWriteResult(written=False, reason="embedding_generation_failed")

    session.execute(
        text(
            """
            INSERT INTO embedding_projections (
                id, workspace_id, document_id, model_id, model_version,
                dimensions, embedding, content_hash, created_at, updated_at
            ) VALUES (
                gen_random_uuid(), :workspace_id, :document_id, :model_id, :model_version,
                :dimensions, CAST(:embedding AS vector), :content_hash, :now, :now
            )
            ON CONFLICT (workspace_id, document_id, model_id) DO UPDATE SET
                model_version = EXCLUDED.model_version,
                dimensions = EXCLUDED.dimensions,
                embedding = EXCLUDED.embedding,
                content_hash = EXCLUDED.content_hash,
                updated_at = EXCLUDED.updated_at
            """
        ),
        {
            "workspace_id": workspace_id,
            "document_id": row["id"],
            "model_id": MODEL_ID,
            "model_version": MODEL_VERSION,
            "dimensions": EMBEDDING_DIMENSIONS,
            "embedding": vector_literal(vector),
            "content_hash": content_hash,
            "now": now,
        },
    )
    return EmbeddingWriteResult(written=True)


@dataclass(frozen=True)
class EmbeddingRebuildReport:
    workspace_id: UUID
    embedded: int
    skipped: int


def rebuild_embeddings(session: Session, workspace_id: UUID) -> EmbeddingRebuildReport:
    """Deterministically regenerate embedding_projections for a workspace's
    current retrieval_documents -- delete-then-reinsert, matching
    retrieval.py's rebuild_retrieval_documents and timeline.py's
    rebuild_timeline. If embeddings are disabled or the model can't load,
    every document is counted as skipped rather than the rebuild failing --
    consistent with queue_embedding's best-effort contract.
    """
    session.execute(
        text("DELETE FROM embedding_projections WHERE workspace_id = :workspace_id"),
        {"workspace_id": workspace_id},
    )
    documents = session.execute(
        text("SELECT entity_id FROM retrieval_documents WHERE workspace_id = :workspace_id"),
        {"workspace_id": workspace_id},
    ).all()
    now = datetime.now(UTC)
    embedded = 0
    skipped = 0
    for (entity_id,) in documents:
        result = queue_embedding(session, workspace_id, entity_id, now)
        if result.written:
            embedded += 1
        else:
            skipped += 1
    return EmbeddingRebuildReport(workspace_id=workspace_id, embedded=embedded, skipped=skipped)
