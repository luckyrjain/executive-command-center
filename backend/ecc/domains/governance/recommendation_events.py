from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext
from ecc.observability import queue_lifecycle_event, queue_recommendation_transition
from ecc.platform import audit_outbox


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
    # Extracted (no fallback) solely to supply event_outbox.causation_id --
    # write_audit_and_outbox derives its own request_id/correlation_id from
    # `request` independently (with its own fallback for a malformed
    # request.state), but a malformed request.state here must still raise
    # before any DB write happens, matching this function's prior behavior.
    request_id = UUID(request.state.request_id)
    occurred_at = datetime.now(UTC)
    after = {
        "status": row["status"],
        "version": int(row["version"]),
        "pinned": bool(row["pinned"]),
        "deferred_until": row["deferred_until"].isoformat() if row["deferred_until"] else None,
    }
    audit_outbox.write_audit_and_outbox(
        session,
        auth,
        request,
        event_type=event_name,
        aggregate_type="recommendation",
        aggregate_id=row["id"],
        aggregate_version=row["version"],
        changed_fields=changed_fields,
        payload=payload or {"recommendation_id": str(row["id"])},
        now=occurred_at,
        domain="recommendations",
        before=before,
        after=after,
        causation_id=request_id,
    )
    queue_lifecycle_event(session, "recommendation", event_name, "allowed")
    queue_recommendation_transition(session, event_name)
