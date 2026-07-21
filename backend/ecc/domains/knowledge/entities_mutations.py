from datetime import UTC, datetime, timedelta
from hashlib import sha256
from json import dumps
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session
from ecc.domains.knowledge.entities import EntityResponse, _project
from ecc.domains.knowledge.retrieval import queue_retrieval_document
from ecc.domains.knowledge.timeline import queue_timeline_entry
from ecc.observability import (
    queue_lifecycle_event,
    record_audit_outbox_failure,
    record_idempotency_conflict,
)

router = APIRouter(prefix="/api/v1/knowledge/entities", tags=["knowledge-entities"])
SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]

_ENTITY_FIELDS = """
id, entity_id, node_type, canonical_name, attributes, status, confidence,
version, created_at, updated_at
"""


class EntityPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    canonical_name: str | None = Field(default=None, min_length=1, max_length=500)
    summary: str | None = None

    @model_validator(mode="after")
    def reject_null_required_fields(self) -> EntityPatch:
        if "canonical_name" in self.model_fields_set and self.canonical_name is None:
            raise ValueError("canonical_name cannot be null")
        if len(self.model_fields_set - {"expected_version"}) == 0:
            raise ValueError("at least one mutable field is required")
        return self


class EntityAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


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


def _load_cached(
    session: Session,
    auth: AuthContext,
    key: str,
    request_hash: str,
) -> EntityResponse | None:
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
        record_idempotency_conflict("knowledge_entities")
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
    return EntityResponse.model_validate(row["response_body"])


def _store_cached(
    session: Session,
    auth: AuthContext,
    key: str,
    request_hash: str,
    response: EntityResponse,
    now: datetime,
) -> None:
    session.execute(
        text(
            """
            INSERT INTO idempotency_records (
                workspace_id, actor_id, key, request_hash, response_status,
                response_body, created_at, expires_at
            ) VALUES (
                :workspace_id, :actor_id, :key, :request_hash, 200,
                CAST(:response_body AS jsonb), :created_at, :expires_at
            )
            """
        ),
        {
            "workspace_id": auth.workspace_id,
            "actor_id": auth.user_id,
            "key": key,
            "request_hash": request_hash,
            "response_body": dumps(response.model_dump(mode="json")),
            "created_at": now,
            "expires_at": now + timedelta(days=365),
        },
    )


def _get_row(
    session: Session,
    auth: AuthContext,
    entity_id: UUID,
    *,
    for_update: bool = False,
) -> dict[str, Any] | None:
    suffix = " FOR UPDATE" if for_update else ""
    row = (
        session.execute(
            text(
                f"""
                SELECT {_ENTITY_FIELDS}
                FROM pkos_nodes
                WHERE workspace_id = :workspace_id AND id = :entity_id
                {suffix}
                """
            ),
            {"workspace_id": auth.workspace_id, "entity_id": entity_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _write_side_effects(
    session: Session,
    auth: AuthContext,
    request: Request,
    event_type: str,
    entity_id: UUID,
    version: int,
    changed_fields: list[str],
    now: datetime,
) -> None:
    request_id, correlation_id = _request_ids(request)
    try:
        session.execute(
            text(
                """
                INSERT INTO audit_events (
                    id, workspace_id, event_type, aggregate_type, aggregate_id,
                    aggregate_version, actor_id, request_id, correlation_id,
                    changed_fields, authorization_result, source, metadata, occurred_at
                ) VALUES (
                    :id, :workspace_id, :event_type, 'knowledge_entity', :aggregate_id,
                    :aggregate_version, :actor_id, :request_id, :correlation_id,
                    :changed_fields, 'allowed', 'user', '{}'::jsonb, :occurred_at
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": auth.workspace_id,
                "event_type": event_type,
                "aggregate_id": entity_id,
                "aggregate_version": version,
                "actor_id": auth.user_id,
                "request_id": request_id,
                "correlation_id": correlation_id,
                "changed_fields": changed_fields,
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
                    :event_id, :workspace_id, :event_type, 1,
                    :correlation_id, CAST(:payload AS jsonb), :occurred_at, 0
                )
                """
            ),
            {
                "event_id": uuid4(),
                "workspace_id": auth.workspace_id,
                "event_type": f"{event_type}.v1",
                "correlation_id": correlation_id,
                "payload": dumps({"entity_id": str(entity_id), "version": version}),
                "occurred_at": now,
            },
        )
    except SQLAlchemyError:
        record_audit_outbox_failure("knowledge_entities")
        raise
    queue_lifecycle_event(session, "knowledge_entity", event_type, "allowed")


@router.patch("/{entity_id}", response_model=EntityResponse)
def update_entity(
    entity_id: UUID,
    payload: EntityPatch,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> EntityResponse:
    request_hash = _request_hash(payload, f"update:{entity_id}")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached
        current = _get_row(session, auth, entity_id, for_update=True)
        if current is None:
            raise HTTPException(status_code=404, detail="ENTITY_NOT_FOUND")
        if current["version"] != payload.expected_version:
            raise HTTPException(status_code=409, detail="VERSION_CONFLICT")

        assignments = ["updated_at = :now", "version = version + 1"]
        params: dict[str, Any] = {
            "workspace_id": auth.workspace_id,
            "entity_id": entity_id,
            "now": now,
        }
        changed_fields: list[str] = []
        if payload.canonical_name is not None:
            assignments.append("canonical_name = :canonical_name")
            params["canonical_name"] = payload.canonical_name
            changed_fields.append("canonical_name")
        if "summary" in payload.model_fields_set:
            attributes = dict(current.get("attributes") or {})
            attributes["summary"] = payload.summary
            assignments.append("attributes = CAST(:attributes AS jsonb)")
            params["attributes"] = dumps(attributes)
            changed_fields.append("summary")

        row = (
            session.execute(
                text(
                    f"""
                    UPDATE pkos_nodes
                    SET {", ".join(assignments)}
                    WHERE workspace_id = :workspace_id AND id = :entity_id
                    RETURNING {_ENTITY_FIELDS}
                    """
                ),
                params,
            )
            .mappings()
            .one()
        )
        response = _project(dict(row))
        _write_side_effects(
            session,
            auth,
            request,
            "knowledge_entity.updated",
            entity_id,
            response.version,
            sorted(changed_fields),
            now,
        )
        queue_timeline_entry(
            session,
            auth.workspace_id,
            entity_id,
            "knowledge_entity.updated",
            f"updated: {', '.join(sorted(changed_fields))}",
            now,
        )
        queue_retrieval_document(
            session,
            auth.workspace_id,
            entity_id,
            response.kind,
            response.canonical_name,
            response.summary,
            response.version,
            now,
        )
        _store_cached(session, auth, idempotency_key, request_hash, response, now)
        return response


def _transition_action(
    entity_id: UUID,
    payload: EntityAction,
    request: Request,
    auth: AuthContext,
    session: Session,
    idempotency_key: str,
    action: str,
) -> EntityResponse:
    request_hash = _request_hash(payload, f"{action}:{entity_id}")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached
        current = _get_row(session, auth, entity_id, for_update=True)
        if current is None:
            raise HTTPException(status_code=404, detail="ENTITY_NOT_FOUND")
        if current["version"] != payload.expected_version:
            raise HTTPException(status_code=409, detail="VERSION_CONFLICT")
        if action == "archive":
            if current["status"] != "active":
                raise HTTPException(status_code=409, detail="ENTITY_NOT_ACTIVE")
            new_status = "archived"
            event_type = "knowledge_entity.archived"
        else:
            if current["status"] != "archived":
                raise HTTPException(status_code=409, detail="ENTITY_NOT_ARCHIVED")
            new_status = "active"
            event_type = "knowledge_entity.restored"
        row = (
            session.execute(
                text(
                    f"""
                    UPDATE pkos_nodes
                    SET status = :status, updated_at = :now, version = version + 1
                    WHERE workspace_id = :workspace_id AND id = :entity_id
                    RETURNING {_ENTITY_FIELDS}
                    """
                ),
                {
                    "workspace_id": auth.workspace_id,
                    "entity_id": entity_id,
                    "status": new_status,
                    "now": now,
                },
            )
            .mappings()
            .one()
        )
        response = _project(dict(row))
        _write_side_effects(
            session, auth, request, event_type, entity_id, response.version, ["status"], now
        )
        queue_timeline_entry(
            session, auth.workspace_id, entity_id, event_type, event_type.split(".")[-1], now
        )
        _store_cached(session, auth, idempotency_key, request_hash, response, now)
        return response


@router.post("/{entity_id}/archive", response_model=EntityResponse)
def archive_entity(
    entity_id: UUID,
    payload: EntityAction,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> EntityResponse:
    return _transition_action(
        entity_id, payload, request, auth, session, idempotency_key, "archive"
    )


@router.post("/{entity_id}/restore", response_model=EntityResponse)
def restore_entity(
    entity_id: UUID,
    payload: EntityAction,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> EntityResponse:
    return _transition_action(
        entity_id, payload, request, auth, session, idempotency_key, "restore"
    )
