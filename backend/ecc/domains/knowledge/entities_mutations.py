from datetime import UTC, datetime
from json import dumps
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session
from ecc.domains.knowledge.embeddings import queue_embedding
from ecc.domains.knowledge.entities import EntityResponse, project_entity
from ecc.domains.knowledge.retrieval import queue_retrieval_document
from ecc.domains.knowledge.timeline import queue_timeline_entry
from ecc.observability import queue_lifecycle_event
from ecc.platform import audit_outbox, authz
from ecc.platform.idempotency import load_cached, lock_idempotency, request_hash, store_idempotency

router = APIRouter(prefix="/api/v1/knowledge/entities", tags=["knowledge-entities"])
SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]

_ENTITY_FIELDS = """
id, node_type, canonical_name, attributes, status, confidence,
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
    req_hash = request_hash(payload, f"update:{entity_id}")
    now = datetime.now(UTC)
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
        # Two-phase read-then-write authz check -- see calendar/events.py's
        # update_calendar_event for the identical existence-leak reasoning.
        if not authz.authorize(
            session, auth, resource_type="pkos_nodes", resource_id=entity_id, action="read"
        ):
            raise HTTPException(status_code=404, detail="ENTITY_NOT_FOUND")
        if not authz.authorize(
            session, auth, resource_type="pkos_nodes", resource_id=entity_id, action="write"
        ):
            raise HTTPException(status_code=403, detail="INSUFFICIENT_ROLE")
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
        response = project_entity(dict(row))
        audit_outbox.write_audit_and_outbox(
            session,
            auth,
            request,
            event_type="knowledge_entity.updated",
            aggregate_type="knowledge_entity",
            aggregate_id=entity_id,
            aggregate_version=response.version,
            changed_fields=sorted(changed_fields),
            payload={"entity_id": str(entity_id), "version": response.version},
            now=now,
            domain="knowledge_entities",
        )
        queue_lifecycle_event(session, "knowledge_entity", "knowledge_entity.updated", "allowed")
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
        queue_embedding(session, auth.workspace_id, entity_id, now)
        store_idempotency(
            session, auth, idempotency_key, req_hash, response.model_dump(mode="json"), now
        )
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
    req_hash = request_hash(payload, f"{action}:{entity_id}")
    now = datetime.now(UTC)
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
        if not authz.authorize(
            session, auth, resource_type="pkos_nodes", resource_id=entity_id, action="read"
        ):
            raise HTTPException(status_code=404, detail="ENTITY_NOT_FOUND")
        if not authz.authorize(
            session, auth, resource_type="pkos_nodes", resource_id=entity_id, action="write"
        ):
            raise HTTPException(status_code=403, detail="INSUFFICIENT_ROLE")
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
        response = project_entity(dict(row))
        audit_outbox.write_audit_and_outbox(
            session,
            auth,
            request,
            event_type=event_type,
            aggregate_type="knowledge_entity",
            aggregate_id=entity_id,
            aggregate_version=response.version,
            changed_fields=["status"],
            payload={"entity_id": str(entity_id), "version": response.version},
            now=now,
            domain="knowledge_entities",
        )
        queue_lifecycle_event(session, "knowledge_entity", event_type, "allowed")
        queue_timeline_entry(
            session, auth.workspace_id, entity_id, event_type, event_type.split(".")[-1], now
        )
        store_idempotency(
            session, auth, idempotency_key, req_hash, response.model_dump(mode="json"), now
        )
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
