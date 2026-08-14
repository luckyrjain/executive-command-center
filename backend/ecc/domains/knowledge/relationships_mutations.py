from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthDep, CsrfDep
from ecc.database import get_session
from ecc.domains.knowledge.relationships import (
    RelationshipResponse,
    _fetch_relationship,
    _source_entity_version,
    _write_side_effects,
)
from ecc.domains.knowledge.timeline import queue_timeline_entry
from ecc.platform import authz
from ecc.platform.idempotency import load_cached, lock_idempotency, request_hash, store_idempotency

router = APIRouter(prefix="/api/v1/knowledge/relationships", tags=["knowledge-relationships"])
SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]


class RelationshipInvalidate(BaseModel):
    model_config = ConfigDict(extra="forbid")


@router.post("/{relationship_id}/invalidate", response_model=RelationshipResponse)
def invalidate_relationship(
    relationship_id: UUID,
    payload: RelationshipInvalidate,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> RelationshipResponse:
    req_hash = request_hash(payload, f"invalidate:{relationship_id}")
    now = datetime.now(UTC)
    with session.begin():
        lock_idempotency(session, auth, idempotency_key)
        cached = load_cached(
            session,
            auth,
            idempotency_key,
            req_hash,
            domain="knowledge_relationships",
            response_model=RelationshipResponse,
        )
        if cached is not None:
            return cached
        if not authz.authorize(
            session,
            auth,
            resource_type="pkos_edges",
            resource_id=relationship_id,
            action="read",
        ):
            raise HTTPException(status_code=404, detail="RELATIONSHIP_NOT_FOUND")
        if not authz.authorize(
            session,
            auth,
            resource_type="pkos_edges",
            resource_id=relationship_id,
            action="write",
        ):
            raise HTTPException(status_code=403, detail="INSUFFICIENT_ROLE")
        current = (
            session.execute(
                text(
                    """
                    SELECT status FROM pkos_edges
                    WHERE workspace_id = :workspace_id AND id = :relationship_id
                    FOR UPDATE
                    """
                ),
                {"workspace_id": auth.workspace_id, "relationship_id": relationship_id},
            )
            .mappings()
            .one_or_none()
        )
        if current is None:
            raise HTTPException(status_code=404, detail="RELATIONSHIP_NOT_FOUND")
        if current["status"] != "active":
            raise HTTPException(status_code=409, detail="RELATIONSHIP_NOT_ACTIVE")
        session.execute(
            text(
                """
                UPDATE pkos_edges
                SET status = 'invalidated', valid_to = :now
                WHERE workspace_id = :workspace_id AND id = :relationship_id
                """
            ),
            {
                "workspace_id": auth.workspace_id,
                "relationship_id": relationship_id,
                "now": now,
            },
        )
        # Fetched separately (not via `RETURNING`) since the join that
        # resolves `from_entity_name`/`to_entity_name` reads other rows in
        # `pkos_nodes` -- see `relationships.py`'s own `_fetch_relationship`.
        response = _fetch_relationship(session, auth, relationship_id)
        source_version = _source_entity_version(session, auth, relationship_id)
        _write_side_effects(
            session,
            auth,
            request,
            "relationship.invalidated",
            relationship_id,
            source_version,
            now,
        )
        queue_timeline_entry(
            session,
            auth.workspace_id,
            response.from_entity_id,
            "relationship.invalidated",
            f"{response.relationship_type} -> {response.to_entity_id} invalidated",
            now,
            source_id=response.evidence_id,
        )
        store_idempotency(
            session, auth, idempotency_key, req_hash, response.model_dump(mode="json"), now, 200
        )
        return response
