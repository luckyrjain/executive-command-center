"""Cross-domain grants (Phase 7 Task 5, part 1): `GET|POST /api/v1/
personal/grants`, `POST .../{id}/revoke` (`docs/phases/phase-007/
API-SCHEMAS.md`).

A grant names a `source_domain_key` (whose data may be drawn from),
a `purpose` (why -- a closed, extensible enum, one value this task
defines: `insight_generation`, per `docs/superpowers/specs/2026-07-31-
phase-7-personal-intelligence-design.md` Decision 1 item 5), a list of
`granted_categories` (which parts of the source domain's data), and an
optional `expires_at`. This module ships the schema and grant lifecycle
(create/list/revoke) only -- no consumer reads a grant yet, matching
`0056_phase7_cross_domain_grants.py`'s own docstring: the `trend`/
`correlation` AI-generated insight types that will actually check a
grant before combining domains are a separate follow-up PR.

Lifecycle mirrors `domains.py`'s `domain_consents` handling exactly
(grant once, later only ever revoke) -- see migration `0056`'s own
docstring for why this table has no `version`/`updated_at`/`created_by`
the way a generally-editable resource (`domain_records`) would.
"""

from datetime import UTC, datetime
from json import dumps
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.observability import queue_lifecycle_event
from ecc.platform import audit_outbox
from ecc.platform.idempotency import load_cached, lock_idempotency, request_hash, store_idempotency

from .domains import DomainKey, IdempotencyHeader, SessionDep, require_enabled_domain

router = APIRouter(prefix="/api/v1/personal", tags=["personal"])

Purpose = Annotated[str, Field(pattern="^(insight_generation)$")]


class GrantCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_domain_key: DomainKey
    purpose: Purpose
    granted_categories: list[str] = Field(min_length=1)
    expires_at: datetime | None = None


class GrantResponse(BaseModel):
    id: UUID
    source_domain_key: str
    purpose: str
    granted_categories: list[str]
    granted_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class GrantListResponse(BaseModel):
    grants: list[GrantResponse]


class _EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


_GRANT_FIELDS = (
    "id, source_domain_key, purpose, granted_categories, granted_at, "
    "expires_at, revoked_at, created_at"
)


def _get_grant_row(session: Session, auth: AuthContext, grant_id: UUID) -> dict[str, Any] | None:
    row = (
        session.execute(
            text(
                f"SELECT {_GRANT_FIELDS} FROM cross_domain_grants "  # noqa: S608
                "WHERE workspace_id = :workspace_id AND owner_id = :owner_id AND id = :id"
            ),
            {"workspace_id": auth.workspace_id, "owner_id": auth.user_id, "id": grant_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _active_only_clause(active_only: bool) -> str:
    if not active_only:
        return ""
    return " AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > now())"


@router.get("/grants", response_model=GrantListResponse)
def list_grants_endpoint(
    auth: AuthDep,
    session: SessionDep,
    source_domain_key: Annotated[DomainKey | None, Query()] = None,
    active_only: Annotated[bool, Query()] = False,
) -> GrantListResponse:
    params: dict[str, Any] = {"workspace_id": auth.workspace_id, "owner_id": auth.user_id}
    where = "workspace_id = :workspace_id AND owner_id = :owner_id"
    if source_domain_key is not None:
        where += " AND source_domain_key = :source_domain_key"
        params["source_domain_key"] = source_domain_key
    where += _active_only_clause(active_only)
    rows = (
        session.execute(
            text(
                f"SELECT {_GRANT_FIELDS} FROM cross_domain_grants WHERE {where} "  # noqa: S608
                "ORDER BY granted_at DESC"
            ),
            params,
        )
        .mappings()
        .all()
    )
    return GrantListResponse(grants=[GrantResponse(**dict(r)) for r in rows])


@router.post("/grants", response_model=GrantResponse, status_code=status.HTTP_201_CREATED)
def create_grant_endpoint(
    payload: GrantCreateRequest,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> GrantResponse:
    req_hash = request_hash(payload, "create_grant")
    now = datetime.now(UTC)
    with session.begin():
        lock_idempotency(session, auth, idempotency_key)
        cached = load_cached(session, auth, idempotency_key, req_hash, domain="cross_domain_grant")
        if cached is not None:
            return GrantResponse.model_validate(cached)

        require_enabled_domain(session, auth, payload.source_domain_key)

        grant_id = uuid4()
        session.execute(
            text(
                """
                INSERT INTO cross_domain_grants (
                    id, workspace_id, owner_id, source_domain_key, purpose,
                    granted_categories, granted_at, expires_at, created_at
                ) VALUES (
                    :id, :workspace_id, :owner_id, :source_domain_key, :purpose,
                    CAST(:granted_categories AS jsonb), :now, :expires_at, :now
                )
                """
            ),
            {
                "id": grant_id,
                "workspace_id": auth.workspace_id,
                "owner_id": auth.user_id,
                "source_domain_key": payload.source_domain_key,
                "purpose": payload.purpose,
                "granted_categories": dumps(payload.granted_categories),
                "now": now,
                "expires_at": payload.expires_at,
            },
        )
        row = _get_grant_row(session, auth, grant_id)
        assert row is not None
        response = GrantResponse(**row)
        audit_outbox.write_audit_and_outbox(
            session,
            auth,
            request,
            event_type="cross_domain_grant.created",
            aggregate_type="cross_domain_grant",
            aggregate_id=grant_id,
            aggregate_version=1,
            changed_fields=["*"],
            payload={"aggregate_id": str(grant_id), "version": 1},
            now=now,
            domain="cross_domain_grant",
        )
        queue_lifecycle_event(
            session, "cross_domain_grant", "cross_domain_grant.created", "allowed"
        )
        store_idempotency(
            session,
            auth,
            idempotency_key,
            req_hash,
            response.model_dump(mode="json"),
            now,
            response_status=status.HTTP_201_CREATED,
        )
        return response


@router.post("/grants/{grant_id}/revoke", response_model=GrantResponse)
def revoke_grant_endpoint(
    grant_id: UUID,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> GrantResponse:
    req_hash = request_hash(_EmptyBody(), f"revoke_grant:{grant_id}")
    now = datetime.now(UTC)
    with session.begin():
        lock_idempotency(session, auth, idempotency_key)
        cached = load_cached(session, auth, idempotency_key, req_hash, domain="cross_domain_grant")
        if cached is not None:
            return GrantResponse.model_validate(cached)

        existing = _get_grant_row(session, auth, grant_id)
        if existing is None:
            raise HTTPException(status_code=404, detail="GRANT_NOT_FOUND")

        if existing["revoked_at"] is None:
            session.execute(
                text(
                    "UPDATE cross_domain_grants SET revoked_at = :now "
                    "WHERE id = :id AND revoked_at IS NULL"
                ),
                {"now": now, "id": grant_id},
            )

        row = _get_grant_row(session, auth, grant_id)
        assert row is not None
        response = GrantResponse(**row)
        audit_outbox.write_audit_and_outbox(
            session,
            auth,
            request,
            event_type="cross_domain_grant.revoked",
            aggregate_type="cross_domain_grant",
            aggregate_id=grant_id,
            aggregate_version=1,
            changed_fields=["*"],
            payload={"aggregate_id": str(grant_id), "version": 1},
            now=now,
            domain="cross_domain_grant",
        )
        queue_lifecycle_event(
            session, "cross_domain_grant", "cross_domain_grant.revoked", "allowed"
        )
        store_idempotency(
            session, auth, idempotency_key, req_hash, response.model_dump(mode="json"), now
        )
        return response
