from datetime import UTC, datetime
from json import dumps
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session
from ecc.domains.knowledge.embeddings import queue_embedding
from ecc.domains.knowledge.entity_lookup import (
    entity_retrieval_fields as _entity_retrieval_fields,
)
from ecc.domains.knowledge.entity_lookup import (
    entity_status as _entity_status,
)
from ecc.domains.knowledge.entity_lookup import (
    entity_version as _entity_version,
)
from ecc.domains.knowledge.retrieval import queue_retrieval_document
from ecc.domains.knowledge.timeline import queue_timeline_entry
from ecc.observability import queue_lifecycle_event
from ecc.platform import audit_outbox, authz, cursor_pagination
from ecc.platform.idempotency import load_cached, lock_idempotency, request_hash, store_idempotency

router = APIRouter(prefix="/api/v1/knowledge/entities", tags=["knowledge-claims"])
SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]

_CLAIM_FIELDS = """
id, subject_id, predicate, value_json, source_id, confidence, valid_from,
valid_to, superseded_by, created_at
"""


class ClaimCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    predicate: str = Field(min_length=1, max_length=100)
    value: dict[str, Any]
    source_id: UUID
    confidence: float = Field(default=1.0, ge=0, le=1)
    valid_from: datetime | None = None
    valid_to: datetime | None = None

    # Mirrors relationships.py's identical rule (added there when an audit
    # of the shipped code found relationships enforcing this and claims not,
    # despite both sharing the same valid_from/valid_to shape).
    @model_validator(mode="after")
    def validate_valid_interval(self) -> ClaimCreate:
        if (
            self.valid_from is not None
            and self.valid_to is not None
            and self.valid_to <= self.valid_from
        ):
            raise ValueError("valid_to must be after valid_from")
        return self


class ClaimResponse(BaseModel):
    id: UUID
    subject_id: UUID
    predicate: str
    value: dict[str, Any]
    source_id: UUID
    confidence: float
    valid_from: datetime | None
    valid_to: datetime | None
    superseded_by: UUID | None
    created_at: datetime


class ClaimListResponse(BaseModel):
    items: list[ClaimResponse]
    next_cursor: str | None = None


def _encode_cursor(created_at: datetime, claim_id: UUID) -> str:
    return cursor_pagination.encode_cursor(
        {"created_at": created_at.isoformat(), "id": str(claim_id)}
    )


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    decoded = cursor_pagination.decode_cursor(cursor)
    try:
        return datetime.fromisoformat(decoded["created_at"]), UUID(decoded["id"])
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="MALFORMED_CURSOR") from exc


def _project(row: dict[str, Any]) -> ClaimResponse:
    return ClaimResponse(
        id=row["id"],
        subject_id=row["subject_id"],
        predicate=row["predicate"],
        value=row["value_json"],
        source_id=row["source_id"],
        confidence=float(row["confidence"]),
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        superseded_by=row["superseded_by"],
        created_at=row["created_at"],
    )


def _evidence_state(session: Session, auth: AuthContext, evidence_id: UUID) -> str | None:
    row = session.execute(
        text(
            "SELECT evidence_state FROM pkos_evidence"
            " WHERE workspace_id = :workspace_id AND id = :evidence_id"
        ),
        {"workspace_id": auth.workspace_id, "evidence_id": evidence_id},
    ).one_or_none()
    return row[0] if row is not None else None


def _insert_claim(
    session: Session,
    auth: AuthContext,
    entity_id: UUID,
    payload: ClaimCreate,
    now: datetime,
) -> dict[str, Any]:
    return dict(
        session.execute(
            text(
                f"""
                INSERT INTO knowledge_claims (
                    id, workspace_id, subject_id, predicate, value_json, source_id,
                    confidence, valid_from, valid_to, version, created_at, updated_at,
                    owner_id, visibility
                ) VALUES (
                    :id, :workspace_id, :subject_id, :predicate, CAST(:value_json AS jsonb),
                    :source_id, :confidence, :valid_from, :valid_to, 1, :now, :now,
                    :actor_id, 'workspace'
                )
                RETURNING {_CLAIM_FIELDS}
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": auth.workspace_id,
                "subject_id": entity_id,
                "predicate": payload.predicate,
                "value_json": dumps(payload.value),
                "source_id": payload.source_id,
                "confidence": payload.confidence,
                "valid_from": payload.valid_from,
                "valid_to": payload.valid_to,
                "now": now,
                "actor_id": auth.user_id,
            },
        )
        .mappings()
        .one()
    )


@router.post(
    "/{entity_id}/claims", response_model=ClaimResponse, status_code=status.HTTP_201_CREATED
)
def create_claim(
    entity_id: UUID,
    payload: ClaimCreate,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> ClaimResponse:
    req_hash = request_hash(payload, f"create:{entity_id}")
    now = datetime.now(UTC)
    with session.begin():
        lock_idempotency(session, auth, idempotency_key)
        cached = load_cached(
            session,
            auth,
            idempotency_key,
            req_hash,
            domain="knowledge_claims",
            response_model=ClaimResponse,
        )
        if cached is not None:
            return cached
        # A claim's authorization boundary is its subject entity -- claims
        # have no independent ownership/visibility meaningful apart from
        # the entity they're claims about, so the two-phase check runs
        # against pkos_nodes (entity_id), the same as every other entity-
        # scoped child-resource endpoint in this domain.
        if not authz.authorize(
            session, auth, resource_type="pkos_nodes", resource_id=entity_id, action="read"
        ):
            raise HTTPException(status_code=404, detail="ENTITY_NOT_FOUND")
        if not authz.authorize(
            session, auth, resource_type="pkos_nodes", resource_id=entity_id, action="write"
        ):
            raise HTTPException(status_code=403, detail="INSUFFICIENT_ROLE")
        entity_version = _entity_version(session, auth, entity_id)
        if entity_version is None:
            raise HTTPException(status_code=404, detail="ENTITY_NOT_FOUND")
        # relationships.py's create_relationship has the identical check for
        # the same reason (DATA-MODEL.md's canonical-identity invariant: an
        # archived entity is paused, not gone, but a redirected one has
        # already been superseded by a merge target, so new activity should
        # attach to the survivor instead) -- claims.py never mirrored it, a
        # gap an audit's adversarial re-review caught.
        entity_status = _entity_status(session, auth, entity_id)
        if entity_status != "active":
            raise HTTPException(status_code=409, detail="ENTITY_NOT_ACTIVE")
        # DATA-MODEL.md's "a claim ... has at least one source reference" is
        # enforced by the FK alone (source_id must reference a real evidence
        # row), but a reference to evidence that exists yet is no longer
        # `available` (deleted, missing, permission_denied) is not a
        # not-found -- it is a claim citing a source that can no longer back
        # it, closing a gap an audit of the shipped code found: this endpoint
        # never checked evidence_state at all before evidence-deletion (Task
        # 22) made a non-`available` state reachable in practice.
        evidence_state = _evidence_state(session, auth, payload.source_id)
        if evidence_state is None:
            raise HTTPException(status_code=404, detail="EVIDENCE_NOT_FOUND")
        if evidence_state != "available":
            raise HTTPException(status_code=422, detail="EVIDENCE_UNAVAILABLE")
        row = _insert_claim(session, auth, entity_id, payload, now)
        response = _project(row)
        audit_outbox.write_audit_and_outbox(
            session,
            auth,
            request,
            event_type="knowledge_entity.claim_recorded",
            aggregate_type="knowledge_entity",
            aggregate_id=entity_id,
            aggregate_version=entity_version,
            changed_fields=["*"],
            payload={"entity_id": str(entity_id), "claim_id": str(response.id)},
            now=now,
            domain="knowledge_claims",
        )
        queue_lifecycle_event(
            session, "knowledge_entity", "knowledge_entity.claim_recorded", "allowed"
        )
        queue_timeline_entry(
            session,
            auth.workspace_id,
            entity_id,
            "knowledge_entity.claim_recorded",
            f"claim recorded: {payload.predicate}",
            now,
            source_id=payload.source_id,
        )
        retrieval_fields = _entity_retrieval_fields(session, auth, entity_id)
        if retrieval_fields is not None:
            kind, canonical_name, summary, version = retrieval_fields
            queue_retrieval_document(
                session, auth.workspace_id, entity_id, kind, canonical_name, summary, version, now
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


@router.get("/{entity_id}/claims", response_model=ClaimListResponse)
def list_claims(
    entity_id: UUID,
    auth: AuthDep,
    session: SessionDep,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
) -> ClaimListResponse:
    # A cross-workspace/unknown entity_id never 404s here -- this is a list
    # endpoint, and this module's own established convention (see the
    # cross-workspace-isolation tests) returns an empty list, not 404, the
    # same way every other list endpoint in this domain does.
    visible = authz.authorize(
        session, auth, resource_type="pkos_nodes", resource_id=entity_id, action="read"
    )
    session.rollback()
    if not visible:
        return ClaimListResponse(items=[])
    clauses = ["workspace_id = :workspace_id", "subject_id = :entity_id"]
    params: dict[str, Any] = {
        "workspace_id": auth.workspace_id,
        "entity_id": entity_id,
        "limit": limit + 1,
    }
    if cursor is not None:
        created_at, cursor_id = _decode_cursor(cursor)
        clauses.append("(created_at, id) < (:cursor_created_at, :cursor_id)")
        params.update({"cursor_created_at": created_at, "cursor_id": cursor_id})
    rows = (
        session.execute(
            text(
                f"""
                SELECT {_CLAIM_FIELDS}
                FROM knowledge_claims
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
    page = rows[:limit]
    next_cursor = None
    if len(rows) > limit and page:
        last = page[-1]
        next_cursor = _encode_cursor(last["created_at"], last["id"])
    return ClaimListResponse(
        items=[_project(dict(row)) for row in page],
        next_cursor=next_cursor,
    )


@router.post(
    "/{entity_id}/claims/{claim_id}/supersede",
    response_model=ClaimResponse,
    status_code=status.HTTP_201_CREATED,
)
def supersede_claim(
    entity_id: UUID,
    claim_id: UUID,
    payload: ClaimCreate,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> ClaimResponse:
    req_hash = request_hash(payload, f"supersede:{entity_id}:{claim_id}")
    now = datetime.now(UTC)
    with session.begin():
        lock_idempotency(session, auth, idempotency_key)
        cached = load_cached(
            session,
            auth,
            idempotency_key,
            req_hash,
            domain="knowledge_claims",
            response_model=ClaimResponse,
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
        current = (
            session.execute(
                text(
                    """
                    SELECT id, superseded_by FROM knowledge_claims
                    WHERE workspace_id = :workspace_id AND id = :claim_id
                      AND subject_id = :entity_id
                    FOR UPDATE
                    """
                ),
                {
                    "workspace_id": auth.workspace_id,
                    "claim_id": claim_id,
                    "entity_id": entity_id,
                },
            )
            .mappings()
            .one_or_none()
        )
        if current is None:
            raise HTTPException(status_code=404, detail="CLAIM_NOT_FOUND")
        if current["superseded_by"] is not None:
            raise HTTPException(status_code=409, detail="CLAIM_ALREADY_SUPERSEDED")
        entity_version = _entity_version(session, auth, entity_id)
        if entity_version is None:
            raise HTTPException(status_code=404, detail="ENTITY_NOT_FOUND")
        entity_status = _entity_status(session, auth, entity_id)
        if entity_status != "active":
            raise HTTPException(status_code=409, detail="ENTITY_NOT_ACTIVE")
        evidence_state = _evidence_state(session, auth, payload.source_id)
        if evidence_state is None:
            raise HTTPException(status_code=404, detail="EVIDENCE_NOT_FOUND")
        if evidence_state != "available":
            raise HTTPException(status_code=422, detail="EVIDENCE_UNAVAILABLE")

        new_row = _insert_claim(session, auth, entity_id, payload, now)
        session.execute(
            text(
                """
                UPDATE knowledge_claims
                SET superseded_by = :new_id, valid_to = :now, updated_at = :now,
                    version = version + 1
                WHERE workspace_id = :workspace_id AND id = :claim_id
                """
            ),
            {
                "new_id": new_row["id"],
                "now": now,
                "workspace_id": auth.workspace_id,
                "claim_id": claim_id,
            },
        )
        response = _project(new_row)
        audit_outbox.write_audit_and_outbox(
            session,
            auth,
            request,
            event_type="knowledge_entity.claim_recorded",
            aggregate_type="knowledge_entity",
            aggregate_id=entity_id,
            aggregate_version=entity_version,
            changed_fields=["*"],
            payload={"entity_id": str(entity_id), "claim_id": str(response.id)},
            now=now,
            domain="knowledge_claims",
        )
        queue_lifecycle_event(
            session, "knowledge_entity", "knowledge_entity.claim_recorded", "allowed"
        )
        queue_timeline_entry(
            session,
            auth.workspace_id,
            entity_id,
            "knowledge_entity.claim_recorded",
            f"claim superseded: {payload.predicate}",
            now,
            source_id=payload.source_id,
        )
        retrieval_fields = _entity_retrieval_fields(session, auth, entity_id)
        if retrieval_fields is not None:
            kind, canonical_name, summary, version = retrieval_fields
            queue_retrieval_document(
                session, auth.workspace_id, entity_id, kind, canonical_name, summary, version, now
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
