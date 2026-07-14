from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import compare_digest, new
from json import dumps, loads
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.config import get_settings
from ecc.database import get_session

router = APIRouter(prefix="/api/v1/risks", tags=["risks"])

RiskStatus = Literal[
    "identified",
    "assessed",
    "monitoring",
    "mitigating",
    "materialized",
    "closed",
]
SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]

_RISK_FIELDS = """
id, description, probability, impact, status, owner_id, mitigation, trigger,
review_at, project_id, pinned, created_at, updated_at, version, archived_at,
pre_archive_status
"""


class RiskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    description: str = Field(min_length=1, max_length=5000)
    probability: int = Field(ge=1, le=5)
    impact: int = Field(ge=1, le=5)
    status: RiskStatus = "identified"
    mitigation: str | None = None
    trigger: str | None = None
    review_at: datetime | None = None
    project_id: UUID | None = None
    pinned: bool = False

    @field_validator("review_at")
    @classmethod
    def validate_review_at(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("review_at must include a timezone offset")
        return value


class RiskResponse(BaseModel):
    id: UUID
    description: str
    probability: int
    impact: int
    status: RiskStatus
    owner_id: UUID
    mitigation: str | None
    trigger: str | None
    review_at: datetime | None
    project_id: UUID | None
    pinned: bool
    priority_impact: int
    score: int
    factors: list[dict[str, Any]]
    explanation: str
    created_at: datetime
    updated_at: datetime
    version: int
    archived_at: datetime | None
    pre_archive_status: str | None


class RiskListResponse(BaseModel):
    items: list[RiskResponse]
    next_cursor: str | None = None


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


def _risk_factors(row: dict[str, Any], now: datetime) -> tuple[int, list[dict[str, Any]], str]:
    factors: list[dict[str, Any]] = []
    risk_impact = int(row["probability"]) * int(row["impact"])
    if risk_impact >= 20:
        points = 25
    elif risk_impact >= 12:
        points = 15
    elif risk_impact >= 6:
        points = 8
    else:
        points = 0
    if points:
        factors.append(
            {
                "code": "risk_impact",
                "label": f"Risk impact {risk_impact}",
                "points": points,
                "source_field": "probability,impact",
            }
        )
    if row["pinned"]:
        factors.append(
            {
                "code": "pinned",
                "label": "Explicitly pinned",
                "points": 20,
                "source_field": "pinned",
            }
        )
    review_at = row.get("review_at")
    if review_at is not None:
        delta = review_at - now
        if delta.total_seconds() < 0:
            factors.append(
                {
                    "code": "review_overdue",
                    "label": "Risk review overdue",
                    "points": 35,
                    "source_field": "review_at",
                }
            )
        elif delta <= timedelta(hours=48):
            factors.append(
                {
                    "code": "review_due_soon",
                    "label": "Risk review due within 48 hours",
                    "points": 15,
                    "source_field": "review_at",
                }
            )
    age = now - row["updated_at"]
    if age >= timedelta(days=14):
        factors.append(
            {
                "code": "stale_14d",
                "label": "No movement for 14 days",
                "points": 8,
                "source_field": "updated_at",
            }
        )
    elif age >= timedelta(days=7):
        factors.append(
            {
                "code": "stale_7d",
                "label": "No movement for 7 days",
                "points": 4,
                "source_field": "updated_at",
            }
        )
    score = sum(int(factor["points"]) for factor in factors)
    score = min(100 if row["pinned"] else 95, score)
    explanation = (
        "; ".join(str(factor["label"]) for factor in factors) or "No active priority factors"
    )
    return score, factors, explanation


def _project(row: dict[str, Any], now: datetime | None = None) -> RiskResponse:
    current = now or datetime.now(UTC)
    score, factors, explanation = _risk_factors(row, current)
    return RiskResponse(
        **{key: value for key, value in row.items() if key in RiskResponse.model_fields},
        priority_impact=int(row["probability"]) * int(row["impact"]),
        score=score,
        factors=factors,
        explanation=explanation,
    )


def _encode_cursor(updated_at: datetime, risk_id: UUID) -> str:
    payload = dumps({"updated_at": updated_at.isoformat(), "id": str(risk_id)}).encode()
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
        return datetime.fromisoformat(decoded["updated_at"]), UUID(decoded["id"])
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="MALFORMED_CURSOR") from exc


def _get_row(session: Session, auth: AuthContext, risk_id: UUID) -> dict[str, Any] | None:
    row = (
        session.execute(
            text(
                f"""
                SELECT {_RISK_FIELDS}
                FROM risks
                WHERE workspace_id = :workspace_id AND id = :risk_id
                """
            ),
            {"workspace_id": auth.workspace_id, "risk_id": risk_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _load_cached(
    session: Session,
    auth: AuthContext,
    key: str,
    request_hash: str,
) -> RiskResponse | None:
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
    return RiskResponse.model_validate(row["response_body"])


def _write_side_effects(
    session: Session,
    auth: AuthContext,
    request: Request,
    risk_id: UUID,
    version: int,
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
                :id, :workspace_id, 'risk.created', 'risk', :aggregate_id,
                :aggregate_version, :actor_id, :request_id, :correlation_id,
                ARRAY['*'], 'allowed', 'user', '{}'::jsonb, :occurred_at
            )
            """
        ),
        {
            "id": uuid4(),
            "workspace_id": auth.workspace_id,
            "aggregate_id": risk_id,
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
                :event_id, :workspace_id, 'risk.created.v1', 1,
                :correlation_id, CAST(:payload AS jsonb), :occurred_at, 0
            )
            """
        ),
        {
            "event_id": uuid4(),
            "workspace_id": auth.workspace_id,
            "correlation_id": correlation_id,
            "payload": dumps({"risk_id": str(risk_id), "version": version}),
            "occurred_at": now,
        },
    )


@router.post("", response_model=RiskResponse, status_code=status.HTTP_201_CREATED)
def create_risk(
    payload: RiskCreate,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> RiskResponse:
    request_hash = _request_hash(payload, "create")
    now = datetime.now(UTC)
    risk_id = uuid4()
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached
        row = (
            session.execute(
                text(
                    f"""
                    INSERT INTO risks (
                        id, workspace_id, description, probability, impact, status,
                        owner_id, mitigation, trigger, review_at, project_id, pinned,
                        created_by, updated_by, created_at, updated_at, version
                    ) VALUES (
                        :id, :workspace_id, :description, :probability, :impact, :status,
                        :owner_id, :mitigation, :trigger, :review_at, :project_id, :pinned,
                        :actor_id, :actor_id, :now, :now, 1
                    )
                    RETURNING {_RISK_FIELDS}
                    """
                ),
                {
                    "id": risk_id,
                    "workspace_id": auth.workspace_id,
                    "description": payload.description,
                    "probability": payload.probability,
                    "impact": payload.impact,
                    "status": payload.status,
                    "owner_id": auth.user_id,
                    "mitigation": payload.mitigation,
                    "trigger": payload.trigger,
                    "review_at": payload.review_at,
                    "project_id": payload.project_id,
                    "pinned": payload.pinned,
                    "actor_id": auth.user_id,
                    "now": now,
                },
            )
            .mappings()
            .one()
        )
        response = _project(dict(row), now)
        _write_side_effects(session, auth, request, risk_id, 1, now)
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


@router.get("", response_model=RiskListResponse)
def list_risks(
    auth: AuthDep,
    session: SessionDep,
    status_filter: Annotated[RiskStatus | None, Query(alias="status")] = None,
    include_archived: bool = False,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> RiskListResponse:
    clauses = ["workspace_id = :workspace_id"]
    params: dict[str, Any] = {"workspace_id": auth.workspace_id, "limit": limit + 1}
    if not include_archived:
        clauses.append("archived_at IS NULL")
    if status_filter is not None:
        clauses.append("status = :status")
        params["status"] = status_filter
    if cursor is not None:
        updated_at, risk_id = _decode_cursor(cursor)
        clauses.append("(updated_at, id) < (:cursor_updated_at, :cursor_id)")
        params.update({"cursor_updated_at": updated_at, "cursor_id": risk_id})
    rows = (
        session.execute(
            text(
                f"""
                SELECT {_RISK_FIELDS}
                FROM risks
                WHERE {" AND ".join(clauses)}
                ORDER BY updated_at DESC, id DESC
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
        next_cursor = _encode_cursor(last["updated_at"], last["id"])
    return RiskListResponse(
        items=[_project(dict(row)) for row in page],
        next_cursor=next_cursor,
    )


@router.get("/{risk_id}", response_model=RiskResponse)
def get_risk(risk_id: UUID, auth: AuthDep, session: SessionDep) -> RiskResponse:
    row = _get_row(session, auth, risk_id)
    if row is None:
        raise HTTPException(status_code=404, detail="RISK_NOT_FOUND")
    return _project(row)
