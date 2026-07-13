from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from hmac import compare_digest, new
from json import dumps, loads
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.config import get_settings
from ecc.database import get_session

router = APIRouter(prefix="/api/v1/tasks", tags=["tasks"])

TaskStatus = Literal[
    "captured",
    "planned",
    "in_progress",
    "blocked",
    "completed",
    "cancelled",
]
TaskPriority = Literal["low", "medium", "high", "critical"]
SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]
StatusFilter = Annotated[list[TaskStatus] | None, Query(alias="status[]")]
PriorityFilter = Annotated[list[TaskPriority] | None, Query(alias="priority[]")]

_SELECT_FIELDS = """
id, owner_id, title, description, status, manual_priority, due_date, due_at,
blocked_reason, blocked_on_person_id, completed_at, pinned, source_type, source_ref,
created_at, updated_at, version, archived_at, pre_archive_status
"""


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
    def validate_due_precision(self) -> TaskCreate:
        if self.due_date is not None and self.due_at is not None:
            raise ValueError("due_date and due_at are mutually exclusive")
        return self


class TaskPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = None
    manual_priority: TaskPriority | None = None
    due_date: date | None = None
    due_at: datetime | None = None
    status: Literal["captured", "planned", "in_progress", "blocked"] | None = None
    blocked_reason: str | None = None
    blocked_on_person_id: UUID | None = None
    pinned: bool | None = None
    source_ref: str | None = None

    @model_validator(mode="after")
    def validate_due_precision(self) -> TaskPatch:
        if self.due_date is not None and self.due_at is not None:
            raise ValueError("due_date and due_at are mutually exclusive")
        return self


class TaskAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=2000)


class TaskLinks(BaseModel):
    audit: str


class TaskResponse(BaseModel):
    id: UUID
    owner_id: UUID
    title: str
    description: str | None
    status: TaskStatus
    manual_priority: TaskPriority
    due_date: date | None
    due_at: datetime | None
    blocked_reason: str | None
    blocked_on_person_id: UUID | None
    completed_at: datetime | None
    pinned: bool
    source_type: str
    source_ref: str | None
    created_at: datetime
    updated_at: datetime
    version: int
    archived_at: datetime | None
    pre_archive_status: str | None
    links: TaskLinks


class TaskListResponse(BaseModel):
    items: list[TaskResponse]
    next_cursor: str | None = None


def _to_response(row: dict[str, Any]) -> TaskResponse:
    task_id = row["id"]
    row["links"] = {"audit": f"/api/v1/audit?aggregate_type=task&aggregate_id={task_id}"}
    return TaskResponse.model_validate(row)


def _request_ids(request: Request) -> tuple[UUID, UUID]:
    request_id = uuid4()
    raw = request.headers.get("X-Correlation-ID")
    try:
        correlation_id = UUID(raw) if raw else uuid4()
    except ValueError:
        correlation_id = uuid4()
    return request_id, correlation_id


def _request_hash(payload: BaseModel, action: str) -> str:
    material = {
        "action": action,
        "payload": payload.model_dump(mode="json", exclude_none=False),
    }
    encoded = dumps(material, sort_keys=True, separators=(",", ":")).encode()
    return sha256(encoded).hexdigest()


def _load_idempotent_response(
    session: Session,
    auth: AuthContext,
    key: str,
    request_hash: str,
) -> TaskResponse | None:
    existing = (
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
    if existing is None:
        return None
    if existing["request_hash"] != request_hash:
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
    return TaskResponse.model_validate(existing["response_body"])


def _store_idempotency(
    session: Session,
    auth: AuthContext,
    key: str,
    request_hash: str,
    response: TaskResponse,
    response_status: int,
    now: datetime,
) -> None:
    session.execute(
        text(
            """
            INSERT INTO idempotency_records (
                workspace_id, actor_id, key, request_hash, response_status,
                response_body, created_at, expires_at
            ) VALUES (
                :workspace_id, :actor_id, :key, :request_hash, :response_status,
                CAST(:response_body AS jsonb), :created_at, :expires_at
            )
            """
        ),
        {
            "workspace_id": auth.workspace_id,
            "actor_id": auth.user_id,
            "key": key,
            "request_hash": request_hash,
            "response_status": response_status,
            "response_body": dumps(response.model_dump(mode="json")),
            "created_at": now,
            "expires_at": now + timedelta(days=365),
        },
    )


def _write_audit(
    session: Session,
    auth: AuthContext,
    event_type: str,
    task_id: UUID,
    aggregate_version: int,
    request_id: UUID,
    correlation_id: UUID,
    idempotency_key: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    changed_fields: list[str],
    now: datetime,
    authorization_result: str = "allowed",
    failure_code: str | None = None,
) -> None:
    session.execute(
        text(
            """
            INSERT INTO audit_events (
                id, workspace_id, event_type, aggregate_type, aggregate_id,
                aggregate_version, actor_id, request_id, correlation_id,
                idempotency_key_hash, before, after, changed_fields,
                authorization_result, source, failure_code, metadata, occurred_at
            ) VALUES (
                :id, :workspace_id, :event_type, 'task', :aggregate_id,
                :aggregate_version, :actor_id, :request_id, :correlation_id,
                :key_hash, CAST(:before AS jsonb), CAST(:after AS jsonb),
                :changed_fields, :authorization_result, 'user', :failure_code,
                '{}'::jsonb, :occurred_at
            )
            """
        ),
        {
            "id": uuid4(),
            "workspace_id": auth.workspace_id,
            "event_type": event_type,
            "aggregate_id": task_id,
            "aggregate_version": aggregate_version,
            "actor_id": auth.user_id,
            "request_id": request_id,
            "correlation_id": correlation_id,
            "key_hash": sha256(idempotency_key.encode()).hexdigest(),
            "before": dumps(before) if before is not None else None,
            "after": dumps(after) if after is not None else None,
            "changed_fields": changed_fields,
            "authorization_result": authorization_result,
            "failure_code": failure_code,
            "occurred_at": now,
        },
    )


def _write_outbox(
    session: Session,
    auth: AuthContext,
    event_type: str,
    task_id: UUID,
    task_version: int,
    correlation_id: UUID,
    payload: dict[str, Any],
    now: datetime,
) -> None:
    event_payload = {
        "task_id": str(task_id),
        "task_version": task_version,
        **payload,
    }
    session.execute(
        text(
            """
            INSERT INTO event_outbox (
                event_id, workspace_id, event_type, event_version,
                correlation_id, payload, occurred_at, attempt_count
            ) VALUES (
                :event_id, :workspace_id, :event_type, 1,
                :correlation_id, CAST(:payload AS jsonb), :occurred_at, 0
            )
            """
        ),
        {
            "event_id": uuid4(),
            "workspace_id": auth.workspace_id,
            "event_type": event_type,
            "correlation_id": correlation_id,
            "payload": dumps(event_payload),
            "occurred_at": now,
        },
    )


def _get_task_row(
    session: Session,
    auth: AuthContext,
    task_id: UUID,
    for_update: bool = False,
) -> dict[str, Any] | None:
    lock = "FOR UPDATE" if for_update else ""
    row = (
        session.execute(
            text(
                f"""
            SELECT {_SELECT_FIELDS}
            FROM tasks
            WHERE workspace_id = :workspace_id AND id = :task_id
            {lock}
            """
            ),
            {"workspace_id": auth.workspace_id, "task_id": task_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _raise_version_conflict(current: dict[str, Any], expected_version: int) -> None:
    if current["version"] != expected_version:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "VERSION_CONFLICT",
                "current_version": current["version"],
            },
        )


def _encode_cursor(created_at: datetime, task_id: UUID) -> str:
    payload = dumps(
        {"created_at": created_at.isoformat(), "id": str(task_id)},
        separators=(",", ":"),
    ).encode()
    secret = get_settings().session_secret.encode()
    signature = new(secret, payload, "sha256").hexdigest().encode()
    return urlsafe_b64encode(payload + b"." + signature).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = urlsafe_b64decode(padded.encode())
        payload, signature = raw.rsplit(b".", 1)
        expected = new(get_settings().session_secret.encode(), payload, "sha256").hexdigest()
        if not compare_digest(signature.decode(), expected):
            raise ValueError
        decoded = loads(payload)
        return datetime.fromisoformat(decoded["created_at"]), UUID(decoded["id"])
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="MALFORMED_CURSOR") from exc


@router.post("", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
def create_task(
    payload: TaskCreate,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> TaskResponse:
    request_hash = _request_hash(payload, "create")
    now = datetime.now(UTC)
    request_id, correlation_id = _request_ids(request)

    with session.begin():
        cached = _load_idempotent_response(
            session,
            auth,
            idempotency_key,
            request_hash,
        )
        if cached is not None:
            return cached

        task_id = uuid4()
        row = (
            session.execute(
                text(
                    f"""
                INSERT INTO tasks (
                    id, workspace_id, owner_id, title, description, status,
                    manual_priority, due_date, due_at, pinned, source_type,
                    source_ref, created_by, updated_by, created_at, updated_at,
                    version
                ) VALUES (
                    :id, :workspace_id, :owner_id, :title, :description, :status,
                    :manual_priority, :due_date, :due_at, false, 'local',
                    :source_ref, :actor_id, :actor_id, :now, :now, 1
                )
                RETURNING {_SELECT_FIELDS}
                """
                ),
                {
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
                },
            )
            .mappings()
            .one()
        )
        response = _to_response(dict(row))
        after = response.model_dump(mode="json")
        changed = [
            "title",
            "description",
            "status",
            "manual_priority",
            "due_date",
            "due_at",
        ]
        _write_audit(
            session,
            auth,
            "task.created",
            task_id,
            response.version,
            request_id,
            correlation_id,
            idempotency_key,
            None,
            after,
            changed,
            now,
        )
        _write_outbox(
            session,
            auth,
            "task.created.v1",
            task_id,
            response.version,
            correlation_id,
            {
                "owner_id": str(auth.user_id),
                "status": response.status,
                "priority": response.manual_priority,
            },
            now,
        )
        _store_idempotency(
            session,
            auth,
            idempotency_key,
            request_hash,
            response,
            201,
            now,
        )
        return response


@router.get("", response_model=TaskListResponse)
def list_tasks(
    auth: AuthDep,
    session: SessionDep,
    status_filter: StatusFilter = None,
    priority_filter: PriorityFilter = None,
    due_before: date | None = None,
    due_after: date | None = None,
    pinned: bool | None = None,
    include_archived: bool = False,
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
) -> TaskListResponse:
    clauses = ["workspace_id = :workspace_id"]
    params: dict[str, Any] = {"workspace_id": auth.workspace_id, "limit": limit + 1}
    if not include_archived:
        clauses.append("archived_at IS NULL")
    if status_filter:
        clauses.append("status = ANY(:statuses)")
        params["statuses"] = status_filter
    if priority_filter:
        clauses.append("manual_priority = ANY(:priorities)")
        params["priorities"] = priority_filter
    if due_before:
        clauses.append("COALESCE(due_date, due_at::date) <= :due_before")
        params["due_before"] = due_before
    if due_after:
        clauses.append("COALESCE(due_date, due_at::date) >= :due_after")
        params["due_after"] = due_after
    if pinned is not None:
        clauses.append("pinned = :pinned")
        params["pinned"] = pinned
    if cursor:
        cursor_created_at, cursor_id = _decode_cursor(cursor)
        clauses.append("(created_at, id) < (:cursor_created_at, :cursor_id)")
        params["cursor_created_at"] = cursor_created_at
        params["cursor_id"] = cursor_id

    rows = (
        session.execute(
            text(
                f"""
            SELECT {_SELECT_FIELDS}
            FROM tasks
            WHERE {" AND ".join(clauses)}
            ORDER BY created_at DESC, id DESC
            LIMIT :limit
            """
            ),
            params,
        )
        .mappings()
        .all()
    )
    has_more = len(rows) > limit
    page = rows[:limit]
    items = [_to_response(dict(row)) for row in page]
    next_cursor = None
    if has_more and page:
        last = page[-1]
        next_cursor = _encode_cursor(last["created_at"], last["id"])
    return TaskListResponse(items=items, next_cursor=next_cursor)


@router.get("/{task_id}", response_model=TaskResponse)
def get_task(task_id: UUID, auth: AuthDep, session: SessionDep) -> TaskResponse:
    row = _get_task_row(session, auth, task_id)
    if row is None:
        raise HTTPException(status_code=404, detail="TASK_NOT_FOUND")
    return _to_response(row)


@router.patch("/{task_id}", response_model=TaskResponse)
def update_task(
    task_id: UUID,
    payload: TaskPatch,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> TaskResponse:
    request_hash = _request_hash(payload, f"update:{task_id}")
    now = datetime.now(UTC)
    request_id, correlation_id = _request_ids(request)

    with session.begin():
        cached = _load_idempotent_response(
            session,
            auth,
            idempotency_key,
            request_hash,
        )
        if cached is not None:
            return cached
        current = _get_task_row(session, auth, task_id, for_update=True)
        if current is None:
            raise HTTPException(status_code=404, detail="TASK_NOT_FOUND")
        _raise_version_conflict(current, payload.expected_version)
        if current["archived_at"] is not None:
            raise HTTPException(status_code=409, detail="TASK_ARCHIVED")

        fields = payload.model_fields_set - {"expected_version"}
        if not fields:
            response = _to_response(current)
            _store_idempotency(
                session,
                auth,
                idempotency_key,
                request_hash,
                response,
                200,
                now,
            )
            return response

        effective_due_date = payload.due_date if "due_date" in fields else current["due_date"]
        effective_due_at = payload.due_at if "due_at" in fields else current["due_at"]
        if effective_due_date is not None and effective_due_at is not None:
            raise HTTPException(status_code=422, detail="MUTUALLY_EXCLUSIVE_FIELDS")

        allowed = {
            "title",
            "description",
            "manual_priority",
            "due_date",
            "due_at",
            "status",
            "blocked_reason",
            "blocked_on_person_id",
            "pinned",
            "source_ref",
        }
        changed_fields = sorted(fields & allowed)
        assignments = [f"{field} = :{field}" for field in changed_fields]
        assignments.extend(
            ["updated_by = :updated_by", "updated_at = :now", "version = version + 1"]
        )
        values = payload.model_dump(include=set(changed_fields))
        values.update(
            {
                "workspace_id": auth.workspace_id,
                "task_id": task_id,
                "updated_by": auth.user_id,
                "now": now,
            }
        )
        row = (
            session.execute(
                text(
                    f"""
                UPDATE tasks
                SET {", ".join(assignments)}
                WHERE workspace_id = :workspace_id AND id = :task_id
                RETURNING {_SELECT_FIELDS}
                """
                ),
                values,
            )
            .mappings()
            .one()
        )
        response = _to_response(dict(row))
        before = _to_response(current).model_dump(mode="json")
        after = response.model_dump(mode="json")
        _write_audit(
            session,
            auth,
            "task.updated",
            task_id,
            response.version,
            request_id,
            correlation_id,
            idempotency_key,
            before,
            after,
            changed_fields,
            now,
        )
        _write_outbox(
            session,
            auth,
            "task.updated.v1",
            task_id,
            response.version,
            correlation_id,
            {"changed_fields": changed_fields},
            now,
        )
        _store_idempotency(
            session,
            auth,
            idempotency_key,
            request_hash,
            response,
            200,
            now,
        )
        return response


def _lifecycle_task(
    task_id: UUID,
    payload: TaskAction,
    request: Request,
    auth: AuthContext,
    session: Session,
    idempotency_key: str,
    action: Literal["complete", "cancel", "archive", "restore"],
) -> TaskResponse:
    request_hash = _request_hash(payload, f"{action}:{task_id}")
    now = datetime.now(UTC)
    request_id, correlation_id = _request_ids(request)

    with session.begin():
        cached = _load_idempotent_response(
            session,
            auth,
            idempotency_key,
            request_hash,
        )
        if cached is not None:
            return cached
        current = _get_task_row(session, auth, task_id, for_update=True)
        if current is None:
            raise HTTPException(status_code=404, detail="TASK_NOT_FOUND")
        _raise_version_conflict(current, payload.expected_version)

        target_reached = (
            (action == "complete" and current["status"] == "completed")
            or (action == "cancel" and current["status"] == "cancelled")
            or (action == "archive" and current["archived_at"] is not None)
            or (action == "restore" and current["archived_at"] is None)
        )
        if target_reached:
            response = _to_response(current)
            _store_idempotency(
                session,
                auth,
                idempotency_key,
                request_hash,
                response,
                200,
                now,
            )
            return response

        if action in {"complete", "cancel"} and current["archived_at"] is not None:
            raise HTTPException(status_code=409, detail="TASK_ARCHIVED")

        if action == "complete":
            assignments = "status = 'completed', completed_at = :now"
            audit_type = "task.completed"
            event_type = "task.completed.v1"
            event_payload = {"completed_at": now.isoformat()}
            changed_fields = ["status", "completed_at"]
        elif action == "cancel":
            assignments = "status = 'cancelled', completed_at = NULL"
            audit_type = "task.cancelled"
            event_type = "task.cancelled.v1"
            event_payload = {"reason": payload.reason}
            changed_fields = ["status", "completed_at"]
        elif action == "archive":
            assignments = "archived_at = :now, pre_archive_status = status"
            audit_type = "task.archived"
            event_type = "task.archived.v1"
            event_payload = {
                "archived_at": now.isoformat(),
                "pre_archive_status": current["status"],
            }
            changed_fields = ["archived_at", "pre_archive_status"]
        else:
            restored_status = current["pre_archive_status"] or "captured"
            assignments = "archived_at = NULL, pre_archive_status = NULL, status = :restored_status"
            audit_type = "task.restored"
            event_type = "task.restored.v1"
            event_payload = {"restored_status": restored_status}
            changed_fields = ["archived_at", "pre_archive_status", "status"]

        row = (
            session.execute(
                text(
                    f"""
                UPDATE tasks
                SET {assignments}, updated_by = :updated_by,
                    updated_at = :now, version = version + 1
                WHERE workspace_id = :workspace_id AND id = :task_id
                RETURNING {_SELECT_FIELDS}
                """
                ),
                {
                    "workspace_id": auth.workspace_id,
                    "task_id": task_id,
                    "updated_by": auth.user_id,
                    "restored_status": current["pre_archive_status"] or "captured",
                    "now": now,
                },
            )
            .mappings()
            .one()
        )
        response = _to_response(dict(row))
        before = _to_response(current).model_dump(mode="json")
        after = response.model_dump(mode="json")
        _write_audit(
            session,
            auth,
            audit_type,
            task_id,
            response.version,
            request_id,
            correlation_id,
            idempotency_key,
            before,
            after,
            changed_fields,
            now,
        )
        _write_outbox(
            session,
            auth,
            event_type,
            task_id,
            response.version,
            correlation_id,
            event_payload,
            now,
        )
        _store_idempotency(
            session,
            auth,
            idempotency_key,
            request_hash,
            response,
            200,
            now,
        )
        return response


@router.post("/{task_id}/complete", response_model=TaskResponse)
def complete_task(
    task_id: UUID,
    payload: TaskAction,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> TaskResponse:
    return _lifecycle_task(
        task_id,
        payload,
        request,
        auth,
        session,
        idempotency_key,
        "complete",
    )


@router.post("/{task_id}/cancel", response_model=TaskResponse)
def cancel_task(
    task_id: UUID,
    payload: TaskAction,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> TaskResponse:
    return _lifecycle_task(
        task_id,
        payload,
        request,
        auth,
        session,
        idempotency_key,
        "cancel",
    )


@router.post("/{task_id}/archive", response_model=TaskResponse)
def archive_task(
    task_id: UUID,
    payload: TaskAction,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> TaskResponse:
    return _lifecycle_task(
        task_id,
        payload,
        request,
        auth,
        session,
        idempotency_key,
        "archive",
    )


@router.post("/{task_id}/restore", response_model=TaskResponse)
def restore_task(
    task_id: UUID,
    payload: TaskAction,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> TaskResponse:
    return _lifecycle_task(
        task_id,
        payload,
        request,
        auth,
        session,
        idempotency_key,
        "restore",
    )
