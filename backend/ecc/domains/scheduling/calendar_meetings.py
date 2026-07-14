from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session

router = APIRouter(tags=["calendar", "meetings"])
SessionDep = Annotated[Session, Depends(get_session)]

CalendarStatus = Literal["confirmed", "tentative", "cancelled"]
MeetingStatus = Literal["planned", "in_progress", "completed", "cancelled"]

_CALENDAR_FIELDS = """
id, title, starts_at, ends_at, all_day, timezone, location, description,
status, source_authoritative, created_at, updated_at, version, archived_at,
pre_archive_status
"""

_MEETING_FIELDS = """
id, calendar_event_id, title, standalone_starts_at, standalone_ends_at,
standalone_timezone, status, agenda, preparation, notes_summary, created_at,
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
    status: CalendarStatus = "confirmed"

    @model_validator(mode="after")
    def validate_timing(self) -> "CalendarEventCreate":
        _validate_aware(self.starts_at, "starts_at")
        _validate_aware(self.ends_at, "ends_at")
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
    status: CalendarStatus | None = None

    @model_validator(mode="after")
    def validate_values(self) -> "CalendarEventPatch":
        for field in ("title", "starts_at", "ends_at", "all_day", "timezone", "status"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        if self.starts_at is not None:
            _validate_aware(self.starts_at, "starts_at")
        if self.ends_at is not None:
            _validate_aware(self.ends_at, "ends_at")
        if self.timezone is not None:
            _validate_timezone(self.timezone)
        return self


class CalendarEventResponse(BaseModel):
    id: UUID
    title: str
    starts_at: datetime
    ends_at: datetime
    all_day: bool
    timezone: str
    location: str | None
    description: str | None
    status: CalendarStatus
    source_authoritative: bool
    created_at: datetime
    updated_at: datetime
    version: int
    archived_at: datetime | None
    pre_archive_status: str | None


class CalendarEventListResponse(BaseModel):
    items: list[CalendarEventResponse]


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
    def validate_timing(self) -> "MeetingCreate":
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
    def reject_null_required_fields(self) -> "MeetingPatch":
        for field in ("title", "status"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


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


def _check_version(row: dict[str, Any], expected_version: int) -> None:
    if row["version"] != expected_version:
        raise HTTPException(
            status_code=409,
            detail={"code": "VERSION_CONFLICT", "current_version": row["version"]},
        )


def _calendar_row(session: Session, auth: AuthContext, event_id: UUID, *, lock: bool = False) -> dict[str, Any] | None:
    suffix = " FOR UPDATE" if lock else ""
    row = session.execute(
        text(
            f"""
            SELECT {_CALENDAR_FIELDS}
            FROM calendar_events
            WHERE workspace_id = :workspace_id AND id = :event_id
            {suffix}
            """
        ),
        {"workspace_id": auth.workspace_id, "event_id": event_id},
    ).mappings().one_or_none()
    return dict(row) if row is not None else None


def _meeting_row(session: Session, auth: AuthContext, meeting_id: UUID, *, lock: bool = False) -> dict[str, Any] | None:
    suffix = " FOR UPDATE" if lock else ""
    row = session.execute(
        text(
            f"""
            SELECT {_MEETING_FIELDS}
            FROM meetings
            WHERE workspace_id = :workspace_id AND id = :meeting_id
            {suffix}
            """
        ),
        {"workspace_id": auth.workspace_id, "meeting_id": meeting_id},
    ).mappings().one_or_none()
    return dict(row) if row is not None else None


def _project_meeting(session: Session, auth: AuthContext, row: dict[str, Any]) -> MeetingResponse:
    if row["calendar_event_id"] is None:
        starts_at = row["standalone_starts_at"]
        ends_at = row["standalone_ends_at"]
        timezone = row["standalone_timezone"]
    else:
        event = _calendar_row(session, auth, row["calendar_event_id"])
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


@router.post("/api/v1/calendar/events", response_model=CalendarEventResponse, status_code=status.HTTP_201_CREATED)
def create_calendar_event(
    payload: CalendarEventCreate,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
) -> CalendarEventResponse:
    del request
    now = datetime.now(UTC)
    with session.begin():
        row = session.execute(
            text(
                f"""
                INSERT INTO calendar_events (
                    id, workspace_id, external_source, title, starts_at, ends_at,
                    all_day, timezone, location, description, status,
                    source_authoritative, created_by, updated_by, created_at,
                    updated_at, version
                ) VALUES (
                    :id, :workspace_id, 'local', :title, :starts_at, :ends_at,
                    :all_day, :timezone, :location, :description, :status,
                    true, :actor_id, :actor_id, :now, :now, 1
                ) RETURNING {_CALENDAR_FIELDS}
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": auth.workspace_id,
                "actor_id": auth.user_id,
                "now": now,
                **payload.model_dump(),
            },
        ).mappings().one()
        return CalendarEventResponse.model_validate(dict(row))


@router.get("/api/v1/calendar/events", response_model=CalendarEventListResponse)
def list_calendar_events(
    auth: AuthDep,
    session: SessionDep,
    include_archived: bool = False,
    starts_after: datetime | None = None,
    starts_before: datetime | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> CalendarEventListResponse:
    clauses = ["workspace_id = :workspace_id"]
    params: dict[str, Any] = {"workspace_id": auth.workspace_id, "limit": limit}
    if not include_archived:
        clauses.append("archived_at IS NULL")
    if starts_after is not None:
        _validate_aware(starts_after, "starts_after")
        clauses.append("starts_at >= :starts_after")
        params["starts_after"] = starts_after
    if starts_before is not None:
        _validate_aware(starts_before, "starts_before")
        clauses.append("starts_at < :starts_before")
        params["starts_before"] = starts_before
    rows = session.execute(
        text(
            f"""
            SELECT {_CALENDAR_FIELDS}
            FROM calendar_events
            WHERE {' AND '.join(clauses)}
            ORDER BY starts_at, id
            LIMIT :limit
            """
        ),
        params,
    ).mappings().all()
    return CalendarEventListResponse(
        items=[CalendarEventResponse.model_validate(dict(row)) for row in rows]
    )


@router.get("/api/v1/calendar/events/{event_id}", response_model=CalendarEventResponse)
def get_calendar_event(event_id: UUID, auth: AuthDep, session: SessionDep) -> CalendarEventResponse:
    row = _calendar_row(session, auth, event_id)
    if row is None:
        raise HTTPException(status_code=404, detail="CALENDAR_EVENT_NOT_FOUND")
    return CalendarEventResponse.model_validate(row)


@router.patch("/api/v1/calendar/events/{event_id}", response_model=CalendarEventResponse)
def update_calendar_event(
    event_id: UUID,
    payload: CalendarEventPatch,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
) -> CalendarEventResponse:
    del request
    with session.begin():
        current = _calendar_row(session, auth, event_id, lock=True)
        if current is None:
            raise HTTPException(status_code=404, detail="CALENDAR_EVENT_NOT_FOUND")
        _check_version(current, payload.expected_version)
        fields = payload.model_fields_set - {"expected_version"}
        if not fields:
            return CalendarEventResponse.model_validate(current)
        values = payload.model_dump(include=fields)
        starts_at = values.get("starts_at", current["starts_at"])
        ends_at = values.get("ends_at", current["ends_at"])
        if ends_at <= starts_at:
            raise HTTPException(status_code=422, detail="INVALID_TIME_RANGE")
        assignments = [f"{field} = :{field}" for field in sorted(fields)]
        assignments.extend(["updated_by = :actor_id", "updated_at = :now", "version = version + 1"])
        values.update(
            {
                "workspace_id": auth.workspace_id,
                "event_id": event_id,
                "actor_id": auth.user_id,
                "now": datetime.now(UTC),
            }
        )
        row = session.execute(
            text(
                f"""
                UPDATE calendar_events
                SET {', '.join(assignments)}
                WHERE workspace_id = :workspace_id AND id = :event_id
                RETURNING {_CALENDAR_FIELDS}
                """
            ),
            values,
        ).mappings().one()
        return CalendarEventResponse.model_validate(dict(row))


@router.post("/api/v1/meetings", response_model=MeetingResponse, status_code=status.HTTP_201_CREATED)
def create_meeting(
    payload: MeetingCreate,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
) -> MeetingResponse:
    del request
    now = datetime.now(UTC)
    with session.begin():
        if payload.calendar_event_id is not None:
            event = _calendar_row(session, auth, payload.calendar_event_id, lock=True)
            if event is None:
                raise HTTPException(status_code=404, detail="CALENDAR_EVENT_NOT_FOUND")
        row = session.execute(
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
                "id": uuid4(),
                "workspace_id": auth.workspace_id,
                "actor_id": auth.user_id,
                "now": now,
                **payload.model_dump(),
            },
        ).mappings().one()
        return _project_meeting(session, auth, dict(row))


@router.get("/api/v1/meetings", response_model=MeetingListResponse)
def list_meetings(
    auth: AuthDep,
    session: SessionDep,
    include_archived: bool = False,
    limit: int = Query(default=50, ge=1, le=100),
) -> MeetingListResponse:
    clauses = ["workspace_id = :workspace_id"]
    if not include_archived:
        clauses.append("archived_at IS NULL")
    rows = session.execute(
        text(
            f"""
            SELECT {_MEETING_FIELDS}
            FROM meetings
            WHERE {' AND '.join(clauses)}
            ORDER BY updated_at DESC, id DESC
            LIMIT :limit
            """
        ),
        {"workspace_id": auth.workspace_id, "limit": limit},
    ).mappings().all()
    return MeetingListResponse(
        items=[_project_meeting(session, auth, dict(row)) for row in rows]
    )


@router.get("/api/v1/meetings/{meeting_id}", response_model=MeetingResponse)
def get_meeting(meeting_id: UUID, auth: AuthDep, session: SessionDep) -> MeetingResponse:
    row = _meeting_row(session, auth, meeting_id)
    if row is None:
        raise HTTPException(status_code=404, detail="MEETING_NOT_FOUND")
    return _project_meeting(session, auth, row)


@router.patch("/api/v1/meetings/{meeting_id}", response_model=MeetingResponse)
def update_meeting(
    meeting_id: UUID,
    payload: MeetingPatch,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
) -> MeetingResponse:
    del request
    with session.begin():
        current = _meeting_row(session, auth, meeting_id, lock=True)
        if current is None:
            raise HTTPException(status_code=404, detail="MEETING_NOT_FOUND")
        _check_version(current, payload.expected_version)
        fields = payload.model_fields_set - {"expected_version"}
        if not fields:
            return _project_meeting(session, auth, current)
        assignments = [f"{field} = :{field}" for field in sorted(fields)]
        assignments.extend(["updated_by = :actor_id", "updated_at = :now", "version = version + 1"])
        values = payload.model_dump(include=fields)
        values.update(
            {
                "workspace_id": auth.workspace_id,
                "meeting_id": meeting_id,
                "actor_id": auth.user_id,
                "now": datetime.now(UTC),
            }
        )
        row = session.execute(
            text(
                f"""
                UPDATE meetings
                SET {', '.join(assignments)}
                WHERE workspace_id = :workspace_id AND id = :meeting_id
                RETURNING {_MEETING_FIELDS}
                """
            ),
            values,
        ).mappings().one()
        return _project_meeting(session, auth, dict(row))
