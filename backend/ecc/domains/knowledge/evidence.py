from datetime import UTC, datetime, timedelta
from hashlib import sha256
from json import dumps
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session
from ecc.domains.knowledge.embeddings import queue_embedding
from ecc.domains.knowledge.retrieval import queue_retrieval_document
from ecc.observability import (
    queue_lifecycle_event,
    record_audit_outbox_failure,
    record_idempotency_conflict,
)

router = APIRouter(prefix="/api/v1/evidence", tags=["evidence"])
SessionDep = Annotated[Session, Depends(get_session)]
IdsQuery = Annotated[list[UUID] | None, Query(alias="id")]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]

EvidenceStatus = Literal["available", "missing"]

# Redaction placeholder for a deleted evidence row's locator -- DATA-MODEL.md:
# "Source deletion changes evidence to `deleted`... and retains only minimal
# redacted lineage required for audit integrity." The row itself is never
# deleted (matching ADR-0003's "never overwrite source evidence" and this
# codebase's existing no-authoritative-record-is-deleted convention), only
# its content-bearing locator is replaced; id/node_id/source_type/captured_at
# and the pre-existing sha256 integrity fingerprint are kept for audit trail.
_REDACTED_SOURCE_REF = "[redacted: source deleted]"


class EvidenceItem(BaseModel):
    id: UUID
    status: EvidenceStatus
    source_type: str | None
    label: str | None
    captured_at: datetime | None


class EvidenceListResponse(BaseModel):
    items: list[EvidenceItem]


class EvidenceDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=2000)


class EvidenceDeleteResponse(BaseModel):
    id: UUID
    evidence_state: str


@router.get("", response_model=EvidenceListResponse)
def resolve_evidence(
    auth: AuthDep,
    session: SessionDep,
    ids: IdsQuery = None,
) -> EvidenceListResponse:
    requested = ids or []
    if not requested:
        return EvidenceListResponse(items=[])

    rows = (
        session.execute(
            text(
                """
                SELECT e.id AS id, e.source_type AS source_type,
                       e.captured_at AS captured_at, n.canonical_name AS label
                FROM pkos_evidence AS e
                JOIN pkos_nodes AS n
                  ON n.workspace_id = e.workspace_id AND n.id = e.node_id
                WHERE e.workspace_id = :workspace_id
                  AND e.id = ANY(CAST(:ids AS uuid[]))
                """
            ),
            {"workspace_id": auth.workspace_id, "ids": requested},
        )
        .mappings()
        .all()
    )
    found = {row["id"]: row for row in rows}

    items: list[EvidenceItem] = []
    for evidence_id in requested:
        row = found.get(evidence_id)
        if row is None:
            items.append(
                EvidenceItem(
                    id=evidence_id,
                    status="missing",
                    source_type=None,
                    label=None,
                    captured_at=None,
                )
            )
        else:
            items.append(
                EvidenceItem(
                    id=evidence_id,
                    status="available",
                    source_type=row["source_type"],
                    label=row["label"],
                    captured_at=row["captured_at"],
                )
            )
    return EvidenceListResponse(items=items)


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
) -> EvidenceDeleteResponse | None:
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
        record_idempotency_conflict("evidence")
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
    return EvidenceDeleteResponse.model_validate(row["response_body"])


def _store_cached(
    session: Session,
    auth: AuthContext,
    key: str,
    request_hash: str,
    response: EvidenceDeleteResponse,
    now: datetime,
    status_code: int,
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
            "response_status": status_code,
            "response_body": dumps(response.model_dump(mode="json")),
            "created_at": now,
            "expires_at": now + timedelta(days=365),
        },
    )


def _write_side_effects(
    session: Session, auth: AuthContext, request: Request, evidence_id: UUID, now: datetime
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
                    :id, :workspace_id, 'evidence.deleted', 'evidence', :aggregate_id,
                    1, :actor_id, :request_id, :correlation_id,
                    ARRAY['evidence_state'], 'allowed', 'user', '{}'::jsonb, :occurred_at
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": auth.workspace_id,
                "aggregate_id": evidence_id,
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
                    :event_id, :workspace_id, 'evidence.deleted.v1', 1,
                    :correlation_id, CAST(:payload AS jsonb), :occurred_at, 0
                )
                """
            ),
            {
                "event_id": uuid4(),
                "workspace_id": auth.workspace_id,
                "correlation_id": correlation_id,
                "payload": dumps({"evidence_id": str(evidence_id)}),
                "occurred_at": now,
            },
        )
    except SQLAlchemyError:
        record_audit_outbox_failure("evidence")
        raise
    queue_lifecycle_event(session, "evidence", "evidence.deleted", "allowed")


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


def _refresh_projections(
    session: Session, auth: AuthContext, entity_id: UUID, now: datetime
) -> None:
    fields = _entity_retrieval_fields(session, auth, entity_id)
    if fields is not None:
        kind, canonical_name, summary, version = fields
        queue_retrieval_document(
            session, auth.workspace_id, entity_id, kind, canonical_name, summary, version, now
        )
        queue_embedding(session, auth.workspace_id, entity_id, now)


@router.post(
    "/{evidence_id}/delete", response_model=EvidenceDeleteResponse, status_code=200
)
def delete_evidence(
    evidence_id: UUID,
    payload: EvidenceDeleteRequest,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> EvidenceDeleteResponse:
    """DATA-MODEL.md's Isolation and deletion section: "Source deletion
    changes evidence to `deleted`, removes derived searchable content and
    embeddings, and retains only minimal redacted lineage required for
    audit integrity." Every entity whose claims cited this evidence has its
    retrieval_documents/embeddings refreshed afterward -- _build_body
    (retrieval.py) already excludes claims whose evidence_state isn't
    'available', so this refresh is what actually removes the now-invalid
    content from search, not just a state flag with no observable effect."""
    request_hash = _request_hash(payload, f"delete:{evidence_id}")
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
                    SELECT id, node_id, evidence_state FROM pkos_evidence
                    WHERE workspace_id = :workspace_id AND id = :evidence_id
                    FOR UPDATE
                    """
                ),
                {"workspace_id": auth.workspace_id, "evidence_id": evidence_id},
            )
            .mappings()
            .one_or_none()
        )
        if current is None:
            raise HTTPException(status_code=404, detail="EVIDENCE_NOT_FOUND")

        if current["evidence_state"] == "deleted":
            response = EvidenceDeleteResponse(id=evidence_id, evidence_state="deleted")
            _store_cached(session, auth, idempotency_key, request_hash, response, now, 200)
            return response

        # Every entity a claim or relationship attributes to this evidence
        # -- their retrieval_documents/embeddings are now stale and must be
        # refreshed, per the invariant's "removes derived searchable
        # content and embeddings."
        affected_entities = {
            row[0]
            for row in session.execute(
                text(
                    """
                    SELECT subject_id FROM knowledge_claims
                    WHERE workspace_id = :workspace_id AND source_id = :evidence_id
                    UNION
                    SELECT source_node_id FROM pkos_edges
                    WHERE workspace_id = :workspace_id AND evidence_id = :evidence_id
                    UNION
                    SELECT target_node_id FROM pkos_edges
                    WHERE workspace_id = :workspace_id AND evidence_id = :evidence_id
                    """
                ),
                {"workspace_id": auth.workspace_id, "evidence_id": evidence_id},
            ).all()
        }
        affected_entities.add(current["node_id"])

        session.execute(
            text(
                """
                UPDATE pkos_evidence
                SET evidence_state = 'deleted', source_ref = :redacted
                WHERE workspace_id = :workspace_id AND id = :evidence_id
                """
            ),
            {
                "workspace_id": auth.workspace_id,
                "evidence_id": evidence_id,
                "redacted": _REDACTED_SOURCE_REF,
            },
        )

        for entity_id in affected_entities:
            _refresh_projections(session, auth, entity_id, now)

        _write_side_effects(session, auth, request, evidence_id, now)
        response = EvidenceDeleteResponse(id=evidence_id, evidence_state="deleted")
        _store_cached(session, auth, idempotency_key, request_hash, response, now, 200)
        return response
