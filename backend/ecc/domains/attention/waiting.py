from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session
from ecc.observability import queue_lifecycle_event
from ecc.platform import audit_outbox, authz, cursor_pagination, idempotency

router = APIRouter(prefix="/api/v1/waiting", tags=["waiting"])
SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]

SubjectType = Literal["task", "commitment", "knowledge_entity"]
Direction = Literal["waiting_on_me", "waiting_on_them", "blocked_by", "delegated"]
Status = Literal["open", "fulfilled", "cancelled", "superseded"]

_FIELDS = """
    id, subject_type, subject_id, counterparty_entity_id, direction, status, note,
    since_at, expected_at, superseded_by, created_at, updated_at, version
"""


class WaitingLink(BaseModel):
    id: UUID
    subject_type: SubjectType
    subject_id: UUID
    counterparty_entity_id: UUID
    direction: Direction
    status: Status
    note: str | None
    since_at: datetime
    expected_at: datetime | None
    superseded_by: UUID | None
    created_at: datetime
    updated_at: datetime
    version: int


class WaitingLinkList(BaseModel):
    items: list[WaitingLink]
    next_cursor: str | None = None


class WaitingLinkCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject_type: SubjectType
    subject_id: UUID
    counterparty_entity_id: UUID
    direction: Direction
    note: str | None = Field(default=None, max_length=2000)
    since_at: datetime | None = None
    expected_at: datetime | None = None

    @field_validator("since_at", "expected_at")
    @classmethod
    def _require_tz(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("timestamps must include a timezone offset")
        return value


class WaitingLinkPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int
    direction: Direction | None = None
    note: str | None = Field(default=None, max_length=2000)
    expected_at: datetime | None = None

    @field_validator("expected_at")
    @classmethod
    def _require_tz(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("expected_at must include a timezone offset")
        return value


class WaitingLinkTerminal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int


# Subject-type -> the authz resource_type it maps to, for the read-only
# existence-and-visibility check create_waiting_link runs before creating a
# link -- a waiting_link never mutates its subject/counterparty, so both
# get a read-only authorize (matching resolution.py's create_candidate,
# not relationships.py's create_relationship, which does mutate its source).
_SUBJECT_RESOURCE_TYPES: dict[str, str] = {
    "task": "tasks",
    "commitment": "commitments",
    "knowledge_entity": "pkos_nodes",
}

# Per-parent-table locking read. `live` is the existence predicate (a task/
# commitment must not be archived, a pkos node must be active); the row is
# locked even when not live so every parent read below is a locked one.
_PARENT_LOCK_QUERIES: dict[str, Any] = {
    "tasks": text(
        "SELECT archived_at IS NULL AS live, NULL AS node_type FROM tasks "
        "WHERE workspace_id = :workspace_id AND id = :id FOR SHARE"
    ),
    "commitments": text(
        "SELECT archived_at IS NULL AS live, NULL AS node_type FROM commitments "
        "WHERE workspace_id = :workspace_id AND id = :id FOR SHARE"
    ),
    "pkos_nodes": text(
        "SELECT status = 'active' AS live, node_type FROM pkos_nodes "
        "WHERE workspace_id = :workspace_id AND id = :id FOR SHARE"
    ),
}


def _lock_link_parents(
    session: Session,
    auth: AuthContext,
    subject_type: SubjectType,
    subject_id: UUID,
    counterparty_entity_id: UUID,
) -> tuple[bool, str | None]:
    """Lock a new link's subject and counterparty rows `FOR SHARE`, held to
    commit, before either is authorized.

    `FOR SHARE` conflicts with an ownership transfer's `FOR UPDATE`
    (`authz_grants`), so a transfer that commits while this waits is seen by
    the authorize() calls that follow (READ COMMITTED: each later statement
    reads the committed row), and one that starts after this lock waits
    until the link is committed. Rows are locked in (table, id) order so two
    requests naming the same pair in opposite roles, or any other
    multi-row locker using the same order, cannot deadlock.

    Returns whether the subject is live and the counterparty's node_type
    (None when it is missing or not active).
    """
    subject_key = (_SUBJECT_RESOURCE_TYPES[subject_type], subject_id)
    counterparty_key = ("pkos_nodes", counterparty_entity_id)
    locked: dict[tuple[str, UUID], Any] = {}
    for table, row_id in sorted({subject_key, counterparty_key}):
        locked[(table, row_id)] = (
            session.execute(
                _PARENT_LOCK_QUERIES[table],
                {"workspace_id": auth.workspace_id, "id": row_id},
            )
            .mappings()
            .one_or_none()
        )
    subject = locked[subject_key]
    counterparty = locked[counterparty_key]
    subject_live = subject is not None and bool(subject["live"])
    node_type = (
        str(counterparty["node_type"])
        if counterparty is not None and counterparty["live"]
        else None
    )
    return subject_live, node_type


def _would_create_cycle(
    session: Session,
    auth: AuthContext,
    subject_type: SubjectType,
    subject_id: UUID,
    counterparty_entity_id: UUID,
    direction: Direction,
) -> bool:
    """Reject a ``blocked_by`` link that would close a cycle back to its own
    subject.

    A cycle is only reachable when the subject is itself a knowledge entity
    (the only subject type that can also appear as a counterparty -- tasks
    and commitments live in a different id space and can never be a
    counterparty), so this only walks the graph in that case. Bounded: each
    step follows one open ``blocked_by`` edge, and the workspace's total
    edge count is small at Phase 3's target scale, matching Phase 2's
    resolution-neighborhood query pattern rather than a new graph library.
    """
    if direction != "blocked_by" or subject_type != "knowledge_entity":
        return False
    # Serialize concurrent blocked_by graph mutations for this workspace so
    # this read-then-decide check-then-write can't race with another
    # transaction inserting a conflicting link in between (TOCTOU, finding
    # #4): held for the rest of the caller's transaction (pg_advisory_xact_
    # lock, same pattern as idempotency.lock_idempotency but a distinct hash salt so
    # the two lock keyspaces never collide), so a second concurrent create/
    # direction-change targeting the same workspace's blocked_by graph
    # blocks here until this one commits, then re-reads the now-committed
    # graph before deciding.
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 1))"),
        {"lock_key": f"{auth.workspace_id}:waiting_cycle"},
    )
    # Single-round-trip equivalent of the old per-hop BFS loop: a recursive
    # CTE walks the same subject_id -> counterparty_entity_id edges from
    # counterparty_entity_id, capped at depth 63 (the old loop checked
    # frontiers at depth 0..63 across its 64 iterations before giving up --
    # see the range(64) history in git blame -- so this preserves that exact
    # termination bound rather than searching indefinitely). The `path`
    # array blocks re-descending into an already-visited node on the same
    # path, which is what the old loop's `frontier -= visited` did to avoid
    # cycling forever; since this function is precisely what keeps the
    # blocked_by graph acyclic, that only ever matters defensively.
    row = session.execute(
        text(
            """
            WITH RECURSIVE reachable(node_id, depth, path) AS (
                SELECT (:counterparty_entity_id)::uuid, 0, ARRAY[(:counterparty_entity_id)::uuid]
                UNION ALL
                SELECT wl.counterparty_entity_id, r.depth + 1, r.path || wl.counterparty_entity_id
                FROM waiting_links wl
                JOIN reachable r ON wl.subject_id = r.node_id
                WHERE wl.workspace_id = :workspace_id
                  AND wl.direction = 'blocked_by'
                  AND wl.status = 'open'
                  AND wl.subject_type = 'knowledge_entity'
                  AND r.depth < 63
                  AND NOT (wl.counterparty_entity_id = ANY(r.path))
            )
            SELECT 1 FROM reachable WHERE node_id = :subject_id LIMIT 1
            """
        ),
        {
            "workspace_id": auth.workspace_id,
            "counterparty_entity_id": counterparty_entity_id,
            "subject_id": subject_id,
        },
    ).one_or_none()
    return row is not None


@router.post("", response_model=WaitingLink, status_code=status.HTTP_201_CREATED)
def create_waiting_link(
    payload: WaitingLinkCreate,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> WaitingLink:
    authz.require_role_action(session, auth, "write")
    request_hash = idempotency.request_hash(payload, "create")
    now = datetime.now(UTC)
    link_id = uuid4()
    with session.begin():
        idempotency.lock_idempotency(session, auth, idempotency_key)
        cached = idempotency.load_cached(
            session,
            auth,
            idempotency_key,
            request_hash,
            domain="waiting",
            response_model=WaitingLink,
        )
        if cached is not None:
            return cached
        subject_live, node_type = _lock_link_parents(
            session, auth, payload.subject_type, payload.subject_id, payload.counterparty_entity_id
        )
        if not subject_live:
            raise HTTPException(status_code=404, detail="WAITING_SUBJECT_NOT_FOUND")
        if not authz.authorize(
            session,
            auth,
            resource_type=_SUBJECT_RESOURCE_TYPES[payload.subject_type],
            resource_id=payload.subject_id,
            action="read",
        ):
            raise HTTPException(status_code=404, detail="WAITING_SUBJECT_NOT_FOUND")
        if node_type is None:
            raise HTTPException(status_code=404, detail="WAITING_COUNTERPARTY_NOT_FOUND")
        if not authz.authorize(
            session,
            auth,
            resource_type="pkos_nodes",
            resource_id=payload.counterparty_entity_id,
            action="read",
        ):
            raise HTTPException(status_code=404, detail="WAITING_COUNTERPARTY_NOT_FOUND")
        if node_type not in ("person", "organization"):
            raise HTTPException(status_code=422, detail="INVALID_WAITING_DIRECTION")
        if _would_create_cycle(
            session,
            auth,
            payload.subject_type,
            payload.subject_id,
            payload.counterparty_entity_id,
            payload.direction,
        ):
            raise HTTPException(status_code=422, detail="INVALID_WAITING_DIRECTION")
        since_at = payload.since_at or now
        row = (
            session.execute(
                text(
                    f"""
                    INSERT INTO waiting_links (
                        id, workspace_id, subject_type, subject_id, counterparty_entity_id,
                        direction, status, note, since_at, expected_at,
                        created_by, updated_by, created_at, updated_at, version,
                        owner_id, visibility
                    ) VALUES (
                        :id, :workspace_id, :subject_type, :subject_id, :counterparty_entity_id,
                        :direction, 'open', :note, :since_at, :expected_at,
                        :actor_id, :actor_id, :now, :now, 1,
                        :actor_id, 'workspace'
                    )
                    RETURNING {_FIELDS}
                    """
                ),
                {
                    "id": link_id,
                    "workspace_id": auth.workspace_id,
                    "subject_type": payload.subject_type,
                    "subject_id": payload.subject_id,
                    "counterparty_entity_id": payload.counterparty_entity_id,
                    "direction": payload.direction,
                    "note": payload.note,
                    "since_at": since_at,
                    "expected_at": payload.expected_at,
                    "actor_id": auth.user_id,
                    "now": now,
                },
            )
            .mappings()
            .one()
        )
        response = WaitingLink.model_validate(dict(row))
        audit_outbox.write_audit_and_outbox(
            session,
            auth,
            request,
            event_type="waiting_link.opened",
            aggregate_type="waiting_link",
            aggregate_id=link_id,
            aggregate_version=1,
            changed_fields=["*"],
            payload={"waiting_link_id": str(link_id), "version": 1},
            now=now,
            domain="waiting",
        )
        queue_lifecycle_event(session, "waiting_link", "waiting_link.opened", "allowed")
        idempotency.store_idempotency(
            session,
            auth,
            idempotency_key,
            request_hash,
            response.model_dump(mode="json"),
            now,
            response_status=201,
        )
        return response


def _encode_cursor(created_at: datetime, link_id: UUID) -> str:
    return cursor_pagination.encode_cursor(
        {"created_at": created_at.isoformat(), "id": str(link_id)}
    )


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    decoded = cursor_pagination.decode_cursor(cursor, detail="CURSOR_INVALID")
    try:
        return datetime.fromisoformat(decoded["created_at"]), UUID(decoded["id"])
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="CURSOR_INVALID") from exc


@router.get("", response_model=WaitingLinkList)
def list_waiting_links(
    auth: AuthDep,
    session: SessionDep,
    status_filter: Annotated[Status | None, Query(alias="status")] = None,
    direction_filter: Annotated[Direction | None, Query(alias="direction")] = None,
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
) -> WaitingLinkList:
    extra_clauses = []
    extra_params: dict[str, Any] = {"limit": limit + 1}
    if status_filter:
        extra_clauses.append("status = :status")
        extra_params["status"] = status_filter
    if direction_filter:
        extra_clauses.append("direction = :direction")
        extra_params["direction"] = direction_filter
    if cursor:
        cursor_created_at, cursor_id = _decode_cursor(cursor)
        extra_clauses.append("(created_at, id) < (:cursor_created_at, :cursor_id)")
        extra_params["cursor_created_at"] = cursor_created_at
        extra_params["cursor_id"] = cursor_id

    rows = authz.list_visible_resources(
        session,
        auth,
        resource_type="waiting_links",
        columns=_FIELDS,
        order_by="created_at DESC, id DESC",
        extra_clauses=extra_clauses,
        extra_params=extra_params,
        limit_clause="LIMIT :limit",
    )
    session.rollback()
    has_more = len(rows) > limit
    page = rows[:limit]
    items = [WaitingLink.model_validate(dict(row)) for row in page]
    next_cursor = None
    if has_more and page:
        last = page[-1]
        next_cursor = _encode_cursor(last["created_at"], last["id"])
    return WaitingLinkList(items=items, next_cursor=next_cursor)


def _get_row(session: Session, auth: AuthContext, link_id: UUID) -> dict[str, Any] | None:
    row = (
        session.execute(
            text(
                f"SELECT {_FIELDS} FROM waiting_links "
                "WHERE workspace_id = :workspace_id AND id = :link_id"
            ),
            {"workspace_id": auth.workspace_id, "link_id": link_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


@router.get("/{link_id}", response_model=WaitingLink)
def get_waiting_link(link_id: UUID, auth: AuthDep, session: SessionDep) -> WaitingLink:
    visible = authz.authorize(
        session, auth, resource_type="waiting_links", resource_id=link_id, action="read"
    )
    session.rollback()
    if not visible:
        raise HTTPException(status_code=404, detail="WAITING_LINK_NOT_FOUND")
    row = _get_row(session, auth, link_id)
    if row is None:
        raise HTTPException(status_code=404, detail="WAITING_LINK_NOT_FOUND")
    return WaitingLink.model_validate(row)


@router.patch("/{link_id}", response_model=WaitingLink)
def patch_waiting_link(
    link_id: UUID,
    payload: WaitingLinkPatch,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> WaitingLink:
    """A ``direction`` change supersedes the current row with a brand new one
    (ATTENTION-MODEL.md: direction changes create history, they do not
    overwrite the original obligation) -- mirroring Phase 2's
    knowledge_claims supersede pattern. Any other field (``note``,
    ``expected_at``) alone is a normal versioned in-place update.
    """
    request_hash = idempotency.request_hash(payload, f"patch:{link_id}")
    now = datetime.now(UTC)
    with session.begin():
        idempotency.lock_idempotency(session, auth, idempotency_key)
        cached = idempotency.load_cached(
            session,
            auth,
            idempotency_key,
            request_hash,
            domain="waiting",
            response_model=WaitingLink,
        )
        if cached is not None:
            return cached
        if not authz.authorize(
            session, auth, resource_type="waiting_links", resource_id=link_id, action="read"
        ):
            raise HTTPException(status_code=404, detail="WAITING_LINK_NOT_FOUND")
        if not authz.authorize(
            session, auth, resource_type="waiting_links", resource_id=link_id, action="write"
        ):
            raise HTTPException(status_code=403, detail="INSUFFICIENT_ROLE")
        current = (
            session.execute(
                text(
                    f"SELECT {_FIELDS} FROM waiting_links "
                    "WHERE workspace_id = :workspace_id AND id = :link_id FOR UPDATE"
                ),
                {"workspace_id": auth.workspace_id, "link_id": link_id},
            )
            .mappings()
            .one_or_none()
        )
        if current is None:
            raise HTTPException(status_code=404, detail="WAITING_LINK_NOT_FOUND")
        if current["version"] != payload.expected_version:
            raise HTTPException(
                status_code=409,
                detail={"code": "VERSION_CONFLICT", "current_version": current["version"]},
            )
        if current["status"] != "open":
            raise HTTPException(status_code=409, detail="WAITING_LINK_NOT_OPEN")

        if payload.direction is not None and payload.direction != current["direction"]:
            if _would_create_cycle(
                session,
                auth,
                current["subject_type"],
                current["subject_id"],
                current["counterparty_entity_id"],
                payload.direction,
            ):
                raise HTTPException(status_code=422, detail="INVALID_WAITING_DIRECTION")
            new_id = uuid4()
            new_row = (
                session.execute(
                    text(
                        f"""
                        INSERT INTO waiting_links (
                            id, workspace_id, subject_type, subject_id,
                            counterparty_entity_id, direction, status, note,
                            since_at, expected_at, created_by, updated_by,
                            created_at, updated_at, version, owner_id, visibility
                        ) VALUES (
                            :id, :workspace_id, :subject_type, :subject_id,
                            :counterparty_entity_id, :direction, 'open',
                            :note, :since_at, :expected_at, :actor_id, :actor_id,
                            :now, :now, 1, :actor_id, 'workspace'
                        )
                        RETURNING {_FIELDS}
                        """
                    ),
                    {
                        "id": new_id,
                        "workspace_id": auth.workspace_id,
                        "subject_type": current["subject_type"],
                        "subject_id": current["subject_id"],
                        "counterparty_entity_id": current["counterparty_entity_id"],
                        "direction": payload.direction,
                        "note": (payload.note if payload.note is not None else current["note"]),
                        # Carry over the original since_at: a direction flip
                        # (e.g. waiting_on_me -> waiting_on_them) supersedes
                        # the row for history purposes, but the underlying
                        # wait has existed continuously since since_at, not
                        # since this edit -- resetting it to `now` would
                        # understate how long the wait has actually been
                        # open (finding #3).
                        "since_at": current["since_at"],
                        "expected_at": (
                            payload.expected_at
                            if payload.expected_at is not None
                            else current["expected_at"]
                        ),
                        "actor_id": auth.user_id,
                        "now": now,
                    },
                )
                .mappings()
                .one()
            )
            session.execute(
                text(
                    """
                    UPDATE waiting_links
                    SET status = 'superseded', superseded_by = :new_id,
                        updated_at = :now, updated_by = :actor_id, version = version + 1
                    WHERE workspace_id = :workspace_id AND id = :link_id
                    """
                ),
                {
                    "new_id": new_id,
                    "now": now,
                    "actor_id": auth.user_id,
                    "workspace_id": auth.workspace_id,
                    "link_id": link_id,
                },
            )
            response = WaitingLink.model_validate(dict(new_row))
            audit_outbox.write_audit_and_outbox(
                session,
                auth,
                request,
                event_type="waiting_link.opened",
                aggregate_type="waiting_link",
                aggregate_id=new_id,
                aggregate_version=1,
                changed_fields=["*"],
                payload={"waiting_link_id": str(new_id), "version": 1},
                now=now,
                domain="waiting",
            )
            queue_lifecycle_event(session, "waiting_link", "waiting_link.opened", "allowed")
        else:
            updated = (
                session.execute(
                    text(
                        f"""
                        UPDATE waiting_links
                        SET note = :note, expected_at = :expected_at,
                            updated_at = :now, updated_by = :actor_id, version = version + 1
                        WHERE workspace_id = :workspace_id AND id = :link_id
                        RETURNING {_FIELDS}
                        """
                    ),
                    {
                        "note": (payload.note if payload.note is not None else current["note"]),
                        "expected_at": (
                            payload.expected_at
                            if payload.expected_at is not None
                            else current["expected_at"]
                        ),
                        "now": now,
                        "actor_id": auth.user_id,
                        "workspace_id": auth.workspace_id,
                        "link_id": link_id,
                    },
                )
                .mappings()
                .one()
            )
            response = WaitingLink.model_validate(dict(updated))
        idempotency.store_idempotency(
            session,
            auth,
            idempotency_key,
            request_hash,
            response.model_dump(mode="json"),
            now,
            response_status=201,
        )
        return response


def _terminate(
    link_id: UUID,
    new_status: Literal["fulfilled", "cancelled"],
    payload: WaitingLinkTerminal,
    request: Request,
    auth: AuthContext,
    session: Session,
) -> WaitingLink:
    now = datetime.now(UTC)
    with session.begin():
        if not authz.authorize(
            session, auth, resource_type="waiting_links", resource_id=link_id, action="read"
        ):
            raise HTTPException(status_code=404, detail="WAITING_LINK_NOT_FOUND")
        if not authz.authorize(
            session, auth, resource_type="waiting_links", resource_id=link_id, action="write"
        ):
            raise HTTPException(status_code=403, detail="INSUFFICIENT_ROLE")
        current = (
            session.execute(
                text(
                    f"SELECT {_FIELDS} FROM waiting_links "
                    "WHERE workspace_id = :workspace_id AND id = :link_id FOR UPDATE"
                ),
                {"workspace_id": auth.workspace_id, "link_id": link_id},
            )
            .mappings()
            .one_or_none()
        )
        if current is None:
            raise HTTPException(status_code=404, detail="WAITING_LINK_NOT_FOUND")
        if current["version"] != payload.expected_version:
            raise HTTPException(
                status_code=409,
                detail={"code": "VERSION_CONFLICT", "current_version": current["version"]},
            )
        if current["status"] != "open":
            if current["status"] == new_status:
                return WaitingLink.model_validate(dict(current))
            raise HTTPException(status_code=409, detail="WAITING_LINK_NOT_OPEN")
        updated = (
            session.execute(
                text(
                    f"""
                    UPDATE waiting_links
                    SET status = :new_status, updated_at = :now, updated_by = :actor_id,
                        version = version + 1
                    WHERE workspace_id = :workspace_id AND id = :link_id
                    RETURNING {_FIELDS}
                    """
                ),
                {
                    "new_status": new_status,
                    "now": now,
                    "actor_id": auth.user_id,
                    "workspace_id": auth.workspace_id,
                    "link_id": link_id,
                },
            )
            .mappings()
            .one()
        )
        response = WaitingLink.model_validate(dict(updated))
        event_type = (
            "waiting_link.fulfilled" if new_status == "fulfilled" else "waiting_link.cancelled"
        )
        audit_outbox.write_audit_and_outbox(
            session,
            auth,
            request,
            event_type=event_type,
            aggregate_type="waiting_link",
            aggregate_id=link_id,
            aggregate_version=updated["version"],
            changed_fields=["*"],
            payload={"waiting_link_id": str(link_id), "version": updated["version"]},
            now=now,
            domain="waiting",
        )
        queue_lifecycle_event(session, "waiting_link", event_type, "allowed")
        return response


@router.post("/{link_id}/fulfil", response_model=WaitingLink)
def fulfil_waiting_link(
    link_id: UUID,
    payload: WaitingLinkTerminal,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
) -> WaitingLink:
    return _terminate(link_id, "fulfilled", payload, request, auth, session)


@router.post("/{link_id}/cancel", response_model=WaitingLink)
def cancel_waiting_link(
    link_id: UUID,
    payload: WaitingLinkTerminal,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
) -> WaitingLink:
    return _terminate(link_id, "cancelled", payload, request, auth, session)
