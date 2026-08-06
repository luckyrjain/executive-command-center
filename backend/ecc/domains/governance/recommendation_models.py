from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from ecc.domains.communication.commitments import CommitmentCreate
from ecc.domains.governance.risks import RiskCreate
from ecc.domains.planning.tasks import TaskCreate

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

_CREATE_MODELS: dict[str, type[BaseModel]] = {
    "task": TaskCreate,
    "commitment": CommitmentCreate,
    "risk": RiskCreate,
}


class RecommendationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    recommendation_type: str = Field(min_length=1, max_length=100)
    target_type: Literal["task", "commitment", "risk"]
    target_id: UUID | None = None
    proposed_action: dict[str, Any]
    proposed_fields: dict[str, Any] | None = None
    expected_version: int | None = Field(default=None, ge=1)
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

    @model_validator(mode="after")
    def validate_create_shape(self) -> RecommendationCreate:
        """`operation="create"` proposes a brand-new row rather than a change
        to an existing one, so it has no `target_id`/`expected_version` to
        check against (there's nothing to look up yet) and instead carries
        `proposed_fields` -- reusing `TaskCreate`/`CommitmentCreate`/
        `RiskCreate` rather than a fourth schema shape, validated eagerly
        here (not deferred to confirm-time) so a malformed create-type
        recommendation is rejected at generation, the same point every
        other malformed recommendation already is.
        """
        is_create = self.proposed_action.get("operation") == "create"
        if is_create:
            if self.target_id is not None:
                raise ValueError("target_id must be omitted when operation is 'create'")
            if self.expected_version is not None:
                raise ValueError("expected_version must be omitted when operation is 'create'")
            if not self.proposed_fields:
                raise ValueError("proposed_fields is required when operation is 'create'")
            model = _CREATE_MODELS[self.target_type]
            try:
                model(**self.proposed_fields)
            except ValidationError as exc:
                raise ValueError(
                    f"proposed_fields is not a valid {self.target_type}: {exc}"
                ) from exc
        else:
            if self.target_id is None:
                raise ValueError("target_id is required unless operation is 'create'")
            if self.expected_version is None:
                raise ValueError("expected_version is required unless operation is 'create'")
            if self.proposed_fields is not None:
                raise ValueError("proposed_fields is only valid when operation is 'create'")
        return self


class VersionAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


class ConfirmAction(VersionAction):
    target_expected_version: int | None = Field(default=None, ge=1)


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
    proposed_fields: dict[str, Any] | None
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
