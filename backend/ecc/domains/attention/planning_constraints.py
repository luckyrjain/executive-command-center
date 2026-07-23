"""Planning constraints: internal data access, no dedicated public endpoint.

phase-003/API-SCHEMAS.md's published surface lists only GET|PUT
/planning/capacity for this task's area -- planning_constraints is
"plan-scoped input" per the implementation plan: Task 5's ``POST /plans``
persists constraints submitted alongside a plan request through the
functions here, rather than this task exposing its own CRUD router.
"""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext

ConstraintKind = Literal["fixed_time", "deadline", "preference"]
ConstraintSourceType = Literal["task", "commitment", "calendar_event"]
ConstraintHardness = Literal["hard", "soft"]

_FIELDS = """
    id, kind, source_type, source_id, label, starts_at, ends_at, hardness,
    priority, created_at, updated_at, version, archived_at
"""


class PlanningConstraint(BaseModel):
    id: UUID
    kind: ConstraintKind
    source_type: ConstraintSourceType | None
    source_id: UUID | None
    label: str
    starts_at: datetime | None
    ends_at: datetime | None
    hardness: ConstraintHardness
    priority: int
    created_at: datetime
    updated_at: datetime
    version: int
    archived_at: datetime | None


class PlanningConstraintCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: ConstraintKind
    source_type: ConstraintSourceType | None = None
    source_id: UUID | None = None
    label: str = Field(min_length=1, max_length=500)
    starts_at: datetime | None = None
    ends_at: datetime | None = None
    hardness: ConstraintHardness = "hard"
    priority: int = Field(default=0, ge=0, le=100)

    @model_validator(mode="after")
    def _validate_shape(self) -> PlanningConstraintCreate:
        if (self.source_type is None) != (self.source_id is None):
            raise ValueError("source_type and source_id must be provided together")
        for value in (self.starts_at, self.ends_at):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError("starts_at/ends_at must include a timezone offset")
        if self.kind == "fixed_time" and (self.starts_at is None or self.ends_at is None):
            raise ValueError("fixed_time constraints require starts_at and ends_at")
        if self.kind == "deadline" and self.ends_at is None:
            raise ValueError("deadline constraints require ends_at")
        if (
            self.starts_at is not None
            and self.ends_at is not None
            and self.starts_at >= self.ends_at
        ):
            raise ValueError("starts_at must be before ends_at")
        return self


def create_constraint(
    session: Session, auth: AuthContext, payload: PlanningConstraintCreate
) -> PlanningConstraint:
    now = datetime.now(UTC)
    row = (
        session.execute(
            text(
                f"""
                INSERT INTO planning_constraints (
                    id, workspace_id, user_id, kind, source_type, source_id, label,
                    starts_at, ends_at, hardness, priority, created_at, updated_at, version
                ) VALUES (
                    :id, :workspace_id, :user_id, :kind, :source_type, :source_id, :label,
                    :starts_at, :ends_at, :hardness, :priority, :now, :now, 1
                )
                RETURNING {_FIELDS}
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": auth.workspace_id,
                "user_id": auth.user_id,
                "kind": payload.kind,
                "source_type": payload.source_type,
                "source_id": payload.source_id,
                "label": payload.label,
                "starts_at": payload.starts_at,
                "ends_at": payload.ends_at,
                "hardness": payload.hardness,
                "priority": payload.priority,
                "now": now,
            },
        )
        .mappings()
        .one()
    )
    return PlanningConstraint.model_validate(dict(row))


def list_active_constraints(session: Session, auth: AuthContext) -> list[PlanningConstraint]:
    rows = (
        session.execute(
            text(
                f"""
                SELECT {_FIELDS} FROM planning_constraints
                WHERE workspace_id = :workspace_id AND user_id = :user_id
                  AND archived_at IS NULL
                ORDER BY starts_at ASC NULLS LAST, priority DESC, created_at ASC
                """
            ),
            {"workspace_id": auth.workspace_id, "user_id": auth.user_id},
        )
        .mappings()
        .all()
    )
    return [PlanningConstraint.model_validate(dict(row)) for row in rows]


def archive_constraint(session: Session, auth: AuthContext, constraint_id: UUID) -> bool:
    """Used by Task 5's replan flow to retire a constraint that no longer
    applies (e.g. its source task was completed). Returns False if the
    constraint doesn't exist in this workspace or is already archived.
    """
    now = datetime.now(UTC)
    row = session.execute(
        text(
            """
            UPDATE planning_constraints
            SET archived_at = :now, updated_at = :now, version = version + 1
            WHERE workspace_id = :workspace_id AND id = :constraint_id AND archived_at IS NULL
            RETURNING id
            """
        ),
        {"now": now, "workspace_id": auth.workspace_id, "constraint_id": constraint_id},
    ).one_or_none()
    return row is not None
