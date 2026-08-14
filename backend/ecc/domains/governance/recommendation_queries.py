from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthDep
from ecc.database import get_session
from ecc.domains.governance.recommendation_models import (
    RecommendationListResponse,
    RecommendationResponse,
    RecommendationStatus,
)
from ecc.domains.governance.recommendation_storage import FIELDS, expire_if_needed, get_row, project
from ecc.platform import authz, cursor_pagination

router = APIRouter(prefix="/api/v1/recommendations", tags=["recommendations"])
SessionDep = Annotated[Session, Depends(get_session)]
StatusQuery = Annotated[list[RecommendationStatus] | None, Query(alias="status")]
LimitQuery = Annotated[int, Query(ge=1, le=100)]


def _encode_cursor(created_at: datetime, recommendation_id: UUID) -> str:
    return cursor_pagination.encode_cursor(
        {"created_at": created_at.isoformat(), "id": str(recommendation_id)}
    )


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    decoded = cursor_pagination.decode_cursor(cursor)
    try:
        return datetime.fromisoformat(decoded["created_at"]), UUID(decoded["id"])
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="MALFORMED_CURSOR") from exc


@router.get("", response_model=RecommendationListResponse)
def list_recommendations(
    auth: AuthDep,
    session: SessionDep,
    statuses: StatusQuery = None,
    include_archived: bool = False,
    cursor: str | None = None,
    limit: LimitQuery = 20,
    recommendation_type: str | None = None,
) -> RecommendationListResponse:
    # `recommendation_type` (Phase 10 Task 8 Loop 2 round 5 review): a
    # domain-scoped embed of `RecommendationPanel` (e.g. `GmailPanel`
    # filtering to `email_action_detected`) was previously filtering the
    # already-paginated page client-side -- in a workspace with 20+ newer
    # recommendations of *other* types, a genuine, unconfirmed item of the
    # embedded type would never enter the fetched page at all, silently
    # hiding it from the one view whose whole purpose is surfacing it for
    # human confirmation. Filtering server-side, before `LIMIT`, closes
    # that gap regardless of how many other-typed recommendations exist.
    cursor_created: datetime | None = None
    cursor_id: UUID | None = None
    if cursor:
        cursor_created, cursor_id = _decode_cursor(cursor)
    visibility_sql, visibility_params = authz.visible_resource_filter_sql(
        session, auth, resource_type="recommendations", action="read", table_alias="recommendations"
    )
    rows = (
        session.execute(
            text(
                f"""
                SELECT {FIELDS} FROM recommendations
                WHERE workspace_id=:workspace_id
                  AND ({visibility_sql})
                  AND (:include_archived OR archived_at IS NULL)
                  AND (
                    CAST(:statuses AS text[]) IS NULL
                    OR status=ANY(CAST(:statuses AS text[]))
                  )
                  AND (
                    CAST(:recommendation_type AS text) IS NULL
                    OR recommendation_type=CAST(:recommendation_type AS text)
                  )
                  AND (
                    CAST(:cursor_created AS timestamptz) IS NULL
                    OR (created_at,id)<(
                        CAST(:cursor_created AS timestamptz),
                        CAST(:cursor_id AS uuid)
                    )
                  )
                ORDER BY created_at DESC,id DESC
                LIMIT :fetch_limit
                """
            ),
            {
                "workspace_id": auth.workspace_id,
                "include_archived": include_archived,
                "statuses": statuses,
                "recommendation_type": recommendation_type,
                "cursor_created": cursor_created,
                "cursor_id": cursor_id,
                "fetch_limit": limit + 1,
                **visibility_params,
            },
        )
        .mappings()
        .all()
    )
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
    request: Request,
    auth: AuthDep,
    session: SessionDep,
) -> RecommendationResponse:
    visible = authz.authorize(
        session, auth, resource_type="recommendations", resource_id=recommendation_id, action="read"
    )
    session.rollback()
    if not visible:
        raise HTTPException(status_code=404, detail="RECOMMENDATION_NOT_FOUND")
    row = expire_if_needed(
        session, auth, get_row(session, auth, recommendation_id), request=request
    )
    session.commit()
    return project(row)
