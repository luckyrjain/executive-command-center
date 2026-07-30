from datetime import UTC, datetime, timedelta
from hashlib import sha256
from json import dumps
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session
from ecc.domains.knowledge.timeline import queue_timeline_entry
from ecc.observability import (
    queue_lifecycle_event,
    record_audit_outbox_failure,
    record_idempotency_conflict,
)

router = APIRouter(prefix="/api/v1/knowledge", tags=["knowledge-relationships"])
SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]

# Phase 1 vocabulary from docs/domain/PKOS-SCHEMA.md, extended per
# phase-002/DATA-MODEL.md's "typed directed connection" requirement.
# Extendable -- this is a controlled vocabulary, not a closed one; adding a
# new value here is not a breaking change to existing relationships.
RelationshipType = Literal[
    "MEMBER_OF",
    "PARTICIPATES_IN",
    "OWNS",
    "ASSIGNED_TO",
    "MAKES",
    "MADE_TO",
    "RELATES_TO",
    "ADVANCES",
    "THREATENS",
    "BLOCKS",
    "DEPENDS_ON",
    "PRODUCES",
    "SUPPORTS",
    "SUPERSEDES",
    "ABOUT",
    "MENTIONS",
    "DERIVED_FROM",
    "SCHEDULED_FOR",
    "PROPOSES_ACTION_ON",
    "HIGHLIGHTS",
    "WORKS_ON",
]
RelationshipStatus = Literal["active", "disputed", "invalidated"]
RelationshipDirection = Literal["incoming", "outgoing"]

# Resolves both endpoints' `canonical_name`/`node_type` via a join -- a real
# gap a team-concept gap analysis found: `RelationshipResponse` previously
# carried only raw entity UUIDs, so `EntityDetail.tsx`'s relationships list
# rendered bare IDs with no way to tell who/what the other end actually is.
# This is what makes a team's `MEMBER_OF` roster legible at all -- without
# resolved names, "who's on this team" would still require a human to
# manually look up each UUID one at a time. INNER JOIN, not LEFT, is safe:
# `pkos_nodes` rows are archived/redirected, never hard-deleted, and
# `create_relationship` already requires both endpoints to exist and be
# `active` at creation time.
_RELATIONSHIP_FIELDS_WITH_NAMES = """
    e.id, e.source_node_id, e.target_node_id, e.edge_type, e.confidence,
    e.evidence_id, e.valid_from, e.valid_to, e.status,
    src.canonical_name AS from_entity_name, src.node_type AS from_entity_kind,
    tgt.canonical_name AS to_entity_name, tgt.node_type AS to_entity_kind
"""


class RelationshipCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    relationship_type: RelationshipType
    to_entity_id: UUID
    # Required, matching claims.py's identical rule: DATA-MODEL.md's invariant
    # is "a claim or relationship has at least one source reference" -- claims
    # already enforced this in the DB and API, relationships did not (a gap
    # found by an audit of the shipped code against the contract).
    evidence_id: UUID
    confidence: float = Field(default=1.0, ge=0, le=1)
    valid_from: datetime | None = None
    valid_to: datetime | None = None

    @model_validator(mode="after")
    def validate_valid_interval(self) -> RelationshipCreate:
        if (
            self.valid_from is not None
            and self.valid_to is not None
            and self.valid_to <= self.valid_from
        ):
            raise ValueError("valid_to must be after valid_from")
        return self


class RelationshipResponse(BaseModel):
    id: UUID
    from_entity_id: UUID
    to_entity_id: UUID
    relationship_type: RelationshipType
    confidence: float
    evidence_id: UUID
    valid_from: datetime | None
    valid_to: datetime | None
    status: RelationshipStatus
    # Resolved from `pkos_nodes` -- see `_RELATIONSHIP_FIELDS_WITH_NAMES`'s
    # own comment for why raw IDs alone made this response unusable for a
    # human-facing roster/relationship list.
    from_entity_name: str
    from_entity_kind: str
    to_entity_name: str
    to_entity_kind: str


class RelationshipListResponse(BaseModel):
    items: list[RelationshipResponse]


def _project(row: dict[str, Any]) -> RelationshipResponse:
    return RelationshipResponse(
        id=row["id"],
        from_entity_id=row["source_node_id"],
        to_entity_id=row["target_node_id"],
        relationship_type=row["edge_type"],
        confidence=float(row["confidence"]),
        evidence_id=row["evidence_id"],
        valid_from=row["valid_from"],
        valid_to=row["valid_to"],
        status=row["status"],
        from_entity_name=row["from_entity_name"],
        from_entity_kind=row["from_entity_kind"],
        to_entity_name=row["to_entity_name"],
        to_entity_kind=row["to_entity_kind"],
    )


def _fetch_relationship(
    session: Session, auth: AuthContext, relationship_id: UUID
) -> RelationshipResponse:
    row = (
        session.execute(
            text(
                f"""
                SELECT {_RELATIONSHIP_FIELDS_WITH_NAMES}
                FROM pkos_edges e
                JOIN pkos_nodes src
                    ON src.workspace_id = e.workspace_id AND src.id = e.source_node_id
                JOIN pkos_nodes tgt
                    ON tgt.workspace_id = e.workspace_id AND tgt.id = e.target_node_id
                WHERE e.workspace_id = :workspace_id AND e.id = :relationship_id
                """
            ),
            {"workspace_id": auth.workspace_id, "relationship_id": relationship_id},
        )
        .mappings()
        .one()
    )
    return _project(dict(row))


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
) -> RelationshipResponse | None:
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
        record_idempotency_conflict("knowledge_relationships")
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
    return RelationshipResponse.model_validate(row["response_body"])


def _store_cached(
    session: Session,
    auth: AuthContext,
    key: str,
    request_hash: str,
    response: RelationshipResponse,
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


def _entity_version(session: Session, auth: AuthContext, entity_id: UUID) -> int | None:
    row = session.execute(
        text(
            "SELECT version FROM pkos_nodes WHERE workspace_id = :workspace_id AND id = :entity_id"
        ),
        {"workspace_id": auth.workspace_id, "entity_id": entity_id},
    ).one_or_none()
    return row[0] if row is not None else None


def _entity_exists(session: Session, auth: AuthContext, entity_id: UUID) -> bool:
    return _entity_version(session, auth, entity_id) is not None


def _entity_status(session: Session, auth: AuthContext, entity_id: UUID) -> str | None:
    row = session.execute(
        text(
            "SELECT status FROM pkos_nodes WHERE workspace_id = :workspace_id AND id = :entity_id"
        ),
        {"workspace_id": auth.workspace_id, "entity_id": entity_id},
    ).one_or_none()
    return row[0] if row is not None else None


def _source_entity_version(session: Session, auth: AuthContext, relationship_id: UUID) -> int:
    """`audit_events.aggregate_version` is NOT NULL, but relationships have no
    version of their own (DATA-MODEL.md lists one for knowledge_entities, not
    for relationships -- a relationship's only mutation is the one-way
    active -> invalidated transition, which needs no optimistic-concurrency
    counter). Mirrors claims.py's identical resolution: use the relationship's
    source `knowledge_entity`'s current version as the audit proxy."""
    row = session.execute(
        text(
            """
            SELECT n.version FROM pkos_edges e
            JOIN pkos_nodes n ON n.workspace_id = e.workspace_id AND n.id = e.source_node_id
            WHERE e.workspace_id = :workspace_id AND e.id = :relationship_id
            """
        ),
        {"workspace_id": auth.workspace_id, "relationship_id": relationship_id},
    ).one()
    return int(row[0])


def _write_side_effects(
    session: Session,
    auth: AuthContext,
    request: Request,
    event_type: str,
    relationship_id: UUID,
    source_version: int,
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
                    :id, :workspace_id, :event_type, 'relationship', :aggregate_id,
                    :aggregate_version, :actor_id, :request_id, :correlation_id,
                    ARRAY['*'], 'allowed', 'user', '{}'::jsonb, :occurred_at
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": auth.workspace_id,
                "event_type": event_type,
                "aggregate_id": relationship_id,
                "aggregate_version": source_version,
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
                "payload": dumps({"relationship_id": str(relationship_id)}),
                "occurred_at": now,
            },
        )
    except SQLAlchemyError:
        record_audit_outbox_failure("knowledge_relationships")
        raise
    queue_lifecycle_event(session, "relationship", event_type, "allowed")


@router.post(
    "/entities/{entity_id}/relationships",
    response_model=RelationshipResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_relationship(
    entity_id: UUID,
    payload: RelationshipCreate,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> RelationshipResponse:
    if entity_id == payload.to_entity_id:
        raise HTTPException(status_code=422, detail="SELF_RELATIONSHIP_NOT_PERMITTED")
    request_hash = _request_hash(payload, f"create:{entity_id}")
    now = datetime.now(UTC)
    relationship_id = uuid4()
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached
        source_version = _entity_version(session, auth, entity_id)
        target_status = _entity_status(session, auth, payload.to_entity_id)
        if source_version is None or target_status is None:
            raise HTTPException(status_code=404, detail="ENTITY_NOT_FOUND")
        # DATA-MODEL.md's "typed directed connection" is meant to connect
        # canonical, live identities -- an archived entity is paused, not
        # gone, but a redirected one has already been superseded by a merge
        # target (its whole purpose is that new activity attaches to the
        # survivor instead). Allowing a fresh relationship to attach to
        # either leaves the graph pointing at a non-canonical stand-in, which
        # is exactly what `invalid_relationship` names in the contract's
        # required error codes -- a gap an audit of the shipped code found
        # was never actually checked.
        source_status = _entity_status(session, auth, entity_id)
        if source_status != "active" or target_status != "active":
            raise HTTPException(status_code=422, detail="INVALID_RELATIONSHIP")
        # See claims.py's identical check: evidence that exists but is no
        # longer `available` (deleted, missing, permission_denied) cannot
        # back a new relationship either.
        evidence_state = session.execute(
            text(
                "SELECT evidence_state FROM pkos_evidence"
                " WHERE workspace_id = :workspace_id AND id = :evidence_id"
            ),
            {"workspace_id": auth.workspace_id, "evidence_id": payload.evidence_id},
        ).scalar_one_or_none()
        if evidence_state is None:
            raise HTTPException(status_code=404, detail="EVIDENCE_NOT_FOUND")
        if evidence_state != "available":
            raise HTTPException(status_code=422, detail="EVIDENCE_UNAVAILABLE")
        session.execute(
            text(
                """
                INSERT INTO pkos_edges (
                    id, workspace_id, source_node_id, target_node_id, edge_type,
                    attributes, confidence, evidence_id, valid_from, valid_to, status
                ) VALUES (
                    :id, :workspace_id, :source, :target, :edge_type,
                    '{}'::jsonb, :confidence, :evidence_id, :valid_from, :valid_to, 'active'
                )
                """
            ),
            {
                "id": relationship_id,
                "workspace_id": auth.workspace_id,
                "source": entity_id,
                "target": payload.to_entity_id,
                "edge_type": payload.relationship_type,
                "confidence": payload.confidence,
                "evidence_id": payload.evidence_id,
                "valid_from": payload.valid_from,
                "valid_to": payload.valid_to,
            },
        )
        # Fetched separately (not via `RETURNING`) since the join that
        # resolves `from_entity_name`/`to_entity_name` reads other rows in
        # `pkos_nodes`, which a plain `INSERT ... RETURNING` cannot express.
        response = _fetch_relationship(session, auth, relationship_id)
        _write_side_effects(
            session, auth, request, "relationship.created", relationship_id, source_version, now
        )
        queue_timeline_entry(
            session,
            auth.workspace_id,
            entity_id,
            "relationship.created",
            f"{payload.relationship_type} -> {payload.to_entity_id}",
            now,
            source_id=payload.evidence_id,
        )
        _store_cached(session, auth, idempotency_key, request_hash, response, now, 201)
        return response


@router.get("/entities/{entity_id}/relationships", response_model=RelationshipListResponse)
def list_relationships(
    entity_id: UUID,
    auth: AuthDep,
    session: SessionDep,
    relationship_type: Annotated[RelationshipType | None, Query()] = None,
    direction: Annotated[RelationshipDirection | None, Query()] = None,
) -> RelationshipListResponse:
    """`relationship_type`/`direction` are optional, additive filters -- a
    team-concept gap analysis found `MEMBER_OF` was a real, working
    relationship type with no way to query it as a roster: a team's
    membership list is exactly `relationship_type=MEMBER_OF&direction=
    incoming` against this same endpoint, composed from these two filters
    rather than a bespoke `/teams/{id}/members` route this system's own
    fully-generic entity-kind design doesn't otherwise need. Omitting both
    preserves this endpoint's original both-directions, every-type
    behavior unchanged.
    """
    if direction == "incoming":
        entity_clause = "e.target_node_id = :entity_id"
    elif direction == "outgoing":
        entity_clause = "e.source_node_id = :entity_id"
    else:
        entity_clause = "(e.source_node_id = :entity_id OR e.target_node_id = :entity_id)"
    clauses = [entity_clause]
    params: dict[str, Any] = {"workspace_id": auth.workspace_id, "entity_id": entity_id}
    if relationship_type is not None:
        clauses.append("e.edge_type = :relationship_type")
        params["relationship_type"] = relationship_type

    rows = (
        session.execute(
            text(
                f"""
                SELECT {_RELATIONSHIP_FIELDS_WITH_NAMES}
                FROM pkos_edges e
                JOIN pkos_nodes src
                    ON src.workspace_id = e.workspace_id AND src.id = e.source_node_id
                JOIN pkos_nodes tgt
                    ON tgt.workspace_id = e.workspace_id AND tgt.id = e.target_node_id
                WHERE e.workspace_id = :workspace_id AND {" AND ".join(clauses)}
                ORDER BY e.id
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    return RelationshipListResponse(items=[_project(dict(row)) for row in rows])
