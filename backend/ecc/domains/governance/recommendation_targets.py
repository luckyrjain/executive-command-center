from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext

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
    },
    "commitment": {
        "set_status": {"confirmed", "active", "fulfilled", "broken", "cancelled"},
        "set_importance": {"low", "medium", "high", "critical"},
        "set_pinned": {True, False},
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


def execute_target(
    session: Session,
    auth: AuthContext,
    target_type: str,
    target_id: UUID,
    action: dict[str, Any],
    expected_version: int,
) -> dict[str, Any]:
    validate_action(target_type, action)
    operation = action["operation"]
    columns = {
        ("task", "set_status"): ("tasks", "status"),
        ("task", "set_priority"): ("tasks", "manual_priority"),
        ("task", "set_pinned"): ("tasks", "pinned"),
        ("commitment", "set_status"): ("commitments", "status"),
        ("commitment", "set_importance"): ("commitments", "importance"),
        ("commitment", "set_pinned"): ("commitments", "pinned"),
        ("risk", "set_status"): ("risks", "status"),
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
