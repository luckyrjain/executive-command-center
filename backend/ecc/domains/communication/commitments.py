from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from json import dumps
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session

router = APIRouter(prefix="/api/v1/commitments", tags=["commitments"])

CommitmentDirection = Literal["made_by_me", "made_to_me"]
CommitmentStatus = Literal[
    "detected",
    "confirmed",
    "active",
    "fulfilled",
    "broken",
    "cancelled",
]
CommitmentImportance = Literal["low", "medium", "high", "critical"]
SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]
StatusFilter = Annotated[list[CommitmentStatus] | None, Query(alias="status[]")]
ImportanceFilter = Annotated[list[CommitmentImportance] | None, Query(alias="importance[]")]

_SELECT_FIELDS = """
id, owner_id, summary, description, direction, counterparty_person_id,
counterparty_name, status, due_date, due_at, importance, evidence_id,
confidence, fulfilled_at, pinned, created_at, updated_at, version,
archived_at, pre_archive_status
"""


class CommitmentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: str = Field(min_length=1, max_length=500)
    description: str | None = None
    direction: CommitmentDirection
    counterparty_person_id: UUID | None = None
    counterparty_name: str | None = Field(default=None, max_length=500)
    due_date: date | None = None
    due_at: datetime | None = None
    importance: CommitmentImportance = "medium"
    evidence_id: UUID | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    status: Literal["detected", "confirmed"] = "confirmed"
    pinned: bool = False

    @model_validator(mode="after")
    def validate_due_precision(self) -> "CommitmentCreate":
        if self.due_date is not None and self.due_at is not None:
            raise ValueError("due_date and due_at are mutually exclusive")
        if self.due_at is not None and self.due_at.utcoffset() is None:
            raise ValueError("due_at must include a timezone offset")
        if self.status == "detected" and self.evidence_id is None:
            raise ValueError("detected commitments require evidence_id")
        return self


class CommitmentPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    summary: str | None = Field(default=None, min_length=1, max_length=500)
    description: str | None = None
    counterparty_person_id: UUID | None = None
    counterparty_name: str | None = Field(default=None, max_length=500)
    due_date: date | None = None
    due_at: datetime | None = None
    importance: CommitmentImportance | None = None
    pinned: bool | None = None

    @model_validator(mode="after")
    def validate_due_precision(self) -> "CommitmentPatch":
        if self.due_date is not None and self.due_at is not None:
            raise ValueError("due_date and due_at are mutually exclusive")
        if self.due_at is not None and self.due_at.utcoffset() is None:
            raise ValueError("due_at must include a timezone offset")
        return self


class CommitmentAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    reason: str | None = Field(default=None, max_length=2000)


class CommitmentLinks(BaseModel):
    audit: str


class CommitmentResponse(BaseModel):
    id: UUID
    owner_id: UUID
    summary: str
    description: str | None
    direction: CommitmentDirection
    counterparty_person_id: UUID | None
    counterparty_name: str | None
    status: CommitmentStatus
    due_date: date | None
    due_at: datetime | None
    importance: CommitmentImportance
    evidence_id: UUID | None
    confidence: float | None
    fulfilled_at: datetime | None
    pinned: bool
    created_at: datetime
    updated_at: datetime
    version: int
    archived_at: datetime | None
    pre_archive_status: str | None
    links: CommitmentLinks


class CommitmentListResponse(BaseModel):
    items: list[CommitmentResponse]
    next_cursor: str | None = None


def _to_response(row: dict[str, Any]) -> CommitmentResponse:
    commitment_id = row["id"]
    row["links"] = {
        "audit": (
            "/api/v1/audit?aggregate_type=commitment"
            f"&aggregate_id={commitment_id}"
        )
    }
    return CommitmentResponse.model_validate(row)


def _request_ids(request: Request) -> tuple[UUID, UUID]:
    try:
        return UUID(request.state.request_id), UUID(request.state.correlation_id)
    except (AttributeError, TypeError, ValueError):
        return uuid4(), uuid4()


def _request_hash(payload: BaseModel, action: str) -> str:
    material = {
        "action": action,
        "payload": payload.model_dump(mode="json", exclude_none=False),
    }
    return sha256(
        dumps(material, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _lock_idempotency(session: Session, auth: AuthContext, key: str) -> None:
    lock_key = f"{auth.workspace_id}:{auth.user_id}:{key}"
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": lock_key},
    )


def _load_cached(
    session: Session,
    auth: AuthContext,
    key: str,
    request_hash: str,
) -> CommitmentResponse | None:
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
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
    return CommitmentResponse.model_validate(row["response_body"])


def _store_cached(
    session: Session,
    auth: AuthContext,
    key: str,
    request_hash: str,
    response: CommitmentResponse,
    response_status: int,
    now: datetime,
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
            "response_status": response_status,
            "response_body": dumps(response.model_dump(mode="json")),
            "created_at": now,
            "expires_at": now + timedelta(days=365),
        },
    )


def _write_audit(
    session: Session,
    auth: AuthContext,
    event_type: str,
    commitment_id: UUID,
    aggregate_version: int,
    request_id: UUID,
    correlation_id: UUID,
    idempotency_key: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    changed_fields: list[str],
    now: datetime,
) -> None:
    session.execute(
        text(
            """
            INSERT INTO audit_events (
                id, workspace_id, event_type, aggregate_type, aggregate_id,
                aggregate_version, actor_id, request_id, correlation_id,
                idempotency_key_hash, before, after, changed_fields,
                authorization_result, source, metadata, occurred_at
            ) VALUES (
                :id, :workspace_id, :event_type, 'commitment', :aggregate_id,
                :aggregate_version, :actor_id, :request_id, :correlation_id,
                :key_hash, CAST(:before AS jsonb), CAST(:after AS jsonb),
                :changed_fields, 'allowed', 'user', '{}'::jsonb, :occurred_at
            )
            """
        ),
        {
            "id": uuid4(),
            "workspace_id": auth.workspace_id,
            "event_type": event_type,
            "aggregate_id": commitment_id,
            "aggregate_version": aggregate_version,
            "actor_id": auth.user_id,
            "request_id": request_id,
            "correlation_id": correlation_id,
            "key_hash": sha256(idempotency_key.encode()).hexdigest(),
            "before": dumps(before) if before is not None else None,
            "after": dumps(after) if after is not None else None,
            "changed_fields": changed_fields,
            "occurred_at": now,
        },
    )


def _write_outbox(
    session: Session,
    auth: AuthContext,
    event_type: str,
    commitment_id: UUID,
    version: int,
    correlation_id: UUID,
    payload: dict[str, Any],
    now: datetime,
) -> None:
    session.execute(
        text(
            """
            INSERT INTO event_outbox (
                event_id, workspace_id, event_type, event_version,
                correlation_id, payload, occurred_at
            ) VALUES (
                :event_id, :workspace_id, :event_type, 1,
                :correlation_id, CAST(:payload AS jsonb), :occurred_at
            )
            """
        ),
        {
            "event_id": uuid4(),
            "workspace_id": auth.workspace_id,
            "event_type": event_type,
            "correlation_id": correlation_id,
            "payload": dumps(
                {"commitment_id": str(commitment_id), "version": version, **payload}
            ),
            "occurred_at": now,
        },
    )


def _get_row(
    session: Session,
    auth: AuthContext,
    commitment_id: UUID,
    *,
    for_update: bool = False,
) -> dict[str, Any] | None:
    suffix = " FOR UPDATE" if for_update else ""
    row = (
        session.execute(
            text(
                f"""
                SELECT {_SELECT_FIELDS}
                FROM commitments
                WHERE workspace_id = :workspace_id AND id = :commitment_id
                {suffix}
                """
            ),
            {
                "workspace_id": auth.workspace_id,
                "commitment_id": commitment_id,
            },
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _check_version(row: dict[str, Any], expected_version: int) -> None:
    if row["version"] != expected_version:
        raise HTTPException(status_code=409, detail="VERSION_CONFLICT")


@router.post("", response_model=CommitmentResponse, status_code=status.HTTP_201_CREATED)
def create_commitment(
    payload: CommitmentCreate,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> CommitmentResponse:
    request_hash = _request_hash(payload, "create")
    request_id, correlation_id = _request_ids(request)
    now = datetime.now(UTC)
    commitment_id = uuid4()
    initial_status = payload.status

    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached
        row = (
            session.execute(
                text(
                    f"""
                    INSERT INTO commitments (
                        id, workspace_id, owner_id, summary, description,
                        direction, counterparty_person_id, counterparty_name,
                        status, due_date, due_at, importance, evidence_id,
                        confidence, pinned, created_by, updated_by,
                        created_at, updated_at, version
                    ) VALUES (
                        :id, :workspace_id, :owner_id, :summary, :description,
                        :direction, :counterparty_person_id, :counterparty_name,
                        :status, :due_date, :due_at, :importance, :evidence_id,
                        :confidence, :pinned, :actor_id, :actor_id,
                        :now, :now, 1
                    ) RETURNING {_SELECT_FIELDS}
                    """
                ),
                {
                    "id": commitment_id,
                    "workspace_id": auth.workspace_id,
                    "owner_id": auth.user_id,
                    "actor_id": auth.user_id,
                    "now": now,
                    **payload.model_dump(),
                },
            )
            .mappings()
            .one()
        )
        response = _to_response(dict(row))
        after = response.model_dump(mode="json")
        _write_audit(
            session,
            auth,
            "commitment.created",
            commitment_id,
            1,
            request_id,
            correlation_id,
            idempotency_key,
            None,
            after,
            ["*"],
            now,
        )
        event_type = (
            "commitment.detected.v1"
            if initial_status == "detected"
            else "commitment.created.v1"
        )
        _write_outbox(
            session,
            auth,
            event_type,
            commitment_id,
            1,
            correlation_id,
            {
                "direction": payload.direction,
                "importance": payload.importance,
                "evidence_id": str(payload.evidence_id) if payload.evidence_id else None,
                "confidence": payload.confidence,
            },
            now,
        )
        _store_cached(
            session,
            auth,
            idempotency_key,
            request_hash,
            response,
            201,
            now,
        )
        return response


@router.get("", response_model=CommitmentListResponse)
def list_commitments(
    auth: AuthDep,
    session: SessionDep,
    status_filter: StatusFilter = None,
    importance_filter: ImportanceFilter = None,
    direction: CommitmentDirection | None = None,
    due_before: date | None = None,
    due_after: date | None = None,
    pinned: bool | None = None,
    include_archived: bool = False,
    limit: int = Query(default=20, ge=1, le=100),
) -> CommitmentListResponse:
    clauses = ["workspace_id = :workspace_id"]
    params: dict[str, Any] = {
        "workspace_id": auth.workspace_id,
        "limit": limit,
    }
    if not include_archived:
        clauses.append("archived_at IS NULL")
    if status_filter:
        clauses.append("status = ANY(:statuses)")
        params["statuses"] = status_filter
    if importance_filter:
        clauses.append("importance = ANY(:importance)")
        params["importance"] = importance_filter
    if direction:
        clauses.append("direction = :direction")
        params["direction"] = direction
    if due_before:
        clauses.append(
            "COALESCE(due_date, (due_at AT TIME ZONE :timezone)::date) <= :due_before"
        )
        params.update({"timezone": auth.timezone, "due_before": due_before})
    if due_after:
        clauses.append(
            "COALESCE(due_date, (due_at AT TIME ZONE :timezone)::date) >= :due_after"
        )
        params.update({"timezone": auth.timezone, "due_after": due_after})
    if pinned is not None:
        clauses.append("pinned = :pinned")
        params["pinned"] = pinned
    rows = (
        session.execute(
            text(
                f"""
                SELECT {_SELECT_FIELDS}
                FROM commitments
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
    return CommitmentListResponse(items=[_to_response(dict(row)) for row in rows])


@router.get("/{commitment_id}", response_model=CommitmentResponse)
def get_commitment(
    commitment_id: UUID,
    auth: AuthDep,
    session: SessionDep,
) -> CommitmentResponse:
    row = _get_row(session, auth, commitment_id)
    if row is None:
        raise HTTPException(status_code=404, detail="COMMITMENT_NOT_FOUND")
    return _to_response(row)


@router.patch("/{commitment_id}", response_model=CommitmentResponse)
def update_commitment(
    commitment_id: UUID,
    payload: CommitmentPatch,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> CommitmentResponse:
    return _mutate_commitment(
        commitment_id,
        payload,
        request,
        auth,
        session,
        idempotency_key,
    )


def _mutate_commitment(
    commitment_id: UUID,
    payload: CommitmentPatch,
    request: Request,
    auth: AuthContext,
    session: Session,
    idempotency_key: str,
) -> CommitmentResponse:
    request_hash = _request_hash(payload, f"update:{commitment_id}")
    request_id, correlation_id = _request_ids(request)
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached
        current = _get_row(session, auth, commitment_id, for_update=True)
        if current is None:
            raise HTTPException(status_code=404, detail="COMMITMENT_NOT_FOUND")
        _check_version(current, payload.expected_version)
        if current["archived_at"] is not None:
            raise HTTPException(status_code=409, detail="COMMITMENT_ARCHIVED")
        if current["status"] in {"fulfilled", "broken", "cancelled"}:
            raise HTTPException(status_code=409, detail="COMMITMENT_TERMINAL")
        fields = payload.model_fields_set - {"expected_version"}
        if not fields:
            response = _to_response(current)
            _store_cached(
                session, auth, idempotency_key, request_hash, response, 200, now
            )
            return response
        effective_due_date = (
            payload.due_date if "due_date" in fields else current["due_date"]
        )
        effective_due_at = payload.due_at if "due_at" in fields else current["due_at"]
        if effective_due_date is not None and effective_due_at is not None:
            raise HTTPException(status_code=422, detail="MUTUALLY_EXCLUSIVE_FIELDS")
        changed_fields = sorted(fields)
        assignments = [f"{field} = :{field}" for field in changed_fields]
        assignments.extend(
            ["updated_by = :updated_by", "updated_at = :now", "version = version + 1"]
        )
        values = payload.model_dump(include=set(changed_fields))
        values.update(
            {
                "workspace_id": auth.workspace_id,
                "commitment_id": commitment_id,
                "updated_by": auth.user_id,
                "now": now,
            }
        )
        row = (
            session.execute(
                text(
                    f"""
                    UPDATE commitments
                    SET {", ".join(assignments)}
                    WHERE workspace_id = :workspace_id AND id = :commitment_id
                    RETURNING {_SELECT_FIELDS}
                    """
                ),
                values,
            )
            .mappings()
            .one()
        )
        response = _to_response(dict(row))
        before = _to_response(current).model_dump(mode="json")
        after = response.model_dump(mode="json")
        _write_audit(
            session,
            auth,
            "commitment.updated",
            commitment_id,
            response.version,
            request_id,
            correlation_id,
            idempotency_key,
            before,
            after,
            changed_fields,
            now,
        )
        _write_outbox(
            session,
            auth,
            "commitment.updated.v1",
            commitment_id,
            response.version,
            correlation_id,
            {"changed_fields": changed_fields},
            now,
        )
        _store_cached(
            session, auth, idempotency_key, request_hash, response, 200, now
        )
        return response


def _lifecycle(
    commitment_id: UUID,
    payload: CommitmentAction,
    request: Request,
    auth: AuthContext,
    session: Session,
    idempotency_key: str,
    action: Literal["confirm", "fulfil", "cancel", "archive", "restore"],
) -> CommitmentResponse:
    request_hash = _request_hash(payload, f"{action}:{commitment_id}")
    request_id, correlation_id = _request_ids(request)
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached
        current = _get_row(session, auth, commitment_id, for_update=True)
        if current is None:
            raise HTTPException(status_code=404, detail="COMMITMENT_NOT_FOUND")
        _check_version(current, payload.expected_version)

        target_reached = (
            (action == "confirm" and current["status"] == "active")
            or (action == "fulfil" and current["status"] == "fulfilled")
            or (action == "cancel" and current["status"] == "cancelled")
            or (action == "archive" and current["archived_at"] is not None)
            or (action == "restore" and current["archived_at"] is None)
        )
        if target_reached:
            response = _to_response(current)
            _store_cached(
                session, auth, idempotency_key, request_hash, response, 200, now
            )
            return response

        if action in {"confirm", "fulfil", "cancel"} and current["archived_at"]:
            raise HTTPException(status_code=409, detail="COMMITMENT_ARCHIVED")
        if action == "confirm" and current["status"] not in {"detected", "confirmed"}:
            raise HTTPException(status_code=409, detail="INVALID_COMMITMENT_TRANSITION")
        if action in {"fulfil", "cancel"} and current["status"] not in {
            "confirmed",
            "active",
        }:
            raise HTTPException(status_code=409, detail="INVALID_COMMITMENT_TRANSITION")
        if action == "restore" and current["archived_at"] is None:
            raise HTTPException(status_code=409, detail="COMMITMENT_NOT_ARCHIVED")

        if action == "confirm":
            assignments = "status = 'active'"
            audit_type = "commitment.confirmed"
            event_type = "commitment.confirmed.v1"
            event_payload = {
                "owner_id": str(auth.user_id),
                "due_date": str(current["due_date"]) if current["due_date"] else None,
                "due_at": current["due_at"].isoformat() if current["due_at"] else None,
            }
            changed_fields = ["status"]
        elif action == "fulfil":
            assignments = "status = 'fulfilled', fulfilled_at = :now"
            audit_type = "commitment.fulfilled"
            event_type = "commitment.fulfilled.v1"
            event_payload = {"fulfilled_at": now.isoformat()}
            changed_fields = ["status", "fulfilled_at"]
        elif action == "cancel":
            assignments = "status = 'cancelled', fulfilled_at = NULL"
            audit_type = "commitment.cancelled"
            event_type = "commitment.cancelled.v1"
            event_payload = {"reason": payload.reason}
            changed_fields = ["status", "fulfilled_at"]
        elif action == "archive":
            assignments = "archived_at = :now, pre_archive_status = status"
            audit_type = "commitment.archived"
            event_type = "commitment.archived.v1"
            event_payload = {
                "archived_at": now.isoformat(),
                "pre_archive_status": current["status"],
            }
            changed_fields = ["archived_at", "pre_archive_status"]
        else:
            restored_status = current["pre_archive_status"] or "confirmed"
            assignments = (
                "archived_at = NULL, pre_archive_status = NULL, "
                "status = :restored_status"
            )
            audit_type = "commitment.restored"
            event_type = "commitment.restored.v1"
            event_payload = {"restored_status": restored_status}
            changed_fields = ["archived_at", "pre_archive_status", "status"]

        params: dict[str, Any] = {
            "workspace_id": auth.workspace_id,
            "commitment_id": commitment_id,
            "updated_by": auth.user_id,
            "now": now,
        }
        if action == "restore":
            params["restored_status"] = current["pre_archive_status"] or "confirmed"
        row = (
            session.execute(
                text(
                    f"""
                    UPDATE commitments
                    SET {assignments}, updated_by = :updated_by,
                        updated_at = :now, version = version + 1
                    WHERE workspace_id = :workspace_id AND id = :commitment_id
                    RETURNING {_SELECT_FIELDS}
                    """
                ),
                params,
            )
            .mappings()
            .one()
        )
        response = _to_response(dict(row))
        before = _to_response(current).model_dump(mode="json")
        after = response.model_dump(mode="json")
        _write_audit(
            session,
            auth,
            audit_type,
            commitment_id,
            response.version,
            request_id,
            correlation_id,
            idempotency_key,
            before,
            after,
            changed_fields,
            now,
        )
        _write_outbox(
            session,
            auth,
            event_type,
            commitment_id,
            response.version,
            correlation_id,
            event_payload,
            now,
        )
        _store_cached(
            session, auth, idempotency_key, request_hash, response, 200, now
        )
        return response


@router.post("/{commitment_id}/confirm", response_model=CommitmentResponse)
def confirm_commitment(
    commitment_id: UUID,
    payload: CommitmentAction,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> CommitmentResponse:
    return _lifecycle(
        commitment_id, payload, request, auth, session, idempotency_key, "confirm"
    )


@router.post("/{commitment_id}/fulfil", response_model=CommitmentResponse)
def fulfil_commitment(
    commitment_id: UUID,
    payload: CommitmentAction,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> CommitmentResponse:
    return _lifecycle(
        commitment_id, payload, request, auth, session, idempotency_key, "fulfil"
    )


@router.post("/{commitment_id}/cancel", response_model=CommitmentResponse)
def cancel_commitment(
    commitment_id: UUID,
    payload: CommitmentAction,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> CommitmentResponse:
    return _lifecycle(
        commitment_id, payload, request, auth, session, idempotency_key, "cancel"
    )


@router.post("/{commitment_id}/archive", response_model=CommitmentResponse)
def archive_commitment(
    commitment_id: UUID,
    payload: CommitmentAction,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> CommitmentResponse:
    return _lifecycle(
        commitment_id, payload, request, auth, session, idempotency_key, "archive"
    )


@router.post("/{commitment_id}/restore", response_model=CommitmentResponse)
def restore_commitment(
    commitment_id: UUID,
    payload: CommitmentAction,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> CommitmentResponse:
    return _lifecycle(
        commitment_id, payload, request, auth, session, idempotency_key, "restore"
    )
