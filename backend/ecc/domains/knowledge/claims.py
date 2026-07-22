from datetime import UTC, datetime, timedelta
from hashlib import sha256
from json import dumps
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session
from ecc.domains.knowledge.embeddings import queue_embedding
from ecc.domains.knowledge.retrieval import queue_retrieval_document
from ecc.domains.knowledge.timeline import queue_timeline_entry
from ecc.observability import (
    queue_lifecycle_event,
    record_audit_outbox_failure,
    record_idempotency_conflict,
)

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
    session: Session, auth: AuthContext, key: str, request_hash: str
) -> ClaimResponse | None:
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
        record_idempotency_conflict("knowledge_claims")
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
    return ClaimResponse.model_validate(row["response_body"])


def _store_cached(
    session: Session,
    auth: AuthContext,
    key: str,
    request_hash: str,
    response: ClaimResponse,
    now: datetime,
) -> None:
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
            "key": key,
            "request_hash": request_hash,
            "response_body": dumps(response.model_dump(mode="json")),
            "created_at": now,
            "expires_at": now + timedelta(days=365),
        },
    )


def _entity_version(session: Session, auth: AuthContext, entity_id: UUID) -> int | None:
    row = session.execute(
        text(
            "SELECT version FROM pkos_nodes WHERE workspace_id = :workspace_id AND id = :entity_id"
        ),
        {"workspace_id": auth.workspace_id, "entity_id": entity_id},
    ).one_or_none()
    return row[0] if row is not None else None


def _evidence_state(session: Session, auth: AuthContext, evidence_id: UUID) -> str | None:
    row = session.execute(
        text(
            "SELECT evidence_state FROM pkos_evidence"
            " WHERE workspace_id = :workspace_id AND id = :evidence_id"
        ),
        {"workspace_id": auth.workspace_id, "evidence_id": evidence_id},
    ).one_or_none()
    return row[0] if row is not None else None


def _entity_retrieval_fields(
    session: Session, auth: AuthContext, entity_id: UUID
) -> tuple[str, str, str | None, int] | None:
    row = session.execute(
        text(
            """
            SELECT node_type, canonical_name, attributes, version FROM pkos_nodes
            WHERE workspace_id = :workspace_id AND id = :entity_id
            """
        ),
        {"workspace_id": auth.workspace_id, "entity_id": entity_id},
    ).one_or_none()
    if row is None:
        return None
    node_type, canonical_name, attributes, version = row
    summary = (attributes or {}).get("summary")
    return node_type, canonical_name, summary, version


def _write_side_effects(
    session: Session,
    auth: AuthContext,
    request: Request,
    event_type: str,
    entity_id: UUID,
    entity_version: int,
    claim_id: UUID,
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
                    ARRAY['*'], 'allowed', 'user', '{}'::jsonb, :occurred_at
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": auth.workspace_id,
                "event_type": event_type,
                "aggregate_id": entity_id,
                "aggregate_version": entity_version,
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
                "payload": dumps({"entity_id": str(entity_id), "claim_id": str(claim_id)}),
                "occurred_at": now,
            },
        )
    except SQLAlchemyError:
        record_audit_outbox_failure("knowledge_claims")
        raise
    queue_lifecycle_event(session, "knowledge_entity", event_type, "allowed")


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
                    confidence, valid_from, valid_to, created_at
                ) VALUES (
                    :id, :workspace_id, :subject_id, :predicate, CAST(:value_json AS jsonb),
                    :source_id, :confidence, :valid_from, :valid_to, :now
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
    request_hash = _request_hash(payload, f"create:{entity_id}")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached
        entity_version = _entity_version(session, auth, entity_id)
        if entity_version is None:
            raise HTTPException(status_code=404, detail="ENTITY_NOT_FOUND")
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
        _write_side_effects(
            session,
            auth,
            request,
            "knowledge_entity.claim_recorded",
            entity_id,
            entity_version,
            response.id,
            now,
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
        _store_cached(session, auth, idempotency_key, request_hash, response, now)
        return response


@router.get("/{entity_id}/claims", response_model=ClaimListResponse)
def list_claims(entity_id: UUID, auth: AuthDep, session: SessionDep) -> ClaimListResponse:
    rows = (
        session.execute(
            text(
                f"""
                SELECT {_CLAIM_FIELDS}
                FROM knowledge_claims
                WHERE workspace_id = :workspace_id AND subject_id = :entity_id
                ORDER BY created_at DESC
                """
            ),
            {"workspace_id": auth.workspace_id, "entity_id": entity_id},
        )
        .mappings()
        .all()
    )
    return ClaimListResponse(items=[_project(dict(row)) for row in rows])


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
    request_hash = _request_hash(payload, f"supersede:{entity_id}:{claim_id}")
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
                SET superseded_by = :new_id, valid_to = :now
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
        _write_side_effects(
            session,
            auth,
            request,
            "knowledge_entity.claim_recorded",
            entity_id,
            entity_version,
            response.id,
            now,
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
        _store_cached(session, auth, idempotency_key, request_hash, response, now)
        return response
