from datetime import UTC, datetime
from json import dumps
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session
from ecc.domains.governance.recommendation_events import record_event, record_feedback
from ecc.domains.governance.recommendation_models import (
    ConfirmAction,
    DeferAction,
    PinAction,
    RecommendationCreate,
    RecommendationResponse,
    RejectAction,
    VersionAction,
)
from ecc.domains.governance.recommendation_storage import (
    FIELDS,
    check_version,
    expire_if_needed,
    get_row,
    load_cached,
    lock_idempotency,
    project,
    request_hash,
    save_cached,
)
from ecc.domains.governance.recommendation_targets import (
    execute_target,
    target_version,
    validate_action,
)

router = APIRouter(prefix="/api/v1/recommendations", tags=["recommendations"])
SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]


def _start(
    session: Session,
    auth: AuthContext,
    idempotency_key: str,
    digest: str,
) -> RecommendationResponse | None:
    lock_idempotency(session, auth, idempotency_key)
    return load_cached(session, auth, idempotency_key, digest)


@router.post("", response_model=RecommendationResponse, status_code=201)
def generate_recommendation(
    payload: RecommendationCreate,
    request: Request,
    auth: AuthDep,
    _: CsrfDep,
    session: SessionDep,
    idempotency_key: IdempotencyHeader,
) -> RecommendationResponse:
    validate_action(payload.target_type, payload.proposed_action)
    digest = request_hash(payload, "generate")
    cached = _start(session, auth, idempotency_key, digest)
    if cached is not None:
        return cached
    current_version = target_version(
        session,
        auth.workspace_id,
        payload.target_type,
        payload.target_id,
    )
    if current_version is None:
        raise HTTPException(status_code=404, detail="TARGET_NOT_FOUND")
    if current_version != payload.expected_version:
        raise HTTPException(status_code=409, detail="TARGET_VERSION_CONFLICT")
    now = datetime.now(UTC)
    row = (
        session.execute(
            text(
                f"""
            INSERT INTO recommendations (
                id, workspace_id, recommendation_type, target_type, target_id,
                proposed_action, expected_version, rationale, confidence, status,
                evidence_ids, expires_at, source, pinned, created_by, updated_by,
                created_at, updated_at, version
            ) VALUES (
                :id, :workspace_id, :recommendation_type, :target_type, :target_id,
                CAST(:proposed_action AS jsonb), :expected_version, :rationale,
                :confidence, 'proposed', :evidence_ids, :expires_at, :source,
                false, :actor_id, :actor_id, :created_at, :created_at, 1
            ) RETURNING {FIELDS}
            """
            ),
            {
                "id": uuid4(),
                "workspace_id": auth.workspace_id,
                "recommendation_type": payload.recommendation_type,
                "target_type": payload.target_type,
                "target_id": payload.target_id,
                "proposed_action": dumps(payload.proposed_action),
                "expected_version": payload.expected_version,
                "rationale": payload.rationale,
                "confidence": payload.confidence,
                "evidence_ids": payload.evidence_ids,
                "expires_at": payload.expires_at,
                "source": payload.source,
                "actor_id": auth.user_id,
                "created_at": now,
            },
        )
        .mappings()
        .one()
    )
    current = dict(row)
    response = project(current)
    record_event(
        request,
        session,
        auth,
        current,
        "recommendation.generated",
        None,
        ["status"],
        {
            "recommendation_id": str(current["id"]),
            "source": current["source"],
            "evidence_ids": [str(value) for value in current["evidence_ids"]],
            "confidence": float(current["confidence"]),
        },
    )
    save_cached(session, auth, idempotency_key, digest, response, 201, now)
    session.commit()
    return response


def _transition(
    recommendation_id: UUID,
    payload: BaseModel,
    request: Request,
    auth: AuthContext,
    session: Session,
    idempotency_key: str,
    *,
    action_name: str,
    allowed_statuses: set[str],
    updates: dict[str, Any],
    feedback_action: str | None = None,
    feedback_reason: str | None = None,
    feedback_defer_until: datetime | None = None,
) -> RecommendationResponse:
    digest = request_hash(payload, action_name)
    cached = _start(session, auth, idempotency_key, digest)
    if cached is not None:
        return cached
    row = expire_if_needed(
        session,
        auth,
        get_row(session, auth, recommendation_id, for_update=True),
    )
    check_version(row, int(payload.expected_version))
    if row["status"] not in allowed_statuses:
        raise HTTPException(status_code=409, detail="INVALID_RECOMMENDATION_STATE")
    before = {
        "status": row["status"],
        "pinned": row["pinned"],
        "deferred_until": row["deferred_until"].isoformat() if row["deferred_until"] else None,
    }
    clauses = ["version=version+1", "updated_at=:updated_at", "updated_by=:actor_id"]
    params: dict[str, Any] = {
        "workspace_id": auth.workspace_id,
        "recommendation_id": recommendation_id,
        "updated_at": datetime.now(UTC),
        "actor_id": auth.user_id,
    }
    for field, value in updates.items():
        clauses.append(f"{field}=:{field}")
        params[field] = value
    updated = (
        session.execute(
            text(
                f"UPDATE recommendations SET {', '.join(clauses)} "
                f"WHERE workspace_id=:workspace_id AND id=:recommendation_id RETURNING {FIELDS}"
            ),
            params,
        )
        .mappings()
        .one()
    )
    current = dict(updated)
    if feedback_action is not None:
        record_feedback(
            session,
            auth,
            recommendation_id,
            feedback_action,
            reason=feedback_reason,
            defer_until=feedback_defer_until,
        )
    record_event(
        request,
        session,
        auth,
        current,
        action_name,
        before,
        list(updates),
    )
    response = project(current)
    save_cached(
        session,
        auth,
        idempotency_key,
        digest,
        response,
        200,
        params["updated_at"],
    )
    session.commit()
    return response


@router.post("/{recommendation_id}/publish", response_model=RecommendationResponse)
def publish_recommendation(
    recommendation_id: UUID,
    payload: VersionAction,
    request: Request,
    auth: AuthDep,
    _: CsrfDep,
    session: SessionDep,
    idempotency_key: IdempotencyHeader,
) -> RecommendationResponse:
    return _transition(
        recommendation_id,
        payload,
        request,
        auth,
        session,
        idempotency_key,
        action_name="recommendation.confirmation_requested",
        allowed_statuses={"proposed"},
        updates={"status": "pending_confirmation"},
    )


@router.post("/{recommendation_id}/reject", response_model=RecommendationResponse)
def reject_recommendation(
    recommendation_id: UUID,
    payload: RejectAction,
    request: Request,
    auth: AuthDep,
    _: CsrfDep,
    session: SessionDep,
    idempotency_key: IdempotencyHeader,
) -> RecommendationResponse:
    return _transition(
        recommendation_id,
        payload,
        request,
        auth,
        session,
        idempotency_key,
        action_name="recommendation.rejected",
        allowed_statuses={"pending_confirmation"},
        updates={"status": "rejected"},
        feedback_action="reject",
        feedback_reason=payload.reason,
    )


@router.post("/{recommendation_id}/defer", response_model=RecommendationResponse)
def defer_recommendation(
    recommendation_id: UUID,
    payload: DeferAction,
    request: Request,
    auth: AuthDep,
    _: CsrfDep,
    session: SessionDep,
    idempotency_key: IdempotencyHeader,
) -> RecommendationResponse:
    if payload.defer_until <= datetime.now(UTC):
        raise HTTPException(status_code=422, detail="DEFER_UNTIL_MUST_BE_FUTURE")
    return _transition(
        recommendation_id,
        payload,
        request,
        auth,
        session,
        idempotency_key,
        action_name="recommendation.deferred",
        allowed_statuses={"proposed", "pending_confirmation"},
        updates={"deferred_until": payload.defer_until},
        feedback_action="defer",
        feedback_defer_until=payload.defer_until,
    )


@router.post("/{recommendation_id}/pin", response_model=RecommendationResponse)
def pin_recommendation(
    recommendation_id: UUID,
    payload: PinAction,
    request: Request,
    auth: AuthDep,
    _: CsrfDep,
    session: SessionDep,
    idempotency_key: IdempotencyHeader,
) -> RecommendationResponse:
    return _transition(
        recommendation_id,
        payload,
        request,
        auth,
        session,
        idempotency_key,
        action_name="recommendation.pinned",
        allowed_statuses={"proposed", "pending_confirmation"},
        updates={"pinned": payload.pinned},
        feedback_action="pin",
    )


@router.post("/{recommendation_id}/confirm", response_model=RecommendationResponse)
def confirm_recommendation(
    recommendation_id: UUID,
    payload: ConfirmAction,
    request: Request,
    auth: AuthDep,
    _: CsrfDep,
    session: SessionDep,
    idempotency_key: IdempotencyHeader,
) -> RecommendationResponse:
    digest = request_hash(payload, "recommendation.confirm")
    cached = _start(session, auth, idempotency_key, digest)
    if cached is not None:
        return cached
    row = expire_if_needed(
        session,
        auth,
        get_row(session, auth, recommendation_id, for_update=True),
    )
    check_version(row, payload.expected_version)
    if row["status"] != "pending_confirmation":
        raise HTTPException(status_code=409, detail="INVALID_RECOMMENDATION_STATE")
    if row["deferred_until"] is not None and row["deferred_until"] > datetime.now(UTC):
        raise HTTPException(status_code=409, detail="RECOMMENDATION_DEFERRED")
    accepted_at = datetime.now(UTC)
    accepted = (
        session.execute(
            text(
                f"""
            UPDATE recommendations
            SET status='accepted', confirmed_by=:actor_id, confirmed_at=:confirmed_at,
                version=version+1, updated_at=:confirmed_at, updated_by=:actor_id
            WHERE workspace_id=:workspace_id AND id=:recommendation_id
            RETURNING {FIELDS}
            """
            ),
            {
                "actor_id": auth.user_id,
                "confirmed_at": accepted_at,
                "workspace_id": auth.workspace_id,
                "recommendation_id": recommendation_id,
            },
        )
        .mappings()
        .one()
    )
    accepted_row = dict(accepted)
    record_feedback(session, auth, recommendation_id, "accept")
    record_event(
        request,
        session,
        auth,
        accepted_row,
        "recommendation.accepted",
        {"status": "pending_confirmation"},
        ["status", "confirmed_by", "confirmed_at"],
    )
    execution_result = execute_target(
        session,
        auth,
        row["target_type"],
        row["target_id"],
        row["proposed_action"],
        payload.target_expected_version,
    )
    executed_at = datetime.now(UTC)
    executed = (
        session.execute(
            text(
                f"""
            UPDATE recommendations
            SET status='executed', execution_result=CAST(:execution_result AS jsonb),
                version=version+1, updated_at=:updated_at, updated_by=:actor_id
            WHERE workspace_id=:workspace_id AND id=:recommendation_id
            RETURNING {FIELDS}
            """
            ),
            {
                "execution_result": dumps(execution_result),
                "updated_at": executed_at,
                "actor_id": auth.user_id,
                "workspace_id": auth.workspace_id,
                "recommendation_id": recommendation_id,
            },
        )
        .mappings()
        .one()
    )
    current = dict(executed)
    record_event(
        request,
        session,
        auth,
        current,
        "recommendation.executed",
        {"status": "accepted"},
        ["status", "execution_result"],
        {"recommendation_id": str(recommendation_id), **execution_result},
    )
    response = project(current)
    save_cached(session, auth, idempotency_key, digest, response, 200, executed_at)
    session.commit()
    return response
