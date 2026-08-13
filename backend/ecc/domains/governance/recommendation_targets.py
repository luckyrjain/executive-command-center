from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi import HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext
from ecc.domains.communication.commitments import (
    CommitmentCreate,
    insert_commitment,
    lifecycle_write,
)
from ecc.domains.governance.risk_mutations import set_risk_status_write
from ecc.domains.governance.risks import RiskCreate, insert_risk
from ecc.domains.planning.tasks import (
    TaskCreate,
    insert_task,
    lifecycle_task_write,
    set_task_status_write,
)

# Commitment status values a recommendation may target -- "confirmed" is
# deliberately absent: it's `CommitmentCreate`'s own create-time-only value
# (see that model's `status` field), never a real transition target, and
# nothing in this codebase has ever generated a recommendation naming it.
# "broken" IS a real transition as of the `POST .../break` endpoint added
# alongside this fix -- see `commitments.py`'s `lifecycle_write` docstring.
_COMMITMENT_STATUS_ACTIONS: dict[
    str, Literal["confirm", "fulfil", "cancel", "break", "archive", "restore"]
] = {
    "active": "confirm",
    "fulfilled": "fulfil",
    "broken": "break",
    "cancelled": "cancel",
}
# Task status values with a dedicated lifecycle action (side-effect fields,
# terminal-state guard) vs. values `set_task_status_write` handles as a
# plain, non-terminal field write -- see that function's own docstring.
_TASK_STATUS_ACTIONS: dict[str, Literal["complete", "cancel", "archive", "restore"]] = {
    "completed": "complete",
    "cancelled": "cancel",
}

_ALLOWED: dict[str, dict[str, set[Any]]] = {
    "task": {
        "set_status": {
            "captured",
            "planned",
            "in_progress",
            "blocked",
            "completed",
            "cancelled",
        },
        "set_priority": {"low", "medium", "high", "critical"},
        "set_pinned": {True, False},
        # Phase 10 Task 4: proposes a brand-new row rather than a change to
        # an existing one -- no scalar "value" to whitelist the way every
        # other operation has, so the allowed set is the single sentinel
        # `validate_action`'s own generic `value not in _ALLOWED[...]`
        # check expects `proposed_action={"operation": "create", "value":
        # None}` to satisfy; the actual new-row fields live in
        # `proposed_fields`, validated separately against `TaskCreate`/
        # `CommitmentCreate`/`RiskCreate` (see `_execute_create`).
        "create": {None},
    },
    "commitment": {
        "set_status": {"active", "fulfilled", "broken", "cancelled"},
        "set_importance": {"low", "medium", "high", "critical"},
        "set_pinned": {True, False},
        "create": {None},
    },
    "risk": {
        "set_status": {
            "identified",
            "assessed",
            "monitoring",
            "mitigating",
            "materialized",
            "closed",
        },
        "set_probability": {1, 2, 3, 4, 5},
        "set_impact": {1, 2, 3, 4, 5},
        "set_pinned": {True, False},
        "create": {None},
    },
}


def validate_action(target_type: str, action: dict[str, Any]) -> None:
    operation = action.get("operation")
    value = action.get("value")
    if set(action) != {"operation", "value"}:
        raise HTTPException(status_code=422, detail="INVALID_PROPOSED_ACTION_SHAPE")
    if target_type not in _ALLOWED or operation not in _ALLOWED[target_type]:
        raise HTTPException(status_code=422, detail="UNSUPPORTED_PROPOSED_ACTION")
    if value not in _ALLOWED[target_type][operation]:
        raise HTTPException(status_code=422, detail="INVALID_PROPOSED_ACTION_VALUE")


def target_version(
    session: Session,
    workspace_id: UUID,
    target_type: str,
    target_id: UUID,
) -> int | None:
    queries = {
        "task": """
            SELECT version FROM tasks
            WHERE workspace_id=:workspace_id AND id=:target_id
              AND archived_at IS NULL
        """,
        "commitment": """
            SELECT version FROM commitments
            WHERE workspace_id=:workspace_id AND id=:target_id
              AND archived_at IS NULL
        """,
        "risk": """
            SELECT version FROM risks
            WHERE workspace_id=:workspace_id AND id=:target_id
              AND archived_at IS NULL
        """,
    }
    query = queries.get(target_type)
    if query is None:
        raise HTTPException(status_code=422, detail="UNSUPPORTED_TARGET_TYPE")
    value = session.execute(
        text(query),
        {"workspace_id": workspace_id, "target_id": target_id},
    ).scalar_one_or_none()
    return int(value) if value is not None else None


def _update_query(table: str, column: str) -> str:
    return f"""
        UPDATE {table}
        SET {column}=:value, version=version+1,
            updated_at=:now, updated_by=:actor
        WHERE workspace_id=:workspace_id AND id=:target_id
          AND version=:expected_version AND archived_at IS NULL
        RETURNING id,version
    """


def _request_ids(request: Request) -> tuple[UUID, UUID]:
    request_id = getattr(request.state, "request_id", None)
    correlation_id = getattr(request.state, "correlation_id", None)
    try:
        return UUID(request_id), UUID(correlation_id)
    except (TypeError, ValueError):
        return uuid4(), uuid4()


def _execute_create(
    session: Session,
    auth: AuthContext,
    request: Request,
    recommendation_id: UUID,
    target_type: str,
    proposed_fields: dict[str, Any] | None,
) -> dict[str, Any]:
    """Confirming an `operation="create"` recommendation calls the exact
    same `insert_task`/`insert_commitment`/`insert_risk` helper `POST
    /api/v1/tasks`/`/commitments`/`/risks` itself calls -- no second insert
    path -- inside `confirm_recommendation`'s own already-open transaction
    (so none of the three take a `session.begin()` of their own). Re-parses
    `proposed_fields` against the matching `*Create` model at execute time,
    not just at generation time (`RecommendationCreate`'s own model
    validator already did this once) -- defense in depth against a stored
    JSONB payload that predates a later, stricter version of that model.
    """
    if not proposed_fields:
        raise HTTPException(status_code=422, detail="PROPOSED_FIELDS_REQUIRED")
    now = datetime.now(UTC)
    request_id, correlation_id = _request_ids(request)
    idempotency_key = f"recommendation-confirm:{recommendation_id}"
    if target_type == "task":
        try:
            task_payload = TaskCreate(**proposed_fields)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail="INVALID_PROPOSED_FIELDS") from exc
        task_response = insert_task(
            session,
            auth,
            task_payload,
            request_id=request_id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            now=now,
        )
        return {
            "target_type": "task",
            "target_id": str(task_response.id),
            "resulting_version": task_response.version,
            "operation": "create",
        }
    if target_type == "commitment":
        try:
            commitment_payload = CommitmentCreate(**proposed_fields)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail="INVALID_PROPOSED_FIELDS") from exc
        commitment_response = insert_commitment(
            session,
            auth,
            commitment_payload,
            request_id=request_id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            now=now,
        )
        return {
            "target_type": "commitment",
            "target_id": str(commitment_response.id),
            "resulting_version": commitment_response.version,
            "operation": "create",
        }
    if target_type == "risk":
        try:
            risk_payload = RiskCreate(**proposed_fields)
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail="INVALID_PROPOSED_FIELDS") from exc
        risk_response = insert_risk(session, auth, risk_payload, request, now)
        return {
            "target_type": "risk",
            "target_id": str(risk_response.id),
            "resulting_version": risk_response.version,
            "operation": "create",
        }
    raise HTTPException(status_code=422, detail="UNSUPPORTED_TARGET_TYPE")


def _execute_set_status(
    session: Session,
    auth: AuthContext,
    request: Request,
    recommendation_id: UUID,
    target_type: str,
    target_id: UUID,
    value: Any,
    expected_version: int,
) -> dict[str, Any]:
    """Routes a `set_status` recommendation through the target's own real
    transition logic instead of the generic column-patch `_update_query`
    every other operation below uses. Status is a state-machine
    transition -- side-effect fields (`fulfilled_at`/`completed_at`),
    terminal-state guards, and an audit trail -- not a plain field write;
    the generic raw `UPDATE` used to bypass all three, letting a
    recommendation un-terminal an already-fulfilled/broken/cancelled
    commitment with no audit trail at all (architecture-review finding).
    `idempotency_key` is synthetic, matching `_execute_create`'s own
    precedent exactly -- distinct from `confirm_recommendation`'s own
    caller-supplied `Idempotency-Key`, which already wraps this whole call
    in its own lock/cache/replay.
    """
    now = datetime.now(UTC)
    request_id, correlation_id = _request_ids(request)
    idempotency_key = f"recommendation-confirm:{recommendation_id}"
    if target_type == "commitment":
        commitment_response = lifecycle_write(
            session,
            auth,
            target_id,
            _COMMITMENT_STATUS_ACTIONS[value],
            expected_version=expected_version,
            reason=None,
            request_id=request_id,
            correlation_id=correlation_id,
            idempotency_key=idempotency_key,
            now=now,
        )
        version = commitment_response.version
    elif target_type == "task":
        if value in _TASK_STATUS_ACTIONS:
            task_response = lifecycle_task_write(
                session,
                auth,
                target_id,
                _TASK_STATUS_ACTIONS[value],
                expected_version=expected_version,
                reason=None,
                request_id=request_id,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
                now=now,
            )
        else:
            task_response = set_task_status_write(
                session,
                auth,
                target_id,
                value,
                expected_version=expected_version,
                request_id=request_id,
                correlation_id=correlation_id,
                idempotency_key=idempotency_key,
                now=now,
            )
        version = task_response.version
    elif target_type == "risk":
        risk_response = set_risk_status_write(
            session, auth, request, target_id, value, expected_version=expected_version, now=now
        )
        version = risk_response.version
    else:
        raise HTTPException(status_code=422, detail="UNSUPPORTED_TARGET_TYPE")
    return {
        "target_type": target_type,
        "target_id": str(target_id),
        "resulting_version": version,
        "operation": "set_status",
    }


def execute_target(
    session: Session,
    auth: AuthContext,
    request: Request,
    recommendation_id: UUID,
    target_type: str,
    target_id: UUID | None,
    action: dict[str, Any],
    expected_version: int | None,
    proposed_fields: dict[str, Any] | None,
) -> dict[str, Any]:
    validate_action(target_type, action)
    operation = action["operation"]
    if operation == "create":
        return _execute_create(
            session, auth, request, recommendation_id, target_type, proposed_fields
        )
    if target_id is None or expected_version is None:
        # `RecommendationCreate`'s own model validator guarantees every
        # non-create recommendation has both set at generation time, so
        # this should be unreachable -- a defensive 500, not a user-facing
        # validation path, if that invariant is ever violated.
        raise HTTPException(status_code=500, detail="TARGET_ID_OR_VERSION_MISSING")
    if operation == "set_status":
        return _execute_set_status(
            session,
            auth,
            request,
            recommendation_id,
            target_type,
            target_id,
            action["value"],
            expected_version,
        )
    columns = {
        ("task", "set_priority"): ("tasks", "manual_priority"),
        ("task", "set_pinned"): ("tasks", "pinned"),
        ("commitment", "set_importance"): ("commitments", "importance"),
        ("commitment", "set_pinned"): ("commitments", "pinned"),
        ("risk", "set_probability"): ("risks", "probability"),
        ("risk", "set_impact"): ("risks", "impact"),
        ("risk", "set_pinned"): ("risks", "pinned"),
    }
    table, column = columns[(target_type, operation)]
    result = (
        session.execute(
            text(_update_query(table, column)),
            {
                "value": action["value"],
                "now": datetime.now(UTC),
                "actor": auth.user_id,
                "workspace_id": auth.workspace_id,
                "target_id": target_id,
                "expected_version": expected_version,
            },
        )
        .mappings()
        .one_or_none()
    )
    if result is None:
        current = target_version(session, auth.workspace_id, target_type, target_id)
        if current is None:
            raise HTTPException(status_code=404, detail="TARGET_NOT_FOUND")
        raise HTTPException(status_code=409, detail="TARGET_VERSION_CONFLICT")
    return {
        "target_type": target_type,
        "target_id": str(result["id"]),
        "resulting_version": int(result["version"]),
        "operation": operation,
    }
