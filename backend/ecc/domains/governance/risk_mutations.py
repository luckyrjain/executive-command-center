from datetime import UTC, datetime, timedelta
from hashlib import sha256
from json import dumps
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session
from ecc.domains.governance.risks import RiskResponse, RiskStatus, _project
from ecc.observability import (
    record_audit_outbox_failure,
    record_idempotency_conflict,
    record_lifecycle_event,
)

router = APIRouter(prefix="/api/v1/risks", tags=["risks"])
SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]

_RISK_FIELDS = """
id, description, probability, impact, status, owner_id, mitigation, trigger,
review_at, project_id, pinned, created_at, updated_at, version, archived_at,
pre_archive_status
"""


class RiskPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    description: str | None = Field(default=None, min_length=1, max_length=5000)
    probability: int | None = Field(default=None, ge=1, le=5)
    impact: int | None = Field(default=None, ge=1, le=5)
    status: RiskStatus | None = None
    mitigation: str | None = None
    trigger: str | None = None
    review_at: datetime | None = None
    project_id: UUID | None = None
    pinned: bool | None = None

    @field_validator("review_at")
    @classmethod
    def validate_review_at(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("review_at must include a timezone offset")
        return value

    @model_validator(mode="after")
    def reject_null_required_fields(self) -> RiskPatch:
        for field in ("description", "probability", "impact", "status", "pinned"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        if len(self.model_fields_set - {"expected_version"}) == 0:
            raise ValueError("at least one mutable field is required")
        return self


class RiskAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)


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
    session: Session,
    auth: AuthContext,
    key: str,
    request_hash: str,
) -> RiskResponse | None:
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
        record_idempotency_conflict("risks")
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
    return RiskResponse.model_validate(row["response_body"])


def _store_cached(
    session: Session,
    auth: AuthContext,
    key: str,
    request_hash: str,
    response: RiskResponse,
    now: datetime,
) -> None:
    session.execute(
        text(
            """
            INSERT INTO idempotency_records (
                workspace_id, actor_id, key, request_hash, response_status,
                response_body, created_at, expires_at
            ) VALUES (
                :workspace_id, :actor_id, :key, :request_hash, 200,
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


def _get_row(
    session: Session,
    auth: AuthContext,
    risk_id: UUID,
    *,
    for_update: bool = False,
) -> dict[str, Any] | None:
    suffix = " FOR UPDATE" if for_update else ""
    row = (
        session.execute(
            text(
                f"""
                SELECT {_RISK_FIELDS}
                FROM risks
                WHERE workspace_id = :workspace_id AND id = :risk_id
                {suffix}
                """
            ),
            {"workspace_id": auth.workspace_id, "risk_id": risk_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _write_side_effects(
    session: Session,
    auth: AuthContext,
    request: Request,
    event_type: str,
    risk_id: UUID,
    version: int,
    changed_fields: list[str],
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
                    :id, :workspace_id, :event_type, 'risk', :aggregate_id,
                    :aggregate_version, :actor_id, :request_id, :correlation_id,
                    :changed_fields, 'allowed', 'user', '{}'::jsonb, :occurred_at
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": auth.workspace_id,
                "event_type": event_type,
                "aggregate_id": risk_id,
                "aggregate_version": version,
                "actor_id": auth.user_id,
                "request_id": request_id,
                "correlation_id": correlation_id,
                "changed_fields": changed_fields,
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
                "payload": dumps({"risk_id": str(risk_id), "version": version}),
                "occurred_at": now,
            },
        )
    except SQLAlchemyError:
        record_audit_outbox_failure("risks")
        raise
    record_lifecycle_event("risk", event_type, "allowed")


@router.patch("/{risk_id}", response_model=RiskResponse)
def update_risk(
    risk_id: UUID,
    payload: RiskPatch,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> RiskResponse:
    request_hash = _request_hash(payload, f"update:{risk_id}")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached
        current = _get_row(session, auth, risk_id, for_update=True)
        if current is None:
            raise HTTPException(status_code=404, detail="RISK_NOT_FOUND")
        if current["archived_at"] is not None:
            raise HTTPException(status_code=409, detail="RISK_ARCHIVED")
        if current["version"] != payload.expected_version:
            raise HTTPException(status_code=409, detail="VERSION_CONFLICT")
        if (
            current["status"] == "closed"
            and payload.status is not None
            and payload.status != "closed"
        ):
            raise HTTPException(status_code=409, detail="RISK_TERMINAL")

        fields = payload.model_fields_set - {"expected_version"}
        assignments: list[str] = []
        params: dict[str, Any] = {
            "workspace_id": auth.workspace_id,
            "risk_id": risk_id,
            "actor_id": auth.user_id,
            "now": now,
        }
        for field in sorted(fields):
            assignments.append(f"{field} = :{field}")
            params[field] = getattr(payload, field)
        assignments.extend(["updated_by = :actor_id", "updated_at = :now", "version = version + 1"])
        row = (
            session.execute(
                text(
                    f"""
                    UPDATE risks
                    SET {", ".join(assignments)}
                    WHERE workspace_id = :workspace_id AND id = :risk_id
                    RETURNING {_RISK_FIELDS}
                    """
                ),
                params,
            )
            .mappings()
            .one()
        )
        response = _project(dict(row), now)
        _write_side_effects(
            session,
            auth,
            request,
            "risk.updated",
            risk_id,
            response.version,
            sorted(fields),
            now,
        )
        _store_cached(session, auth, idempotency_key, request_hash, response, now)
        return response


def _archive_action(
    risk_id: UUID,
    payload: RiskAction,
    request: Request,
    auth: AuthContext,
    session: Session,
    idempotency_key: str,
    action: str,
) -> RiskResponse:
    request_hash = _request_hash(payload, f"{action}:{risk_id}")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached
        current = _get_row(session, auth, risk_id, for_update=True)
        if current is None:
            raise HTTPException(status_code=404, detail="RISK_NOT_FOUND")
        if current["version"] != payload.expected_version:
            raise HTTPException(status_code=409, detail="VERSION_CONFLICT")
        if action == "archive":
            if current["archived_at"] is not None:
                raise HTTPException(status_code=409, detail="RISK_ALREADY_ARCHIVED")
            archived_at = now
            pre_archive_status = current["status"]
            event_type = "risk.archived"
        else:
            if current["archived_at"] is None:
                raise HTTPException(status_code=409, detail="RISK_NOT_ARCHIVED")
            archived_at = None
            pre_archive_status = None
            event_type = "risk.restored"
        row = (
            session.execute(
                text(
                    f"""
                    UPDATE risks
                    SET archived_at = :archived_at,
                        pre_archive_status = :pre_archive_status,
                        updated_by = :actor_id,
                        updated_at = :now,
                        version = version + 1
                    WHERE workspace_id = :workspace_id AND id = :risk_id
                    RETURNING {_RISK_FIELDS}
                    """
                ),
                {
                    "workspace_id": auth.workspace_id,
                    "risk_id": risk_id,
                    "archived_at": archived_at,
                    "pre_archive_status": pre_archive_status,
                    "actor_id": auth.user_id,
                    "now": now,
                },
            )
            .mappings()
            .one()
        )
        response = _project(dict(row), now)
        _write_side_effects(
            session,
            auth,
            request,
            event_type,
            risk_id,
            response.version,
            ["archived_at", "pre_archive_status"],
            now,
        )
        _store_cached(session, auth, idempotency_key, request_hash, response, now)
        return response


@router.post("/{risk_id}/archive", response_model=RiskResponse)
def archive_risk(
    risk_id: UUID,
    payload: RiskAction,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> RiskResponse:
    return _archive_action(risk_id, payload, request, auth, session, idempotency_key, "archive")


@router.post("/{risk_id}/restore", response_model=RiskResponse)
def restore_risk(
    risk_id: UUID,
    payload: RiskAction,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> RiskResponse:
    return _archive_action(risk_id, payload, request, auth, session, idempotency_key, "restore")
