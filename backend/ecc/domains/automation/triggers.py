"""Trigger CRUD (`triggers`) -- design doc Decision 7 / `DATA-MODEL.md`.

Deliberately **no `APIRouter` in this module** -- `docs/phases/phase-005/
API-SCHEMAS.md`'s Task-1-scoped surface (`GET|POST /automations/workflows`,
`GET /automations/workflows/{id}`, `POST .../publish|disable`, `GET|POST
/automations/policies`, `POST /automations/policies/{id}/revoke`) names no
trigger endpoint; the design doc's own scheduler/event-subscriber (Decision
7) is later worker work (Task 2+) this task does not build. This mirrors
`ecc.domains.ai_runtime.tools`'s shape exactly: a pure data-layer module
with no HTTP surface of its own, whose functions exist for a later task
(and, here, this task's own tests) to call directly.

Every function is workspace-scoped the same way `workflows.py`/`policy.py`
are -- no function accepts anything that could substitute for the caller's
own `workspace_id`.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

TriggerType = Literal["manual", "event", "schedule"]

_TRIGGER_FIELDS = """
    id, workspace_id, workflow_id, trigger_type, event_type_filter,
    schedule_expression, timezone, skip_missed, created_by, updated_by,
    created_at, updated_at
"""


@dataclass(frozen=True, slots=True)
class Trigger:
    id: UUID
    workspace_id: UUID
    workflow_id: str
    trigger_type: TriggerType
    event_type_filter: str | None
    schedule_expression: str | None
    timezone: str | None
    skip_missed: bool
    created_by: UUID
    updated_by: UUID
    created_at: datetime
    updated_at: datetime


def _row_to_trigger(row: dict[str, Any]) -> Trigger:
    return Trigger(
        id=row["id"],
        workspace_id=row["workspace_id"],
        workflow_id=row["workflow_id"],
        trigger_type=row["trigger_type"],
        event_type_filter=row["event_type_filter"],
        schedule_expression=row["schedule_expression"],
        timezone=row["timezone"],
        skip_missed=row["skip_missed"],
        created_by=row["created_by"],
        updated_by=row["updated_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def get_trigger(session: Session, workspace_id: UUID, trigger_id: UUID) -> Trigger | None:
    row = (
        session.execute(
            text(
                f"SELECT {_TRIGGER_FIELDS} FROM triggers "
                "WHERE workspace_id = :workspace_id AND id = :id"
            ),
            {"workspace_id": workspace_id, "id": trigger_id},
        )
        .mappings()
        .one_or_none()
    )
    return _row_to_trigger(dict(row)) if row is not None else None


def list_triggers(
    session: Session, workspace_id: UUID, *, workflow_id: str | None = None
) -> list[Trigger]:
    clause = "AND workflow_id = :workflow_id" if workflow_id is not None else ""
    params: dict[str, Any] = {"workspace_id": workspace_id}
    if workflow_id is not None:
        params["workflow_id"] = workflow_id
    rows = (
        session.execute(
            text(
                f"SELECT {_TRIGGER_FIELDS} FROM triggers "
                f"WHERE workspace_id = :workspace_id {clause} ORDER BY created_at ASC"
            ),
            params,
        )
        .mappings()
        .all()
    )
    return [_row_to_trigger(dict(row)) for row in rows]


def workflow_family_exists(session: Session, workspace_id: UUID, workflow_id: str) -> bool:
    return (
        session.execute(
            text(
                "SELECT 1 FROM workflow_definitions WHERE workspace_id = :workspace_id "
                "AND workflow_id = :workflow_id LIMIT 1"
            ),
            {"workspace_id": workspace_id, "workflow_id": workflow_id},
        ).first()
        is not None
    )


def create_trigger(
    session: Session,
    workspace_id: UUID,
    actor_id: UUID,
    *,
    workflow_id: str,
    trigger_type: TriggerType,
    event_type_filter: str | None = None,
    schedule_expression: str | None = None,
    timezone: str | None = None,
    skip_missed: bool = False,
) -> Trigger:
    """Raw insert -- the `ck_triggers_schedule_requires_expression_and_
    timezone`/`ck_triggers_event_requires_type_filter` CHECK constraints
    (migration `0038_phase5_workflow_schema.py`) are the authoritative
    enforcement of "a `schedule` trigger always names its timezone
    explicitly" (design doc Decision 7); this function does not duplicate
    that validation in Python, matching how `workflows.py`'s immutability
    guarantee is enforced by a trigger, not re-checked here. A caller
    passing a malformed combination gets a `CheckViolation` (a subclass of
    `sqlalchemy.exc.IntegrityError`) directly from Postgres.
    """
    now = datetime.now(UTC)
    trigger_id = uuid4()
    session.execute(
        text(
            """
            INSERT INTO triggers (
                id, workspace_id, workflow_id, trigger_type, event_type_filter,
                schedule_expression, timezone, skip_missed, created_by, updated_by,
                created_at, updated_at
            ) VALUES (
                :id, :workspace_id, :workflow_id, :trigger_type, :event_type_filter,
                :schedule_expression, :timezone, :skip_missed, :created_by, :updated_by,
                :now, :now
            )
            """
        ),
        {
            "id": trigger_id,
            "workspace_id": workspace_id,
            "workflow_id": workflow_id,
            "trigger_type": trigger_type,
            "event_type_filter": event_type_filter,
            "schedule_expression": schedule_expression,
            "timezone": timezone,
            "skip_missed": skip_missed,
            "created_by": actor_id,
            "updated_by": actor_id,
            "now": now,
        },
    )
    result = get_trigger(session, workspace_id, trigger_id)
    assert result is not None
    return result
