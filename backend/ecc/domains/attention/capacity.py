from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthDep, CsrfDep
from ecc.database import get_session

router = APIRouter(prefix="/api/v1/planning", tags=["planning"])
SessionDep = Annotated[Session, Depends(get_session)]

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


def _current_profile(session: Session, workspace_id: UUID, user_id: UUID) -> CapacityProfile:
    rows = (
        session.execute(
            text(
                """
                SELECT weekday, available_minutes, focus_minutes, timezone, version
                FROM capacity_profiles
                WHERE workspace_id = :workspace_id AND user_id = :user_id
                ORDER BY weekday
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
    payload: CapacityProfilePut, auth: AuthDep, session: SessionDep, _csrf: CsrfDep
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
    """
    now = datetime.now(UTC)
    with session.begin():
        current = _current_profile(session, auth.workspace_id, auth.user_id)
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
        return CapacityProfile(
            timezone=payload.timezone,
            version=new_version,
            days=sorted(payload.days, key=lambda day: day.weekday),
        )
