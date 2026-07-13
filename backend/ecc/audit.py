import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from hashlib import sha256
from uuid import UUID, uuid4

from fastapi import Request, Response
from sqlalchemy import text

from ecc.database import engine

logger = logging.getLogger(__name__)
_REJECTED_STATUSES = {403, 404, 409, 422}
_NIL_UUID = UUID(int=0)


def _task_id_from_path(path: str) -> UUID:
    parts = path.strip("/").split("/")
    if len(parts) < 4:
        return _NIL_UUID
    try:
        return UUID(parts[3])
    except ValueError:
        return _NIL_UUID


def _correlation_id(request: Request) -> UUID:
    raw = getattr(request.state, "correlation_id", None)
    try:
        return UUID(raw) if raw else uuid4()
    except ValueError:
        return uuid4()


def _record_rejected_task_mutation(request: Request, response: Response) -> None:
    session_token = request.cookies.get("ecc_session")
    if not session_token:
        return

    token_hash = sha256(session_token.encode("utf-8")).hexdigest()
    idempotency_key = request.headers.get("Idempotency-Key")
    idempotency_hash = (
        sha256(idempotency_key.encode("utf-8")).hexdigest() if idempotency_key else None
    )
    now = datetime.now(UTC)

    with engine.begin() as connection:
        auth = (
            connection.execute(
                text(
                    """
                    SELECT workspace_id, user_id
                    FROM sessions
                    WHERE token_hash = :token_hash
                      AND revoked_at IS NULL
                      AND expires_at > :now
                    """
                ),
                {"token_hash": token_hash, "now": now},
            )
            .mappings()
            .one_or_none()
        )
        if auth is None:
            return

        connection.execute(
            text(
                """
                INSERT INTO audit_events (
                    id, workspace_id, event_type, aggregate_type, aggregate_id,
                    aggregate_version, actor_id, request_id, correlation_id,
                    idempotency_key_hash, changed_fields, authorization_result,
                    source, failure_code, metadata, occurred_at
                ) VALUES (
                    :id, :workspace_id, 'task.mutation_rejected', 'task',
                    :aggregate_id, 0, :actor_id, :request_id, :correlation_id,
                    :idempotency_key_hash, '{}'::text[], 'rejected',
                    'user', :failure_code, '{}'::jsonb, :occurred_at
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": auth["workspace_id"],
                "aggregate_id": _task_id_from_path(request.url.path),
                "actor_id": auth["user_id"],
                "request_id": uuid4(),
                "correlation_id": _correlation_id(request),
                "idempotency_key_hash": idempotency_hash,
                "failure_code": f"HTTP_{response.status_code}",
                "occurred_at": now,
            },
        )


async def rejected_mutation_audit_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    response = await call_next(request)
    is_task_mutation = request.method in {"POST", "PATCH"} and request.url.path.startswith(
        "/api/v1/tasks"
    )
    if is_task_mutation and response.status_code in _REJECTED_STATUSES:
        try:
            _record_rejected_task_mutation(request, response)
        except Exception:
            logger.exception("failed_to_record_rejected_task_mutation")
    return response
