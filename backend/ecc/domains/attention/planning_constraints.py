"""Planning constraints: hard calendar reservations and deadlines.

Previously internal data access with no route wired to it: nothing in
``main.py``'s router registration ever pulled this module in, so there was
no way for a user to actually create a hard constraint through the API even
though ``planning.py`` reads and honors ``planning_constraints`` rows in
every plan proposal/move -- dead code serving a table nothing could ever
populate (finding #10). Wired in here as ``POST|GET /api/v1/planning/
constraints`` and ``DELETE /api/v1/planning/constraints/{id}`` (archive),
following ``capacity.py``'s sibling-router convention for this same
``/api/v1/planning`` prefix and ``waiting.py``'s idempotent-create pattern
for the mutating endpoint.
"""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from json import dumps
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
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
    constraint doesn't exist for this workspace/user or is already
    archived. Scoped by ``user_id`` in addition to ``workspace_id``,
    matching ``create_constraint``/``list_active_constraints`` -- planning
    constraints are per-(workspace, user) everywhere else, so a different
    user in the same workspace must not be able to archive it either.
    """
    now = datetime.now(UTC)
    row = session.execute(
        text(
            """
            UPDATE planning_constraints
            SET archived_at = :now, updated_at = :now, version = version + 1
            WHERE workspace_id = :workspace_id AND user_id = :user_id
              AND id = :constraint_id AND archived_at IS NULL
            RETURNING id
            """
        ),
        {
            "now": now,
            "workspace_id": auth.workspace_id,
            "user_id": auth.user_id,
            "constraint_id": constraint_id,
        },
    ).one_or_none()
    return row is not None


# ---------------------------------------------------------------------------
# HTTP endpoints. Not previously reachable from any route (finding #10) --
# wired here as siblings of capacity.py's GET|PUT /planning/capacity under
# the same /api/v1/planning prefix. Archiving is exposed as an action route
# (POST .../archive) rather than a bare DELETE, matching every other
# terminal-state mutation in this codebase (waiting_links' fulfil/cancel,
# attention_items' dismiss/restore) instead of introducing a new HTTP verb
# convention.
# ---------------------------------------------------------------------------


class PlanningConstraintList(BaseModel):
    items: list[PlanningConstraint]


def _lock_idempotency(session: Session, auth: AuthContext, key: str) -> None:
    lock_key = f"{auth.workspace_id}:{auth.user_id}:{key}"
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": lock_key},
    )


def _request_hash(payload: BaseModel, action: str) -> str:
    material = {"action": action, "payload": payload.model_dump(mode="json")}
    return sha256(dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _request_ids(request: Request) -> tuple[UUID, UUID]:
    try:
        return UUID(request.state.request_id), UUID(request.state.correlation_id)
    except (AttributeError, TypeError, ValueError):
        return uuid4(), uuid4()


def _load_cached(
    session: Session, auth: AuthContext, key: str, request_hash: str
) -> PlanningConstraint | None:
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
        record_idempotency_conflict("planning_constraints")
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
    return PlanningConstraint.model_validate(row["response_body"])


def _store_idempotency(
    session: Session,
    auth: AuthContext,
    key: str,
    request_hash: str,
    response: PlanningConstraint,
    now: datetime,
) -> None:
    session.execute(
        text(
            """
            INSERT INTO idempotency_records (
                workspace_id, actor_id, key, request_hash, response_status,
                response_body, created_at, expires_at
            ) VALUES (
                :workspace_id, :actor_id, :key, :request_hash, 201,
                CAST(:response_body AS jsonb), :created_at, :expires_at
            )
            """
        ),
        {
            "workspace_id": auth.workspace_id,
            "actor_id": auth.user_id,
            "key": key,
            "request_hash": request_hash,
            "response_body": dumps(response.model_dump(mode="json")),
            "created_at": now,
            "expires_at": now + timedelta(days=365),
        },
    )


def _write_event(
    session: Session,
    auth: AuthContext,
    request: Request,
    event_type: str,
    constraint_id: UUID,
    now: datetime,
) -> None:
    """Audit-only, no outbox/catalog event -- a minor sub-action, matching
    meeting_prep.py's participant-linking precedent."""
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
                    :id, :workspace_id, :event_type, 'planning_constraint', :aggregate_id,
                    1, :actor_id, :request_id, :correlation_id,
                    ARRAY['*'], 'allowed', 'user', '{}'::jsonb, :occurred_at
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": auth.workspace_id,
                "event_type": event_type,
                "aggregate_id": constraint_id,
                "actor_id": auth.user_id,
                "request_id": request_id,
                "correlation_id": correlation_id,
                "occurred_at": now,
            },
        )
    except SQLAlchemyError:
        record_audit_outbox_failure("planning_constraints")
        raise
    queue_lifecycle_event(session, "planning_constraint", event_type, "allowed")


@router.post("/constraints", response_model=PlanningConstraint, status_code=status.HTTP_201_CREATED)
def create_constraint_endpoint(
    payload: PlanningConstraintCreate,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> PlanningConstraint:
    request_hash = _request_hash(payload, "create")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached
        response = create_constraint(session, auth, payload)
        _write_event(session, auth, request, "planning_constraint.created", response.id, now)
        _store_idempotency(session, auth, idempotency_key, request_hash, response, now)
        return response


@router.get("/constraints", response_model=PlanningConstraintList)
def list_constraints_endpoint(auth: AuthDep, session: SessionDep) -> PlanningConstraintList:
    return PlanningConstraintList(items=list_active_constraints(session, auth))


@router.post("/constraints/{constraint_id}/archive", response_model=PlanningConstraint)
def archive_constraint_endpoint(
    constraint_id: UUID,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
) -> PlanningConstraint:
    """Idempotent, mirroring waiting.py's ``_terminate`` pattern: read the
    current row first (without the ``archived_at IS NULL`` filter) so a
    genuinely-missing row (404) can be told apart from a row that's already
    archived (200, no-op) -- a retried archive call must not 404 just
    because the first call already succeeded.
    """
    now = datetime.now(UTC)
    with session.begin():
        current = (
            session.execute(
                text(
                    f"SELECT {_FIELDS} FROM planning_constraints "
                    "WHERE workspace_id = :workspace_id AND user_id = :user_id "
                    "AND id = :id FOR UPDATE"
                ),
                {
                    "workspace_id": auth.workspace_id,
                    "user_id": auth.user_id,
                    "id": constraint_id,
                },
            )
            .mappings()
            .one_or_none()
        )
        if current is None:
            raise HTTPException(status_code=404, detail="PLANNING_CONSTRAINT_NOT_FOUND")
        if current["archived_at"] is not None:
            return PlanningConstraint.model_validate(dict(current))
        if not archive_constraint(session, auth, constraint_id):
            raise HTTPException(status_code=404, detail="PLANNING_CONSTRAINT_NOT_FOUND")
        row = (
            session.execute(
                text(
                    f"SELECT {_FIELDS} FROM planning_constraints "
                    "WHERE workspace_id = :workspace_id AND user_id = :user_id AND id = :id"
                ),
                {
                    "workspace_id": auth.workspace_id,
                    "user_id": auth.user_id,
                    "id": constraint_id,
                },
            )
            .mappings()
            .one()
        )
        response = PlanningConstraint.model_validate(dict(row))
        _write_event(session, auth, request, "planning_constraint.archived", constraint_id, now)
        return response
