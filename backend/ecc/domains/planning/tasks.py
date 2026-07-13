from datetime import UTC, date, datetime
from hashlib import sha256
from json import dumps
from typing import Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, require_auth_context
from ecc.database import get_session

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])

TaskStatus = Literal["captured", "planned", "in_progress", "blocked", "completed", "cancelled"]
TaskPriority = Literal["low", "medium", "high", "critical"]


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=500)
    description: str | None = None
    manual_priority: TaskPriority = "medium"
    due_date: date | None = None
    due_at: datetime | None = None
    status: Literal["captured", "planned", "in_progress", "blocked"] = "captured"
    source_ref: str | None = None

    @model_validator(mode="after")
    def validate_due_precision(self) -> "TaskCreate":
        if self.due_date is not None and self.due_at is not None:
            raise ValueError("due_date and due_at are mutually exclusive")
        return self


class TaskResponse(BaseModel):
    id: UUID
    title: str
    description: str | None
    status: TaskStatus
    manual_priority: TaskPriority
    due_date: date | None
    due_at: datetime | None
    pinned: bool
    source_type: str
    source_ref: str | None
    created_at: datetime
    updated_at: datetime
    version: int
    archived_at: datetime | None


class TaskListResponse(BaseModel):
    items: list[TaskResponse]
    next_cursor: str | None = None


def _to_response(row: dict[str, object]) -> TaskResponse:
    return TaskResponse.model_validate(row)


@router.post("", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
def create_task(
    payload: TaskCreate,
    request: Request,
    idempotency_key: str = Header(alias="Idempotency-Key", min_length=1, max_length=255),
    auth: AuthContext = Depends(require_auth_context),
    session: Session = Depends(get_session),
) -> TaskResponse:
    request_hash = sha256(
        dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()

    existing = session.execute(
        text(
            """
            SELECT request_hash, response_body
            FROM idempotency_records
            WHERE workspace_id = :workspace_id AND actor_id = :actor_id AND key = :key
            """
        ),
        {"workspace_id": auth.workspace_id, "actor_id": auth.user_id, "key": idempotency_key},
    ).mappings().one_or_none()
    if existing is not None:
        if existing["request_hash"] != request_hash:
            raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
        return TaskResponse.model_validate(existing["response_body"])

    now = datetime.now(UTC)
    task_id = uuid4()
    request_id = uuid4()
    correlation_raw = request.headers.get("X-Correlation-ID")
    try:
        correlation_id = UUID(correlation_raw) if correlation_raw else uuid4()
    except ValueError:
        correlation_id = uuid4()

    values = {
        "id": task_id,
        "workspace_id": auth.workspace_id,
        "owner_id": auth.user_id,
        "title": payload.title,
        "description": payload.description,
        "status": payload.status,
        "manual_priority": payload.manual_priority,
        "due_date": payload.due_date,
        "due_at": payload.due_at,
        "source_ref": payload.source_ref,
        "actor_id": auth.user_id,
        "now": now,
    }

    with session.begin():
        row = session.execute(
            text(
                """
                INSERT INTO tasks (
                    id, workspace_id, owner_id, title, description, status, manual_priority,
                    due_date, due_at, pinned, source_type, source_ref, created_by, updated_by,
                    created_at, updated_at, version
                ) VALUES (
                    :id, :workspace_id, :owner_id, :title, :description, :status, :manual_priority,
                    :due_date, :due_at, false, 'local', :source_ref, :actor_id, :actor_id,
                    :now, :now, 1
                )
                RETURNING id, title, description, status, manual_priority, due_date, due_at,
                          pinned, source_type, source_ref, created_at, updated_at, version, archived_at
                """
            ),
            values,
        ).mappings().one()
        response = _to_response(dict(row))
        response_body = response.model_dump(mode="json")
        session.execute(
            text(
                """
                INSERT INTO audit_events (
                    id, workspace_id, event_type, aggregate_type, aggregate_id, aggregate_version,
                    actor_id, request_id, correlation_id, idempotency_key_hash, before, after,
                    changed_fields, authorization_result, source, metadata, occurred_at
                ) VALUES (
                    :audit_id, :workspace_id, 'task.created', 'task', :task_id, 1,
                    :actor_id, :request_id, :correlation_id, :key_hash, NULL, CAST(:after AS jsonb),
                    ARRAY['title','description','status','manual_priority','due_date','due_at'],
                    'allowed', 'user', '{}'::jsonb, :now
                )
                """
            ),
            {
                "audit_id": uuid4(),
                "workspace_id": auth.workspace_id,
                "task_id": task_id,
                "actor_id": auth.user_id,
                "request_id": request_id,
                "correlation_id": correlation_id,
                "key_hash": sha256(idempotency_key.encode()).hexdigest(),
                "after": dumps(response_body),
                "now": now,
            },
        )
        session.execute(
            text(
                """
                INSERT INTO idempotency_records (
                    workspace_id, actor_id, key, request_hash, response_status, response_body,
                    created_at, expires_at
                ) VALUES (
                    :workspace_id, :actor_id, :key, :request_hash, 201, CAST(:response_body AS jsonb),
                    :created_at, :expires_at
                )
                """
            ),
            {
                "workspace_id": auth.workspace_id,
                "actor_id": auth.user_id,
                "key": idempotency_key,
                "request_hash": request_hash,
                "response_body": dumps(response_body),
                "created_at": now,
                "expires_at": now.replace(year=now.year + 1),
            },
        )
    return response


@router.get("", response_model=TaskListResponse)
def list_tasks(
    include_archived: bool = False,
    limit: int = 20,
    auth: AuthContext = Depends(require_auth_context),
    session: Session = Depends(get_session),
) -> TaskListResponse:
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 100")
    rows = session.execute(
        text(
            """
            SELECT id, title, description, status, manual_priority, due_date, due_at,
                   pinned, source_type, source_ref, created_at, updated_at, version, archived_at
            FROM tasks
            WHERE workspace_id = :workspace_id
              AND (:include_archived OR archived_at IS NULL)
            ORDER BY created_at DESC, id
            LIMIT :limit
            """
        ),
        {"workspace_id": auth.workspace_id, "include_archived": include_archived, "limit": limit},
    ).mappings().all()
    return TaskListResponse(items=[_to_response(dict(row)) for row in rows])


@router.get("/{task_id}", response_model=TaskResponse)
def get_task(
    task_id: UUID,
    auth: AuthContext = Depends(require_auth_context),
    session: Session = Depends(get_session),
) -> TaskResponse:
    row = session.execute(
        text(
            """
            SELECT id, title, description, status, manual_priority, due_date, due_at,
                   pinned, source_type, source_ref, created_at, updated_at, version, archived_at
            FROM tasks
            WHERE workspace_id = :workspace_id AND id = :task_id
            """
        ),
        {"workspace_id": auth.workspace_id, "task_id": task_id},
    ).mappings().one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="TASK_NOT_FOUND")
    return _to_response(dict(row))
