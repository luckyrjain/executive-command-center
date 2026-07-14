from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import UTC, date, datetime, time, timedelta
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

router = APIRouter(prefix="/api/v1/calendar/events", tags=["calendar-events"])

EventStatus = Literal["confirmed", "tentative", "cancelled"]
SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]

_SELECT_FIELDS = """
id, title, starts_at, ends_at, all_day, timezone, location, description,
status, external_source, external_id, source_authoritative, created_at,
updated_at, version, archived_at, pre_archive_status
"""


class CalendarEventCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=500)
    starts_at: datetime
    ends_at: datetime
    all_day: bool = False
    timezone: str = Field(min_length=1, max_length=128)
    location: str | None = None
    description: str | None = None
    status: EventStatus = "confirmed"
    external_id: str | None = None

    @model_validator(mode="after")
    def validate_timing(self) -> CalendarEventCreate:
        _validate_datetime(self.starts_at, "starts_at")
        _validate_datetime(self.ends_at, "ends_at")
        _validate_timezone(self.timezone)
        if self.ends_at <= self.starts_at:
            raise ValueError("ends_at must be after starts_at")
        return self


class CalendarEventPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=500)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    all_day: bool | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=128)
    location: str | None = None
    description: str | None = None
    status: EventStatus | None = None

    @model_validator(mode="after")
    def validate_patch(self) -> CalendarEventPatch:
        for field in ("title", "starts_at", "ends_at", "all_day", "timezone", "status"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        if self.starts_at is not None:
            _validate_datetime(self.starts_at, "starts_at")
        if self.ends_at is not None:
            _validate_datetime(self.ends_at, "ends_at")
        if self.timezone is not None:
            _validate_timezone(self.timezone)
        return self


class CalendarEventAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


class CalendarEventResponse(BaseModel):
    id: UUID
    title: str
    starts_at: datetime
    ends_at: datetime
    all_day: bool
    timezone: str
    location: str | None
    description: str | None
    status: EventStatus
    external_source: str
    external_id: str | None
    source_authoritative: bool
    created_at: datetime
    updated_at: datetime
    version: int
    archived_at: datetime | None
    pre_archive_status: str | None


class CalendarEventListResponse(BaseModel):
    items: list[CalendarEventResponse]
    next_cursor: str | None = None


def _validate_datetime(value: datetime, field: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone offset")


def _validate_timezone(value: str) -> None:
    try:
        ZoneInfo(value)
    except ZoneInfoNotFoundError as exc:
        raise ValueError("timezone must be a valid IANA timezone") from exc


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
) -> CalendarEventResponse | None:
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
    return CalendarEventResponse.model_validate(row["response_body"])


def _store_cached(
    session: Session,
    auth: AuthContext,
    key: str,
    request_hash: str,
    response: CalendarEventResponse,
    status_code: int,
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
            "response_status": status_code,
            "response_body": dumps(response.model_dump(mode="json")),
            "created_at": now,
            "expires_at": now + timedelta(days=365),
        },
    )


def _request_ids(request: Request) -> tuple[UUID, UUID]:
    try:
        return UUID(request.state.request_id), UUID(request.state.correlation_id)
    except (AttributeError, TypeError, ValueError):
        return uuid4(), uuid4()


def _write_audit_and_outbox(
    session: Session,
    auth: AuthContext,
    request: Request,
    event_type: str,
    event_id: UUID,
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
                :id, :workspace_id, :event_type, 'calendar_event', :aggregate_id,
                :aggregate_version, :actor_id, :request_id, :correlation_id,
                :changed_fields, 'allowed', 'user', '{}'::jsonb, :occurred_at
            )
            """
        ),
        {
            "id": uuid4(),
            "workspace_id": auth.workspace_id,
            "event_type": event_type,
            "aggregate_id": event_id,
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
            "payload": dumps({"calendar_event_id": str(event_id), "version": version}),
            "occurred_at": now,
        },
    )


def _get_row(
    session: Session,
    auth: AuthContext,
    event_id: UUID,
    *,
    for_update: bool = False,
) -> dict[str, Any] | None:
    suffix = " FOR UPDATE" if for_update else ""
    row = (
        session.execute(
            text(
                f"""
                SELECT {_SELECT_FIELDS}
                FROM calendar_events
                WHERE workspace_id = :workspace_id AND id = :event_id
                {suffix}
                """
            ),
            {"workspace_id": auth.workspace_id, "event_id": event_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _encode_cursor(starts_at: datetime, event_id: UUID) -> str:
    payload = dumps({"starts_at": starts_at.isoformat(), "id": str(event_id)}).encode()
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
        return datetime.fromisoformat(decoded["starts_at"]), UUID(decoded["id"])
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="MALFORMED_CURSOR") from exc


def _workspace_day_bounds(day: date, timezone: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone)
    start = datetime.combine(day, time.min, zone)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


@router.post("", response_model=CalendarEventResponse, status_code=status.HTTP_201_CREATED)
def create_calendar_event(
    payload: CalendarEventCreate,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> CalendarEventResponse:
    request_hash = _request_hash(payload, "create")
    now = datetime.now(UTC)
    event_id = uuid4()
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached
        row = (
            session.execute(
                text(
                    f"""
                    INSERT INTO calendar_events (
                        id, workspace_id, external_source, external_id, title,
                        starts_at, ends_at, all_day, timezone, location, description,
                        status, source_authoritative, created_by, updated_by,
                        created_at, updated_at, version
                    ) VALUES (
                        :id, :workspace_id, 'local', :external_id, :title,
                        :starts_at, :ends_at, :all_day, :timezone, :location, :description,
                        :status, true, :actor_id, :actor_id, :now, :now, 1
                    ) RETURNING {_SELECT_FIELDS}
                    """
                ),
                {
                    "id": event_id,
                    "workspace_id": auth.workspace_id,
                    "actor_id": auth.user_id,
                    "now": now,
                    **payload.model_dump(),
                },
            )
            .mappings()
            .one()
        )
        response = CalendarEventResponse.model_validate(row)
        _write_audit_and_outbox(
            session, auth, request, "calendar_event.created", event_id, 1, ["*"], now
        )
        _store_cached(session, auth, idempotency_key, request_hash, response, 201, now)
        return response


@router.get("", response_model=CalendarEventListResponse)
def list_calendar_events(
    auth: AuthDep,
    session: SessionDep,
    day: date | None = None,
    timezone: str | None = None,
    include_archived: bool = False,
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
) -> CalendarEventListResponse:
    clauses = ["workspace_id = :workspace_id"]
    params: dict[str, Any] = {"workspace_id": auth.workspace_id, "limit": limit + 1}
    if not include_archived:
        clauses.append("archived_at IS NULL")
    if day is not None:
        if timezone is None:
            raise HTTPException(status_code=422, detail="TIMEZONE_REQUIRED")
        _validate_timezone(timezone)
        start, end = _workspace_day_bounds(day, timezone)
        clauses.append("starts_at < :day_end AND ends_at > :day_start")
        params.update({"day_start": start, "day_end": end})
    if cursor:
        starts_at, event_id = _decode_cursor(cursor)
        clauses.append("(starts_at, id) > (:cursor_starts_at, :cursor_id)")
        params.update({"cursor_starts_at": starts_at, "cursor_id": event_id})
    rows = (
        session.execute(
            text(
                f"""
                SELECT {_SELECT_FIELDS}
                FROM calendar_events
                WHERE {" AND ".join(clauses)}
                ORDER BY starts_at ASC, id ASC
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
        next_cursor = _encode_cursor(last["starts_at"], last["id"])
    return CalendarEventListResponse(
        items=[CalendarEventResponse.model_validate(row) for row in page],
        next_cursor=next_cursor,
    )


@router.get("/{event_id}", response_model=CalendarEventResponse)
def get_calendar_event(
    event_id: UUID,
    auth: AuthDep,
    session: SessionDep,
) -> CalendarEventResponse:
    row = _get_row(session, auth, event_id)
    if row is None:
        raise HTTPException(status_code=404, detail="CALENDAR_EVENT_NOT_FOUND")
    return CalendarEventResponse.model_validate(row)


@router.patch("/{event_id}", response_model=CalendarEventResponse)
def update_calendar_event(
    event_id: UUID,
    payload: CalendarEventPatch,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> CalendarEventResponse:
    request_hash = _request_hash(payload, f"update:{event_id}")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached
        current = _get_row(session, auth, event_id, for_update=True)
        if current is None:
            raise HTTPException(status_code=404, detail="CALENDAR_EVENT_NOT_FOUND")
        if current["version"] != payload.expected_version:
            raise HTTPException(
                status_code=409,
                detail={"code": "VERSION_CONFLICT", "current_version": current["version"]},
            )
        if current["archived_at"] is not None:
            raise HTTPException(status_code=409, detail="CALENDAR_EVENT_ARCHIVED")
        fields = payload.model_fields_set - {"expected_version"}
        if not fields:
            response = CalendarEventResponse.model_validate(current)
            _store_cached(session, auth, idempotency_key, request_hash, response, 200, now)
            return response
        candidate = current.copy()
        for field in fields:
            candidate[field] = getattr(payload, field)
        if candidate["ends_at"] <= candidate["starts_at"]:
            raise HTTPException(status_code=422, detail="INVALID_EVENT_TIME_RANGE")
        assignments = [f"{field} = :{field}" for field in sorted(fields)]
        assignments.extend(
            ["updated_by = :updated_by", "updated_at = :now", "version = version + 1"]
        )
        values = payload.model_dump(include=set(fields))
        values.update(
            {
                "workspace_id": auth.workspace_id,
                "event_id": event_id,
                "updated_by": auth.user_id,
                "now": now,
            }
        )
        row = (
            session.execute(
                text(
                    f"""
                    UPDATE calendar_events
                    SET {", ".join(assignments)}
                    WHERE workspace_id = :workspace_id AND id = :event_id
                    RETURNING {_SELECT_FIELDS}
                    """
                ),
                values,
            )
            .mappings()
            .one()
        )
        response = CalendarEventResponse.model_validate(row)
        _write_audit_and_outbox(
            session,
            auth,
            request,
            "calendar_event.updated",
            event_id,
            response.version,
            sorted(fields),
            now,
        )
        _store_cached(session, auth, idempotency_key, request_hash, response, 200, now)
        return response


def _lifecycle(
    event_id: UUID,
    payload: CalendarEventAction,
    request: Request,
    auth: AuthContext,
    session: Session,
    idempotency_key: str,
    action: Literal["archive", "restore"],
) -> CalendarEventResponse:
    request_hash = _request_hash(payload, f"{action}:{event_id}")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached
        current = _get_row(session, auth, event_id, for_update=True)
        if current is None:
            raise HTTPException(status_code=404, detail="CALENDAR_EVENT_NOT_FOUND")
        if current["version"] != payload.expected_version:
            raise HTTPException(
                status_code=409,
                detail={"code": "VERSION_CONFLICT", "current_version": current["version"]},
            )
        if action == "archive" and current["archived_at"] is not None:
            response = CalendarEventResponse.model_validate(current)
            _store_cached(session, auth, idempotency_key, request_hash, response, 200, now)
            return response
        if action == "restore" and current["archived_at"] is None:
            raise HTTPException(status_code=409, detail="CALENDAR_EVENT_NOT_ARCHIVED")
        assignments = (
            "archived_at = :now, pre_archive_status = status"
            if action == "archive"
            else "archived_at = NULL, pre_archive_status = NULL"
        )
        row = (
            session.execute(
                text(
                    f"""
                    UPDATE calendar_events
                    SET {assignments}, updated_by = :updated_by,
                        updated_at = :now, version = version + 1
                    WHERE workspace_id = :workspace_id AND id = :event_id
                    RETURNING {_SELECT_FIELDS}
                    """
                ),
                {
                    "workspace_id": auth.workspace_id,
                    "event_id": event_id,
                    "updated_by": auth.user_id,
                    "now": now,
                },
            )
            .mappings()
            .one()
        )
        response = CalendarEventResponse.model_validate(row)
        event_type = "calendar_event.archived" if action == "archive" else "calendar_event.restored"
        _write_audit_and_outbox(
            session,
            auth,
            request,
            event_type,
            event_id,
            response.version,
            ["archived_at", "pre_archive_status"],
            now,
        )
        _store_cached(session, auth, idempotency_key, request_hash, response, 200, now)
        return response


@router.post("/{event_id}/archive", response_model=CalendarEventResponse)
def archive_calendar_event(
    event_id: UUID,
    payload: CalendarEventAction,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> CalendarEventResponse:
    return _lifecycle(event_id, payload, request, auth, session, idempotency_key, "archive")


@router.post("/{event_id}/restore", response_model=CalendarEventResponse)
def restore_calendar_event(
    event_id: UUID,
    payload: CalendarEventAction,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> CalendarEventResponse:
    return _lifecycle(event_id, payload, request, auth, session, idempotency_key, "restore")
