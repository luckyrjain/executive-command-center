from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import compare_digest, new
from json import dumps, loads
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.config import get_settings
from ecc.database import get_session
from ecc.observability import (
    queue_lifecycle_event,
    record_audit_outbox_failure,
    record_idempotency_conflict,
)

router = APIRouter(prefix="/api/v1/knowledge/entities", tags=["knowledge-entities"])

EntityKind = Literal["person", "organization", "project", "topic", "decision", "document"]
EntityStatus = Literal["active", "archived", "redirected"]
SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]

_ENTITY_FIELDS = """
id, entity_id, node_type, canonical_name, attributes, status, confidence,
version, created_at, updated_at
"""


class EntityCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: EntityKind
    canonical_name: str = Field(min_length=1, max_length=500)
    summary: str | None = Field(default=None, max_length=5000)


class EntityResponse(BaseModel):
    id: UUID
    entity_id: UUID | None
    kind: EntityKind
    canonical_name: str
    summary: str | None
    status: EntityStatus
    confidence: float
    version: int
    created_at: datetime
    updated_at: datetime


class EntityListResponse(BaseModel):
    items: list[EntityResponse]
    next_cursor: str | None = None


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


def _project(row: dict[str, Any]) -> EntityResponse:
    attributes = row.get("attributes") or {}
    return EntityResponse(
        id=row["id"],
        entity_id=row["entity_id"],
        kind=row["node_type"],
        canonical_name=row["canonical_name"],
        summary=attributes.get("summary"),
        status=row["status"],
        confidence=float(row["confidence"]),
        version=row["version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _encode_cursor(updated_at: datetime, entity_id: UUID) -> str:
    payload = dumps({"updated_at": updated_at.isoformat(), "id": str(entity_id)}).encode()
    signature = new(get_settings().session_secret.encode(), payload, "sha256").hexdigest().encode()
    return urlsafe_b64encode(payload + b"." + signature).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        raw = urlsafe_b64decode((cursor + "=" * (-len(cursor) % 4)).encode())
        payload, signature = raw.rsplit(b".", 1)
        expected = new(get_settings().session_secret.encode(), payload, "sha256").hexdigest()
        if not compare_digest(signature.decode(), expected):
            raise ValueError
        decoded = loads(payload)
        return datetime.fromisoformat(decoded["updated_at"]), UUID(decoded["id"])
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="MALFORMED_CURSOR") from exc


def _get_row(session: Session, auth: AuthContext, entity_id: UUID) -> dict[str, Any] | None:
    row = (
        session.execute(
            text(
                f"""
                SELECT {_ENTITY_FIELDS}
                FROM pkos_nodes
                WHERE workspace_id = :workspace_id AND id = :entity_id
                """
            ),
            {"workspace_id": auth.workspace_id, "entity_id": entity_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


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


def _write_side_effects(
    session: Session,
    auth: AuthContext,
    request: Request,
    entity_id: UUID,
    version: int,
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
                    :id, :workspace_id, 'knowledge_entity.created', 'knowledge_entity',
                    :aggregate_id, :aggregate_version, :actor_id, :request_id,
                    :correlation_id, ARRAY['*'], 'allowed', 'user', '{}'::jsonb, :occurred_at
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": auth.workspace_id,
                "aggregate_id": entity_id,
                "aggregate_version": version,
                "actor_id": auth.user_id,
                "request_id": request_id,
                "correlation_id": correlation_id,
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
                    :event_id, :workspace_id, 'knowledge_entity.created.v1', 1,
                    :correlation_id, CAST(:payload AS jsonb), :occurred_at, 0
                )
                """
            ),
            {
                "event_id": uuid4(),
                "workspace_id": auth.workspace_id,
                "correlation_id": correlation_id,
                "payload": dumps({"entity_id": str(entity_id), "version": version}),
                "occurred_at": now,
            },
        )
    except SQLAlchemyError:
        record_audit_outbox_failure("knowledge_entities")
        raise
    queue_lifecycle_event(session, "knowledge_entity", "knowledge_entity.created", "allowed")


def create_entity_core(
    payload: EntityCreate,
    request: Request,
    auth: AuthContext,
    session: Session,
    idempotency_key: str,
) -> EntityResponse:
    """Shared entity-creation path behind both this router's POST /entities
    and backend/ecc/domains/identity/person_organizations.py's thin
    kind-constrained wrappers -- Person/Organization are Identity-owned per
    docs/domain/DOMAIN-MODEL.md's ownership map but physically the same
    pkos_nodes table every other knowledge entity uses, since PKOS is the
    shared canonical store (see the Phase 2 design doc's Open decision 1)."""
    request_hash = _request_hash(payload, "create")
    now = datetime.now(UTC)
    entity_id = uuid4()
    attributes = {"summary": payload.summary} if payload.summary is not None else {}
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached
        row = (
            session.execute(
                text(
                    f"""
                    INSERT INTO pkos_nodes (
                        id, workspace_id, node_type, canonical_name, attributes,
                        status, confidence, version, created_at, updated_at
                    ) VALUES (
                        :id, :workspace_id, :kind, :canonical_name, CAST(:attributes AS jsonb),
                        'active', 1.00, 1, :now, :now
                    )
                    RETURNING {_ENTITY_FIELDS}
                    """
                ),
                {
                    "id": entity_id,
                    "workspace_id": auth.workspace_id,
                    "kind": payload.kind,
                    "canonical_name": payload.canonical_name,
                    "attributes": dumps(attributes),
                    "now": now,
                },
            )
            .mappings()
            .one()
        )
        response = _project(dict(row))
        _write_side_effects(session, auth, request, entity_id, 1, now)
        session.execute(
            text(
                """
                INSERT INTO idempotency_records (
                    workspace_id, actor_id, key, request_hash, response_status,
                    response_body, created_at, expires_at
                ) VALUES (
                    :workspace_id, :actor_id, :key, :request_hash, 201,
                    CAST(:response_body AS jsonb), :created_at, :expires_at
                )
                """
            ),
            {
                "workspace_id": auth.workspace_id,
                "actor_id": auth.user_id,
                "key": idempotency_key,
                "request_hash": request_hash,
                "response_body": dumps(response.model_dump(mode="json")),
                "created_at": now,
                "expires_at": now + timedelta(days=365),
            },
        )
        return response


@router.post("", response_model=EntityResponse, status_code=status.HTTP_201_CREATED)
def create_entity(
    payload: EntityCreate,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> EntityResponse:
    return create_entity_core(payload, request, auth, session, idempotency_key)


@router.get("", response_model=EntityListResponse)
def list_entities(
    auth: AuthDep,
    session: SessionDep,
    kind: Annotated[EntityKind | None, Query()] = None,
    status_filter: Annotated[EntityStatus | None, Query(alias="status")] = None,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> EntityListResponse:
    clauses = ["workspace_id = :workspace_id"]
    params: dict[str, Any] = {"workspace_id": auth.workspace_id, "limit": limit + 1}
    if kind is not None:
        clauses.append("node_type = :kind")
        params["kind"] = kind
    if status_filter is not None:
        clauses.append("status = :status")
        params["status"] = status_filter
    if cursor is not None:
        updated_at, cursor_id = _decode_cursor(cursor)
        clauses.append("(updated_at, id) < (:cursor_updated_at, :cursor_id)")
        params.update({"cursor_updated_at": updated_at, "cursor_id": cursor_id})
    rows = (
        session.execute(
            text(
                f"""
                SELECT {_ENTITY_FIELDS}
                FROM pkos_nodes
                WHERE {" AND ".join(clauses)}
                ORDER BY updated_at DESC, id DESC
                LIMIT :limit
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit and page:
        last = page[-1]
        next_cursor = _encode_cursor(last["updated_at"], last["id"])
    return EntityListResponse(
        items=[_project(dict(row)) for row in page],
        next_cursor=next_cursor,
    )


@router.get("/{entity_id}", response_model=EntityResponse)
def get_entity(entity_id: UUID, auth: AuthDep, session: SessionDep) -> EntityResponse:
    row = _get_row(session, auth, entity_id)
    if row is None:
        raise HTTPException(status_code=404, detail="ENTITY_NOT_FOUND")
    return _project(row)
