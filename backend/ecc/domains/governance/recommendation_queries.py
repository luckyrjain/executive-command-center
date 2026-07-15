from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import datetime
from hmac import compare_digest, new
from json import dumps, loads
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthDep
from ecc.config import get_settings
from ecc.database import get_session
from ecc.domains.governance.recommendation_models import (
    RecommendationListResponse,
    RecommendationResponse,
    RecommendationStatus,
)
from ecc.domains.governance.recommendation_storage import FIELDS, expire_if_needed, get_row, project

router = APIRouter(prefix="/api/v1/recommendations", tags=["recommendations"])
SessionDep = Annotated[Session, Depends(get_session)]


def _encode_cursor(created_at: datetime, recommendation_id: UUID) -> str:
    payload = dumps({"created_at": created_at.isoformat(), "id": str(recommendation_id)}).encode()
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
        return datetime.fromisoformat(decoded["created_at"]), UUID(decoded["id"])
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="MALFORMED_CURSOR") from exc


@router.get("", response_model=RecommendationListResponse)
def list_recommendations(
    auth: AuthDep,
    session: SessionDep,
    statuses: list[RecommendationStatus] | None = Query(default=None, alias="status"),
    include_archived: bool = False,
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
) -> RecommendationListResponse:
    cursor_created: datetime | None = None
    cursor_id: UUID | None = None
    if cursor:
        cursor_created, cursor_id = _decode_cursor(cursor)
    rows = session.execute(
        text(
            f"""
            SELECT {FIELDS} FROM recommendations
            WHERE workspace_id=:workspace_id
              AND (:include_archived OR archived_at IS NULL)
              AND (CAST(:statuses AS text[]) IS NULL OR status=ANY(CAST(:statuses AS text[])))
              AND (:cursor_created IS NULL OR (created_at,id)<(:cursor_created,:cursor_id))
            ORDER BY created_at DESC,id DESC
            LIMIT :fetch_limit
            """
        ),
        {
            "workspace_id": auth.workspace_id,
            "include_archived": include_archived,
            "statuses": statuses,
            "cursor_created": cursor_created,
            "cursor_id": cursor_id,
            "fetch_limit": limit + 1,
        },
    ).mappings().all()
    session.rollback()
    items = [project(dict(row)) for row in rows[:limit]]
    next_cursor = None
    if len(rows) > limit and items:
        last = items[-1]
        next_cursor = _encode_cursor(last.created_at, last.id)
    return RecommendationListResponse(items=items, next_cursor=next_cursor)


@router.get("/{recommendation_id}", response_model=RecommendationResponse)
def get_recommendation(
    recommendation_id: UUID,
    auth: AuthDep,
    session: SessionDep,
) -> RecommendationResponse:
    row = expire_if_needed(session, auth, get_row(session, auth, recommendation_id))
    session.commit()
    return project(row)
