"""Engineering decisions and incidents (Phase 6 Task 6 -- "Decisions,
incidents and knowledge linking"), `POST|GET /api/v1/engineering/
decisions`, `POST|GET /api/v1/engineering/incidents`,
`POST .../incidents/{id}/resolve`, `POST .../decisions/{id}/decide`.

**Neither table is a synced-content projection.** Unlike `repositories`/
`engineering_work_items`/`changes`/`reviews`, no connector adapter writes
these rows -- both are workspace-authored records (an operator captures
"we had an incident" / "we decided X"), the same kind of human-editable,
optimistically-concurrent write this codebase's `connector_accounts.py`
already established (`version`/`created_by`/`updated_by`, idempotency-key
replay, audit/outbox side effects) -- mirrored here rather than reusing
Phase 2's own knowledge-capture endpoints, since decisions/incidents are
Phase 6's own domain concepts (linked to `changes`, not `pkos_nodes`).

**`POST /engineering/incidents` is a real addition beyond `API-SCHEMAS.md`'s
original route sketch** (which named only `GET /engineering/incidents`).
No incident-management provider connector exists in this phase's scope
(GitHub/GitLab/Jira are not incident-management tools) -- manual capture
is the only feasible source for this task, matching `GET /engineering/
metrics`'s own precedent (Task 5) of disclosing a real addition beyond
the route sketch rather than leaving a route unimplementable. Both
disclosed in `API-SCHEMAS.md`'s own "Task 6 status" section.

**Correlation to `changes` only, not `deployments`/work items, disclosed.**
`change_failure_rate`'s own contract population ("deployments followed
by a linked incident within 24h") still cannot be computed after this
task -- `deployments` has no task assigned to it at all (see migration
`0049_phase6_decisions_incidents.py`'s own docstring). Only `time_to_
restore` (median detected-to-resolved duration for incidents resolved in
the last 30 days, needing only `incidents` itself) becomes real.
Work-item correlation (`incident_work_items`/`decision_work_items`) is
deferred -- no concrete caller populates it yet; adding the join tables
speculatively was judged not worth the schema surface until real usage
need is established.

**Ambiguous-identity resolution against Phase 2's `resolution_candidates`/
`merge_entities` is explicitly deferred to a Task 6 follow-up, disclosed.**
`engineering_work_items.reporter_external_id`/`assignee_external_id`,
`changes.author_external_id` and `reviews.reviewer_external_id` all
remain unresolved raw provider identifiers after this task -- wiring
each into Phase 2's real identity-resolution machinery (`backend/ecc/
domains/knowledge/resolution.py`'s scoring/threshold/deterministic-match
logic) is a substantial integration in its own right (a duplicate-
candidate-avoidance strategy across repeated syncs, a `Person`-entity
find-or-create step, and a decision about which of four columns to wire
first), not a small addition to this PR's own decisions/incidents scope.
Matches this phase's own established precedent for splitting a task's
real scope across PRs when it turns out larger than one reviewable unit
(Task 5's GitLab change/review sync deferral is the identical pattern).
"""

from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session
from ecc.observability import queue_lifecycle_event
from ecc.platform import audit_outbox, authz
from ecc.platform.idempotency import load_cached, lock_idempotency, request_hash, store_idempotency

router = APIRouter(prefix="/api/v1/engineering", tags=["engineering"])

SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]

IncidentSeverity = Literal["low", "medium", "high", "critical"]
IncidentStatus = Literal["open", "resolved"]
# `superseded` (an ADR-style "a later decision replaced this one" state) is
# schema-level future-proofing only -- migration 0049's own CHECK
# constraint allows it, but no endpoint in this task can ever transition a
# decision into it. Disclosed, not a missing feature of this task: adding
# a real "supersede" transition without a concrete caller yet would be
# speculative scope, the same reasoning this task already applies to
# deferring deployment/work-item correlation.
DecisionStatus = Literal["proposed", "decided", "superseded"]


class IncidentCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=500)
    description: str | None = None
    severity: IncidentSeverity = "medium"
    detected_at: datetime
    change_ids: list[UUID] = Field(default_factory=list)


class IncidentResolveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resolved_at: datetime


class IncidentResponse(BaseModel):
    id: UUID
    title: str
    description: str | None
    severity: IncidentSeverity
    status: IncidentStatus
    detected_at: datetime
    resolved_at: datetime | None
    change_ids: list[UUID]
    version: int
    created_at: datetime
    updated_at: datetime
    # Task 5's `API-SCHEMAS.md` "resource responses expose effective
    # permissions" requirement, proven here in this reference domain first
    # (mirrors Task 3's own "one reference domain, then breadth" framing).
    # Always populated for a row the caller is looking at (it was either
    # just created/mutated by them, or already passed `visible_resource_
    # filter_sql`'s own visibility check to appear in a list at all) --
    # `None` only as a defensive fallback `_to_incident_response` never
    # actually expects to hit.
    sharing: authz.EffectivePermissions | None


class IncidentListResponse(BaseModel):
    incidents: list[IncidentResponse]


class DecisionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=500)
    description: str | None = None
    rationale: str | None = None
    change_ids: list[UUID] = Field(default_factory=list)


class DecisionDecideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decided_at: datetime
    rationale: str | None = None


class DecisionResponse(BaseModel):
    id: UUID
    title: str
    description: str | None
    rationale: str | None
    status: DecisionStatus
    decided_at: datetime | None
    change_ids: list[UUID]
    version: int
    created_at: datetime
    updated_at: datetime
    # See IncidentResponse.sharing's own docstring comment.
    sharing: authz.EffectivePermissions | None


class DecisionListResponse(BaseModel):
    decisions: list[DecisionResponse]


def _deduplicated(change_ids: list[UUID]) -> list[UUID]:
    """A duplicate `change_id` in the request payload would otherwise reach
    the per-id `INSERT INTO incident_changes`/`decision_changes` loop
    twice, violating `uq_incident_changes_pair`/`uq_decision_changes_pair`
    (migration `0049`) and raising an unhandled `IntegrityError` -- 500ing
    a request that should simply link the change once. Dedupes up front,
    preserving order, so both the FK-existence check and the insert loop
    below only ever see each id once.
    """
    return list(dict.fromkeys(change_ids))


def _validate_change_ids(session: Session, workspace_id: UUID, change_ids: list[UUID]) -> None:
    if not change_ids:
        return
    found = (
        session.execute(
            text(
                "SELECT id FROM changes WHERE workspace_id = :workspace_id "
                "AND id = ANY(:change_ids)"
            ),
            {"workspace_id": workspace_id, "change_ids": change_ids},
        )
        .scalars()
        .all()
    )
    missing = set(change_ids) - set(found)
    if missing:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "CHANGE_NOT_FOUND",
                "change_ids": sorted(str(m) for m in missing),
            },
        )


def _incident_change_ids(session: Session, incident_id: UUID) -> list[UUID]:
    return list(
        session.execute(
            text(
                "SELECT change_id FROM incident_changes WHERE incident_id = :id ORDER BY change_id"
            ),
            {"id": incident_id},
        ).scalars()
    )


def _decision_change_ids(session: Session, decision_id: UUID) -> list[UUID]:
    return list(
        session.execute(
            text(
                "SELECT change_id FROM decision_changes WHERE decision_id = :id ORDER BY change_id"
            ),
            {"id": decision_id},
        ).scalars()
    )


def _get_incident(session: Session, workspace_id: UUID, incident_id: UUID) -> dict[str, Any] | None:
    row = (
        session.execute(
            text(
                "SELECT id, title, description, severity, status, detected_at, "
                "resolved_at, version, created_at, updated_at FROM incidents "
                "WHERE workspace_id = :workspace_id AND id = :id"
            ),
            {"workspace_id": workspace_id, "id": incident_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _get_decision(session: Session, workspace_id: UUID, decision_id: UUID) -> dict[str, Any] | None:
    row = (
        session.execute(
            text(
                "SELECT id, title, description, rationale, status, decided_at, "
                "version, created_at, updated_at FROM engineering_decisions "
                "WHERE workspace_id = :workspace_id AND id = :id"
            ),
            {"workspace_id": workspace_id, "id": decision_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _to_incident_response(
    session: Session, auth: AuthContext, row: dict[str, Any]
) -> IncidentResponse:
    sharing = authz.effective_permissions(
        session, auth, resource_type="incidents", resource_id=row["id"]
    )
    return IncidentResponse(
        **row, change_ids=_incident_change_ids(session, row["id"]), sharing=sharing
    )


def _to_decision_response(
    session: Session, auth: AuthContext, row: dict[str, Any]
) -> DecisionResponse:
    sharing = authz.effective_permissions(
        session, auth, resource_type="engineering_decisions", resource_id=row["id"]
    )
    return DecisionResponse(
        **row, change_ids=_decision_change_ids(session, row["id"]), sharing=sharing
    )


@router.post("/incidents", response_model=IncidentResponse, status_code=status.HTTP_201_CREATED)
def create_incident_endpoint(
    payload: IncidentCreateRequest,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> IncidentResponse:
    authz.require_role_action(session, auth, "write")
    req_hash = request_hash(payload, "create_incident")
    now = datetime.now(UTC)
    with session.begin():
        lock_idempotency(session, auth, idempotency_key)
        cached = load_cached(
            session, auth, idempotency_key, req_hash, domain="engineering_decisions_incidents"
        )
        if cached is not None:
            return IncidentResponse.model_validate(cached)

        change_ids = _deduplicated(payload.change_ids)
        _validate_change_ids(session, auth.workspace_id, change_ids)

        incident_id = uuid4()
        session.execute(
            text(
                """
                INSERT INTO incidents (
                    id, workspace_id, title, description, severity, status,
                    detected_at, version, created_by, updated_by, created_at, updated_at,
                    owner_id, visibility
                ) VALUES (
                    :id, :workspace_id, :title, :description, :severity, 'open',
                    :detected_at, 1, :actor_id, :actor_id, :now, :now,
                    :actor_id, 'workspace'
                )
                """
            ),
            {
                "id": incident_id,
                "workspace_id": auth.workspace_id,
                "title": payload.title,
                "description": payload.description,
                "severity": payload.severity,
                "detected_at": payload.detected_at,
                "actor_id": auth.user_id,
                "now": now,
            },
        )
        for change_id in change_ids:
            session.execute(
                text(
                    "INSERT INTO incident_changes "
                    "(id, workspace_id, incident_id, change_id, created_at, owner_id, visibility) "
                    "VALUES (:id, :workspace_id, :incident_id, :change_id, :now, "
                    ":actor_id, 'workspace')"
                ),
                {
                    "id": uuid4(),
                    "workspace_id": auth.workspace_id,
                    "incident_id": incident_id,
                    "change_id": change_id,
                    "now": now,
                    "actor_id": auth.user_id,
                },
            )

        row = _get_incident(session, auth.workspace_id, incident_id)
        assert row is not None  # just inserted, same transaction
        response = _to_incident_response(session, auth, row)
        audit_outbox.write_audit_and_outbox(
            session,
            auth,
            request,
            event_type="incident.created",
            aggregate_type="incident",
            aggregate_id=incident_id,
            aggregate_version=1,
            changed_fields=["*"],
            payload={"aggregate_id": str(incident_id), "version": 1},
            now=now,
            domain="engineering_decisions_incidents",
        )
        queue_lifecycle_event(
            session, "engineering_decisions_incidents", "incident.created", "allowed"
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


@router.post("/incidents/{incident_id}/resolve", response_model=IncidentResponse)
def resolve_incident_endpoint(
    incident_id: UUID,
    payload: IncidentResolveRequest,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> IncidentResponse:
    req_hash = request_hash(payload, f"resolve_incident:{incident_id}")
    now = datetime.now(UTC)
    with session.begin():
        lock_idempotency(session, auth, idempotency_key)
        cached = load_cached(
            session, auth, idempotency_key, req_hash, domain="engineering_decisions_incidents"
        )
        if cached is not None:
            return IncidentResponse.model_validate(cached)

        # Two-phase read-then-write authz check (mirrors connector_
        # accounts.py's identical pattern): a plain membership-unaware
        # existence lookup followed by a single write-only authorize()
        # call let a suspended/removed member distinguish 404 (resource
        # doesn't exist) from 403 (exists, denied) for any incident_id in
        # their former workspace -- exactly the existence leak authz.
        # authorize()'s own docstring says must never be observable. The
        # read check's False result (not an active member, resource
        # missing, or visibility=private) is uniformly reported as 404;
        # only once that has already confirmed the caller can see this
        # incident does a write-check failure reveal anything new via 403.
        if not authz.authorize(
            session, auth, resource_type="incidents", resource_id=incident_id, action="read"
        ):
            raise HTTPException(status_code=404, detail="INCIDENT_NOT_FOUND")
        if not authz.authorize(
            session, auth, resource_type="incidents", resource_id=incident_id, action="write"
        ):
            raise HTTPException(status_code=403, detail="INSUFFICIENT_ROLE")
        existing = _get_incident(session, auth.workspace_id, incident_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="INCIDENT_NOT_FOUND")
        if existing["status"] == "resolved":
            raise HTTPException(status_code=409, detail="INCIDENT_ALREADY_RESOLVED")
        if payload.resolved_at < existing["detected_at"]:
            raise HTTPException(status_code=422, detail="RESOLVED_AT_BEFORE_DETECTED_AT")

        # `AND status = 'open'` makes this UPDATE the actual serialization
        # point, not the `existing["status"]` check above (a stale read two
        # concurrent resolve calls -- different Idempotency-Keys, so the
        # advisory lock above doesn't serialize them -- could both pass).
        # `version = version + 1 ... RETURNING version` computes the new
        # version from the database's real current value, never a
        # possibly-stale Python-side `existing["version"] + 1`, mirroring
        # `connector_accounts.py`'s `_finalize_account_version` pattern. A
        # `None` result means another transaction already resolved this
        # incident between our read above and this UPDATE -- report the
        # identical 409 the plain read-based check above would have given
        # had it run a moment later, rather than silently overwriting.
        updated = (
            session.execute(
                text(
                    "UPDATE incidents SET status = 'resolved', resolved_at = :resolved_at, "
                    "version = version + 1, updated_by = :actor_id, updated_at = :now "
                    "WHERE id = :id AND workspace_id = :workspace_id AND status = 'open' "
                    "RETURNING version"
                ),
                {
                    "resolved_at": payload.resolved_at,
                    "actor_id": auth.user_id,
                    "now": now,
                    "id": incident_id,
                    "workspace_id": auth.workspace_id,
                },
            )
            .mappings()
            .one_or_none()
        )
        if updated is None:
            raise HTTPException(status_code=409, detail="INCIDENT_ALREADY_RESOLVED")
        new_version = int(updated["version"])
        row = _get_incident(session, auth.workspace_id, incident_id)
        assert row is not None
        response = _to_incident_response(session, auth, row)
        audit_outbox.write_audit_and_outbox(
            session,
            auth,
            request,
            event_type="incident.resolved",
            aggregate_type="incident",
            aggregate_id=incident_id,
            aggregate_version=new_version,
            changed_fields=["*"],
            payload={"aggregate_id": str(incident_id), "version": new_version},
            now=now,
            domain="engineering_decisions_incidents",
        )
        queue_lifecycle_event(
            session, "engineering_decisions_incidents", "incident.resolved", "allowed"
        )
        store_idempotency(
            session, auth, idempotency_key, req_hash, response.model_dump(mode="json"), now
        )
        return response


@router.get("/incidents", response_model=IncidentListResponse)
def list_incidents_endpoint(
    auth: AuthDep,
    session: SessionDep,
    status_filter: Annotated[IncidentStatus | None, Query(alias="status")] = None,
) -> IncidentListResponse:
    visibility_sql, visibility_params = authz.visible_resource_filter_sql(
        session, auth, resource_type="incidents", action="read", table_alias="incidents"
    )
    clause = "AND status = :status_filter" if status_filter else ""
    params: dict[str, Any] = {"workspace_id": auth.workspace_id, **visibility_params}
    if status_filter:
        params["status_filter"] = status_filter
    rows = (
        session.execute(
            text(
                "SELECT id, title, description, severity, status, detected_at, "
                "resolved_at, version, created_at, updated_at FROM incidents "
                f"WHERE workspace_id = :workspace_id AND ({visibility_sql}) "  # noqa: S608
                f"{clause} ORDER BY detected_at DESC"
            ),
            params,
        )
        .mappings()
        .all()
    )
    incidents = [_to_incident_response(session, auth, dict(row)) for row in rows]
    session.rollback()
    return IncidentListResponse(incidents=incidents)


@router.post("/decisions", response_model=DecisionResponse, status_code=status.HTTP_201_CREATED)
def create_decision_endpoint(
    payload: DecisionCreateRequest,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> DecisionResponse:
    authz.require_role_action(session, auth, "write")
    req_hash = request_hash(payload, "create_decision")
    now = datetime.now(UTC)
    with session.begin():
        lock_idempotency(session, auth, idempotency_key)
        cached = load_cached(
            session, auth, idempotency_key, req_hash, domain="engineering_decisions_incidents"
        )
        if cached is not None:
            return DecisionResponse.model_validate(cached)

        change_ids = _deduplicated(payload.change_ids)
        _validate_change_ids(session, auth.workspace_id, change_ids)

        decision_id = uuid4()
        session.execute(
            text(
                """
                INSERT INTO engineering_decisions (
                    id, workspace_id, title, description, rationale, status,
                    version, created_by, updated_by, created_at, updated_at,
                    owner_id, visibility
                ) VALUES (
                    :id, :workspace_id, :title, :description, :rationale, 'proposed',
                    1, :actor_id, :actor_id, :now, :now,
                    :actor_id, 'workspace'
                )
                """
            ),
            {
                "id": decision_id,
                "workspace_id": auth.workspace_id,
                "title": payload.title,
                "description": payload.description,
                "rationale": payload.rationale,
                "actor_id": auth.user_id,
                "now": now,
            },
        )
        for change_id in change_ids:
            session.execute(
                text(
                    "INSERT INTO decision_changes "
                    "(id, workspace_id, decision_id, change_id, created_at, owner_id, visibility) "
                    "VALUES (:id, :workspace_id, :decision_id, :change_id, :now, "
                    ":actor_id, 'workspace')"
                ),
                {
                    "id": uuid4(),
                    "workspace_id": auth.workspace_id,
                    "decision_id": decision_id,
                    "change_id": change_id,
                    "now": now,
                    "actor_id": auth.user_id,
                },
            )

        row = _get_decision(session, auth.workspace_id, decision_id)
        assert row is not None
        response = _to_decision_response(session, auth, row)
        audit_outbox.write_audit_and_outbox(
            session,
            auth,
            request,
            event_type="engineering_decision.created",
            aggregate_type="engineering_decision",
            aggregate_id=decision_id,
            aggregate_version=1,
            changed_fields=["*"],
            payload={"aggregate_id": str(decision_id), "version": 1},
            now=now,
            domain="engineering_decisions_incidents",
        )
        queue_lifecycle_event(
            session, "engineering_decisions_incidents", "engineering_decision.created", "allowed"
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


@router.post("/decisions/{decision_id}/decide", response_model=DecisionResponse)
def decide_decision_endpoint(
    decision_id: UUID,
    payload: DecisionDecideRequest,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> DecisionResponse:
    req_hash = request_hash(payload, f"decide_decision:{decision_id}")
    now = datetime.now(UTC)
    with session.begin():
        lock_idempotency(session, auth, idempotency_key)
        cached = load_cached(
            session, auth, idempotency_key, req_hash, domain="engineering_decisions_incidents"
        )
        if cached is not None:
            return DecisionResponse.model_validate(cached)

        # Two-phase read-then-write authz check -- see resolve_incident_
        # endpoint's identical comment for why plain existence-lookup then
        # write-only authorize() leaks existence to a suspended member.
        if not authz.authorize(
            session,
            auth,
            resource_type="engineering_decisions",
            resource_id=decision_id,
            action="read",
        ):
            raise HTTPException(status_code=404, detail="DECISION_NOT_FOUND")
        if not authz.authorize(
            session,
            auth,
            resource_type="engineering_decisions",
            resource_id=decision_id,
            action="write",
        ):
            raise HTTPException(status_code=403, detail="INSUFFICIENT_ROLE")
        existing = _get_decision(session, auth.workspace_id, decision_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="DECISION_NOT_FOUND")
        if existing["status"] != "proposed":
            raise HTTPException(status_code=409, detail="DECISION_NOT_PROPOSED")
        # Mirrors resolve_incident_endpoint's identical `resolved_at <
        # detected_at` guard -- a whole-phase review found this endpoint had
        # no equivalent check at all, so a decision could be "decided"
        # before it was even proposed.
        if payload.decided_at < existing["created_at"]:
            raise HTTPException(status_code=422, detail="DECIDED_AT_BEFORE_CREATED_AT")

        rationale = payload.rationale if payload.rationale is not None else existing["rationale"]
        # `AND status = 'proposed'` makes this UPDATE the actual
        # serialization point -- see resolve_incident_endpoint's identical
        # comment for why the plain read-based check above cannot prevent
        # a race between two concurrent decide calls using different
        # Idempotency-Keys. `version = version + 1 ... RETURNING version`
        # mirrors connector_accounts.py's `_finalize_account_version`.
        updated = (
            session.execute(
                text(
                    "UPDATE engineering_decisions SET status = 'decided', "
                    "decided_at = :decided_at, rationale = :rationale, version = version + 1, "
                    "updated_by = :actor_id, updated_at = :now "
                    "WHERE id = :id AND workspace_id = :workspace_id AND status = 'proposed' "
                    "RETURNING version"
                ),
                {
                    "decided_at": payload.decided_at,
                    "rationale": rationale,
                    "actor_id": auth.user_id,
                    "now": now,
                    "id": decision_id,
                    "workspace_id": auth.workspace_id,
                },
            )
            .mappings()
            .one_or_none()
        )
        if updated is None:
            raise HTTPException(status_code=409, detail="DECISION_NOT_PROPOSED")
        new_version = int(updated["version"])
        row = _get_decision(session, auth.workspace_id, decision_id)
        assert row is not None
        response = _to_decision_response(session, auth, row)
        audit_outbox.write_audit_and_outbox(
            session,
            auth,
            request,
            event_type="engineering_decision.decided",
            aggregate_type="engineering_decision",
            aggregate_id=decision_id,
            aggregate_version=new_version,
            changed_fields=["*"],
            payload={"aggregate_id": str(decision_id), "version": new_version},
            now=now,
            domain="engineering_decisions_incidents",
        )
        queue_lifecycle_event(
            session, "engineering_decisions_incidents", "engineering_decision.decided", "allowed"
        )
        store_idempotency(
            session, auth, idempotency_key, req_hash, response.model_dump(mode="json"), now
        )
        return response


@router.get("/decisions", response_model=DecisionListResponse)
def list_decisions_endpoint(
    auth: AuthDep,
    session: SessionDep,
    status_filter: Annotated[DecisionStatus | None, Query(alias="status")] = None,
) -> DecisionListResponse:
    visibility_sql, visibility_params = authz.visible_resource_filter_sql(
        session,
        auth,
        resource_type="engineering_decisions",
        action="read",
        table_alias="engineering_decisions",
    )
    clause = "AND status = :status_filter" if status_filter else ""
    params: dict[str, Any] = {"workspace_id": auth.workspace_id, **visibility_params}
    if status_filter:
        params["status_filter"] = status_filter
    rows = (
        session.execute(
            text(
                "SELECT id, title, description, rationale, status, decided_at, "
                "version, created_at, updated_at FROM engineering_decisions "
                f"WHERE workspace_id = :workspace_id AND ({visibility_sql}) "  # noqa: S608
                f"{clause} ORDER BY created_at DESC"
            ),
            params,
        )
        .mappings()
        .all()
    )
    decisions = [_to_decision_response(session, auth, dict(row)) for row in rows]
    session.rollback()
    return DecisionListResponse(decisions=decisions)
