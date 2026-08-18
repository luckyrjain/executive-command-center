from datetime import UTC, datetime, timedelta
from hashlib import sha256
from json import dumps
from typing import Annotated
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session
from ecc.observability import (
    queue_lifecycle_event,
    record_idempotency_conflict,
)
from ecc.platform import audit_outbox

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
    # Deliberately no authz.authorize()/visible_resource_filter_sql call in
    # this module. Every query here is scoped to `user_id = auth.user_id`
    # -- there is no resource_id path parameter through which a caller
    # could ever address another member's profile, unlike every other
    # domain Task 4 wires (which all expose a resource_id a caller could
    # substitute to probe someone else's row). `visibility = 'private'` on
    # the INSERT below documents the same fact for anyone reading the
    # schema directly, it isn't load-bearing for access control here.
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
                    focus_minutes, timezone, version, created_at, updated_at,
                    owner_id, visibility
                ) VALUES (
                    :id, :workspace_id, :user_id, :weekday, :available_minutes,
                    :focus_minutes, :timezone, :version, :now, :now,
                    :user_id, 'private'
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
        audit_outbox.write_audit_and_outbox(
            session,
            auth,
            request,
            event_type="capacity_profile.updated",
            aggregate_type="capacity_profile",
            aggregate_id=auth.user_id,
            aggregate_version=new_version,
            changed_fields=["*"],
            payload={"user_id": str(auth.user_id), "version": new_version},
            now=now,
            domain="capacity",
        )
        queue_lifecycle_event(session, "capacity_profile", "capacity_profile.updated", "allowed")
        _store_idempotency(session, auth, idempotency_key, request_hash, response, now)
        return response
