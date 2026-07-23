from datetime import UTC, datetime, timedelta
from hashlib import sha256
from json import dumps
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session
from ecc.observability import queue_lifecycle_event, record_audit_outbox_failure

router = APIRouter(prefix="/api/v1/risks", tags=["risks"])
SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]

ReviewOutcome = Literal["no_change", "escalated", "de_escalated", "mitigated", "closed"]

_REVIEW_FIELDS = """
    id, risk_id, outcome, notes, evidence_refs, reviewed_at, next_review_at, actor_id
"""


class RiskReview(BaseModel):
    id: UUID
    risk_id: UUID
    outcome: ReviewOutcome
    notes: str | None
    evidence_refs: list[str]
    reviewed_at: datetime
    next_review_at: datetime | None
    actor_id: UUID


class RiskReviewCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    outcome: ReviewOutcome
    notes: str | None = Field(default=None, max_length=5000)
    evidence_refs: list[str] = Field(default_factory=list, max_length=50)
    next_review_at: datetime | None = None

    @field_validator("next_review_at")
    @classmethod
    def _require_tz(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("next_review_at must include a timezone offset")
        return value


class ReviewQueueItem(BaseModel):
    risk_id: UUID
    description: str
    status: str
    review_at: datetime | None
    urgency: Literal["overdue", "due_soon", "scheduled", "unscheduled"]
    version: int


class ReviewQueueList(BaseModel):
    items: list[ReviewQueueItem]


def _evidence_state(session: Session, auth: AuthContext, evidence_id: UUID) -> str | None:
    """Same lookup as ``claims.py``'s ``_evidence_state`` -- ``pkos_evidence``
    scoped to the caller's workspace. Returns ``None`` when no such
    evidence row exists in this workspace at all.
    """
    row = session.execute(
        text(
            "SELECT evidence_state FROM pkos_evidence"
            " WHERE workspace_id = :workspace_id AND id = :evidence_id"
        ),
        {"workspace_id": auth.workspace_id, "evidence_id": evidence_id},
    ).one_or_none()
    return row[0] if row is not None else None


def _validate_evidence_refs(session: Session, auth: AuthContext, evidence_refs: list[str]) -> None:
    """API-SCHEMAS.md names ``evidence_unavailable`` as a required Phase 3
    error code, but ``evidence_refs`` (migration 0024) was never validated
    at all -- any string, including a reference to evidence that doesn't
    exist or is no longer available, was accepted and persisted verbatim.

    Migration 0024's own docstring documents ``evidence_refs`` as
    deliberately free text -- "URLs, document names, evidence IDs quoted
    as text" -- since risk reviews predate Phase 2's evidence model and
    not every review cites Phase 2 evidence specifically. So only entries
    that are themselves well-formed UUIDs are treated as a reference into
    ``pkos_evidence`` and checked (mirroring ``claims.py``'s
    ``_evidence_state`` check for the same ``EVIDENCE_UNAVAILABLE`` code);
    a non-UUID ref (a URL, a document name) is free text by design and
    passes through unchecked, exactly as migration 0024 intends.
    """
    for ref in evidence_refs:
        try:
            evidence_id = UUID(ref)
        except ValueError:
            continue
        state = _evidence_state(session, auth, evidence_id)
        if state is None or state != "available":
            raise HTTPException(status_code=422, detail="EVIDENCE_UNAVAILABLE")


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
) -> RiskReview | None:
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
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
    return RiskReview.model_validate(row["response_body"])


@router.post("/{risk_id}/review", response_model=RiskReview, status_code=status.HTTP_201_CREATED)
def record_risk_review(
    risk_id: UUID,
    payload: RiskReviewCreate,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> RiskReview:
    """Records a review and updates ``risks.review_at``/``version`` in the
    same transaction as the ``risk_reviews`` insert -- a single dual-write,
    matching every existing dual-write pattern in this codebase (e.g.
    entity_operations.py's merge/reverse, claims.py's supersede).
    ``risks.py``'s existing CRUD and its ``review_overdue``/
    ``review_due_soon`` scoring factors (already live in
    ``attention.py:_score_risk``) are unmodified by this endpoint.
    """
    request_hash = _request_hash(payload, f"review:{risk_id}")
    now = datetime.now(UTC)
    review_id = uuid4()
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached
        risk = (
            session.execute(
                text(
                    "SELECT version, archived_at, review_at FROM risks "
                    "WHERE workspace_id = :workspace_id AND id = :risk_id FOR UPDATE"
                ),
                {"workspace_id": auth.workspace_id, "risk_id": risk_id},
            )
            .mappings()
            .one_or_none()
        )
        if risk is None:
            raise HTTPException(status_code=404, detail="RISK_NOT_FOUND")
        if risk["archived_at"] is not None:
            raise HTTPException(status_code=409, detail="RISK_ARCHIVED")
        if risk["version"] != payload.expected_version:
            raise HTTPException(status_code=409, detail="VERSION_CONFLICT")

        _validate_evidence_refs(session, auth, payload.evidence_refs)

        # A review only changes the next-review cadence when it explicitly
        # sets one, or when the outcome closes the risk out entirely (no
        # further review is needed, so any existing schedule is cleared).
        # Every other outcome (no_change/escalated/de_escalated/mitigated)
        # recorded *without* an explicit next_review_at must leave the
        # risk's existing review_at alone -- unconditionally nulling it
        # here would silently cancel a previously scheduled review every
        # time someone records an outcome that doesn't happen to set a new
        # one (finding #2).
        if payload.outcome == "closed":
            next_review_at = None
        elif payload.next_review_at is not None:
            next_review_at = payload.next_review_at
        else:
            next_review_at = risk["review_at"]

        review_row = (
            session.execute(
                text(
                    f"""
                    INSERT INTO risk_reviews (
                        id, workspace_id, risk_id, outcome, notes, evidence_refs,
                        reviewed_at, next_review_at, actor_id
                    ) VALUES (
                        :id, :workspace_id, :risk_id, :outcome, :notes, :evidence_refs,
                        :reviewed_at, :next_review_at, :actor_id
                    )
                    RETURNING {_REVIEW_FIELDS}
                    """
                ),
                {
                    "id": review_id,
                    "workspace_id": auth.workspace_id,
                    "risk_id": risk_id,
                    "outcome": payload.outcome,
                    "notes": payload.notes,
                    "evidence_refs": payload.evidence_refs,
                    "reviewed_at": now,
                    "next_review_at": payload.next_review_at,
                    "actor_id": auth.user_id,
                },
            )
            .mappings()
            .one()
        )
        new_status = "closed" if payload.outcome == "closed" else None
        session.execute(
            text(
                """
                UPDATE risks
                SET review_at = :next_review_at, updated_by = :actor_id,
                    updated_at = :now, version = version + 1
                    """
                + (", status = :new_status" if new_status else "")
                + """
                WHERE workspace_id = :workspace_id AND id = :risk_id
                """
            ),
            {
                "next_review_at": next_review_at,
                "actor_id": auth.user_id,
                "now": now,
                "workspace_id": auth.workspace_id,
                "risk_id": risk_id,
                **({"new_status": new_status} if new_status else {}),
            },
        )
        response = RiskReview.model_validate(dict(review_row))
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
                        :id, :workspace_id, 'risk_review.recorded', 'risk', :aggregate_id,
                        :aggregate_version, :actor_id, :request_id, :correlation_id,
                        ARRAY['review_at','version'], 'allowed', 'user', '{}'::jsonb, :occurred_at
                    )
                    """
                ),
                {
                    "id": uuid4(),
                    "workspace_id": auth.workspace_id,
                    "aggregate_id": risk_id,
                    "aggregate_version": risk["version"] + 1,
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
                        :event_id, :workspace_id, 'risk_review.recorded.v1', 1,
                        :correlation_id, CAST(:payload AS jsonb), :occurred_at, 0
                    )
                    """
                ),
                {
                    "event_id": uuid4(),
                    "workspace_id": auth.workspace_id,
                    "correlation_id": correlation_id,
                    "payload": dumps({"risk_id": str(risk_id), "review_id": str(review_id)}),
                    "occurred_at": now,
                },
            )
        except SQLAlchemyError:
            record_audit_outbox_failure("risk_reviews")
            raise
        queue_lifecycle_event(session, "risk", "risk_review.recorded", "allowed")

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
                "key": idempotency_key,
                "request_hash": request_hash,
                "response_body": dumps(response.model_dump(mode="json")),
                "created_at": now,
                "expires_at": now + timedelta(days=365),
            },
        )
        return response


@router.get("/review-queue", response_model=ReviewQueueList)
def list_review_queue(
    auth: AuthDep, session: SessionDep, limit: int = Query(default=50, ge=1, le=100)
) -> ReviewQueueList:
    now = datetime.now(UTC)
    rows = (
        session.execute(
            text(
                """
                SELECT id, description, status, review_at, version
                FROM risks
                WHERE workspace_id = :workspace_id AND archived_at IS NULL
                  AND status <> 'closed' AND review_at IS NOT NULL
                ORDER BY review_at ASC
                LIMIT :limit
                """
            ),
            {"workspace_id": auth.workspace_id, "limit": limit},
        )
        .mappings()
        .all()
    )
    items: list[ReviewQueueItem] = []
    for row in rows:
        review_at: datetime = row["review_at"]
        delta = review_at - now
        if delta.total_seconds() < 0:
            urgency: Literal["overdue", "due_soon", "scheduled", "unscheduled"] = "overdue"
        elif delta <= timedelta(hours=48):
            urgency = "due_soon"
        else:
            urgency = "scheduled"
        items.append(
            ReviewQueueItem(
                risk_id=row["id"],
                description=row["description"],
                status=row["status"],
                review_at=review_at,
                urgency=urgency,
                version=row["version"],
            )
        )
    return ReviewQueueList(items=items)
