from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session
from ecc.domains.knowledge.timeline import queue_timeline_entry
from ecc.observability import queue_lifecycle_event
from ecc.platform import audit_outbox, authz, cursor_pagination
from ecc.platform.idempotency import load_cached, lock_idempotency, request_hash, store_idempotency

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
    next_cursor: str | None = None


def _encode_relationship_cursor(edge_id: UUID) -> str:
    # `pkos_edges` has no `created_at`/`updated_at` column (never added by
    # any migration) -- every other paginated list in this codebase keys
    # its cursor on a `(timestamp, id)` pair, but there is no timestamp
    # available here, so `id` alone (the existing `ORDER BY e.id` sort
    # key) is the cursor.
    return cursor_pagination.encode_cursor({"id": str(edge_id)})


def _decode_relationship_cursor(cursor: str) -> UUID:
    decoded = cursor_pagination.decode_cursor(cursor)
    try:
        return UUID(decoded["id"])
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="MALFORMED_CURSOR") from exc


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


def _entity_version(session: Session, auth: AuthContext, entity_id: UUID) -> int | None:
    row = session.execute(
        text(
            "SELECT version FROM pkos_nodes WHERE workspace_id = :workspace_id AND id = :entity_id"
        ),
        {"workspace_id": auth.workspace_id, "entity_id": entity_id},
    ).one_or_none()
    return row[0] if row is not None else None


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
    req_hash = request_hash(payload, f"create:{entity_id}")
    now = datetime.now(UTC)
    relationship_id = uuid4()
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
        # A relationship touches two entities -- the source (URL entity_id,
        # authorized read+write, the same two-phase shape every other
        # entity-scoped mutation in this domain uses) and the target
        # (payload.to_entity_id, read-only: creating a relationship must
        # not let a caller confirm the existence of, or point a new edge
        # at, a target entity they cannot otherwise see).
        if not authz.authorize(
            session, auth, resource_type="pkos_nodes", resource_id=entity_id, action="read"
        ):
            raise HTTPException(status_code=404, detail="ENTITY_NOT_FOUND")
        if not authz.authorize(
            session, auth, resource_type="pkos_nodes", resource_id=entity_id, action="write"
        ):
            raise HTTPException(status_code=403, detail="INSUFFICIENT_ROLE")
        if not authz.authorize(
            session,
            auth,
            resource_type="pkos_nodes",
            resource_id=payload.to_entity_id,
            action="read",
        ):
            raise HTTPException(status_code=404, detail="ENTITY_NOT_FOUND")
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
                    attributes, confidence, evidence_id, valid_from, valid_to, status,
                    owner_id, visibility
                ) VALUES (
                    :id, :workspace_id, :source, :target, :edge_type,
                    '{}'::jsonb, :confidence, :evidence_id, :valid_from, :valid_to, 'active',
                    :actor_id, 'workspace'
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
                "actor_id": auth.user_id,
            },
        )
        # Fetched separately (not via `RETURNING`) since the join that
        # resolves `from_entity_name`/`to_entity_name` reads other rows in
        # `pkos_nodes`, which a plain `INSERT ... RETURNING` cannot express.
        response = _fetch_relationship(session, auth, relationship_id)
        audit_outbox.write_audit_and_outbox(
            session,
            auth,
            request,
            event_type="relationship.created",
            aggregate_type="relationship",
            aggregate_id=relationship_id,
            aggregate_version=source_version,
            changed_fields=["*"],
            payload={"relationship_id": str(relationship_id)},
            now=now,
            domain="knowledge_relationships",
        )
        queue_lifecycle_event(session, "relationship", "relationship.created", "allowed")
        queue_timeline_entry(
            session,
            auth.workspace_id,
            entity_id,
            "relationship.created",
            f"{payload.relationship_type} -> {payload.to_entity_id}",
            now,
            source_id=payload.evidence_id,
        )
        store_idempotency(
            session,
            auth,
            idempotency_key,
            req_hash,
            response.model_dump(mode="json"),
            now,
            201,
        )
        return response


@router.get("/entities/{entity_id}/relationships", response_model=RelationshipListResponse)
def list_relationships(
    entity_id: UUID,
    auth: AuthDep,
    session: SessionDep,
    relationship_type: Annotated[RelationshipType | None, Query()] = None,
    direction: Annotated[RelationshipDirection | None, Query()] = None,
    cursor: str | None = None,
    limit: int = Query(default=50, ge=1, le=100),
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
    # A cross-workspace/unknown entity_id never 404s here -- this is a
    # list endpoint, and this module's own established convention (see
    # the cross-workspace-isolation tests) returns an empty list, not
    # 404, the same way claims.py's list_claims does.
    visible = authz.authorize(
        session, auth, resource_type="pkos_nodes", resource_id=entity_id, action="read"
    )
    session.rollback()
    if not visible:
        return RelationshipListResponse(items=[])
    if direction == "incoming":
        entity_clause = "e.target_node_id = :entity_id"
    elif direction == "outgoing":
        entity_clause = "e.source_node_id = :entity_id"
    else:
        entity_clause = "(e.source_node_id = :entity_id OR e.target_node_id = :entity_id)"
    # Found in the fourth whole-phase review: `entity_id` itself is
    # authorized above, but the *other* side of every relationship row
    # (`src`/`tgt`, whichever isn't `entity_id`) never was -- this returned
    # a linked entity's real `canonical_name`/`node_type` regardless of
    # that entity's own current visibility. Checking both sides (one of
    # which is always `entity_id`, already known visible) is simpler than
    # branching on `direction` to single out "the other one" and has the
    # identical effect.
    src_visibility_sql, src_visibility_params = authz.visible_resource_filter_sql(
        session, auth, resource_type="pkos_nodes", action="read", table_alias="src"
    )
    tgt_visibility_sql, _ = authz.visible_resource_filter_sql(
        session, auth, resource_type="pkos_nodes", action="read", table_alias="tgt"
    )
    clauses = [entity_clause, f"({src_visibility_sql})", f"({tgt_visibility_sql})"]
    params: dict[str, Any] = {
        "workspace_id": auth.workspace_id,
        "entity_id": entity_id,
        "limit": limit + 1,
        **src_visibility_params,
    }
    if relationship_type is not None:
        clauses.append("e.edge_type = :relationship_type")
        params["relationship_type"] = relationship_type
    if cursor is not None:
        params["cursor_id"] = _decode_relationship_cursor(cursor)
        clauses.append("e.id > :cursor_id")

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
        next_cursor = _encode_relationship_cursor(page[-1]["id"])
    return RelationshipListResponse(
        items=[_project(dict(row)) for row in page],
        next_cursor=next_cursor,
    )
