from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

RecommendationStatus = Literal[
    "proposed",
    "pending_confirmation",
    "accepted",
    "rejected",
    "expired",
    "superseded",
    "executed",
    "failed",
]


class RecommendationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recommendation_type: str = Field(min_length=1, max_length=100)
    target_type: Literal["task", "commitment", "risk"]
    target_id: UUID
    proposed_action: dict[str, Any]
    expected_version: int = Field(ge=1)
    rationale: str = Field(min_length=1, max_length=10000)
    confidence: float = Field(ge=0, le=1)
    evidence_ids: list[UUID] = Field(default_factory=list, max_length=100)
    expires_at: datetime | None = None
    source: Literal["rule", "ai"] = "rule"

    @field_validator("expires_at")
    @classmethod
    def aware_expiry(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("expires_at must include a timezone offset")
        return value


class VersionAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


class ConfirmAction(VersionAction):
    target_expected_version: int = Field(ge=1)


class RejectAction(VersionAction):
    reason: str | None = Field(default=None, max_length=5000)


class DeferAction(VersionAction):
    defer_until: datetime

    @field_validator("defer_until")
    @classmethod
    def aware_defer(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("defer_until must include a timezone offset")
        return value


class PinAction(VersionAction):
    pinned: bool = True


class RecommendationResponse(BaseModel):
    id: UUID
    recommendation_type: str
    target_type: str
    target_id: UUID | None
    proposed_action: dict[str, Any]
    expected_version: int | None
    rationale: str
    confidence: float
    status: RecommendationStatus
    evidence_ids: list[UUID]
    expires_at: datetime | None
    confirmed_by: UUID | None
    confirmed_at: datetime | None
    execution_result: dict[str, Any] | None
    source: str
    pinned: bool
    deferred_until: datetime | None
    created_by: UUID
    updated_by: UUID
    created_at: datetime
    updated_at: datetime
    version: int
    archived_at: datetime | None


class RecommendationListResponse(BaseModel):
    items: list[RecommendationResponse]
    next_cursor: str | None = None
