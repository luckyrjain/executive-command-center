from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import datetime
from hmac import compare_digest, new
from json import dumps, loads
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthDep
from ecc.config import get_settings
from ecc.database import get_session

router = APIRouter(prefix="/api/v1/audit", tags=["audit"])
SessionDep = Annotated[Session, Depends(get_session)]


class AuditEventResponse(BaseModel):
    id: UUID
    event_type: str
    aggregate_type: str
    aggregate_id: UUID
    aggregate_version: int
    actor_id: UUID | None
    request_id: UUID
    correlation_id: UUID
    idempotency_key_hash: str | None
    before: dict[str, Any] | None
    after: dict[str, Any] | None
    changed_fields: list[str]
    authorization_result: str
    source: str
    failure_code: str | None
    metadata: dict[str, Any]
    occurred_at: datetime


class AuditListResponse(BaseModel):
    items: list[AuditEventResponse]
    next_cursor: str | None


def _sign(payload: dict[str, str]) -> str:
    body = urlsafe_b64encode(dumps(payload, separators=(",", ":")).encode()).decode()
    signature = new(
        get_settings().session_secret.encode(),
        body.encode(),
        "sha256",
    ).hexdigest()
    return f"{body}.{signature}"


def _decode(cursor: str) -> tuple[datetime, UUID]:
    try:
        body, signature = cursor.rsplit(".", 1)
        expected = new(
            get_settings().session_secret.encode(),
            body.encode(),
            "sha256",
        ).hexdigest()
        if not compare_digest(signature, expected):
            raise ValueError
        payload = loads(urlsafe_b64decode(body.encode()).decode())
        return datetime.fromisoformat(payload["occurred_at"]), UUID(payload["id"])
    except (ValueError, KeyError, TypeError):
        raise HTTPException(status_code=400, detail="INVALID_CURSOR") from None


@router.get("", response_model=AuditListResponse)
def list_audit_events(
    auth: AuthDep,
    session: SessionDep,
    aggregate_type: str | None = None,
    aggregate_id: UUID | None = None,
    actor_id: UUID | None = None,
    event_type: str | None = None,
    occurred_from: datetime | None = None,
    occurred_to: datetime | None = None,
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
) -> AuditListResponse:
    conditions = ["workspace_id = :workspace_id"]
    params: dict[str, Any] = {"workspace_id": auth.workspace_id, "limit": limit + 1}

    for column, value in (
        ("aggregate_type", aggregate_type),
        ("aggregate_id", aggregate_id),
        ("actor_id", actor_id),
        ("event_type", event_type),
    ):
        if value is not None:
            conditions.append(f"{column} = :{column}")
            params[column] = value
    if occurred_from is not None:
        conditions.append("occurred_at >= :occurred_from")
        params["occurred_from"] = occurred_from
    if occurred_to is not None:
        conditions.append("occurred_at <= :occurred_to")
        params["occurred_to"] = occurred_to
    if cursor is not None:
        cursor_time, cursor_id = _decode(cursor)
        conditions.append("(occurred_at, id) < (:cursor_time, :cursor_id)")
        params.update(cursor_time=cursor_time, cursor_id=cursor_id)

    rows = (
        session.execute(
            text(
                f"""
                SELECT id, event_type, aggregate_type, aggregate_id, aggregate_version,
                       actor_id, request_id, correlation_id, idempotency_key_hash,
                       before, after, changed_fields, authorization_result, source,
                       failure_code, metadata, occurred_at
                FROM audit_events
                WHERE {" AND ".join(conditions)}
                ORDER BY occurred_at DESC, id DESC
                LIMIT :limit
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    has_more = len(rows) > limit
    visible = rows[:limit]
    next_cursor = None
    if has_more and visible:
        tail = visible[-1]
        next_cursor = _sign({"occurred_at": tail["occurred_at"].isoformat(), "id": str(tail["id"])})
    return AuditListResponse(
        items=[AuditEventResponse.model_validate(dict(row)) for row in visible],
        next_cursor=next_cursor,
    )
