from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session
from ecc.domains.knowledge.relationships import (
    RelationshipResponse,
    _load_cached,
    _project,
    _request_hash,
    _source_entity_version,
    _store_cached,
    _write_side_effects,
)

router = APIRouter(prefix="/api/v1/knowledge/relationships", tags=["knowledge-relationships"])
SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]

_RELATIONSHIP_FIELDS = """
id, source_node_id, target_node_id, edge_type, confidence, evidence_id,
valid_from, valid_to, status
"""


class RelationshipInvalidate(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _lock_idempotency(session: Session, auth: AuthContext, key: str) -> None:
    lock_key = f"{auth.workspace_id}:{auth.user_id}:{key}"
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": lock_key},
    )


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
    request_hash = _request_hash(payload, f"invalidate:{relationship_id}")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached
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
        row = (
            session.execute(
                text(
                    f"""
                    UPDATE pkos_edges
                    SET status = 'invalidated', valid_to = :now
                    WHERE workspace_id = :workspace_id AND id = :relationship_id
                    RETURNING {_RELATIONSHIP_FIELDS}
                    """
                ),
                {
                    "workspace_id": auth.workspace_id,
                    "relationship_id": relationship_id,
                    "now": now,
                },
            )
            .mappings()
            .one()
        )
        response = _project(dict(row))
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
        _store_cached(session, auth, idempotency_key, request_hash, response, now, 200)
        return response
