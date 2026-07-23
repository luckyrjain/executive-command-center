from datetime import UTC, datetime, timedelta
from hashlib import sha256
from json import dumps
from typing import Annotated
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session
from ecc.observability import (
    queue_lifecycle_event,
    record_audit_outbox_failure,
    record_idempotency_conflict,
)

router = APIRouter(prefix="/api/v1/planning", tags=["planning"])
SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]

_WEEKDAYS = range(7)


class CapacityDay(BaseModel):
    model_config = ConfigDict(extra="forbid")
    weekday: int = Field(ge=0, le=6)
    available_minutes: int = Field(ge=0, le=1440)
    focus_minutes: int = Field(ge=0, le=1440)

    @model_validator(mode="after")
    def _focus_within_available(self) -> CapacityDay:
        if self.focus_minutes > self.available_minutes:
            raise ValueError("focus_minutes cannot exceed available_minutes")
        return self


class CapacityProfile(BaseModel):
    timezone: str
    version: int
    days: list[CapacityDay]


class CapacityProfilePut(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=0)
    timezone: str
    days: list[CapacityDay] = Field(min_length=7, max_length=7)

    @field_validator("timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown timezone: {value}") from exc
        return value

    @model_validator(mode="after")
    def _covers_every_weekday(self) -> CapacityProfilePut:
        weekdays = {day.weekday for day in self.days}
        if weekdays != set(_WEEKDAYS):
            raise ValueError("days must include exactly one entry per weekday (0-6)")
        return self


def _request_hash(payload: BaseModel, action: str) -> str:
    material = {"action": action, "payload": payload.model_dump(mode="json")}
    return sha256(dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _request_ids(request: Request) -> tuple[UUID, UUID]:
    try:
        return UUID(request.state.request_id), UUID(request.state.correlation_id)
    except (AttributeError, TypeError, ValueError):
        return uuid4(), uuid4()


def _lock_idempotency(session: Session, auth: AuthContext, key: str) -> None:
    lock_key = f"{auth.workspace_id}:{auth.user_id}:{key}"
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": lock_key},
    )


def _load_cached(
    session: Session, auth: AuthContext, key: str, request_hash: str
) -> CapacityProfile | None:
    row = (
        session.execute(
            text(
                """
                SELECT request_hash, response_body FROM idempotency_records
                WHERE workspace_id = :workspace_id AND actor_id = :actor_id
                  AND key = :key AND expires_at > :now
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
        record_idempotency_conflict("capacity")
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
    return CapacityProfile.model_validate(row["response_body"])


def _store_idempotency(
    session: Session,
    auth: AuthContext,
    key: str,
    request_hash: str,
    response: CapacityProfile,
    now: datetime,
    response_status: int = 200,
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


def _write_side_effects(
    session: Session,
    auth: AuthContext,
    request: Request,
    event_type: str,
    version: int,
    now: datetime,
) -> None:
    """Audit + outbox for a capacity profile write, matching every other
    mutating endpoint in this domain (e.g. waiting.py's
    ``_write_side_effects``). Capacity profiles have no single-row id of
    their own -- they are one versioned unit per (workspace_id, user_id),
    so ``auth.user_id`` is the aggregate id, the same key the profile is
    already fetched by in ``_current_profile``.
    """
    request_id, correlation_id = _request_ids(request)
    try:
        session.execute(
            text(
                """
                INSERT INTO audit_events (
                    id, workspace_id, event_type, aggregate_type, aggregate_id,
                    aggregate_version, actor_id, request_id, correlation_id,
                    changed_fields, authorization_result, source, metadata, occurred_at
                ) VALUES (
                    :id, :workspace_id, :event_type, 'capacity_profile', :aggregate_id,
                    :aggregate_version, :actor_id, :request_id, :correlation_id,
                    ARRAY['*'], 'allowed', 'user', '{}'::jsonb, :occurred_at
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": auth.workspace_id,
                "event_type": event_type,
                "aggregate_id": auth.user_id,
                "aggregate_version": version,
                "actor_id": auth.user_id,
                "request_id": request_id,
                "correlation_id": correlation_id,
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
                    :event_id, :workspace_id, :event_type_v1, 1,
                    :correlation_id, CAST(:payload AS jsonb), :occurred_at, 0
                )
                """
            ),
            {
                "event_id": uuid4(),
                "workspace_id": auth.workspace_id,
                "event_type_v1": f"{event_type}.v1",
                "correlation_id": correlation_id,
                "payload": dumps({"user_id": str(auth.user_id), "version": version}),
                "occurred_at": now,
            },
        )
    except SQLAlchemyError:
        record_audit_outbox_failure("capacity")
        raise
    queue_lifecycle_event(session, "capacity_profile", event_type, "allowed")


def _current_profile(
    session: Session, workspace_id: UUID, user_id: UUID, *, for_update: bool = False
) -> CapacityProfile:
    # `for_update=True` locks the caller's existing rows (if any) for the
    # rest of its transaction, closing a lost-update race: without it, two
    # concurrent PUTs can both read the same version, both pass the
    # version check, and the second write silently clobbers the first
    # (finding #5). SELECT ... FOR UPDATE is a no-op (locks zero rows) the
    # first time a user ever PUTs a profile, which is fine -- there is
    # nothing to lose an update against yet.
    lock_clause = " FOR UPDATE" if for_update else ""
    rows = (
        session.execute(
            text(
                f"""
                SELECT weekday, available_minutes, focus_minutes, timezone, version
                FROM capacity_profiles
                WHERE workspace_id = :workspace_id AND user_id = :user_id
                ORDER BY weekday{lock_clause}
                """
            ),
            {"workspace_id": workspace_id, "user_id": user_id},
        )
        .mappings()
        .all()
    )
    if not rows:
        return CapacityProfile(timezone="UTC", version=0, days=[])
    return CapacityProfile(
        timezone=rows[0]["timezone"],
        version=max(row["version"] for row in rows),
        days=[
            CapacityDay(
                weekday=row["weekday"],
                available_minutes=row["available_minutes"],
                focus_minutes=row["focus_minutes"],
            )
            for row in rows
        ],
    )


@router.get("/capacity", response_model=CapacityProfile)
def get_capacity_profile(auth: AuthDep, session: SessionDep) -> CapacityProfile:
    return _current_profile(session, auth.workspace_id, auth.user_id)


@router.put("/capacity", response_model=CapacityProfile)
def put_capacity_profile(
    payload: CapacityProfilePut,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> CapacityProfile:
    """Manages the whole 7-row weekly profile as one versioned unit.

    ``capacity_profiles`` has one row per weekday (DATA-MODEL.md's literal
    field list), but the profile is edited and versioned as a single
    resource -- there is no separate metadata table for a profile-level
    version, so the derived version is ``MAX(version)`` across the user's
    existing weekday rows (0 if none exist yet). A PUT is an atomic
    delete-and-reinsert of all 7 rows at ``expected_version + 1``, guarded
    by that derived value the same way every other mutation in this
    codebase checks ``expected_version`` against a stored row.

    Paired with an ``Idempotency-Key`` + response-replay cache, the same
    convention every other ``expected_version``-guarded mutation in this
    domain uses (waiting-link PATCH, risk review create, plan accept/
    supersede/propose/move/remove): an exact client retry (same key, same
    payload -- e.g. after a dropped response) replays the original response
    instead of hitting a spurious ``VERSION_CONFLICT`` on its own
    already-applied write. A retry with the same key but a *different*
    payload still hits ``IDEMPOTENCY_CONFLICT`` in ``_load_cached``, and a
    stale ``expected_version`` with no matching cached key still hits the
    genuine ``VERSION_CONFLICT`` below.
    """
    request_hash = _request_hash(payload, "put_capacity")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached
        current = _current_profile(session, auth.workspace_id, auth.user_id, for_update=True)
        if current.version != payload.expected_version:
            raise HTTPException(
                status_code=409,
                detail={"code": "VERSION_CONFLICT", "current_version": current.version},
            )
        session.execute(
            text(
                "DELETE FROM capacity_profiles WHERE workspace_id = :workspace_id "
                "AND user_id = :user_id"
            ),
            {"workspace_id": auth.workspace_id, "user_id": auth.user_id},
        )
        new_version = payload.expected_version + 1
        session.execute(
            text(
                """
                INSERT INTO capacity_profiles (
                    id, workspace_id, user_id, weekday, available_minutes,
                    focus_minutes, timezone, version, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, :user_id, :weekday, :available_minutes,
                    :focus_minutes, :timezone, :version, :now, :now
                )
                """
            ),
            [
                {
                    "id": uuid4(),
                    "workspace_id": auth.workspace_id,
                    "user_id": auth.user_id,
                    "weekday": day.weekday,
                    "available_minutes": day.available_minutes,
                    "focus_minutes": day.focus_minutes,
                    "timezone": payload.timezone,
                    "version": new_version,
                    "now": now,
                }
                for day in payload.days
            ],
        )
        response = CapacityProfile(
            timezone=payload.timezone,
            version=new_version,
            days=sorted(payload.days, key=lambda day: day.weekday),
        )
        _write_side_effects(session, auth, request, "capacity_profile.updated", new_version, now)
        _store_idempotency(session, auth, idempotency_key, request_hash, response, now)
        return response
