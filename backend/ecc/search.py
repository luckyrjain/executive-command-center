from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import datetime
from hashlib import sha256
from hmac import compare_digest, new
from html import escape
from json import dumps, loads
from typing import Annotated, Literal, TypedDict
from unicodedata import normalize
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthDep
from ecc.config import get_settings
from ecc.database import get_session

router = APIRouter(prefix="/api/v1/search", tags=["search"])
SessionDep = Annotated[Session, Depends(get_session)]
EntityType = Literal["task", "commitment", "note", "meeting", "calendar_event", "risk"]
EntityTypeFilter = Annotated[list[EntityType] | None, Query(alias="types[]")]

_TYPE_ORDER = {
    "task": 1,
    "commitment": 2,
    "meeting": 3,
    "note": 4,
    "risk": 5,
    "calendar_event": 6,
}


class CursorPayload(TypedDict):
    score: float
    updated_at: datetime
    type_order: int
    id: UUID


class SearchResult(BaseModel):
    entity_type: EntityType
    entity_id: UUID
    title: str
    snippet: str
    matched_fields: list[str]
    score: float = Field(ge=0, le=1)
    score_components: dict[str, float]
    updated_at: datetime
    timestamp_context: datetime | None
    source_type: str
    archived: bool
    evidence_refs: list[UUID]


class SearchResponse(BaseModel):
    items: list[SearchResult]
    next_cursor: str | None
    degraded: bool = False


def _normalize_query(value: str) -> str:
    normalized = " ".join(normalize("NFKC", value).casefold().split())
    if not normalized:
        raise HTTPException(status_code=422, detail="SEARCH_QUERY_REQUIRED")
    if len(normalized) > 500:
        raise HTTPException(status_code=422, detail="SEARCH_QUERY_TOO_LONG")
    return normalized


def _validate_range(start: datetime | None, end: datetime | None) -> None:
    for value in (start, end):
        if value is not None and value.utcoffset() is None:
            raise HTTPException(status_code=422, detail="TIMEZONE_OFFSET_REQUIRED")
    if start is not None and end is not None and start > end:
        raise HTTPException(status_code=422, detail="INVALID_DATE_RANGE")


def _sign_cursor(payload: dict[str, object]) -> str:
    raw = dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = new(get_settings().session_secret.encode(), raw, sha256).digest()
    return urlsafe_b64encode(raw + signature).decode().rstrip("=")


def _decode_cursor(cursor: str) -> CursorPayload:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded = urlsafe_b64decode(padded.encode())
        if len(decoded) <= 32:
            raise ValueError
        raw, signature = decoded[:-32], decoded[-32:]
        expected = new(get_settings().session_secret.encode(), raw, sha256).digest()
        if not compare_digest(signature, expected):
            raise ValueError
        payload = loads(raw)
        if not isinstance(payload, dict):
            raise ValueError
        score = float(payload["score"])
        updated_at = datetime.fromisoformat(str(payload["updated_at"]))
        type_order = int(payload["type_order"])
        entity_id = UUID(str(payload["id"]))
        if not 0 <= score <= 1 or type_order not in _TYPE_ORDER.values():
            raise ValueError
        if updated_at.utcoffset() is None:
            raise ValueError
        return {
            "score": score,
            "updated_at": updated_at,
            "type_order": type_order,
            "id": entity_id,
        }
    except (KeyError, ValueError, TypeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="INVALID_CURSOR") from None


def _snippet(value: str | None) -> str:
    if not value:
        return ""
    collapsed = " ".join(value.split())
    return escape(collapsed)[:240]


@router.get("", response_model=SearchResponse)
def search(
    q: Annotated[str, Query(min_length=1, max_length=500)],
    auth: AuthDep,
    session: SessionDep,
    entity_types: EntityTypeFilter = None,
    include_archived: bool = False,
    updated_from: datetime | None = None,
    updated_to: datetime | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> SearchResponse:
    query = _normalize_query(q)
    _validate_range(updated_from, updated_to)
    selected_types = list(entity_types or _TYPE_ORDER)
    cursor_payload = _decode_cursor(cursor) if cursor else None

    sql = text(
        """
        WITH candidates AS (
            SELECT 'task'::text AS entity_type, id AS entity_id, title,
                   description AS body, updated_at, due_at AS timestamp_context,
                   source_type::text, archived_at, pinned,
                   lower(title) AS normalized_title,
                   similarity(lower(title), :query) AS trigram_score,
                   ts_rank_cd(
                     setweight(to_tsvector('simple', coalesce(title, '')), 'A') ||
                     setweight(to_tsvector('simple', coalesce(description, '')), 'B'),
                     plainto_tsquery('simple', :query)
                   ) AS fulltext_score,
                   NULL::uuid AS evidence_id
            FROM tasks
            WHERE workspace_id = :workspace_id

            UNION ALL
            SELECT 'commitment'::text, id, summary, description, updated_at, due_at,
                   'local'::text, archived_at, pinned, lower(summary),
                   similarity(lower(summary), :query),
                   ts_rank_cd(
                     setweight(to_tsvector('simple', coalesce(summary, '')), 'A') ||
                     setweight(to_tsvector('simple', coalesce(description, '')), 'B'),
                     plainto_tsquery('simple', :query)
                   ), evidence_id
            FROM commitments
            WHERE workspace_id = :workspace_id

            UNION ALL
            SELECT 'note'::text, id, coalesce(title, 'Untitled note'), body, updated_at,
                   NULL::timestamptz, source_type::text, archived_at, false,
                   lower(coalesce(title, '')),
                   similarity(lower(coalesce(title, '')), :query),
                   ts_rank_cd(search_document, plainto_tsquery('simple', :query)), NULL::uuid
            FROM notes
            WHERE workspace_id = :workspace_id

            UNION ALL
            SELECT 'meeting'::text, m.id, m.title,
                   concat_ws(' ', m.agenda, m.preparation, m.notes_summary),
                   m.updated_at, coalesce(ce.starts_at, m.standalone_starts_at),
                   'local'::text, m.archived_at, false, lower(m.title),
                   similarity(lower(m.title), :query),
                   ts_rank_cd(
                     setweight(to_tsvector('simple', coalesce(m.title, '')), 'A') ||
                     setweight(to_tsvector('simple', concat_ws(' ', m.agenda, m.preparation,
                       m.notes_summary)), 'B'),
                     plainto_tsquery('simple', :query)
                   ), NULL::uuid
            FROM meetings m
            LEFT JOIN calendar_events ce
              ON ce.workspace_id = m.workspace_id AND ce.id = m.calendar_event_id
            WHERE m.workspace_id = :workspace_id

            UNION ALL
            SELECT 'calendar_event'::text, id, title, concat_ws(' ', description, location),
                   updated_at, starts_at, external_source::text, archived_at, false,
                   lower(title), similarity(lower(title), :query),
                   ts_rank_cd(
                     setweight(to_tsvector('simple', coalesce(title, '')), 'A') ||
                     setweight(to_tsvector('simple', concat_ws(' ', description, location)), 'B'),
                     plainto_tsquery('simple', :query)
                   ), NULL::uuid
            FROM calendar_events
            WHERE workspace_id = :workspace_id

            UNION ALL
            SELECT 'risk'::text, id, left(description, 500),
                   concat_ws(' ', description, mitigation, trigger), updated_at, review_at,
                   'local'::text, archived_at, pinned, lower(description),
                   similarity(lower(description), :query),
                   ts_rank_cd(
                     setweight(to_tsvector('simple', coalesce(description, '')), 'A') ||
                     setweight(to_tsvector('simple', concat_ws(' ', mitigation, trigger)), 'B'),
                     plainto_tsquery('simple', :query)
                   ), NULL::uuid
            FROM risks
            WHERE workspace_id = :workspace_id
        ), ranked AS (
            SELECT *,
              least(1.0,
                CASE
                  WHEN normalized_title = :query THEN 1.00
                  WHEN normalized_title LIKE :query || '%' THEN 0.85
                  ELSE greatest(
                    least(trigram_score, 0.75),
                    least(fulltext_score, 0.70),
                    CASE WHEN lower(coalesce(body, '')) LIKE :query || '%' THEN 0.55 ELSE 0 END
                  )
                END
                + CASE
                    WHEN updated_at >= now() - interval '7 days' THEN 0.12
                    WHEN updated_at >= now() - interval '30 days' THEN 0.06
                    WHEN updated_at >= now() - interval '90 days' THEN 0.02
                    ELSE 0
                  END
                + CASE WHEN pinned THEN 0.08 ELSE 0 END
                - CASE WHEN archived_at IS NOT NULL THEN 0.20 ELSE 0 END
              )::double precision AS score
            FROM candidates
            WHERE entity_type = ANY(CAST(:entity_types AS text[]))
              AND (CAST(:include_archived AS boolean) OR archived_at IS NULL)
              AND (
                CAST(:updated_from AS timestamptz) IS NULL
                OR updated_at >= CAST(:updated_from AS timestamptz)
              )
              AND (
                CAST(:updated_to AS timestamptz) IS NULL
                OR updated_at <= CAST(:updated_to AS timestamptz)
              )
              AND (
                normalized_title = :query
                OR normalized_title LIKE :query || '%'
                OR trigram_score >= 0.15
                OR fulltext_score > 0
                OR lower(coalesce(body, '')) LIKE :query || '%'
              )
        )
        SELECT entity_type, entity_id, title, body, updated_at, timestamp_context,
               source_type, archived_at, normalized_title, trigram_score,
               fulltext_score, score, evidence_id
        FROM ranked
        WHERE (
          CAST(:cursor_score AS double precision) IS NULL
          OR score < CAST(:cursor_score AS double precision)
          OR (
            score = CAST(:cursor_score AS double precision)
            AND updated_at < CAST(:cursor_updated_at AS timestamptz)
          )
          OR (
            score = CAST(:cursor_score AS double precision)
            AND updated_at = CAST(:cursor_updated_at AS timestamptz)
            AND CASE entity_type
                  WHEN 'task' THEN 1 WHEN 'commitment' THEN 2 WHEN 'meeting' THEN 3
                  WHEN 'note' THEN 4 WHEN 'risk' THEN 5 ELSE 6
                END > CAST(:cursor_type_order AS integer)
          )
          OR (
            score = CAST(:cursor_score AS double precision)
            AND updated_at = CAST(:cursor_updated_at AS timestamptz)
            AND CASE entity_type
                  WHEN 'task' THEN 1 WHEN 'commitment' THEN 2 WHEN 'meeting' THEN 3
                  WHEN 'note' THEN 4 WHEN 'risk' THEN 5 ELSE 6
                END = CAST(:cursor_type_order AS integer)
            AND entity_id > CAST(:cursor_id AS uuid)
          )
        )
        ORDER BY score DESC, updated_at DESC,
          CASE entity_type
            WHEN 'task' THEN 1 WHEN 'commitment' THEN 2 WHEN 'meeting' THEN 3
            WHEN 'note' THEN 4 WHEN 'risk' THEN 5 ELSE 6
          END ASC,
          entity_id ASC
        LIMIT :fetch_limit
        """
    )

    params = {
        "workspace_id": auth.workspace_id,
        "query": query,
        "entity_types": selected_types,
        "include_archived": include_archived,
        "updated_from": updated_from,
        "updated_to": updated_to,
        "cursor_score": cursor_payload["score"] if cursor_payload else None,
        "cursor_updated_at": cursor_payload["updated_at"] if cursor_payload else None,
        "cursor_type_order": cursor_payload["type_order"] if cursor_payload else None,
        "cursor_id": cursor_payload["id"] if cursor_payload else None,
        "fetch_limit": limit + 1,
    }
    rows = session.execute(sql, params).mappings().all()
    page = rows[:limit]

    items: list[SearchResult] = []
    for row in page:
        matched_fields: list[str] = []
        if row["normalized_title"] == query:
            matched_fields.append("title_exact")
        elif row["normalized_title"].startswith(query):
            matched_fields.append("title_prefix")
        if row["trigram_score"] >= 0.15:
            matched_fields.append("title_trigram")
        if row["fulltext_score"] > 0:
            matched_fields.append("full_text")
        evidence_refs = [row["evidence_id"]] if row["evidence_id"] is not None else []
        items.append(
            SearchResult(
                entity_type=row["entity_type"],
                entity_id=row["entity_id"],
                title=row["title"],
                snippet=_snippet(row["body"]),
                matched_fields=matched_fields,
                score=round(float(row["score"]), 6),
                score_components={
                    "trigram": round(min(float(row["trigram_score"]), 0.75), 6),
                    "full_text": round(min(float(row["fulltext_score"]), 0.70), 6),
                },
                updated_at=row["updated_at"],
                timestamp_context=row["timestamp_context"],
                source_type=row["source_type"],
                archived=row["archived_at"] is not None,
                evidence_refs=evidence_refs,
            )
        )

    next_cursor = None
    if len(rows) > limit and page:
        last = page[-1]
        next_cursor = _sign_cursor(
            {
                "score": float(last["score"]),
                "updated_at": last["updated_at"].isoformat(),
                "type_order": _TYPE_ORDER[last["entity_type"]],
                "id": str(last["entity_id"]),
            }
        )
    return SearchResponse(items=items, next_cursor=next_cursor)
