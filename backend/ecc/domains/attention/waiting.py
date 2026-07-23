from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import compare_digest, new
from json import dumps, loads
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.config import get_settings
from ecc.database import get_session
from ecc.observability import (
    queue_lifecycle_event,
    record_audit_outbox_failure,
    record_idempotency_conflict,
)

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


def _lock_idempotency(session: Session, auth: AuthContext, key: str) -> None:
    lock_key = f"{auth.workspace_id}:{auth.user_id}:{key}"
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": lock_key},
    )


def _request_hash(payload: BaseModel, action: str) -> str:
    material = {"action": action, "payload": payload.model_dump(mode="json")}
    return sha256(dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _request_ids(request: Request) -> tuple[UUID, UUID]:
    try:
        return UUID(request.state.request_id), UUID(request.state.correlation_id)
    except (AttributeError, TypeError, ValueError):
        return uuid4(), uuid4()


def _load_cached(
    session: Session, auth: AuthContext, key: str, request_hash: str
) -> WaitingLink | None:
    row = (
        session.execute(
            text(
                """
                SELECT request_hash, response_body FROM idempotency_records
                WHERE workspace_id = :workspace_id AND actor_id = :actor_id
                  AND key = :key AND expires_at > :now
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
        record_idempotency_conflict("waiting")
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
    return WaitingLink.model_validate(row["response_body"])


def _store_idempotency(
    session: Session,
    auth: AuthContext,
    key: str,
    request_hash: str,
    response: WaitingLink,
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


_SUBJECT_EXISTENCE_QUERIES: dict[str, Any] = {
    "task": text(
        "SELECT 1 FROM tasks WHERE workspace_id = :workspace_id AND id = :subject_id "
        "AND archived_at IS NULL"
    ),
    "commitment": text(
        "SELECT 1 FROM commitments WHERE workspace_id = :workspace_id AND id = :subject_id "
        "AND archived_at IS NULL"
    ),
    "knowledge_entity": text(
        "SELECT 1 FROM pkos_nodes WHERE workspace_id = :workspace_id AND id = :subject_id "
        "AND status = 'active'"
    ),
}


def _subject_exists(
    session: Session, auth: AuthContext, subject_type: SubjectType, subject_id: UUID
) -> bool:
    row = session.execute(
        _SUBJECT_EXISTENCE_QUERIES[subject_type],
        {"workspace_id": auth.workspace_id, "subject_id": subject_id},
    ).one_or_none()
    return row is not None


def _counterparty_node_type(
    session: Session, auth: AuthContext, counterparty_entity_id: UUID
) -> str | None:
    row = session.execute(
        text(
            "SELECT node_type FROM pkos_nodes "
            "WHERE workspace_id = :workspace_id AND id = :counterparty_id AND status = 'active'"
        ),
        {"workspace_id": auth.workspace_id, "counterparty_id": counterparty_entity_id},
    ).one_or_none()
    return row[0] if row is not None else None


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
    # lock, same pattern as _lock_idempotency but a distinct hash salt so
    # the two lock keyspaces never collide), so a second concurrent create/
    # direction-change targeting the same workspace's blocked_by graph
    # blocks here until this one commits, then re-reads the now-committed
    # graph before deciding.
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 1))"),
        {"lock_key": f"{auth.workspace_id}:waiting_cycle"},
    )
    frontier = {counterparty_entity_id}
    visited: set[UUID] = set()
    for _ in range(64):
        if subject_id in frontier:
            return True
        frontier -= visited
        if not frontier:
            return False
        visited |= frontier
        rows = session.execute(
            text(
                """
                SELECT counterparty_entity_id FROM waiting_links
                WHERE workspace_id = :workspace_id AND direction = 'blocked_by'
                  AND status = 'open' AND subject_type = 'knowledge_entity'
                  AND subject_id = ANY(:frontier)
                """
            ),
            {"workspace_id": auth.workspace_id, "frontier": list(frontier)},
        ).all()
        frontier = {row[0] for row in rows}
    return False


@router.post("", response_model=WaitingLink, status_code=status.HTTP_201_CREATED)
def create_waiting_link(
    payload: WaitingLinkCreate,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> WaitingLink:
    request_hash = _request_hash(payload, "create")
    now = datetime.now(UTC)
    link_id = uuid4()
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached
        if not _subject_exists(session, auth, payload.subject_type, payload.subject_id):
            raise HTTPException(status_code=404, detail="WAITING_SUBJECT_NOT_FOUND")
        node_type = _counterparty_node_type(session, auth, payload.counterparty_entity_id)
        if node_type is None:
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
                        created_by, updated_by, created_at, updated_at, version
                    ) VALUES (
                        :id, :workspace_id, :subject_type, :subject_id, :counterparty_entity_id,
                        :direction, 'open', :note, :since_at, :expected_at,
                        :actor_id, :actor_id, :now, :now, 1
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
        _write_side_effects(session, auth, request, "waiting_link.opened", link_id, 1, now)
        _store_idempotency(session, auth, idempotency_key, request_hash, response, now)
        return response


def _write_side_effects(
    session: Session,
    auth: AuthContext,
    request: Request,
    event_type: str,
    link_id: UUID,
    version: int,
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
                    :id, :workspace_id, :event_type, 'waiting_link', :aggregate_id,
                    :aggregate_version, :actor_id, :request_id, :correlation_id,
                    ARRAY['*'], 'allowed', 'user', '{}'::jsonb, :occurred_at
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": auth.workspace_id,
                "event_type": event_type,
                "aggregate_id": link_id,
                "aggregate_version": version,
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
                    :event_id, :workspace_id, :event_type_v1, 1,
                    :correlation_id, CAST(:payload AS jsonb), :occurred_at, 0
                )
                """
            ),
            {
                "event_id": uuid4(),
                "workspace_id": auth.workspace_id,
                "event_type_v1": f"{event_type}.v1",
                "correlation_id": correlation_id,
                "payload": dumps({"waiting_link_id": str(link_id), "version": version}),
                "occurred_at": now,
            },
        )
    except SQLAlchemyError:
        record_audit_outbox_failure("waiting")
        raise
    queue_lifecycle_event(session, "waiting_link", event_type, "allowed")


def _encode_cursor(created_at: datetime, link_id: UUID) -> str:
    payload = dumps(
        {"created_at": created_at.isoformat(), "id": str(link_id)}, separators=(",", ":")
    ).encode()
    secret = get_settings().session_secret.encode()
    signature = new(secret, payload, "sha256").hexdigest().encode()
    return urlsafe_b64encode(payload + b"." + signature).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        raw = urlsafe_b64decode(padded.encode())
        payload, signature = raw.rsplit(b".", 1)
        expected = new(get_settings().session_secret.encode(), payload, "sha256").hexdigest()
        if not compare_digest(signature.decode(), expected):
            raise ValueError
        decoded = loads(payload)
        return datetime.fromisoformat(decoded["created_at"]), UUID(decoded["id"])
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
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
    clauses = ["workspace_id = :workspace_id"]
    params: dict[str, Any] = {"workspace_id": auth.workspace_id, "limit": limit + 1}
    if status_filter:
        clauses.append("status = :status")
        params["status"] = status_filter
    if direction_filter:
        clauses.append("direction = :direction")
        params["direction"] = direction_filter
    if cursor:
        cursor_created_at, cursor_id = _decode_cursor(cursor)
        clauses.append("(created_at, id) < (:cursor_created_at, :cursor_id)")
        params["cursor_created_at"] = cursor_created_at
        params["cursor_id"] = cursor_id

    rows = (
        session.execute(
            text(
                f"""
                SELECT {_FIELDS} FROM waiting_links
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
    request_hash = _request_hash(payload, f"patch:{link_id}")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached
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
                            created_at, updated_at, version
                        ) VALUES (
                            :id, :workspace_id, :subject_type, :subject_id,
                            :counterparty_entity_id, :direction, 'open',
                            :note, :since_at, :expected_at, :actor_id, :actor_id,
                            :now, :now, 1
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
            _write_side_effects(session, auth, request, "waiting_link.opened", new_id, 1, now)
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
        _store_idempotency(session, auth, idempotency_key, request_hash, response, now)
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
        _write_side_effects(session, auth, request, event_type, link_id, updated["version"], now)
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
