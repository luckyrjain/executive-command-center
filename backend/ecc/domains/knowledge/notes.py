from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import compare_digest, new
from json import dumps, loads
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field, model_validator
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

router = APIRouter(prefix="/api/v1/notes", tags=["notes"])

NoteType = Literal["general", "meeting", "decision", "journal"]
SourceType = Literal["local", "meeting"]
SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]
NoteTypeFilter = Annotated[list[NoteType] | None, Query(alias="note_type[]")]

_SELECT_FIELDS = """
id, owner_id, title, body, note_type, meeting_id, source_type, source_ref,
created_at, updated_at, version, archived_at, pre_archive_status
"""


class NoteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, max_length=500)
    body: str = Field(min_length=1, max_length=100000)
    note_type: NoteType = "general"
    meeting_id: UUID | None = None
    source_type: SourceType = "local"
    source_ref: str | None = Field(default=None, max_length=2000)


class NotePatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    title: str | None = Field(default=None, max_length=500)
    body: str | None = Field(default=None, min_length=1, max_length=100000)
    note_type: NoteType | None = None
    meeting_id: UUID | None = None
    source_type: SourceType | None = None
    source_ref: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def reject_null_required_fields(self) -> NotePatch:
        for field in ("body", "note_type", "source_type"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class NoteAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)


class NoteLinks(BaseModel):
    audit: str


class NoteResponse(BaseModel):
    id: UUID
    owner_id: UUID
    title: str | None
    body: str
    note_type: NoteType
    meeting_id: UUID | None
    source_type: SourceType
    source_ref: str | None
    created_at: datetime
    updated_at: datetime
    version: int
    archived_at: datetime | None
    pre_archive_status: str | None
    links: NoteLinks


class NoteListResponse(BaseModel):
    items: list[NoteResponse]
    next_cursor: str | None = None


def _to_response(row: dict[str, Any]) -> NoteResponse:
    note_id = row["id"]
    row["links"] = {"audit": f"/api/v1/audit?aggregate_type=note&aggregate_id={note_id}"}
    return NoteResponse.model_validate(row)


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
    return sha256(dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _body_checksum(body: str) -> str:
    return sha256(body.encode("utf-8")).hexdigest()


def _redacted_snapshot(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": str(row["id"]),
        "title": row.get("title"),
        "note_type": row["note_type"],
        "meeting_id": str(row["meeting_id"]) if row.get("meeting_id") else None,
        "source_type": row["source_type"],
        "source_ref": row.get("source_ref"),
        "body_checksum": _body_checksum(row["body"]),
        "body_length": len(row["body"]),
        "version": row["version"],
        "archived_at": row["archived_at"].isoformat() if row.get("archived_at") else None,
    }


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
) -> NoteResponse | None:
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
        record_idempotency_conflict("notes")
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
    return NoteResponse.model_validate(row["response_body"])


def _store_cached(
    session: Session,
    auth: AuthContext,
    key: str,
    request_hash: str,
    response: NoteResponse,
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
    note_id: UUID,
    aggregate_version: int,
    request_id: UUID,
    correlation_id: UUID,
    idempotency_key: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    changed_fields: list[str],
    now: datetime,
) -> None:
    try:
        session.execute(
            text(
                """
                INSERT INTO audit_events (
                    id, workspace_id, event_type, aggregate_type, aggregate_id,
                    aggregate_version, actor_id, request_id, correlation_id,
                    idempotency_key_hash, before, after, changed_fields,
                    authorization_result, source, metadata, occurred_at
                ) VALUES (
                    :id, :workspace_id, :event_type, 'note', :aggregate_id,
                    :aggregate_version, :actor_id, :request_id, :correlation_id,
                    :key_hash, CAST(:before AS jsonb), CAST(:after AS jsonb),
                    :changed_fields, 'allowed', 'user', CAST(:metadata AS jsonb), :occurred_at
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": auth.workspace_id,
                "event_type": event_type,
                "aggregate_id": note_id,
                "aggregate_version": aggregate_version,
                "actor_id": auth.user_id,
                "request_id": request_id,
                "correlation_id": correlation_id,
                "key_hash": sha256(idempotency_key.encode()).hexdigest(),
                "before": dumps(before) if before is not None else None,
                "after": dumps(after) if after is not None else None,
                "changed_fields": changed_fields,
                "metadata": dumps({"body_redacted": True}),
                "occurred_at": now,
            },
        )
    except SQLAlchemyError:
        record_audit_outbox_failure("notes")
        raise
    queue_lifecycle_event(session, "note", event_type, "allowed")


def _write_outbox(
    session: Session,
    auth: AuthContext,
    event_type: str,
    note_id: UUID,
    version: int,
    correlation_id: UUID,
    payload: dict[str, Any],
    now: datetime,
) -> None:
    try:
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
                "event_type": event_type,
                "correlation_id": correlation_id,
                "payload": dumps({"note_id": str(note_id), "version": version, **payload}),
                "occurred_at": now,
            },
        )
    except SQLAlchemyError:
        record_audit_outbox_failure("notes")
        raise


def _get_row(
    session: Session,
    auth: AuthContext,
    note_id: UUID,
    *,
    for_update: bool = False,
) -> dict[str, Any] | None:
    suffix = " FOR UPDATE" if for_update else ""
    row = (
        session.execute(
            text(
                f"""
                SELECT {_SELECT_FIELDS}
                FROM notes
                WHERE workspace_id = :workspace_id AND id = :note_id
                {suffix}
                """
            ),
            {"workspace_id": auth.workspace_id, "note_id": note_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _check_version(row: dict[str, Any], expected_version: int) -> None:
    if row["version"] != expected_version:
        raise HTTPException(
            status_code=409,
            detail={"code": "VERSION_CONFLICT", "current_version": row["version"]},
        )


def _validate_meeting_reference(
    session: Session,
    auth: AuthContext,
    meeting_id: UUID | None,
) -> None:
    if meeting_id is None:
        return
    table_exists = session.execute(text("SELECT to_regclass('public.meetings')")).scalar_one()
    if table_exists is None:
        raise HTTPException(status_code=404, detail="MEETING_NOT_FOUND")
    found = session.execute(
        text(
            """
            SELECT 1 FROM meetings
            WHERE workspace_id = :workspace_id AND id = :meeting_id
            """
        ),
        {"workspace_id": auth.workspace_id, "meeting_id": meeting_id},
    ).scalar_one_or_none()
    if found is None:
        raise HTTPException(status_code=404, detail="MEETING_NOT_FOUND")


def _encode_cursor(updated_at: datetime, note_id: UUID) -> str:
    payload = dumps(
        {"updated_at": updated_at.isoformat(), "id": str(note_id)},
        separators=(",", ":"),
    ).encode()
    signature = new(get_settings().session_secret.encode(), payload, "sha256").hexdigest().encode()
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
        return datetime.fromisoformat(decoded["updated_at"]), UUID(decoded["id"])
    except (ValueError, KeyError, TypeError, UnicodeDecodeError) as exc:
        raise HTTPException(status_code=400, detail="MALFORMED_CURSOR") from exc


@router.post("", response_model=NoteResponse, status_code=status.HTTP_201_CREATED)
def create_note(
    payload: NoteCreate,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> NoteResponse:
    request_hash = _request_hash(payload, "create")
    request_id, correlation_id = _request_ids(request)
    now = datetime.now(UTC)
    note_id = uuid4()

    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached
        _validate_meeting_reference(session, auth, payload.meeting_id)
        row = (
            session.execute(
                text(
                    f"""
                    INSERT INTO notes (
                        id, workspace_id, owner_id, title, body, note_type,
                        meeting_id, source_type, source_ref, created_by,
                        updated_by, created_at, updated_at, version
                    ) VALUES (
                        :id, :workspace_id, :owner_id, :title, :body, :note_type,
                        :meeting_id, :source_type, :source_ref, :actor_id,
                        :actor_id, :now, :now, 1
                    ) RETURNING {_SELECT_FIELDS}
                    """
                ),
                {
                    "id": note_id,
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
        current = dict(row)
        response = _to_response(current.copy())
        redacted = _redacted_snapshot(current)
        _write_audit(
            session,
            auth,
            "note.created",
            note_id,
            1,
            request_id,
            correlation_id,
            idempotency_key,
            None,
            redacted,
            ["*"],
            now,
        )
        _write_outbox(
            session,
            auth,
            "note.created.v1",
            note_id,
            1,
            correlation_id,
            {
                "note_type": payload.note_type,
                "meeting_id": str(payload.meeting_id) if payload.meeting_id else None,
            },
            now,
        )
        _store_cached(session, auth, idempotency_key, request_hash, response, 201, now)
        return response


@router.get("", response_model=NoteListResponse)
def list_notes(
    auth: AuthDep,
    session: SessionDep,
    note_type_filter: NoteTypeFilter = None,
    meeting_id: UUID | None = None,
    q: str | None = Query(default=None, min_length=1, max_length=500),
    include_archived: bool = False,
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
) -> NoteListResponse:
    clauses = ["workspace_id = :workspace_id"]
    params: dict[str, Any] = {"workspace_id": auth.workspace_id, "limit": limit + 1}
    if not include_archived:
        clauses.append("archived_at IS NULL")
    if note_type_filter:
        clauses.append("note_type = ANY(:note_types)")
        params["note_types"] = note_type_filter
    if meeting_id is not None:
        clauses.append("meeting_id = :meeting_id")
        params["meeting_id"] = meeting_id
    if q:
        clauses.append("search_document @@ websearch_to_tsquery('simple', :query)")
        params["query"] = q
    if cursor:
        cursor_updated_at, cursor_id = _decode_cursor(cursor)
        clauses.append("(updated_at, id) < (:cursor_updated_at, :cursor_id)")
        params["cursor_updated_at"] = cursor_updated_at
        params["cursor_id"] = cursor_id
    rows = (
        session.execute(
            text(
                f"""
                SELECT {_SELECT_FIELDS}
                FROM notes
                WHERE {" AND ".join(clauses)}
                ORDER BY updated_at DESC, id DESC
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
    next_cursor = None
    if has_more and page:
        last = page[-1]
        next_cursor = _encode_cursor(last["updated_at"], last["id"])
    return NoteListResponse(
        items=[_to_response(dict(row)) for row in page],
        next_cursor=next_cursor,
    )


@router.get("/{note_id}", response_model=NoteResponse)
def get_note(note_id: UUID, auth: AuthDep, session: SessionDep) -> NoteResponse:
    row = _get_row(session, auth, note_id)
    if row is None:
        raise HTTPException(status_code=404, detail="NOTE_NOT_FOUND")
    return _to_response(row)


@router.patch("/{note_id}", response_model=NoteResponse)
def update_note(
    note_id: UUID,
    payload: NotePatch,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> NoteResponse:
    request_hash = _request_hash(payload, f"update:{note_id}")
    request_id, correlation_id = _request_ids(request)
    now = datetime.now(UTC)

    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached
        current = _get_row(session, auth, note_id, for_update=True)
        if current is None:
            raise HTTPException(status_code=404, detail="NOTE_NOT_FOUND")
        _check_version(current, payload.expected_version)
        if current["archived_at"] is not None:
            raise HTTPException(status_code=409, detail="NOTE_ARCHIVED")
        fields = payload.model_fields_set - {"expected_version"}
        if not fields:
            response = _to_response(current.copy())
            _store_cached(session, auth, idempotency_key, request_hash, response, 200, now)
            return response
        if "meeting_id" in fields:
            _validate_meeting_reference(session, auth, payload.meeting_id)
        changed_fields = sorted(fields)
        assignments = [f"{field} = :{field}" for field in changed_fields]
        assignments.extend(
            ["updated_by = :updated_by", "updated_at = :now", "version = version + 1"]
        )
        values = payload.model_dump(include=set(changed_fields))
        values.update(
            {
                "workspace_id": auth.workspace_id,
                "note_id": note_id,
                "updated_by": auth.user_id,
                "now": now,
            }
        )
        row = (
            session.execute(
                text(
                    f"""
                    UPDATE notes
                    SET {", ".join(assignments)}
                    WHERE workspace_id = :workspace_id AND id = :note_id
                    RETURNING {_SELECT_FIELDS}
                    """
                ),
                values,
            )
            .mappings()
            .one()
        )
        updated = dict(row)
        response = _to_response(updated.copy())
        before = _redacted_snapshot(current)
        after = _redacted_snapshot(updated)
        _write_audit(
            session,
            auth,
            "note.updated",
            note_id,
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
            "note.updated.v1",
            note_id,
            response.version,
            correlation_id,
            {
                "changed_fields": changed_fields,
                "body_checksum": _body_checksum(updated["body"]),
            },
            now,
        )
        _store_cached(session, auth, idempotency_key, request_hash, response, 200, now)
        return response


def _lifecycle(
    note_id: UUID,
    payload: NoteAction,
    request: Request,
    auth: AuthContext,
    session: Session,
    idempotency_key: str,
    action: Literal["archive", "restore"],
) -> NoteResponse:
    request_hash = _request_hash(payload, f"{action}:{note_id}")
    request_id, correlation_id = _request_ids(request)
    now = datetime.now(UTC)

    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached
        current = _get_row(session, auth, note_id, for_update=True)
        if current is None:
            raise HTTPException(status_code=404, detail="NOTE_NOT_FOUND")
        _check_version(current, payload.expected_version)
        if action == "archive" and current["archived_at"] is not None:
            response = _to_response(current.copy())
            _store_cached(session, auth, idempotency_key, request_hash, response, 200, now)
            return response
        if action == "restore" and current["archived_at"] is None:
            raise HTTPException(status_code=409, detail="NOTE_NOT_ARCHIVED")
        if action == "archive":
            assignments = "archived_at = :now, pre_archive_status = 'active'"
            audit_type = "note.archived"
            event_type = "note.archived.v1"
            event_payload = {"archived_at": now.isoformat()}
            changed_fields = ["archived_at", "pre_archive_status"]
        else:
            assignments = "archived_at = NULL, pre_archive_status = NULL"
            audit_type = "note.restored"
            event_type = "note.restored.v1"
            event_payload = {}
            changed_fields = ["archived_at", "pre_archive_status"]
        row = (
            session.execute(
                text(
                    f"""
                    UPDATE notes
                    SET {assignments}, updated_by = :updated_by,
                        updated_at = :now, version = version + 1
                    WHERE workspace_id = :workspace_id AND id = :note_id
                    RETURNING {_SELECT_FIELDS}
                    """
                ),
                {
                    "workspace_id": auth.workspace_id,
                    "note_id": note_id,
                    "updated_by": auth.user_id,
                    "now": now,
                },
            )
            .mappings()
            .one()
        )
        updated = dict(row)
        response = _to_response(updated.copy())
        _write_audit(
            session,
            auth,
            audit_type,
            note_id,
            response.version,
            request_id,
            correlation_id,
            idempotency_key,
            _redacted_snapshot(current),
            _redacted_snapshot(updated),
            changed_fields,
            now,
        )
        _write_outbox(
            session,
            auth,
            event_type,
            note_id,
            response.version,
            correlation_id,
            event_payload,
            now,
        )
        _store_cached(session, auth, idempotency_key, request_hash, response, 200, now)
        return response


@router.post("/{note_id}/archive", response_model=NoteResponse)
def archive_note(
    note_id: UUID,
    payload: NoteAction,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> NoteResponse:
    return _lifecycle(note_id, payload, request, auth, session, idempotency_key, "archive")


@router.post("/{note_id}/restore", response_model=NoteResponse)
def restore_note(
    note_id: UUID,
    payload: NoteAction,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> NoteResponse:
    return _lifecycle(note_id, payload, request, auth, session, idempotency_key, "restore")
