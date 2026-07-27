"""Connector account lifecycle (`connector_accounts`/`sync_cursors`/
`sync_runs`), plus `GET|POST /api/v1/engineering/connectors`,
`POST .../{id}/sync`, `POST .../{id}/disable` and `GET /api/v1/engineering/
sync-runs` (`docs/phases/phase-006/API-SCHEMAS.md`).

Phase 6 Task 1 -- the connector-platform layer only. Dispatches into
`ecc.domains.engineering.connectors.registry` (this task registers exactly
one adapter, `sandbox.github` -- no real GitHub/GitLab/Jira network call
happens anywhere in this module). A real provider's own rate limiting/
pagination/backoff lives inside that provider's own future adapter, not
here -- this module's own responsibility is account lifecycle, credential
encryption, cursor persistence and sync-run bookkeeping, identical in kind
to how `ecc.domains.automation.policy`/`approvals` own policy/approval
lifecycle while `worker.py` owns actual dispatch.

**A connector account's credential is never selected into any response
model in this module** -- `ConnectorAccountResponse` has no field that
could carry it, matching design doc Decision 2 ("Connector creation and
status endpoints return authorization state and granted scopes, never the
credential").

**Sync is dispatched synchronously, inline in the request handler, not
through a durable worker.** Unlike Phase 5's `workflow_runs` (crash-safe,
lease-based, potentially long-running), a Task 1 sync call is a single
adapter method call against a sandbox fake -- instant, in-memory, no I/O.
`sync_runs.status` still records `running` before the call and
`succeeded`/`failed`/`partial` after, so a stuck run remains visible for
scale reasons.

**Must change before Task 2 registers a real network-calling adapter.**
This handler's entire body, including the adapter call, runs inside one
`with session.begin():` against `ecc.database.SessionFactory`, bound to
the shared app-wide `engine` pool (`pool_size=5 + max_overflow=10 = 15`
connections total, `database.py`). That is fine only because this task's
adapter call is instant -- a real GitHub/GitLab/Jira backfill (thousands of
paginated calls under rate limiting, or simply a slow/degraded provider)
would hold one of those 15 connections for the adapter's entire real-world
duration, and a handful of concurrent syncs would exhaust the pool and
start starving every unrelated endpoint in the app, not just engineering
ones. This codebase already solved the identical problem for `ai_runtime`
(`database.py`'s dedicated `NullPool` `lock_engine`, `ai_runtime/runtime.
py`'s `_held_idempotency_lock`) -- Task 2 must apply the same fix (a
dedicated connection, or moving sync dispatch off the request path
entirely into a worker) rather than carrying today's transaction shape
into a real provider call unchanged.

**Accepted limitation, disclosed rather than silently absent:**
`ConnectorAdapter.refresh_permissions` has no HTTP caller in this task --
`API-SCHEMAS.md` names no dedicated permission-refresh endpoint, and
nothing in this task's own scope needs a real permission-loss signal to
verify (the sandbox adapter's `refresh_permissions` exists so its contract
is exercised directly in tests). A later task (a scheduled reconciliation
job, most plausibly alongside real GitHub/GitLab/Jira sync) is the
intended caller; wiring it here would be speculative.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from json import dumps
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session
from ecc.observability import (
    queue_lifecycle_event,
    record_audit_outbox_failure,
    record_idempotency_conflict,
)

from .connectors import (
    AdapterAuthorizationError,
    ConnectorAccountContext,
)
from .connectors import (
    registry as connector_registry,
)
from .crypto import decrypt_credential, encrypt_credential

ConnectorStatus = Literal[
    "pending", "active", "permission_lost", "rate_limited", "disconnected", "error"
]
# The full set `sync_runs.run_type` (and `SyncRunResponse`) can hold --
# 'webhook' rows exist once a later task adds webhook ingestion, but no
# HTTP caller in this task ever writes one (see `ManualSyncRunType` below).
RunType = Literal["backfill", "incremental", "webhook"]
# `POST .../sync` is a manual-trigger endpoint only -- 'webhook' is
# deliberately excluded from the *request* body's own type (unlike `RunType`
# above), since `sync_connector_endpoint` has no code path that would ever
# accept it: the handler's own dispatch only branches on 'backfill'/
# 'incremental'. Reusing `RunType` here previously advertised 'webhook' as
# a legal request value in the generated OpenAPI schema despite every such
# request being unconditionally rejected -- narrowing this type is the fix,
# not a runtime check, so the schema itself stops overclaiming.
ManualSyncRunType = Literal["backfill", "incremental"]
RunStatus = Literal["running", "succeeded", "failed", "partial"]
# Matches migration `0044_phase6_connector_platform.py`'s
# `ck_sync_cursors_resource_type` CHECK constraint exactly -- accepting any
# string here (the previous shape) let a request reach the cursor UPSERT
# with an out-of-set value, which the CHECK constraint then rejected as an
# unhandled `IntegrityError`/500 rather than a clean 422. Validating the
# same closed set at the API boundary means a caller gets a structured
# FastAPI/Pydantic validation error instead.
ResourceType = Literal["repository", "work_item", "change", "review", "deployment", "incident"]

_MAX_ADAPTER_ERROR_LENGTH = 300


def _sanitize_adapter_error(message: str) -> str:
    """Adapter exception text is untrusted, provider-controlled content
    that ends up in workspace-visible fields (`connector_accounts.
    last_error`, `sync_runs.error_summary`, and the immediate 422 response
    body on an authorization failure). A real (non-sandbox) adapter's HTTP
    client exception commonly embeds a full request URL, headers, or an
    echoed-back rejected token -- this sandbox adapter's own fixed-string
    failure never exercises that risk, but Task 2's real GitHub/GitLab/Jira
    adapter will. This is deliberately a blunt length cap, not credential-
    shaped-substring scrubbing (which cannot be done reliably in general)
    -- the actual guarantee an adapter author must uphold is not raising a
    credential in their own exception text at all, matching `ecc.domains.
    automation.local_adapters`' identical "an adapter-author contract
    obligation the runtime cannot mechanically prove" precedent for
    `simulate()`'s side-effect-free requirement.
    """
    return message[:_MAX_ADAPTER_ERROR_LENGTH]


_ACCOUNT_FIELDS = """
    id, workspace_id, provider, external_account_id, display_name,
    granted_scopes, status, status_detail, last_synced_at, last_error,
    disconnected_at, version, created_at, updated_at
"""


@dataclass(frozen=True, slots=True)
class ConnectorAccount:
    id: UUID
    workspace_id: UUID
    provider: str
    external_account_id: str
    display_name: str
    granted_scopes: tuple[str, ...]
    status: ConnectorStatus
    status_detail: str | None
    last_synced_at: datetime | None
    last_error: str | None
    disconnected_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ConnectorAccountNotFound:
    pass


def _row_to_account(row: dict[str, Any]) -> ConnectorAccount:
    return ConnectorAccount(
        id=row["id"],
        workspace_id=row["workspace_id"],
        provider=row["provider"],
        external_account_id=row["external_account_id"],
        display_name=row["display_name"],
        granted_scopes=tuple(row["granted_scopes"]),
        status=row["status"],
        status_detail=row["status_detail"],
        last_synced_at=row["last_synced_at"],
        last_error=row["last_error"],
        disconnected_at=row["disconnected_at"],
        version=row["version"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def get_connector_account(
    session: Session, workspace_id: UUID, account_id: UUID, *, for_update: bool = False
) -> ConnectorAccount | None:
    """`for_update=True` takes `FOR UPDATE`, serializing concurrent mutators
    of the same account (mirrors `automation/policy.py`'s `revoke_policy`
    precedent). Both `sync_connector_endpoint` and `disable_connector_
    endpoint` load the account this way before mutating it: two concurrent
    `/sync` calls for the same account (different Idempotency-Keys, so the
    advisory-lock replay guard alone would not serialize them) would
    otherwise both read the same `sync_cursors` value before either
    commits, racing a lost cursor update -- this row lock closes that
    window by making the second caller block until the first transaction
    commits or rolls back.
    """
    clause = "FOR UPDATE" if for_update else ""
    row = (
        session.execute(
            text(
                f"SELECT {_ACCOUNT_FIELDS} FROM connector_accounts "
                f"WHERE workspace_id = :workspace_id AND id = :id {clause}"
            ),
            {"workspace_id": workspace_id, "id": account_id},
        )
        .mappings()
        .one_or_none()
    )
    return _row_to_account(dict(row)) if row is not None else None


def list_connector_accounts(session: Session, workspace_id: UUID) -> list[ConnectorAccount]:
    rows = (
        session.execute(
            text(
                f"SELECT {_ACCOUNT_FIELDS} FROM connector_accounts "
                "WHERE workspace_id = :workspace_id ORDER BY created_at ASC"
            ),
            {"workspace_id": workspace_id},
        )
        .mappings()
        .all()
    )
    return [_row_to_account(dict(row)) for row in rows]


def _get_encrypted_credential(session: Session, workspace_id: UUID, account_id: UUID) -> bytes:
    row = session.execute(
        text(
            "SELECT encrypted_credentials FROM connector_accounts "
            "WHERE workspace_id = :workspace_id AND id = :id"
        ),
        {"workspace_id": workspace_id, "id": account_id},
    ).one()
    result: bytes = row[0]
    return result


# --- GET|POST /api/v1/engineering/connectors, sync, disable, sync-runs -----

router = APIRouter(prefix="/api/v1/engineering", tags=["engineering"])
SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]


class ConnectorCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=1, max_length=20)
    credential: str = Field(min_length=1, max_length=4096)


class ConnectorAccountResponse(BaseModel):
    id: UUID
    provider: str
    external_account_id: str
    display_name: str
    granted_scopes: list[str]
    status: ConnectorStatus
    status_detail: str | None
    last_synced_at: datetime | None
    last_error: str | None
    disconnected_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime


class ConnectorAccountListResponse(BaseModel):
    connectors: list[ConnectorAccountResponse]


class SyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_type: ManualSyncRunType
    resource_type: ResourceType


class SyncRunResponse(BaseModel):
    id: UUID
    connector_account_id: UUID
    run_type: RunType
    status: RunStatus
    items_processed: int
    error_summary: str | None
    started_at: datetime
    completed_at: datetime | None


class SyncRunListResponse(BaseModel):
    sync_runs: list[SyncRunResponse]


class _EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _to_response(account: ConnectorAccount) -> ConnectorAccountResponse:
    return ConnectorAccountResponse(
        id=account.id,
        provider=account.provider,
        external_account_id=account.external_account_id,
        display_name=account.display_name,
        granted_scopes=list(account.granted_scopes),
        status=account.status,
        status_detail=account.status_detail,
        last_synced_at=account.last_synced_at,
        last_error=account.last_error,
        disconnected_at=account.disconnected_at,
        version=account.version,
        created_at=account.created_at,
        updated_at=account.updated_at,
    )


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
) -> dict[str, Any] | None:
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
        record_idempotency_conflict("engineering_connector_account")
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
    result: dict[str, Any] = row["response_body"]
    return result


def _store_idempotency(
    session: Session,
    auth: AuthContext,
    key: str,
    request_hash: str,
    response_body: dict[str, Any],
    now: datetime,
    response_status: int = 200,
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
            "response_body": dumps(response_body),
            "created_at": now,
            "expires_at": now + timedelta(days=365),
        },
    )


def _write_side_effects(
    session: Session,
    auth: AuthContext,
    request: Request,
    *,
    event_type: str,
    aggregate_id: UUID,
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
                    :id, :workspace_id, :event_type, 'connector_account', :aggregate_id,
                    :aggregate_version, :actor_id, :request_id, :correlation_id,
                    ARRAY['*'], 'allowed', 'user', '{}'::jsonb, :occurred_at
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": auth.workspace_id,
                "event_type": event_type,
                "aggregate_id": aggregate_id,
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
                "payload": dumps({"aggregate_id": str(aggregate_id), "version": version}),
                "occurred_at": now,
            },
        )
    except SQLAlchemyError:
        record_audit_outbox_failure("engineering_connector_account")
        raise
    queue_lifecycle_event(session, "engineering_connector_account", event_type, "allowed")


@router.get("/connectors", response_model=ConnectorAccountListResponse)
def list_connectors_endpoint(auth: AuthDep, session: SessionDep) -> ConnectorAccountListResponse:
    accounts = list_connector_accounts(session, auth.workspace_id)
    return ConnectorAccountListResponse(connectors=[_to_response(a) for a in accounts])


@router.post(
    "/connectors", response_model=ConnectorAccountResponse, status_code=status.HTTP_201_CREATED
)
def create_connector_endpoint(
    payload: ConnectorCreateRequest,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> ConnectorAccountResponse:
    request_hash = _request_hash(payload, "create_connector")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return ConnectorAccountResponse.model_validate(cached)

        adapter = connector_registry.get(payload.provider)
        if adapter is None:
            raise HTTPException(status_code=404, detail="CONNECTOR_PROVIDER_NOT_SUPPORTED")

        try:
            authorization = adapter.authorize(payload.credential)
        except AdapterAuthorizationError as exc:
            detail = {
                "code": "CONNECTOR_AUTHORIZATION_FAILED",
                "error": _sanitize_adapter_error(str(exc)),
            }
            raise HTTPException(status_code=422, detail=detail) from exc

        account_id = uuid4()
        try:
            session.execute(
                text(
                    """
                    INSERT INTO connector_accounts (
                        id, workspace_id, provider, external_account_id, display_name,
                        granted_scopes, encrypted_credentials, status, version,
                        created_by, updated_by, created_at, updated_at
                    ) VALUES (
                        :id, :workspace_id, :provider, :external_account_id, :display_name,
                        :granted_scopes, :encrypted_credentials, 'active', 1,
                        :actor_id, :actor_id, :now, :now
                    )
                    """
                ),
                {
                    "id": account_id,
                    "workspace_id": auth.workspace_id,
                    "provider": payload.provider,
                    "external_account_id": authorization.external_account_id,
                    "display_name": authorization.display_name,
                    "granted_scopes": list(authorization.granted_scopes),
                    "encrypted_credentials": encrypt_credential(payload.credential),
                    "actor_id": auth.user_id,
                    "now": now,
                },
            )
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="CONNECTOR_ALREADY_CONNECTED") from exc

        created = get_connector_account(session, auth.workspace_id, account_id)
        assert created is not None
        response = _to_response(created)
        _write_side_effects(
            session,
            auth,
            request,
            event_type="connector_account.created",
            aggregate_id=created.id,
            version=created.version,
            now=now,
        )
        _store_idempotency(
            session,
            auth,
            idempotency_key,
            request_hash,
            response.model_dump(mode="json"),
            now,
            response_status=status.HTTP_201_CREATED,
        )
        return response


@router.post(
    "/connectors/{account_id}/sync",
    response_model=SyncRunResponse,
    status_code=status.HTTP_201_CREATED,
)
def sync_connector_endpoint(
    account_id: UUID,
    payload: SyncRequest,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> SyncRunResponse:
    request_hash = _request_hash(payload, f"sync:{account_id}")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return SyncRunResponse.model_validate(cached)

        # `for_update=True`: locks this account's row for the whole
        # transaction, serializing a second concurrent `/sync` call for the
        # same account (different Idempotency-Key, so the advisory-lock
        # replay guard above does not itself serialize them) until this one
        # commits -- closes the lost-cursor-update race a plain unlocked
        # read would otherwise allow between the `cursor_row` read below and
        # the cursor UPSERT.
        account = get_connector_account(session, auth.workspace_id, account_id, for_update=True)
        if account is None:
            raise HTTPException(status_code=404, detail="CONNECTOR_NOT_FOUND")
        if account.status == "disconnected":
            raise HTTPException(status_code=409, detail="CONNECTOR_DISCONNECTED")

        adapter = connector_registry.get(account.provider)
        if adapter is None:
            raise HTTPException(status_code=404, detail="CONNECTOR_PROVIDER_NOT_SUPPORTED")

        encrypted = _get_encrypted_credential(session, auth.workspace_id, account_id)
        credential = decrypt_credential(encrypted)
        context = ConnectorAccountContext(
            workspace_id=auth.workspace_id,
            connector_account_id=account_id,
            external_account_id=account.external_account_id,
            credential=credential,
        )

        run_id = uuid4()
        session.execute(
            text(
                """
                INSERT INTO sync_runs (
                    id, workspace_id, connector_account_id, run_type, status,
                    items_processed, started_at, created_at
                ) VALUES (
                    :id, :workspace_id, :connector_account_id, :run_type, 'running',
                    0, :started_at, :started_at
                )
                """
            ),
            {
                "id": run_id,
                "workspace_id": auth.workspace_id,
                "connector_account_id": account_id,
                "run_type": payload.run_type,
                "started_at": now,
            },
        )

        cursor_row = (
            session.execute(
                text(
                    "SELECT cursor_value FROM sync_cursors WHERE workspace_id = :workspace_id "
                    "AND connector_account_id = :connector_account_id "
                    "AND resource_type = :resource_type"
                ),
                {
                    "workspace_id": auth.workspace_id,
                    "connector_account_id": account_id,
                    "resource_type": payload.resource_type,
                },
            )
            .mappings()
            .one_or_none()
        )
        prior_cursor = cursor_row["cursor_value"] if cursor_row is not None else None

        try:
            if payload.run_type == "backfill":
                outcome = adapter.backfill(context, payload.resource_type)
            else:
                outcome = adapter.incremental_sync(context, payload.resource_type, prior_cursor)
        except Exception as exc:  # noqa: BLE001 -- classified as a failed sync run, not a crash
            failure_summary = _sanitize_adapter_error(str(exc))
            completed_at = datetime.now(UTC)
            session.execute(
                text(
                    "UPDATE sync_runs SET status = 'failed', error_summary = :error, "
                    "completed_at = :completed_at WHERE id = :id"
                ),
                {"error": failure_summary, "completed_at": completed_at, "id": run_id},
            )
            session.execute(
                text(
                    "UPDATE connector_accounts SET status = 'error', last_error = :error, "
                    "updated_at = :now, updated_by = :actor_id, version = version + 1 "
                    "WHERE id = :id"
                ),
                {
                    "error": failure_summary,
                    "now": completed_at,
                    "actor_id": auth.user_id,
                    "id": account_id,
                },
            )
            run_row = (
                session.execute(
                    text(
                        "SELECT id, connector_account_id, run_type, status, items_processed, "
                        "error_summary, started_at, completed_at FROM sync_runs WHERE id = :id"
                    ),
                    {"id": run_id},
                )
                .mappings()
                .one()
            )
            response = SyncRunResponse(**dict(run_row))
            _write_side_effects(
                session,
                auth,
                request,
                event_type="connector_account.sync_failed",
                aggregate_id=account_id,
                version=account.version + 1,
                now=completed_at,
            )
            _store_idempotency(
                session, auth, idempotency_key, request_hash, response.model_dump(mode="json"), now
            )
            return response

        completed_at = datetime.now(UTC)
        # `outcome.error_summary` is adapter-returned, untrusted content
        # (same reasoning as `_sanitize_adapter_error`'s own docstring) --
        # sanitized once here and reused below rather than persisted raw.
        error_summary = (
            _sanitize_adapter_error(outcome.error_summary)
            if outcome.error_summary is not None
            else None
        )
        session.execute(
            text(
                "UPDATE sync_runs SET status = :status, items_processed = :items_processed, "
                "error_summary = :error_summary, completed_at = :completed_at WHERE id = :id"
            ),
            {
                "status": outcome.status,
                "items_processed": outcome.items_processed,
                "error_summary": error_summary,
                "completed_at": completed_at,
                "id": run_id,
            },
        )
        if outcome.next_cursor is not None:
            session.execute(
                text(
                    """
                    INSERT INTO sync_cursors (
                        id, workspace_id, connector_account_id, resource_type,
                        cursor_value, updated_at
                    ) VALUES (
                        :id, :workspace_id, :connector_account_id, :resource_type,
                        :cursor_value, :now
                    )
                    ON CONFLICT (workspace_id, connector_account_id, resource_type)
                    DO UPDATE SET cursor_value = EXCLUDED.cursor_value,
                        updated_at = EXCLUDED.updated_at
                    """
                ),
                {
                    "id": uuid4(),
                    "workspace_id": auth.workspace_id,
                    "connector_account_id": account_id,
                    "resource_type": payload.resource_type,
                    "cursor_value": outcome.next_cursor,
                    "now": completed_at,
                },
            )
        session.execute(
            text(
                "UPDATE connector_accounts SET last_synced_at = :now, last_error = :error, "
                "updated_at = :now, updated_by = :actor_id, version = version + 1 "
                "WHERE id = :id"
            ),
            {
                "now": completed_at,
                "error": error_summary,
                "actor_id": auth.user_id,
                "id": account_id,
            },
        )

        response = SyncRunResponse(
            id=run_id,
            connector_account_id=account_id,
            run_type=payload.run_type,
            status=outcome.status,
            items_processed=outcome.items_processed,
            error_summary=error_summary,
            started_at=now,
            completed_at=completed_at,
        )
        _write_side_effects(
            session,
            auth,
            request,
            event_type="connector_account.synced",
            aggregate_id=account_id,
            version=account.version + 1,
            now=completed_at,
        )
        _store_idempotency(
            session, auth, idempotency_key, request_hash, response.model_dump(mode="json"), now
        )
        return response


@router.post("/connectors/{account_id}/disable", response_model=ConnectorAccountResponse)
def disable_connector_endpoint(
    account_id: UUID,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> ConnectorAccountResponse:
    request_hash = _request_hash(_EmptyBody(), f"disable:{account_id}")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return ConnectorAccountResponse.model_validate(cached)

        # `for_update=True`: see sync_connector_endpoint's identical comment
        # -- serializes a concurrent disable/sync against the same account.
        account = get_connector_account(session, auth.workspace_id, account_id, for_update=True)
        if account is None:
            raise HTTPException(status_code=404, detail="CONNECTOR_NOT_FOUND")

        if account.status != "disconnected":
            adapter = connector_registry.get(account.provider)
            if adapter is not None:
                # Decryption and the adapter's own revocation call are both
                # best-effort here, deliberately in the same try/except --
                # a revocation attempt must never leave a connector stuck
                # unable to disconnect. Concretely: if `ECC_CONNECTOR_TOKEN_
                # ENCRYPTION_KEY` was rotated without re-encrypting stored
                # credentials, `decrypt_credential` raises `cryptography.
                # fernet.InvalidToken` -- previously that propagated
                # unhandled out of this block (decrypt happened *outside*
                # the try/except), 500ing the request and leaving the
                # connector permanently stuck in its prior status with no
                # way through this API to disconnect it. Folding decrypt
                # into the same best-effort block means "cannot even
                # attempt revocation" degrades exactly like "attempted
                # revocation and it failed" -- both still disconnect.
                try:
                    encrypted = _get_encrypted_credential(session, auth.workspace_id, account_id)
                    context = ConnectorAccountContext(
                        workspace_id=auth.workspace_id,
                        connector_account_id=account_id,
                        external_account_id=account.external_account_id,
                        credential=decrypt_credential(encrypted),
                    )
                    adapter.disconnect(context)
                except Exception:  # noqa: BLE001 -- best-effort revocation, never blocks disconnect
                    pass

            session.execute(
                text(
                    "UPDATE connector_accounts SET status = 'disconnected', "
                    "disconnected_at = :now, updated_at = :now, updated_by = :actor_id, "
                    "version = version + 1 WHERE id = :id"
                ),
                {"now": now, "actor_id": auth.user_id, "id": account_id},
            )

        updated = get_connector_account(session, auth.workspace_id, account_id)
        assert updated is not None
        response = _to_response(updated)
        _write_side_effects(
            session,
            auth,
            request,
            event_type="connector_account.disabled",
            aggregate_id=updated.id,
            version=updated.version,
            now=now,
        )
        _store_idempotency(
            session, auth, idempotency_key, request_hash, response.model_dump(mode="json"), now
        )
        return response


@router.get("/sync-runs", response_model=SyncRunListResponse)
def list_sync_runs_endpoint(
    auth: AuthDep,
    session: SessionDep,
    connector_account_id: Annotated[UUID | None, Query()] = None,
) -> SyncRunListResponse:
    clause = "AND connector_account_id = :connector_account_id" if connector_account_id else ""
    params: dict[str, Any] = {"workspace_id": auth.workspace_id}
    if connector_account_id:
        params["connector_account_id"] = connector_account_id
    rows = (
        session.execute(
            text(
                "SELECT id, connector_account_id, run_type, status, items_processed, "
                "error_summary, started_at, completed_at FROM sync_runs "
                f"WHERE workspace_id = :workspace_id {clause} ORDER BY started_at DESC"
            ),
            params,
        )
        .mappings()
        .all()
    )
    return SyncRunListResponse(sync_runs=[SyncRunResponse(**dict(row)) for row in rows])
