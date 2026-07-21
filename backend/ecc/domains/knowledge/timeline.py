from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from datetime import datetime
from hmac import compare_digest, new
from json import dumps, loads
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthDep
from ecc.config import get_settings
from ecc.database import get_session

router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge-timeline"])
SessionDep = Annotated[Session, Depends(get_session)]

_TIMELINE_FIELDS = "id, entity_id, effective_at, recorded_at, event_type, source_id, summary"


class TimelineEntryResponse(BaseModel):
    id: UUID
    entity_id: UUID
    effective_at: datetime
    recorded_at: datetime
    event_type: str
    source_id: UUID | None
    summary: str


class TimelineListResponse(BaseModel):
    items: list[TimelineEntryResponse]
    next_cursor: str | None = None


def _project(row: dict[str, Any]) -> TimelineEntryResponse:
    return TimelineEntryResponse(**{key: row[key] for key in TimelineEntryResponse.model_fields})


def queue_timeline_entry(
    session: Session,
    workspace_id: UUID,
    entity_id: UUID,
    event_type: str,
    summary: str,
    now: datetime,
    source_id: UUID | None = None,
    effective_at: datetime | None = None,
) -> None:
    """Insert one timeline_entries row within the caller's own transaction.

    Timeline entries never need the deferred-until-commit pattern
    observability.py's queue_* metric helpers use: a rolled-back mutation
    rolls back this insert along with it automatically, since it shares the
    same transaction, not a separate one -- there is no double-count risk to
    guard against here the way there is for process-lifetime metric
    counters that live outside the database transaction entirely."""
    session.execute(
        text(
            """
            INSERT INTO timeline_entries (
                id, workspace_id, entity_id, effective_at, recorded_at,
                event_type, source_id, summary
            ) VALUES (
                :id, :workspace_id, :entity_id, :effective_at, :recorded_at,
                :event_type, :source_id, :summary
            )
            """
        ),
        {
            "id": uuid4(),
            "workspace_id": workspace_id,
            "entity_id": entity_id,
            "effective_at": effective_at or now,
            "recorded_at": now,
            "event_type": event_type,
            "source_id": source_id,
            "summary": summary,
        },
    )


def _encode_cursor(effective_at: datetime, entry_id: UUID) -> str:
    payload = dumps({"effective_at": effective_at.isoformat(), "id": str(entry_id)}).encode()
    signature = new(get_settings().session_secret.encode(), payload, "sha256").hexdigest().encode()
    return urlsafe_b64encode(payload + b"." + signature).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        raw = urlsafe_b64decode((cursor + "=" * (-len(cursor) % 4)).encode())
        payload, signature = raw.rsplit(b".", 1)
        expected = new(get_settings().session_secret.encode(), payload, "sha256").hexdigest()
        if not compare_digest(signature.decode(), expected):
            raise ValueError
        decoded = loads(payload)
        return datetime.fromisoformat(decoded["effective_at"]), UUID(decoded["id"])
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="MALFORMED_CURSOR") from exc


@router.get("/entities/{entity_id}/timeline", response_model=TimelineListResponse)
def get_timeline(
    entity_id: UUID,
    auth: AuthDep,
    session: SessionDep,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> TimelineListResponse:
    clauses = ["workspace_id = :workspace_id", "entity_id = :entity_id"]
    params: dict[str, Any] = {
        "workspace_id": auth.workspace_id,
        "entity_id": entity_id,
        "limit": limit + 1,
    }
    if cursor is not None:
        effective_at, cursor_id = _decode_cursor(cursor)
        clauses.append("(effective_at, id) < (:cursor_effective_at, :cursor_id)")
        params.update({"cursor_effective_at": effective_at, "cursor_id": cursor_id})
    rows = (
        session.execute(
            text(
                f"""
                SELECT {_TIMELINE_FIELDS}
                FROM timeline_entries
                WHERE {" AND ".join(clauses)}
                ORDER BY effective_at DESC, recorded_at DESC, id DESC
                LIMIT :limit
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit and page:
        last = page[-1]
        next_cursor = _encode_cursor(last["effective_at"], last["id"])
    return TimelineListResponse(
        items=[_project(dict(row)) for row in page],
        next_cursor=next_cursor,
    )


@dataclass(frozen=True)
class RebuildReport:
    workspace_id: UUID
    entries_written: int


def rebuild_timeline(session: Session, workspace_id: UUID) -> RebuildReport:
    """Deterministically regenerate timeline_entries for a workspace from
    audit_events -- the append-only, already-authoritative historical record
    of every knowledge_entity/relationship mutation -- rather than deriving
    from current-state tables like pkos_nodes, which only ever hold an
    entity's *current* status, not its transition history.

    Deliberately delete-then-reinsert (matching DATA-MODEL.md's "rebuildable
    projections may be regenerated" framing), not merge: this table has no
    other writer once this function is called for a workspace, so a stale
    row is never left behind by an intervening change. Row ids are derived
    fresh per call (not tied to the live queue_timeline_entry() ids, which
    is fine -- ids are internal keys here, not stable content the client
    contract exposes across a rebuild) but the *set of rows produced* is
    fully deterministic for fixed inputs, which is what the rebuild-
    determinism test in tests/test_rebuild_knowledge_projections.py proves.
    """
    session.execute(
        text("DELETE FROM timeline_entries WHERE workspace_id = :workspace_id"),
        {"workspace_id": workspace_id},
    )
    entity_events = session.execute(
        text(
            """
            SELECT id, aggregate_id AS entity_id, event_type, occurred_at
            FROM audit_events
            WHERE workspace_id = :workspace_id AND aggregate_type = 'knowledge_entity'
            """
        ),
        {"workspace_id": workspace_id},
    ).all()
    relationship_events = session.execute(
        text(
            """
            SELECT a.id, e.source_node_id AS entity_id, a.event_type, a.occurred_at
            FROM audit_events a
            JOIN pkos_edges e ON e.workspace_id = a.workspace_id AND e.id = a.aggregate_id
            WHERE a.workspace_id = :workspace_id AND a.aggregate_type = 'relationship'
            """
        ),
        {"workspace_id": workspace_id},
    ).all()
    written = 0
    for event_id, entity_id, event_type, occurred_at in (*entity_events, *relationship_events):
        session.execute(
            text(
                """
                INSERT INTO timeline_entries (
                    id, workspace_id, entity_id, effective_at, recorded_at,
                    event_type, source_id, summary
                ) VALUES (
                    :id, :workspace_id, :entity_id, :effective_at, :recorded_at,
                    :event_type, NULL, :summary
                )
                """
            ),
            {
                "id": event_id,
                "workspace_id": workspace_id,
                "entity_id": entity_id,
                "effective_at": occurred_at,
                "recorded_at": occurred_at,
                "event_type": event_type,
                "summary": event_type.replace("_", " ").replace(".", " "),
            },
        )
        written += 1
    return RebuildReport(workspace_id=workspace_id, entries_written=written)
