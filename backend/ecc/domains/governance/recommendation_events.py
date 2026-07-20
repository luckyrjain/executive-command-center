from datetime import UTC, datetime
from json import dumps
from typing import Any
from uuid import UUID, uuid4

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ecc.auth import AuthContext
from ecc.observability import (
    queue_lifecycle_event,
    record_audit_outbox_failure,
    record_recommendation_transition,
)


def record_feedback(
    session: Session,
    auth: AuthContext,
    recommendation_id: UUID,
    action: str,
    *,
    reason: str | None = None,
    defer_until: datetime | None = None,
) -> None:
    session.execute(
        text(
            """
            INSERT INTO recommendation_feedback (
                id, workspace_id, recommendation_id, action, reason,
                defer_until, actor_id, created_at
            ) VALUES (
                :id, :workspace_id, :recommendation_id, :action, :reason,
                :defer_until, :actor_id, :created_at
            )
            """
        ),
        {
            "id": uuid4(),
            "workspace_id": auth.workspace_id,
            "recommendation_id": recommendation_id,
            "action": action,
            "reason": reason,
            "defer_until": defer_until,
            "actor_id": auth.user_id,
            "created_at": datetime.now(UTC),
        },
    )


def record_event(
    request: Request,
    session: Session,
    auth: AuthContext,
    row: dict[str, Any],
    event_name: str,
    before: dict[str, Any] | None,
    changed_fields: list[str],
    payload: dict[str, Any] | None = None,
) -> None:
    request_id = UUID(request.state.request_id)
    correlation_id = UUID(request.state.correlation_id)
    occurred_at = datetime.now(UTC)
    after = {
        "status": row["status"],
        "version": int(row["version"]),
        "pinned": bool(row["pinned"]),
        "deferred_until": row["deferred_until"].isoformat() if row["deferred_until"] else None,
    }
    try:
        session.execute(
            text(
                """
                INSERT INTO audit_events (
                    id, workspace_id, event_type, aggregate_type, aggregate_id,
                    aggregate_version, actor_id, request_id, correlation_id,
                    before, after, changed_fields, authorization_result, source,
                    metadata, occurred_at
                ) VALUES (
                    :id, :workspace_id, :event_type, 'recommendation', :aggregate_id,
                    :aggregate_version, :actor_id, :request_id, :correlation_id,
                    CAST(:before AS jsonb), CAST(:after AS jsonb), :changed_fields,
                    'allowed', 'user', '{}'::jsonb, :occurred_at
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": auth.workspace_id,
                "event_type": event_name,
                "aggregate_id": row["id"],
                "aggregate_version": row["version"],
                "actor_id": auth.user_id,
                "request_id": request_id,
                "correlation_id": correlation_id,
                "before": dumps(before) if before is not None else None,
                "after": dumps(after),
                "changed_fields": changed_fields,
                "occurred_at": occurred_at,
            },
        )
        session.execute(
            text(
                """
                INSERT INTO event_outbox (
                    event_id, workspace_id, event_type, event_version,
                    correlation_id, causation_id, payload, occurred_at, attempt_count
                ) VALUES (
                    :event_id, :workspace_id, :event_type, 1,
                    :correlation_id, :causation_id, CAST(:payload AS jsonb),
                    :occurred_at, 0
                )
                """
            ),
            {
                "event_id": uuid4(),
                "workspace_id": auth.workspace_id,
                "event_type": f"{event_name}.v1",
                "correlation_id": correlation_id,
                "causation_id": request_id,
                "payload": dumps(payload or {"recommendation_id": str(row["id"])}),
                "occurred_at": occurred_at,
            },
        )
    except SQLAlchemyError:
        record_audit_outbox_failure("recommendations")
        raise
    queue_lifecycle_event(session, "recommendation", event_name, "allowed")
    record_recommendation_transition(event_name)
