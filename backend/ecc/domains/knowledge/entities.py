from datetime import UTC, datetime
from json import dumps
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session
from ecc.domains.knowledge.embeddings import queue_embedding
from ecc.domains.knowledge.retrieval import queue_retrieval_document
from ecc.domains.knowledge.timeline import queue_timeline_entry
from ecc.observability import queue_lifecycle_event
from ecc.platform import audit_outbox, authz, cursor_pagination
from ecc.platform.idempotency import load_cached, lock_idempotency, request_hash, store_idempotency

router = APIRouter(prefix="/api/v1/knowledge/entities", tags=["knowledge-entities"])

# "team" added to unblock team-scoped views in later phases (e.g. Phase 6
# engineering ownership, team-scoped dashboards) -- entities_mutations.py/
# resolution.py/entity_operations.py/claims.py are all kind-agnostic
# already, so this Literal is the only code change needed for baseline
# create/list/patch/dedup/merge support.
# Populating team membership/ownership (which repo, work item, or dashboard
# belongs to which team) is deliberately out of scope here -- see
# `docs/phases/phase-002/DATA-MODEL.md`'s own entry for this addition.
EntityKind = Literal["person", "organization", "project", "topic", "decision", "document", "team"]
EntityStatus = Literal["active", "archived", "redirected"]
SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]

_ENTITY_FIELDS = """
id, node_type, canonical_name, attributes, status, confidence,
version, created_at, updated_at
"""


class EntityCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: EntityKind
    canonical_name: str = Field(min_length=1, max_length=500)
    summary: str | None = Field(default=None, max_length=5000)


class EntityResponse(BaseModel):
    id: UUID
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


class EntityAliasResponse(BaseModel):
    id: UUID
    entity_id: UUID
    alias_type: str
    normalized_value: str
    source_id: UUID
    confidence: float
    created_at: datetime


class EntityAliasListResponse(BaseModel):
    items: list[EntityAliasResponse]


def project_entity(row: dict[str, Any]) -> EntityResponse:
    attributes = row.get("attributes") or {}
    return EntityResponse(
        id=row["id"],
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
    return cursor_pagination.encode_cursor(
        {"updated_at": updated_at.isoformat(), "id": str(entity_id)}
    )


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    decoded = cursor_pagination.decode_cursor(cursor)
    try:
        return datetime.fromisoformat(decoded["updated_at"]), UUID(decoded["id"])
    except (ValueError, KeyError, TypeError) as exc:
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
    authz.require_role_action(session, auth, "write")
    req_hash = request_hash(payload, "create")
    now = datetime.now(UTC)
    entity_id = uuid4()
    attributes = {"summary": payload.summary} if payload.summary is not None else {}
    with session.begin():
        lock_idempotency(session, auth, idempotency_key)
        cached = load_cached(
            session,
            auth,
            idempotency_key,
            req_hash,
            domain="knowledge_entities",
            response_model=EntityResponse,
        )
        if cached is not None:
            return cached
        row = (
            session.execute(
                text(
                    f"""
                    INSERT INTO pkos_nodes (
                        id, workspace_id, node_type, canonical_name, attributes,
                        status, confidence, version, created_at, updated_at,
                        owner_id, visibility
                    ) VALUES (
                        :id, :workspace_id, :kind, :canonical_name, CAST(:attributes AS jsonb),
                        'active', 1.00, 1, :now, :now, :actor_id, 'workspace'
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
                    "actor_id": auth.user_id,
                },
            )
            .mappings()
            .one()
        )
        response = project_entity(dict(row))
        audit_outbox.write_audit_and_outbox(
            session,
            auth,
            request,
            event_type="knowledge_entity.created",
            aggregate_type="knowledge_entity",
            aggregate_id=entity_id,
            aggregate_version=1,
            changed_fields=["*"],
            payload={"entity_id": str(entity_id), "version": 1},
            now=now,
            domain="knowledge_entities",
        )
        queue_lifecycle_event(session, "knowledge_entity", "knowledge_entity.created", "allowed")
        queue_timeline_entry(
            session,
            auth.workspace_id,
            entity_id,
            "knowledge_entity.created",
            f"{payload.kind} '{payload.canonical_name}' created",
            now,
        )
        queue_retrieval_document(
            session,
            auth.workspace_id,
            entity_id,
            payload.kind,
            payload.canonical_name,
            payload.summary,
            1,
            now,
        )
        queue_embedding(session, auth.workspace_id, entity_id, now)
        store_idempotency(
            session,
            auth,
            idempotency_key,
            req_hash,
            response.model_dump(mode="json"),
            now,
            response_status=201,
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
    visibility_sql, visibility_params = authz.visible_resource_filter_sql(
        session, auth, resource_type="pkos_nodes", action="read", table_alias="pkos_nodes"
    )
    clauses = ["workspace_id = :workspace_id", f"({visibility_sql})"]
    params: dict[str, Any] = {
        "workspace_id": auth.workspace_id,
        "limit": limit + 1,
        **visibility_params,
    }
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
    session.rollback()
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit and page:
        last = page[-1]
        next_cursor = _encode_cursor(last["updated_at"], last["id"])
    return EntityListResponse(
        items=[project_entity(dict(row)) for row in page],
        next_cursor=next_cursor,
    )


@router.get("/{entity_id}", response_model=EntityResponse)
def get_entity(entity_id: UUID, auth: AuthDep, session: SessionDep) -> EntityResponse:
    visible = authz.authorize(
        session, auth, resource_type="pkos_nodes", resource_id=entity_id, action="read"
    )
    session.rollback()
    if not visible:
        raise HTTPException(status_code=404, detail="ENTITY_NOT_FOUND")
    row = _get_row(session, auth, entity_id)
    if row is None:
        raise HTTPException(status_code=404, detail="ENTITY_NOT_FOUND")
    return project_entity(row)


@router.get("/{entity_id}/aliases", response_model=EntityAliasListResponse)
def list_entity_aliases(
    entity_id: UUID, auth: AuthDep, session: SessionDep
) -> EntityAliasListResponse:
    visible = authz.authorize(
        session, auth, resource_type="pkos_nodes", resource_id=entity_id, action="read"
    )
    session.rollback()
    if not visible:
        raise HTTPException(status_code=404, detail="ENTITY_NOT_FOUND")
    if _get_row(session, auth, entity_id) is None:
        raise HTTPException(status_code=404, detail="ENTITY_NOT_FOUND")
    rows = (
        session.execute(
            text(
                """
                SELECT id, entity_id, alias_type, normalized_value, source_id,
                       confidence, created_at
                FROM entity_aliases
                WHERE workspace_id = :workspace_id AND entity_id = :entity_id
                ORDER BY created_at, id
                """
            ),
            {"workspace_id": auth.workspace_id, "entity_id": entity_id},
        )
        .mappings()
        .all()
    )
    return EntityAliasListResponse(
        items=[
            EntityAliasResponse(
                id=row["id"],
                entity_id=row["entity_id"],
                alias_type=row["alias_type"],
                normalized_value=row["normalized_value"],
                source_id=row["source_id"],
                confidence=float(row["confidence"]),
                created_at=row["created_at"],
            )
            for row in rows
        ]
    )
