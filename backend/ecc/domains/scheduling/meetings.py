from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import compare_digest, new
from json import dumps, loads
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.config import get_settings
from ecc.database import get_session

router = APIRouter(prefix="/api/v1/meetings", tags=["meetings"])

MeetingStatus = Literal["planned", "in_progress", "completed", "cancelled"]
SessionDep = Annotated[Session, Depends(get_session)]
MeetingStatusFilter = Annotated[MeetingStatus | None, Query(alias="status")]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]

_MEETING_FIELDS = """
id, calendar_event_id, title, standalone_starts_at, standalone_ends_at,
standalone_timezone, status, agenda, preparation, notes_summary, created_at,
updated_at, version, archived_at, pre_archive_status
"""


class MeetingCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    calendar_event_id: UUID | None = None
    title: str = Field(min_length=1, max_length=500)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    timezone: str | None = Field(default=None, max_length=128)
    status: MeetingStatus = "planned"
    agenda: str | None = None
    preparation: str | None = None
    notes_summary: str | None = None

    @model_validator(mode="after")
    def validate_timing(self) -> MeetingCreate:
        if self.calendar_event_id is not None:
            if any(value is not None for value in (self.starts_at, self.ends_at, self.timezone)):
                raise ValueError("linked meetings derive timing from the calendar event")
            return self
        if self.starts_at is None or self.ends_at is None or self.timezone is None:
            raise ValueError("standalone meetings require starts_at, ends_at and timezone")
        _validate_aware(self.starts_at, "starts_at")
        _validate_aware(self.ends_at, "ends_at")
        _validate_timezone(self.timezone)
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        return self


class MeetingPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=500)
    status: MeetingStatus | None = None
    agenda: str | None = None
    preparation: str | None = None
    notes_summary: str | None = None

    @model_validator(mode="after")
    def reject_null_required_fields(self) -> MeetingPatch:
        for field in ("title", "status"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class MeetingAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


class MeetingResponse(BaseModel):
    id: UUID
    calendar_event_id: UUID | None
    title: str
    starts_at: datetime
    ends_at: datetime
    timezone: str
    status: MeetingStatus
    agenda: str | None
    preparation: str | None
    notes_summary: str | None
    created_at: datetime
    updated_at: datetime
    version: int
    archived_at: datetime | None
    pre_archive_status: str | None


class MeetingListResponse(BaseModel):
    items: list[MeetingResponse]
    next_cursor: str | None = None
    next_cursor: str | None = None


def _encode_cursor(updated_at: datetime, meeting_id: UUID) -> str:
    payload = dumps({"updated_at": updated_at.isoformat(), "id": str(meeting_id)}).encode()
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
        return datetime.fromisoformat(decoded["updated_at"]), UUID(decoded["id"])
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="MALFORMED_CURSOR") from exc


def _validate_aware(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone offset")


def _validate_timezone(value: str) -> None:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone must be a valid IANA timezone") from exc


def _request_ids(request: Request) -> tuple[UUID, UUID]:
    try:
        return UUID(request.state.request_id), UUID(request.state.correlation_id)
    except (AttributeError, TypeError, ValueError):
        return uuid4(), uuid4()


def _request_hash(payload: BaseModel, action: str) -> str:
    material = {"action": action, "payload": payload.model_dump(mode="json")}
    return sha256(dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _lock_idempotency(session: Session, auth: AuthContext, key: str) -> None:
    lock_key = f"{auth.workspace_id}:{auth.user_id}:{key}"
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": lock_key},
    )


def _load_cached(
    session: Session,
    auth: AuthContext,
    key: str,
    request_hash: str,
) -> MeetingResponse | None:
    row = (
        session.execute(
            text(
                """
                SELECT request_hash, response_body
                FROM idempotency_records
                WHERE workspace_id = :workspace_id
                  AND actor_id = :actor_id
                  AND key = :key
                  AND expires_at > :now
                """
            ),
            {
                "workspace_id": auth.workspace_id,
                "actor_id": auth.user_id,
                "key": key,
                "now": datetime.now(UTC),
            },
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    if row["request_hash"] != request_hash:
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
    return MeetingResponse.model_validate(row["response_body"])


def _store_cached(
    session: Session,
    auth: AuthContext,
    key: str,
    request_hash: str,
    response: MeetingResponse,
    response_status: int,
    now: datetime,
) -> None:
    session.execute(
        text(
            """
            INSERT INTO idempotency_records (
                workspace_id, actor_id, key, request_hash, response_status,
                response_body, created_at, expires_at
            ) VALUES (
                :workspace_id, :actor_id, :key, :request_hash, :response_status,
                CAST(:response_body AS jsonb), :created_at, :expires_at
            )
            """
        ),
        {
            "workspace_id": auth.workspace_id,
            "actor_id": auth.user_id,
            "key": key,
            "request_hash": request_hash,
            "response_status": response_status,
            "response_body": dumps(response.model_dump(mode="json")),
            "created_at": now,
            "expires_at": now + timedelta(days=365),
        },
    )


def _write_audit_and_outbox(
    session: Session,
    auth: AuthContext,
    request: Request,
    event_type: str,
    meeting_id: UUID,
    version: int,
    changed_fields: list[str],
    now: datetime,
) -> None:
    request_id, correlation_id = _request_ids(request)
    session.execute(
        text(
            """
            INSERT INTO audit_events (
                id, workspace_id, event_type, aggregate_type, aggregate_id,
                aggregate_version, actor_id, request_id, correlation_id,
                changed_fields, authorization_result, source, metadata, occurred_at
            ) VALUES (
                :id, :workspace_id, :event_type, 'meeting', :aggregate_id,
                :aggregate_version, :actor_id, :request_id, :correlation_id,
                :changed_fields, 'allowed', 'user', '{}'::jsonb, :occurred_at
            )
            """
        ),
        {
            "id": uuid4(),
            "workspace_id": auth.workspace_id,
            "event_type": event_type,
            "aggregate_id": meeting_id,
            "aggregate_version": version,
            "actor_id": auth.user_id,
            "request_id": request_id,
            "correlation_id": correlation_id,
            "changed_fields": changed_fields,
            "occurred_at": now,
        },
    )
    session.execute(
        text(
            """
            INSERT INTO event_outbox (
                event_id, workspace_id, event_type, event_version,
                correlation_id, payload, occurred_at, attempt_count
            ) VALUES (
                :event_id, :workspace_id, :event_type, 1,
                :correlation_id, CAST(:payload AS jsonb), :occurred_at, 0
            )
            """
        ),
        {
            "event_id": uuid4(),
            "workspace_id": auth.workspace_id,
            "event_type": f"{event_type}.v1",
            "correlation_id": correlation_id,
            "payload": dumps({"meeting_id": str(meeting_id), "version": version}),
            "occurred_at": now,
        },
    )


def _get_row(
    session: Session,
    auth: AuthContext,
    meeting_id: UUID,
    *,
    for_update: bool = False,
) -> dict[str, Any] | None:
    suffix = " FOR UPDATE" if for_update else ""
    row = (
        session.execute(
            text(
                f"""
                SELECT {_MEETING_FIELDS}
                FROM meetings
                WHERE workspace_id = :workspace_id AND id = :meeting_id
                {suffix}
                """
            ),
            {"workspace_id": auth.workspace_id, "meeting_id": meeting_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _calendar_event(
    session: Session,
    auth: AuthContext,
    event_id: UUID,
) -> dict[str, Any] | None:
    row = (
        session.execute(
            text(
                """
                SELECT id, starts_at, ends_at, timezone, archived_at
                FROM calendar_events
                WHERE workspace_id = :workspace_id AND id = :event_id
                """
            ),
            {"workspace_id": auth.workspace_id, "event_id": event_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _project(session: Session, auth: AuthContext, row: dict[str, Any]) -> MeetingResponse:
    if row["calendar_event_id"] is None:
        starts_at = row["standalone_starts_at"]
        ends_at = row["standalone_ends_at"]
        timezone = row["standalone_timezone"]
    else:
        event = _calendar_event(session, auth, row["calendar_event_id"])
        if event is None:
            raise HTTPException(status_code=409, detail="LINKED_CALENDAR_EVENT_MISSING")
        starts_at = event["starts_at"]
        ends_at = event["ends_at"]
        timezone = event["timezone"]
    return MeetingResponse(
        id=row["id"],
        calendar_event_id=row["calendar_event_id"],
        title=row["title"],
        starts_at=starts_at,
        ends_at=ends_at,
        timezone=timezone,
        status=row["status"],
        agenda=row["agenda"],
        preparation=row["preparation"],
        notes_summary=row["notes_summary"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        version=row["version"],
        archived_at=row["archived_at"],
        pre_archive_status=row["pre_archive_status"],
    )


@router.post("", response_model=MeetingResponse, status_code=status.HTTP_201_CREATED)
def create_meeting(
    payload: MeetingCreate,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> MeetingResponse:
    request_hash = _request_hash(payload, "create")
    now = datetime.now(UTC)
    meeting_id = uuid4()
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached
        if payload.calendar_event_id is not None:
            link_key = f"{auth.workspace_id}:calendar-meeting:{payload.calendar_event_id}"
            session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:link_key, 0))"),
                {"link_key": link_key},
            )
            event = _calendar_event(session, auth, payload.calendar_event_id)
            if event is None or event["archived_at"] is not None:
                raise HTTPException(status_code=404, detail="CALENDAR_EVENT_NOT_FOUND")
            existing = session.execute(
                text(
                    """
                    SELECT id FROM meetings
                    WHERE workspace_id = :workspace_id
                      AND calendar_event_id = :calendar_event_id
                    """
                ),
                {
                    "workspace_id": auth.workspace_id,
                    "calendar_event_id": payload.calendar_event_id,
                },
            ).scalar_one_or_none()
            if existing is not None:
                raise HTTPException(
                    status_code=409,
                    detail="CALENDAR_EVENT_ALREADY_LINKED",
                )
        row = (
            session.execute(
                text(
                    f"""
                    INSERT INTO meetings (
                        id, workspace_id, calendar_event_id, title,
                        standalone_starts_at, standalone_ends_at, standalone_timezone,
                        status, agenda, preparation, notes_summary, created_by,
                        updated_by, created_at, updated_at, version
                    ) VALUES (
                        :id, :workspace_id, :calendar_event_id, :title,
                        :starts_at, :ends_at, :timezone, :status, :agenda,
                        :preparation, :notes_summary, :actor_id, :actor_id,
                        :now, :now, 1
                    ) RETURNING {_MEETING_FIELDS}
                    """
                ),
                {
                    "id": meeting_id,
                    "workspace_id": auth.workspace_id,
                    "actor_id": auth.user_id,
                    "now": now,
                    **payload.model_dump(),
                },
            )
            .mappings()
            .one()
        )
        response = _project(session, auth, dict(row))
        _write_audit_and_outbox(
            session, auth, request, "meeting.created", meeting_id, 1, ["*"], now
        )
        _store_cached(session, auth, idempotency_key, request_hash, response, 201, now)
        return response


@router.get("", response_model=MeetingListResponse)
def list_meetings(
    auth: AuthDep,
    session: SessionDep,
    status_filter: MeetingStatusFilter = None,
    include_archived: bool = False,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> MeetingListResponse:
    clauses = ["workspace_id = :workspace_id"]
    params: dict[str, Any] = {"workspace_id": auth.workspace_id, "limit": limit + 1}
    if not include_archived:
        clauses.append("archived_at IS NULL")
    if status_filter is not None:
        clauses.append("status = :status")
        params["status"] = status_filter
    if cursor:
        updated_at, meeting_id = _decode_cursor(cursor)
        clauses.append("(updated_at, id) < (:cursor_updated_at, :cursor_id)")
        params.update({"cursor_updated_at": updated_at, "cursor_id": meeting_id})
    if cursor:
        updated_at, meeting_id = _decode_cursor(cursor)
        clauses.append("(updated_at, id) < (:cursor_updated_at, :cursor_id)")
        params.update({"cursor_updated_at": updated_at, "cursor_id": meeting_id})
    rows = (
        session.execute(
            text(
                f"""
                SELECT {_MEETING_FIELDS}
                FROM meetings
                WHERE {" AND ".join(clauses)}
                ORDER BY updated_at DESC, id DESC
                LIMIT :limit
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    return MeetingListResponse(items=[_project(session, auth, dict(row)) for row in rows])


@router.get("/{meeting_id}", response_model=MeetingResponse)
def get_meeting(meeting_id: UUID, auth: AuthDep, session: SessionDep) -> MeetingResponse:
    row = _get_row(session, auth, meeting_id)
    if row is None:
        raise HTTPException(status_code=404, detail="MEETING_NOT_FOUND")
    return _project(session, auth, row)


@router.patch("/{meeting_id}", response_model=MeetingResponse)
def update_meeting(
    meeting_id: UUID,
    payload: MeetingPatch,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> MeetingResponse:
    request_hash = _request_hash(payload, f"update:{meeting_id}")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached
        current = _get_row(session, auth, meeting_id, for_update=True)
        if current is None:
            raise HTTPException(status_code=404, detail="MEETING_NOT_FOUND")
        if current["version"] != payload.expected_version:
            raise HTTPException(
                status_code=409,
                detail={"code": "VERSION_CONFLICT", "current_version": current["version"]},
            )
        if current["archived_at"] is not None:
            raise HTTPException(status_code=409, detail="MEETING_ARCHIVED")
        if current["status"] in {"completed", "cancelled"} and payload.status not in {
            None,
            current["status"],
        }:
            raise HTTPException(status_code=409, detail="INVALID_MEETING_TRANSITION")
        fields = payload.model_fields_set - {"expected_version"}
        if not fields:
            response = _project(session, auth, current)
            _store_cached(session, auth, idempotency_key, request_hash, response, 200, now)
            return response
        assignments = [f"{field} = :{field}" for field in sorted(fields)]
        assignments.extend(["updated_by = :actor_id", "updated_at = :now", "version = version + 1"])
        values = payload.model_dump(include=fields)
        values.update(
            {
                "workspace_id": auth.workspace_id,
                "meeting_id": meeting_id,
                "actor_id": auth.user_id,
                "now": now,
            }
        )
        row = (
            session.execute(
                text(
                    f"""
                    UPDATE meetings
                    SET {", ".join(assignments)}
                    WHERE workspace_id = :workspace_id AND id = :meeting_id
                    RETURNING {_MEETING_FIELDS}
                    """
                ),
                values,
            )
            .mappings()
            .one()
        )
        response = _project(session, auth, dict(row))
        _write_audit_and_outbox(
            session,
            auth,
            request,
            "meeting.updated",
            meeting_id,
            response.version,
            sorted(fields),
            now,
        )
        _store_cached(session, auth, idempotency_key, request_hash, response, 200, now)
        return response


def _lifecycle(
    meeting_id: UUID,
    payload: MeetingAction,
    request: Request,
    auth: AuthContext,
    session: Session,
    idempotency_key: str,
    action: Literal["archive", "restore"],
) -> MeetingResponse:
    request_hash = _request_hash(payload, f"{action}:{meeting_id}")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached
        current = _get_row(session, auth, meeting_id, for_update=True)
        if current is None:
            raise HTTPException(status_code=404, detail="MEETING_NOT_FOUND")
        if current["version"] != payload.expected_version:
            raise HTTPException(
                status_code=409,
                detail={"code": "VERSION_CONFLICT", "current_version": current["version"]},
            )
        if action == "restore" and current["archived_at"] is None:
            raise HTTPException(status_code=409, detail="MEETING_NOT_ARCHIVED")
        if action == "archive" and current["archived_at"] is not None:
            response = _project(session, auth, current)
            _store_cached(session, auth, idempotency_key, request_hash, response, 200, now)
            return response
        if action == "archive":
            assignments = "archived_at = :now, pre_archive_status = status"
            event_type = "meeting.archived"
        else:
            assignments = "archived_at = NULL, pre_archive_status = NULL"
            event_type = "meeting.restored"
        row = (
            session.execute(
                text(
                    f"""
                    UPDATE meetings
                    SET {assignments}, updated_by = :actor_id,
                        updated_at = :now, version = version + 1
                    WHERE workspace_id = :workspace_id AND id = :meeting_id
                    RETURNING {_MEETING_FIELDS}
                    """
                ),
                {
                    "workspace_id": auth.workspace_id,
                    "meeting_id": meeting_id,
                    "actor_id": auth.user_id,
                    "now": now,
                },
            )
            .mappings()
            .one()
        )
        response = _project(session, auth, dict(row))
        _write_audit_and_outbox(
            session,
            auth,
            request,
            event_type,
            meeting_id,
            response.version,
            ["archived_at", "pre_archive_status"],
            now,
        )
        _store_cached(session, auth, idempotency_key, request_hash, response, 200, now)
        return response


@router.post("/{meeting_id}/archive", response_model=MeetingResponse)
def archive_meeting(
    meeting_id: UUID,
    payload: MeetingAction,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> MeetingResponse:
    return _lifecycle(meeting_id, payload, request, auth, session, idempotency_key, "archive")


@router.post("/{meeting_id}/restore", response_model=MeetingResponse)
def restore_meeting(
    meeting_id: UUID,
    payload: MeetingAction,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> MeetingResponse:
    return _lifecycle(meeting_id, payload, request, auth, session, idempotency_key, "restore")
