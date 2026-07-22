from base64 import urlsafe_b64decode, urlsafe_b64encode
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from hmac import compare_digest, new
from html import escape
from json import dumps, loads
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthDep
from ecc.config import get_settings
from ecc.database import get_session
from ecc.domains.knowledge.embeddings import (
    MODEL_ID,
    EmbeddingUnavailable,
    get_provider,
    vector_literal,
)

router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge-retrieval"])
SessionDep = Annotated[Session, Depends(get_session)]

# RETRIEVAL-CONTRACT.md's ranking: exact trusted identifier > exact
# canonical name > exact alias > lexical relevance > semantic relevance.
# Levels 1-3 are collapsed here the same way resolution.py's deterministic
# match check is: entity_aliases has no separate "trusted identifier"
# column beyond its free-form alias_type, so an exact alias match and an
# exact canonical-name match are the two deterministic levels this schema
# can express, both ranked above any lexical score.
_SCORE_EXACT_ALIAS = 1.00
_SCORE_EXACT_NAME = 0.95
_SCORE_PREFIX_NAME = 0.85
# Hybrid fusion (RETRIEVAL-CONTRACT.md's "versioned deterministic method",
# version 1): a document lexical search already found gets a small boost
# from semantic agreement, capped strictly below _SCORE_PREFIX_NAME so a
# hybrid-boosted lexical relevance hit can never leapfrog a prefix match. A
# document lexical search did NOT find is scored from semantic similarity
# alone, scaled onto a band strictly below plain lexical relevance
# (_SCORE_LEXICAL_CEILING) -- semantic-only recall extends what a query can
# find, but per the contract's ranking order it never outranks a lexical hit.
_SCORE_LEXICAL_CEILING = 0.75
_SCORE_HYBRID_BONUS_CEILING = 0.84
_SCORE_SEMANTIC_CEILING = 0.65
_SEMANTIC_BONUS_WEIGHT = 0.05
_SEMANTIC_MIN_SIMILARITY = 0.35
_SEMANTIC_CANDIDATE_LIMIT = 100
# pg_trgm's similarity() is continuous and rarely returns exactly 0 even for
# unrelated strings (small positive noise from incidental shared trigrams),
# unlike ts_rank_cd which is discrete token-overlap and genuinely 0 when
# nothing matched -- so "does lexical relevance apply at all" must use this
# same meaningful-relevance threshold the candidate WHERE clause already
# uses, not a bare > 0 check, or trigram noise alone would misclassify a
# pure-semantic match as hybrid.
_TRIGRAM_RELEVANCE_THRESHOLD = 0.15


def _build_body(session: Session, workspace_id: UUID, entity_id: UUID, summary: str | None) -> str:
    """Derives a retrieval document's searchable body fresh from current
    state (summary + every non-superseded claim) each time, rather than
    incrementally appending -- so a superseded claim drops out of search
    content automatically, with no separate bookkeeping to keep in sync."""
    parts = [summary] if summary else []
    claims = session.execute(
        text(
            """
            SELECT predicate, value_json FROM knowledge_claims
            WHERE workspace_id = :workspace_id AND subject_id = :entity_id
              AND superseded_by IS NULL
            ORDER BY created_at
            """
        ),
        {"workspace_id": workspace_id, "entity_id": entity_id},
    ).all()
    for predicate, value_json in claims:
        value = value_json if isinstance(value_json, str) else dumps(value_json)
        parts.append(f"{predicate}: {value}")
    return "\n".join(parts)


def queue_retrieval_document(
    session: Session,
    workspace_id: UUID,
    entity_id: UUID,
    entity_type: str,
    title: str,
    summary: str | None,
    version: int,
    now: datetime,
) -> None:
    """Upsert retrieval_documents within the caller's own transaction --
    same reasoning as timeline.py's queue_timeline_entry: a rolled-back
    mutation rolls this write back with it, so no deferred-until-commit
    machinery is needed for the write itself to never go stale."""
    body = _build_body(session, workspace_id, entity_id, summary)
    session.execute(
        text(
            """
            INSERT INTO retrieval_documents (
                id, workspace_id, entity_type, entity_id, title, body,
                source_version, updated_at
            ) VALUES (
                gen_random_uuid(), :workspace_id, :entity_type, :entity_id, :title, :body,
                :source_version, :now
            )
            ON CONFLICT (workspace_id, entity_id) DO UPDATE SET
                entity_type = EXCLUDED.entity_type,
                title = EXCLUDED.title,
                body = EXCLUDED.body,
                source_version = EXCLUDED.source_version,
                updated_at = EXCLUDED.updated_at
            """
        ),
        {
            "workspace_id": workspace_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "title": title,
            "body": body,
            "source_version": version,
            "now": now,
        },
    )


@dataclass(frozen=True)
class RetrievalRebuildReport:
    workspace_id: UUID
    documents_written: int


def rebuild_retrieval_documents(session: Session, workspace_id: UUID) -> RetrievalRebuildReport:
    """Deterministically regenerate retrieval_documents for a workspace from
    pkos_nodes and knowledge_claims -- the authoritative tables -- rather
    than trusting incrementally-written projection state. Delete-then-
    reinsert (matching timeline.py's rebuild_timeline and DATA-MODEL.md's
    "rebuildable projections may be regenerated" framing); only active
    entities get a document, matching the live query's own lifecycle
    filter, so an archived entity's stale document is simply dropped."""
    session.execute(
        text("DELETE FROM retrieval_documents WHERE workspace_id = :workspace_id"),
        {"workspace_id": workspace_id},
    )
    nodes = session.execute(
        text(
            """
            SELECT id, node_type, canonical_name, attributes, version
            FROM pkos_nodes
            WHERE workspace_id = :workspace_id AND status = 'active'
            """
        ),
        {"workspace_id": workspace_id},
    ).all()
    now = datetime.now(UTC)
    written = 0
    for entity_id, node_type, canonical_name, attributes, version in nodes:
        summary = (attributes or {}).get("summary")
        queue_retrieval_document(
            session, workspace_id, entity_id, node_type, canonical_name, summary, version, now
        )
        written += 1
    return RetrievalRebuildReport(workspace_id=workspace_id, documents_written=written)


class RetrievalResult(BaseModel):
    entity_type: str
    entity_id: UUID
    title: str
    snippet: str
    score: float = Field(ge=0, le=1)
    matching_mode: str
    factors: dict[str, float]
    evidence_state: str
    source_version: int
    stale: bool


class RetrievalResponse(BaseModel):
    items: list[RetrievalResult]
    next_cursor: str | None = None
    mode: str
    degraded: bool
    degraded_reason: str | None = None


def _normalize_query(value: str) -> str:
    normalized = " ".join(value.casefold().split())
    if not normalized:
        raise HTTPException(status_code=422, detail="RETRIEVAL_QUERY_REQUIRED")
    if len(normalized) > 500:
        raise HTTPException(status_code=422, detail="RETRIEVAL_QUERY_TOO_LONG")
    return normalized


def _snippet(body: str, title: str) -> str:
    source = body or title
    collapsed = " ".join(source.split())
    return escape(collapsed)[:240]


def _encode_cursor(score: float, entity_id: UUID) -> str:
    payload = dumps({"score": score, "id": str(entity_id)}).encode()
    signature = new(get_settings().session_secret.encode(), payload, sha256).hexdigest().encode()
    return urlsafe_b64encode(payload + b"." + signature).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[float, UUID]:
    try:
        raw = urlsafe_b64decode((cursor + "=" * (-len(cursor) % 4)).encode())
        payload, signature = raw.rsplit(b".", 1)
        expected = new(get_settings().session_secret.encode(), payload, sha256).hexdigest()
        if not compare_digest(signature.decode(), expected):
            raise ValueError
        decoded = loads(payload)
        score = float(decoded["score"])
        if not 0 <= score <= 1:
            raise ValueError
        return score, UUID(decoded["id"])
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="MALFORMED_CURSOR") from exc


_LEXICAL_CANDIDATES_CTE = """
    candidates AS (
        SELECT
            d.entity_type, d.entity_id, d.title, d.body,
            d.source_version, d.updated_at,
            n.version AS live_version, n.status AS live_status,
            lower(d.title) AS normalized_title,
            similarity(lower(d.title), :query)::double precision AS trigram_score,
            ts_rank_cd(
                d.search_document, plainto_tsquery('simple', :query)
            )::double precision AS fulltext_score,
            EXISTS (
                SELECT 1 FROM entity_aliases a
                WHERE a.workspace_id = d.workspace_id
                  AND a.entity_id = d.entity_id
                  AND a.normalized_value = :query
            ) AS exact_alias_match,
            (
                SELECT e.evidence_state FROM pkos_evidence e
                WHERE e.workspace_id = d.workspace_id AND e.node_id = d.entity_id
                ORDER BY e.captured_at DESC LIMIT 1
            ) AS evidence_state
        FROM retrieval_documents d
        JOIN pkos_nodes n
          ON n.workspace_id = d.workspace_id AND n.id = d.entity_id
        WHERE d.workspace_id = :workspace_id
          AND n.status = 'active'
          AND (CAST(:kind AS text) IS NULL OR d.entity_type = :kind)
          AND (
              CAST(:updated_from AS timestamptz) IS NULL
              OR d.updated_at >= CAST(:updated_from AS timestamptz)
          )
          AND (
              CAST(:updated_to AS timestamptz) IS NULL
              OR d.updated_at <= CAST(:updated_to AS timestamptz)
          )
    )
"""


def _run_lexical_query(session: Session, params: dict[str, Any]) -> Sequence[Any]:
    return (
        session.execute(
            text(
                f"""
                WITH {_LEXICAL_CANDIDATES_CTE}, ranked AS (
                    SELECT *,
                        CASE
                            WHEN exact_alias_match THEN {_SCORE_EXACT_ALIAS}
                            WHEN normalized_title = :query THEN {_SCORE_EXACT_NAME}
                            WHEN normalized_title LIKE :query || '%' THEN {_SCORE_PREFIX_NAME}
                            ELSE greatest(
                                least(trigram_score, 0.75),
                                least(fulltext_score, 0.70)
                            )
                        END AS score
                    FROM candidates
                    WHERE exact_alias_match
                       OR normalized_title = :query
                       OR normalized_title LIKE :query || '%'
                       OR trigram_score >= 0.15
                       OR fulltext_score > 0
                )
                SELECT * FROM ranked
                WHERE (
                    CAST(:cursor_score AS double precision) IS NULL
                    OR score < CAST(:cursor_score AS double precision)
                    OR (
                        score = CAST(:cursor_score AS double precision)
                        AND entity_id > CAST(:cursor_id AS uuid)
                    )
                )
                ORDER BY score DESC, entity_id ASC
                LIMIT :fetch_limit
                """
            ),
            params,
        )
        .mappings()
        .all()
    )


def _run_hybrid_query(session: Session, params: dict[str, Any]) -> Sequence[Any]:
    """Fuses lexical candidates with the nearest embedding_projections
    neighbors of the query vector. See the module-level _SCORE_* constants
    for the fusion formula (RETRIEVAL-CONTRACT.md's "versioned deterministic
    method"): a document found lexically gets a small semantic bonus capped
    below the next ranking band up; a document found only semantically is
    scored from similarity alone, capped below plain lexical relevance."""
    return (
        session.execute(
            text(
                f"""
                WITH {_LEXICAL_CANDIDATES_CTE}, semantic AS (
                    SELECT
                        d.entity_type, d.entity_id, d.title, d.body,
                        d.source_version, d.updated_at,
                        n.version AS live_version, n.status AS live_status,
                        greatest(0, least(1, 1 - (e.embedding <=> CAST(:query_vector AS vector))))
                            ::double precision AS semantic_similarity
                    FROM embedding_projections e
                    JOIN retrieval_documents d
                      ON d.workspace_id = e.workspace_id AND d.id = e.document_id
                    JOIN pkos_nodes n
                      ON n.workspace_id = d.workspace_id AND n.id = d.entity_id
                    WHERE e.workspace_id = :workspace_id
                      AND e.model_id = :model_id
                      AND n.status = 'active'
                      AND (CAST(:kind AS text) IS NULL OR d.entity_type = :kind)
                      AND (
                          CAST(:updated_from AS timestamptz) IS NULL
                          OR d.updated_at >= CAST(:updated_from AS timestamptz)
                      )
                      AND (
                          CAST(:updated_to AS timestamptz) IS NULL
                          OR d.updated_at <= CAST(:updated_to AS timestamptz)
                      )
                    ORDER BY e.embedding <=> CAST(:query_vector AS vector)
                    LIMIT :semantic_candidate_limit
                ), merged AS (
                    SELECT
                        COALESCE(l.entity_id, s.entity_id) AS entity_id,
                        COALESCE(l.entity_type, s.entity_type) AS entity_type,
                        COALESCE(l.title, s.title) AS title,
                        COALESCE(l.body, s.body) AS body,
                        COALESCE(l.source_version, s.source_version) AS source_version,
                        COALESCE(l.live_version, s.live_version) AS live_version,
                        lower(COALESCE(l.title, s.title)) AS normalized_title,
                        COALESCE(l.trigram_score, 0) AS trigram_score,
                        COALESCE(l.fulltext_score, 0) AS fulltext_score,
                        COALESCE(l.exact_alias_match, false) AS exact_alias_match,
                        COALESCE(s.semantic_similarity, 0) AS semantic_score,
                        COALESCE(l.evidence_state, (
                            SELECT e2.evidence_state FROM pkos_evidence e2
                            WHERE e2.workspace_id = :workspace_id
                              AND e2.node_id = COALESCE(l.entity_id, s.entity_id)
                            ORDER BY e2.captured_at DESC LIMIT 1
                        )) AS evidence_state
                    FROM candidates l
                    FULL OUTER JOIN semantic s ON s.entity_id = l.entity_id
                ), ranked AS (
                    SELECT *,
                        CASE
                            WHEN exact_alias_match THEN {_SCORE_EXACT_ALIAS}
                            WHEN normalized_title = :query THEN {_SCORE_EXACT_NAME}
                            WHEN normalized_title LIKE :query || '%' THEN {_SCORE_PREFIX_NAME}
                            WHEN trigram_score >= {_TRIGRAM_RELEVANCE_THRESHOLD}
                                OR fulltext_score > 0 THEN least(
                                greatest(
                                    least(trigram_score, {_SCORE_LEXICAL_CEILING}),
                                    least(fulltext_score, 0.70)
                                ) + {_SEMANTIC_BONUS_WEIGHT} * semantic_score,
                                {_SCORE_HYBRID_BONUS_CEILING}
                            )
                            ELSE semantic_score * {_SCORE_SEMANTIC_CEILING}
                        END AS score
                    FROM merged
                    WHERE exact_alias_match
                       OR normalized_title = :query
                       OR normalized_title LIKE :query || '%'
                       OR trigram_score >= 0.15
                       OR fulltext_score > 0
                       OR semantic_score >= {_SEMANTIC_MIN_SIMILARITY}
                )
                SELECT * FROM ranked
                WHERE (
                    CAST(:cursor_score AS double precision) IS NULL
                    OR score < CAST(:cursor_score AS double precision)
                    OR (
                        score = CAST(:cursor_score AS double precision)
                        AND entity_id > CAST(:cursor_id AS uuid)
                    )
                )
                ORDER BY score DESC, entity_id ASC
                LIMIT :fetch_limit
                """
            ),
            params,
        )
        .mappings()
        .all()
    )


def _matching_mode(row: Any, query: str, hybrid: bool) -> str:
    normalized_title = row["normalized_title"]
    if row["exact_alias_match"]:
        return "exact_alias"
    if normalized_title == query:
        return "exact_name"
    if normalized_title.startswith(query):
        return "name_prefix"
    trigram_relevant = float(row["trigram_score"]) >= _TRIGRAM_RELEVANCE_THRESHOLD
    fulltext_relevant = float(row["fulltext_score"]) > 0
    if trigram_relevant or fulltext_relevant:
        if hybrid and float(row.get("semantic_score", 0) or 0) > 0:
            return "hybrid"
        return "lexical"
    return "semantic"


def _build_response(
    rows: Sequence[Any],
    query: str,
    mode: str,
    limit: int,
    hybrid: bool,
    degraded: bool,
    degraded_reason: str | None,
) -> RetrievalResponse:
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit and page:
        last = page[-1]
        next_cursor = _encode_cursor(float(last["score"]), last["entity_id"])

    items: list[RetrievalResult] = []
    for row in page:
        factors = {
            "trigram": round(min(float(row["trigram_score"]), 0.75), 6),
            "fulltext": round(min(float(row["fulltext_score"]), 0.70), 6),
            "exact_alias": 1.0 if row["exact_alias_match"] else 0.0,
        }
        if hybrid:
            factors["semantic"] = round(float(row.get("semantic_score", 0) or 0), 6)
        items.append(
            RetrievalResult(
                entity_type=row["entity_type"],
                entity_id=row["entity_id"],
                title=row["title"],
                snippet=_snippet(row["body"], row["title"]),
                score=round(min(max(float(row["score"]), 0.0), 1.0), 6),
                matching_mode=_matching_mode(row, query, hybrid),
                factors=factors,
                evidence_state=row["evidence_state"] or "unknown",
                source_version=row["source_version"],
                stale=row["source_version"] != row["live_version"],
            )
        )
    return RetrievalResponse(
        items=items,
        next_cursor=next_cursor,
        mode=mode,
        degraded=degraded,
        degraded_reason=degraded_reason,
    )


@router.get("/retrieve", response_model=RetrievalResponse)
def retrieve(
    auth: AuthDep,
    session: SessionDep,
    q: Annotated[str, Query(min_length=1, max_length=500)],
    kind: str | None = None,
    updated_from: datetime | None = None,
    updated_to: datetime | None = None,
    mode: str = "lexical",
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
) -> RetrievalResponse:
    query = _normalize_query(q)
    degraded = False
    degraded_reason: str | None = None
    query_vector: list[float] | None = None

    if mode != "lexical":
        # RETRIEVAL-CONTRACT.md's degradation rule: never fail the request --
        # a disabled feature or a failed model load degrades to lexical-only
        # with degraded=true rather than erroring. Catches Exception broadly
        # (not just our own EmbeddingUnavailable) deliberately: the real
        # provider wraps a third-party ML library that can fail in ways this
        # module cannot enumerate (OOM, a corrupted model cache, a tokenizer
        # error, ...) -- per chapter-04-knowledge-platform.md's "if
        # embeddings fail, graph traversal continues" principle, any of
        # those must degrade the same way a disabled feature does, not
        # surface as a 500.
        try:
            provider = get_provider()
            [query_vector] = provider.embed([query])
        except EmbeddingUnavailable as exc:
            degraded = True
            degraded_reason = str(exc)
            mode = "lexical"
        except Exception:
            degraded = True
            degraded_reason = "embedding_generation_failed"
            mode = "lexical"

    cursor_payload = _decode_cursor(cursor) if cursor else None
    params: dict[str, Any] = {
        "workspace_id": auth.workspace_id,
        "query": query,
        "kind": kind,
        "updated_from": updated_from,
        "updated_to": updated_to,
        "cursor_score": cursor_payload[0] if cursor_payload else None,
        "cursor_id": cursor_payload[1] if cursor_payload else None,
        "fetch_limit": limit + 1,
    }

    if query_vector is not None:
        rows = _run_hybrid_query(
            session,
            {
                **params,
                "query_vector": vector_literal(query_vector),
                "model_id": MODEL_ID,
                "semantic_candidate_limit": _SEMANTIC_CANDIDATE_LIMIT,
            },
        )
        return _build_response(rows, query, mode, limit, True, degraded, degraded_reason)

    rows = _run_lexical_query(session, params)
    return _build_response(rows, query, mode, limit, False, degraded, degraded_reason)
