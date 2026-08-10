"""Connector account lifecycle (`connector_accounts`/`sync_cursors`/
`sync_runs`), plus `GET|POST /api/v1/engineering/connectors`,
`POST .../{id}/sync`, `POST .../{id}/disable` and `GET /api/v1/engineering/
sync-runs` (`docs/phases/phase-006/API-SCHEMAS.md`).

Phase 6 Task 1 added the connector-platform layer with exactly one
adapter, `sandbox.github` (no real network call). Task 2 adds the first
real adapter, `github` (`github_adapter.GitHubAdapter`) -- registered into
the same `ecc.domains.engineering.connectors.registry` this module
dispatches into. A real provider's own rate limiting/pagination/backoff
lives inside that provider's own adapter, not here -- this module's own
responsibility is account lifecycle, credential encryption, cursor
persistence and sync-run bookkeeping, identical in kind to how `ecc.
domains.automation.policy`/`approvals` own policy/approval lifecycle while
`worker.py` owns actual dispatch.

**A connector account's credential is never selected into any response
model in this module** -- `ConnectorAccountResponse` has no field that
could carry it, matching design doc Decision 2 ("Connector creation and
status endpoints return authorization state and granted scopes, never the
credential").

**Sync is dispatched synchronously, inline in the request handler, not
through a durable worker** -- unlike Phase 5's `workflow_runs` (crash-safe,
lease-based, potentially long-running), a sync call is a single adapter
method call with no multi-step graph to crash-recover mid-way through.
`sync_runs.status` still records `running` before the call and
`succeeded`/`failed`/`partial` after, so a stuck run remains visible.

**Phase 10 Task 5's proactive-detection hook (below) rides on that same
synchronous premise, imperfectly.** Unlike a plain `backfill`/`incremental_
sync` call, `GmailAdapter.detect_actions_since` -- reached via this
function's own best-effort `getattr(adapter, "detect_actions_since",
None)` hook after phase 3 -- does its own additional live Gmail-plus-
Ollama round trip per candidate message, sequentially, still inside this
same synchronous request. `detect_actions_since`'s own `_MAX_ACTION_
DETECTIONS_PER_CALL` bound (`gmail_adapter.py`, deliberately much smaller
than `backfill`/`incremental_sync`'s own `_MAX_MESSAGES_PER_CALL`) caps
how bad this gets per call, but does not make the hook itself durable or
non-blocking -- found by Loop 2 round 2 review; feature-flagged off by
default, and moving this hook to `ecc.domains.automation.worker`'s own
durable dispatch (matching `workflow_runs`, not this module's own
`sync_runs`) remains the real fix before wider production rollout.

**Pool-exhaustion fix (Task 1's own disclosed blocker for Task 2, now
resolved).** `sync_connector_endpoint` no longer holds one `session.
begin()` transaction across the entire adapter call. It now runs in three
phases on two separate pooled connections: (1) validate + reserve the
run on the request's own injected `session`, (2) explicitly `session.
close()` -- releasing that connection back to the shared, `pool_size=5 +
max_overflow=10 = 15`-connection-capped `engine` pool -- *before* calling
`adapter.backfill`/`incremental_sync`, so a real provider's HTTP call
(seconds, not microseconds) never holds a pooled connection hostage, and
(3) record the outcome on a fresh `SessionFactory()` session. See that
function's own docstring for the phase-by-phase detail. This mirrors, in
spirit, this codebase's existing `ai_runtime` `NullPool` `lock_engine`
precedent (`database.py`) -- a different mechanism (explicit close/reopen
rather than a second engine), chosen because this handler's short
bookkeeping transactions don't need `NullPool`'s "never pool this
connection at all" property, only "don't hold a pooled connection during
the slow part."

**Closing a pooled connection between phases also releases the
serialization it used to provide, so three narrower guarantees replace
the one broad lock this restructuring removed:** `uq_sync_runs_running_
per_account` (migration `0046`) makes phase 1's own `INSERT INTO
sync_runs (...status='running'...)` the serialization point -- a second
`/sync` call for the same account (concurrent, or a same-Idempotency-Key
retry racing ahead of the first call's not-yet-written idempotency
record) cannot itself reach a `running` row until the first call's phase
3 has already moved its row out of `running`, so it gets `409
CONNECTOR_SYNC_IN_PROGRESS` from the resulting `IntegrityError` rather
than either double-executing the adapter call or racing the `sync_
cursors` UPSERT. Phase 3's `connector_accounts` UPDATEs additionally
guard `AND status != 'disconnected'` (a concurrent `/disable` can still
commit during phase 2, and must win, never be clobbered back by a
stale sync outcome) and read the account's actual post-UPDATE `version`
via `RETURNING` (`_finalize_account_version`) rather than recomputing it
from phase 1's now-stale in-memory snapshot.

**A `running` row this same fix could otherwise wedge forever, also
fixed.** Making a `running` row the serialization point (above) has its
own failure mode a single spanning transaction never had: if the process
crashes/is killed between phase 1's commit and phase 3, nothing ever
rolls that row back, permanently blocking every future `/sync` call for
the account behind a `running` row nothing will ever complete. Phase 1
reaps any `running` row for this account older than
`_STALE_RUNNING_SYNC_THRESHOLD` (marking it `failed`) before attempting
its own `INSERT`, bounded generously above any legitimate call's real
duration so a genuinely still-in-flight sync is never touched.

**`create_connector_endpoint` needed the identical treatment for its own
`Idempotency-Key` contract**, not just the pool-exhaustion fix -- a
same-key retry racing ahead of the first call's phase 3 was returning
`409 CONNECTOR_ALREADY_CONNECTED` instead of the replayed response the
`Idempotency-Key` contract promises. Since phase 3's `INSERT` is the only
place `uq_connector_accounts_workspace_provider_external_id` can conflict
(unlike `/sync`'s phase-1 conflict point), a losing `IntegrityError` here
*does* guarantee the winner's whole phase 3 (including
`_store_idempotency`) already committed -- so the `except IntegrityError`
handler re-checks the idempotency cache before concluding this is a
genuine duplicate connection. See that function's own docstring.

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
from ecc.database import SessionFactory, get_session
from ecc.observability import (
    queue_lifecycle_event,
    record_audit_outbox_failure,
    record_idempotency_conflict,
)
from ecc.platform import authz

from .connectors import (
    AdapterAuthorizationError,
    ConnectorAccountContext,
    ConnectorAdapter,
    SyncOutcome,
)
from .connectors import (
    registry as connector_registry,
)
from .crypto import decrypt_credential, encrypt_credential
from .metrics import compute_and_store_metrics

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
# `ck_sync_cursors_resource_type` CHECK constraint exactly (widened by
# migration `0051_phase6_datadog_connector.py` to add `monitor`/
# `service_definition`/`dashboard`) -- accepting any string here (the
# previous shape) let a request reach the cursor UPSERT with an
# out-of-set value, which the CHECK constraint then rejected as an
# unhandled `IntegrityError`/500 rather than a clean 422. Validating the
# same closed set at the API boundary means a caller gets a structured
# FastAPI/Pydantic validation error instead.
ResourceType = Literal[
    "repository",
    "work_item",
    "change",
    "review",
    "deployment",
    "incident",
    "monitor",
    "service_definition",
    "dashboard",
    # Phase 10 Gmail Connector Task 2: the only resource type `gmail`
    # accounts ever sync against this generic endpoint -- threads are
    # upserted implicitly as a side effect of syncing the messages within
    # them (see `gmail_adapter.py`'s own module docstring), matching this
    # Literal's existing convention of naming the leaf resource a caller
    # actually requests, not every table a sync call happens to write to.
    "message",
]

_MAX_ADAPTER_ERROR_LENGTH = 300
# `uq_sync_runs_running_per_account` (migration 0046) makes a `running`
# `sync_runs` row the thing that serializes concurrent syncs -- but unlike
# the single-transaction handler it replaced, a process crash/kill between
# phase 1's commit and phase 3's outcome-recording (network partition, pod
# eviction, a hard worker timeout) no longer auto-rolls back that row via
# transaction abort. Left unhandled, that would permanently wedge every
# future `/sync` call for the account behind a `running` row nothing will
# ever complete -- a real availability regression security review found in
# the phase restructuring, not a pre-existing/accepted limitation. Bounded
# generously above any legitimate call's real duration (bounded rate-limit
# wait is <=5s, `_MAX_PAGES_PER_CALL` bounds one call's own pagination to a
# handful of HTTP round trips -- real syncs complete in seconds, not
# minutes), so this reaps only genuinely-abandoned rows, never a slow but
# still-in-flight one.
#
# **Accepted limitation, disclosed rather than silently generalized:**
# this bound is justified by `github_adapter.GitHubAdapter`'s own
# contract-level guarantees, not any invariant `ConnectorAdapter` itself
# promises -- a future adapter (GitLab/Jira, Task 3/4, or a later
# GitHub follow-up removing `_MAX_PAGES_PER_CALL`'s cap) whose legitimate
# single call can genuinely run longer than this threshold would have its
# still-in-flight row reaped and a second, genuinely concurrent adapter
# call dispatched against the same account/cursor -- reintroducing the
# lost-cursor-update race this whole mechanism exists to prevent. Revisit
# this constant (or make it adapter-declared) before registering an
# adapter without an equivalent per-call duration bound. Phase 3's own
# `AND status = 'running'` guards (below) at least ensure a late-arriving
# outcome for an already-reaped run can never silently overwrite the
# reaper's recorded status -- the narrower, always-safe half of this
# problem -- even though they do not prevent the concurrent-call overlap
# itself.
_STALE_RUNNING_SYNC_THRESHOLD = timedelta(minutes=10)


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


def _personal_email_domain_exists(session: Session, workspace_id: UUID, account_id: UUID) -> bool:
    """Loop 2 round 25 review finding (MEDIUM), `disable_connector_
    endpoint`'s own `gmail`-provider gate: whether this specific `gmail`
    connector account's owner has any `personal_domains` row for `email`
    at all -- not whether the domain is *enabled*, which `_disable_domain`
    below the redirect this gate points to already checks in its own
    right. Joins through `connector_accounts.owner_id` (migration `0063`)
    rather than taking an `owner_id` parameter directly, since the only
    caller has an `account_id`, not an owner, in hand.
    """
    return (
        session.execute(
            text(
                "SELECT 1 FROM personal_domains pd "
                "JOIN connector_accounts ca ON ca.workspace_id = pd.workspace_id "
                "AND ca.owner_id = pd.owner_id "
                "WHERE ca.id = :account_id AND ca.workspace_id = :workspace_id "
                "AND pd.domain_key = 'email' LIMIT 1"
            ),
            {"account_id": account_id, "workspace_id": workspace_id},
        ).first()
        is not None
    )


def list_connector_accounts(
    session: Session,
    workspace_id: UUID,
    *,
    visibility_sql: str = "TRUE",
    visibility_params: dict[str, Any] | None = None,
) -> list[ConnectorAccount]:
    """`visibility_sql`/`visibility_params` default to no filtering (every
    row in the workspace) so this stays a plain workspace-scoped list for
    any caller that doesn't need authz filtering; `list_connectors_
    endpoint` is this module's own authz-aware caller, passing `ecc.
    platform.authz.visible_resource_filter_sql`'s own output through.
    """
    rows = (
        session.execute(
            text(
                f"SELECT {_ACCOUNT_FIELDS} FROM connector_accounts "  # noqa: S608
                f"WHERE workspace_id = :workspace_id AND ({visibility_sql}) "
                "ORDER BY created_at ASC"
            ),
            {"workspace_id": workspace_id, **(visibility_params or {})},
        )
        .mappings()
        .all()
    )
    return [_row_to_account(dict(row)) for row in rows]


def _finalize_account_version(
    session: Session, account_id: UUID, *, update_sql: str, params: dict[str, Any]
) -> int:
    """`update_sql` must include both `AND status != 'disconnected'` (so a
    concurrent `/disable` that already committed between phase 1 and this
    call is never clobbered back to `'error'`/re-stamped by a stale sync
    outcome) and `RETURNING version`. Returns the account's actual current
    version for the audit trail -- reading the real post-UPDATE value
    (or, when the guard skipped the update, a fresh `SELECT`) rather than
    the caller re-deriving it from phase 1's now-stale `account.version +
    1` snapshot, which review found could diverge from the database's
    actual value once any other write landed on this account between
    phase 1 and phase 3.
    """
    row = session.execute(text(update_sql), params).mappings().one_or_none()
    if row is not None:
        return int(row["version"])
    return int(
        session.execute(
            text("SELECT version FROM connector_accounts WHERE id = :id"),
            {"id": account_id},
        ).scalar_one()
    )


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
    """`credential`'s shape is provider-defined and opaque to this layer
    (`connector_accounts.encrypted_credentials` stores it as-is, encrypted).
    GitHub: a bare token (`github_adapter.py`'s `_headers`, `Bearer
    {credential}`). Jira: `site|email|api_token` (`site` a bare
    `{subdomain}.atlassian.net` hostname -- `jira_adapter.py`'s own
    `_parse_credential`). GitLab: `host|token` (`host` a bare hostname --
    `gitlab.com` or a self-managed instance, e.g. `gitlab-ee.example.com`;
    never a scheme/port/path -- `gitlab_adapter.py`'s own `_parse_
    credential`). Datadog: `site|api_key|app_key` (`site` one of Datadog's
    documented regional API hosts, e.g. `api.datadoghq.com` --
    `datadog_adapter.py`'s own `_parse_credential`).
    """

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


class MetricSnapshotResponse(BaseModel):
    """See `metrics.py`'s own module docstring for `coverage_status`/
    `coverage_percentage`/`coverage_gap_description`'s meaning, and for
    why `value`/`numerator`/`denominator`/`details` are all `None` for a
    metric this activation cannot yet compute (`delivery_frequency`,
    `lead_time_for_changes`, `change_failure_rate` -- `time_to_restore`
    became real as of Phase 6 Task 6).
    """

    id: UUID
    metric_key: str
    window_label: str
    population: int
    numerator: float | None
    denominator: float | None
    value: float | None
    details: dict[str, Any] | None
    coverage_status: Literal["complete", "partial", "insufficient_coverage"]
    coverage_percentage: float
    coverage_gap_description: str | None
    computed_at: datetime


class MetricsListResponse(BaseModel):
    metrics: list[MetricSnapshotResponse]


# Phase 6 Task 8 ("Executive UX and browser acceptance") -- neither this
# plan doc's own route sketch nor any of Tasks 1-7 named a GET query
# surface for `repositories`/`engineering_work_items`, even though Task 2
# has synced `repositories` since migration `0045_phase6_repositories.py`
# and Task 4 has synced `engineering_work_items` since migration
# `0047_phase6_work_items.py`. The Repositories/Source Coverage UX
# surfaces this task builds have nothing to query without these two thin
# list endpoints -- the same "add the query endpoint the UX genuinely
# needs" disclosed-scope pattern every one of Tasks 5-7 already used once
# (`GET /engineering/metrics`, `POST /engineering/incidents`, the write
# adapters themselves).
class RepositoryResponse(BaseModel):
    id: UUID
    connector_account_id: UUID
    provider: str
    external_id: str
    name: str
    source_url: str
    default_branch: str | None
    permission_state: Literal["active", "permission_lost", "deleted"]
    freshness_state: Literal["fresh", "stale", "disconnected"]
    provider_updated_at: datetime | None
    observed_at: datetime
    created_at: datetime
    updated_at: datetime
    # Team linkage (migration `0050_phase6_team_linkage.py`) -- see that
    # migration's own docstring for the "hybrid: auto-suggest, human
    # confirms" design. `suggested_team_name` is refreshed by the owning
    # adapter on every sync; `team_entity_id` is set only through `POST
    # .../repositories/{id}/team` below and never touched by a sync.
    # `team_assignment_version`/`team_assignment_updated_by` are that same
    # endpoint's own optimistic-concurrency/audit fields -- scoped to this
    # one field, not the whole row, since the row's other columns keep
    # being refreshed by sync (see the migration's own docstring for why a
    # whole-row version would spuriously conflict with a sync landing in
    # between).
    team_entity_id: UUID | None
    suggested_team_name: str | None
    team_assignment_version: int
    team_assignment_updated_by: UUID | None


class RepositoryListResponse(BaseModel):
    repositories: list[RepositoryResponse]


class WorkItemResponse(BaseModel):
    id: UUID
    connector_account_id: UUID
    provider: str
    external_id: str
    title: str
    source_url: str
    item_type: str | None
    status: str | None
    reporter_external_id: str | None
    assignee_external_id: str | None
    permission_state: Literal["active", "permission_lost", "deleted"]
    freshness_state: Literal["fresh", "stale", "disconnected"]
    provider_created_at: datetime | None
    observed_at: datetime
    created_at: datetime
    updated_at: datetime
    # See `RepositoryResponse`'s identical fields above.
    team_entity_id: UUID | None
    suggested_team_name: str | None
    team_assignment_version: int
    team_assignment_updated_by: UUID | None


class WorkItemListResponse(BaseModel):
    work_items: list[WorkItemResponse]


# --- Team suggestions review (docs/superpowers/specs/2026-08-06-team-
# suggestions-review-page-design.md) --------------------------------------


class TeamSuggestionSampleItem(BaseModel):
    id: UUID
    resource_type: Literal["repository", "work_item"]
    name: str


class TeamSuggestionGroup(BaseModel):
    suggested_team_name: str
    repository_count: int
    work_item_count: int
    sample_items: list[TeamSuggestionSampleItem]


class TeamSuggestionListResponse(BaseModel):
    items: list[TeamSuggestionGroup]


class TeamSuggestionConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suggested_team_name: str = Field(min_length=1)
    team_entity_id: UUID


class TeamSuggestionDismissRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suggested_team_name: str = Field(min_length=1)


class TeamSuggestionActionResponse(BaseModel):
    updated: list[UUID]
    skipped_unauthorized: list[UUID]


_TEAM_SUGGESTION_SAMPLE_CAP = 5


# --- Datadog projections (monitors/service definitions/dashboards) -----
# Migration `0051_phase6_datadog_connector.py`'s own three new tables --
# deliberately no `team_assignment_version`/`team_assignment_updated_by`
# pair on any of these three response models, mirroring that migration's
# own disclosed reason: no confirm-endpoint exists yet for these tables,
# so a version column no endpoint ever bumps would be exactly as
# unreachable as the fields it would sit next to. `team_entity_id`/
# `suggested_team_name` are still surfaced read-only (queryable via the
# `team_entity_id` filter below), for the same "linkage exists, confirm
# flow is a disclosed follow-up" reasoning as everywhere else in this file.
class MonitorResponse(BaseModel):
    id: UUID
    connector_account_id: UUID
    provider: str
    external_id: str
    source_url: str
    name: str
    monitor_type: str | None
    query: str | None
    overall_state: str | None
    permission_state: Literal["active", "permission_lost", "deleted"]
    freshness_state: Literal["fresh", "stale", "disconnected"]
    provider_updated_at: datetime | None
    observed_at: datetime
    created_at: datetime
    updated_at: datetime
    team_entity_id: UUID | None
    suggested_team_name: str | None


class MonitorListResponse(BaseModel):
    monitors: list[MonitorResponse]


class ServiceDefinitionResponse(BaseModel):
    id: UUID
    connector_account_id: UUID
    provider: str
    external_id: str
    source_url: str
    name: str
    team: str | None
    tier: str | None
    description: str | None
    permission_state: Literal["active", "permission_lost", "deleted"]
    freshness_state: Literal["fresh", "stale", "disconnected"]
    observed_at: datetime
    created_at: datetime
    updated_at: datetime
    team_entity_id: UUID | None
    suggested_team_name: str | None


class ServiceDefinitionListResponse(BaseModel):
    service_definitions: list[ServiceDefinitionResponse]


class DashboardResponse(BaseModel):
    id: UUID
    connector_account_id: UUID
    provider: str
    external_id: str
    source_url: str
    title: str
    description: str | None
    permission_state: Literal["active", "permission_lost", "deleted"]
    freshness_state: Literal["fresh", "stale", "disconnected"]
    provider_updated_at: datetime | None
    observed_at: datetime
    created_at: datetime
    updated_at: datetime
    team_entity_id: UUID | None
    suggested_team_name: str | None


class DashboardListResponse(BaseModel):
    dashboards: list[DashboardResponse]


class TeamAssignmentRequest(BaseModel):
    """Body for `POST .../repositories/{id}/team` and `POST .../work-items/
    {id}/team` -- the "human confirms" half of migration `0050_phase6_
    team_linkage.py`'s hybrid design. `team_entity_id: None` clears an
    existing assignment (a human can change their mind or unassign, not
    just assign once); a UUID must reference an active `kind="team"`
    `pkos_nodes` row in the caller's own workspace, checked by `_validate_
    team_entity` below -- the same "existence + kind, checked at write
    time" precedent `waiting.py`'s `_counterparty_node_type` already
    established for `waiting_links.counterparty_entity_id`.

    `expected_version` mirrors `entities_mutations.py`'s `EntityPatch`
    exactly (checked against `team_assignment_version`, the field-scoped
    counter migration `0050`'s own docstring explains) -- review found the
    first draft of this endpoint had no optimistic-concurrency check at
    all, unlike every other mutating endpoint in this codebase.
    """

    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    team_entity_id: UUID | None = None


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
                    changed_fields, authorization_result, source, metadata, occurred_at,
                    owner_id, visibility
                ) VALUES (
                    :id, :workspace_id, :event_type, 'connector_account', :aggregate_id,
                    :aggregate_version, :actor_id, :request_id, :correlation_id,
                    ARRAY['*'], 'allowed', 'user', '{}'::jsonb, :occurred_at,
                    :actor_id, 'workspace'
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
    visibility_sql, visibility_params = authz.visible_resource_filter_sql(
        session,
        auth,
        resource_type="connector_accounts",
        action="read",
        table_alias="connector_accounts",
    )
    accounts = list_connector_accounts(
        session,
        auth.workspace_id,
        visibility_sql=visibility_sql,
        visibility_params=visibility_params,
    )
    session.rollback()
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
    """Two phases across two pooled connections, mirroring `sync_connector_
    endpoint`'s own pool-exhaustion fix. `adapter.authorize()` is a real
    outbound HTTP call (e.g. GitHub's `GET /user`) with the same latency
    profile as `backfill`/`incremental_sync` -- holding `session`'s pooled
    connection across it would reintroduce, for connector creation, the
    exact pool-exhaustion risk `/sync` was restructured to avoid.

    1. Validate the provider and check the idempotency cache -- inside
       `session`'s own short transaction.
    2. Close `session`, then call `adapter.authorize()` with no pooled
       connection held.
    3. Insert the new connector account on a fresh session.

    A genuine same-Idempotency-Key retry racing ahead of the first
    request's own phase 3 (e.g. a client-side timeout during a slow
    `authorize()` call, retried with the same key) reaches phase 3's
    `INSERT` concurrently with the original request. Postgres blocks a
    concurrent conflicting `INSERT` on `uq_connector_accounts_workspace_
    provider_external_id` until the transaction holding the not-yet-
    committed conflicting row commits or rolls back -- so as long as the
    winner's entire phase 3 (`INSERT` + `_write_side_effects` +
    `_store_idempotency`) is genuinely **one transaction** (the `INSERT`
    runs inside a `begin_nested()` SAVEPOINT of the same outer transaction,
    not a separate top-level transaction of its own), a losing
    `IntegrityError` can only fire *after* the winner has already stored
    its idempotency record. So the `except IntegrityError` handler below
    re-checks the idempotency cache before
    concluding this is a genuine "already connected" duplicate: if found,
    it returns the winner's real response (a correct idempotent replay,
    matching every other endpoint's `Idempotency-Key` contract) instead of
    a `409` the retrying client did not earn. Only an actual pre-existing
    connection (no matching cached response) still gets `409
    CONNECTOR_ALREADY_CONNECTED`. This same reasoning does not transfer to
    `/sync`'s own `409 CONNECTOR_SYNC_IN_PROGRESS` -- there, the conflicting
    unique row is written in *phase 1*, before the slow adapter call, so a
    losing retry's conflict gives no such guarantee the winner has finished
    -- that 409 is a genuine "come back later," not a replayable result.
    """
    authz.require_role_action(session, auth, "write")
    request_hash = _request_hash(payload, "create_connector")
    now = datetime.now(UTC)

    # --- Phase 1: idempotency check, provider validation ------------------
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return ConnectorAccountResponse.model_validate(cached)

        adapter = connector_registry.get(payload.provider)
        if adapter is None:
            raise HTTPException(status_code=404, detail="CONNECTOR_PROVIDER_NOT_SUPPORTED")

    # Phase 1's transaction has committed. Release the connection back to
    # the pool before the (potentially slow) adapter call below.
    session.close()

    # --- Phase 2: the adapter call -- no pooled connection held here ------
    try:
        authorization = adapter.authorize(payload.credential)
    except AdapterAuthorizationError as exc:
        detail = {
            "code": "CONNECTOR_AUTHORIZATION_FAILED",
            "error": _sanitize_adapter_error(str(exc)),
        }
        raise HTTPException(status_code=422, detail=detail) from exc

    # --- Phase 3: persist the account, on a fresh connection --------------
    account_id = uuid4()
    with SessionFactory() as create_session, create_session.begin():
        try:
            # A SAVEPOINT (`begin_nested`), not a second top-level
            # transaction -- the INSERT below must stay inside the SAME
            # outer transaction as the audit/idempotency-store work later
            # in this block. Postgres blocks a concurrent conflicting
            # INSERT until the transaction that owns the not-yet-committed
            # conflicting row commits or rolls back; if that INSERT and
            # this request's own audit/idempotency write were two separate
            # transactions (as an earlier version of this fix had it), the
            # first (insert-only) transaction could commit -- unblocking a
            # competing insert -- before the second transaction (the
            # idempotency store) had even started, defeating the
            # `except IntegrityError` cache re-check below. Keeping both
            # in one transaction is what makes "a losing IntegrityError
            # here proves the winner's idempotency record already
            # committed" true rather than merely asserted.
            with create_session.begin_nested():
                create_session.execute(
                    text(
                        """
                        INSERT INTO connector_accounts (
                            id, workspace_id, provider, external_account_id, display_name,
                            granted_scopes, encrypted_credentials, status, version,
                            created_by, updated_by, created_at, updated_at,
                            owner_id, visibility
                        ) VALUES (
                            :id, :workspace_id, :provider, :external_account_id, :display_name,
                            :granted_scopes, :encrypted_credentials, 'active', 1,
                            :actor_id, :actor_id, :now, :now,
                            :actor_id, 'workspace'
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
        except IntegrityError:
            # `begin_nested()`'s own `__exit__` already rolled back to the
            # SAVEPOINT on the exception above -- the outer transaction
            # itself is still open and healthy, so a fresh read here is
            # safe. See this function's own docstring for why re-checking
            # the cache (rather than assuming this is a genuine duplicate)
            # is correct specifically here.
            cached_after_conflict = _load_cached(
                create_session, auth, idempotency_key, request_hash
            )
            if cached_after_conflict is not None:
                return ConnectorAccountResponse.model_validate(cached_after_conflict)
            raise HTTPException(status_code=409, detail="CONNECTOR_ALREADY_CONNECTED") from None

        created = get_connector_account(create_session, auth.workspace_id, account_id)
        assert created is not None
        response = _to_response(created)
        _write_side_effects(
            create_session,
            auth,
            request,
            event_type="connector_account.created",
            aggregate_id=created.id,
            version=created.version,
            now=now,
        )
        _store_idempotency(
            create_session,
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
    """Three phases, deliberately on **two separate pooled connections**
    rather than one transaction spanning the whole handler -- the fix
    Task 1's own module docstring flagged as required before a real,
    network-calling adapter (`github_adapter.GitHubAdapter`, Task 2)
    could be dispatched from here:

    1. Validate the account/adapter, read the prior cursor, and insert the
       `running` `sync_runs` row -- inside `session`'s own transaction.
    2. Close `session` explicitly (releasing its connection back to the
       engine pool) *before* calling `adapter.backfill`/`incremental_sync`
       -- a real adapter's HTTP call can legitimately take seconds; no
       pooled connection is held for that duration, so a handful of
       concurrent syncs can no longer starve every unrelated endpoint's
       connection pool the way holding `session` open across that call
       would.
    3. Record the outcome on a **fresh** session (`SessionFactory()`, a new
       connection from the same pool, held only for this short bookkeeping
       transaction) -- never the closed `session` from phase 1.

    A real adapter's own domain-projection writes (e.g. `github_adapter.
    _upsert_repository`) already follow this same discipline independently
    -- see that function's own docstring.

    Raises `409 CONNECTOR_SYNC_IN_PROGRESS` if another sync for this
    account is still `running` (`uq_sync_runs_running_per_account`,
    migration `0046`) -- see the module docstring's "closing a pooled
    connection between phases" section for why this, not a held lock,
    is what serializes concurrent syncs here.
    """
    if not authz.authorize(
        session, auth, resource_type="connector_accounts", resource_id=account_id, action="read"
    ):
        session.rollback()
        raise HTTPException(status_code=404, detail="CONNECTOR_NOT_FOUND")
    if not authz.authorize(
        session, auth, resource_type="connector_accounts", resource_id=account_id, action="write"
    ):
        session.rollback()
        raise HTTPException(status_code=403, detail="INSUFFICIENT_ROLE")
    # `authorize()` never touches transaction state itself (it is also
    # called from *inside* an open transaction elsewhere in this
    # codebase) -- these two pre-checks each autobegin their own read-only
    # transaction, which must be rolled back before phase 1's own `with
    # session.begin():` below, or SQLAlchemy raises `InvalidRequestError:
    # A transaction is already begun on this Session`.
    session.rollback()

    request_hash = _request_hash(payload, f"sync:{account_id}")
    now = datetime.now(UTC)

    # --- Phase 1: validate, reserve the run, read the cursor -------------
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return SyncRunResponse.model_validate(cached)

        # `for_update=True`: locks this account's row for this short
        # transaction, serializing a second concurrent `/sync` call for the
        # same account (different Idempotency-Key, so the advisory-lock
        # replay guard above does not itself serialize them) -- closes the
        # lost-cursor-update race a plain unlocked read would otherwise
        # allow between the `cursor_row` read below and the cursor UPSERT
        # in phase 3.
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

        # Reap a `running` row this account's own *own* earlier request left
        # behind by crashing/being killed between phase 1's commit and phase
        # 3's outcome-recording (a real availability regression: unlike the
        # single-transaction handler this replaced, that window is no
        # longer auto-rolled-back by a transaction abort). Scoped to rows
        # older than `_STALE_RUNNING_SYNC_THRESHOLD` so a genuinely still-
        # in-flight concurrent sync is never touched -- see that constant's
        # own docstring.
        session.execute(
            text(
                """
                UPDATE sync_runs SET status = 'failed',
                    error_summary = 'reaped: no outcome recorded within the stale threshold',
                    completed_at = :now
                WHERE workspace_id = :workspace_id AND connector_account_id = :connector_account_id
                    AND status = 'running' AND started_at < :stale_before
                """
            ),
            {
                "now": now,
                "workspace_id": auth.workspace_id,
                "connector_account_id": account_id,
                "stale_before": now - _STALE_RUNNING_SYNC_THRESHOLD,
            },
        )

        run_id = uuid4()
        try:
            session.execute(
                text(
                    """
                    INSERT INTO sync_runs (
                        id, workspace_id, connector_account_id, run_type, status,
                        items_processed, started_at, created_at, owner_id, visibility
                    ) VALUES (
                        :id, :workspace_id, :connector_account_id, :run_type, 'running',
                        0, :started_at, :started_at, :actor_id, 'workspace'
                    )
                    """
                ),
                {
                    "id": run_id,
                    "workspace_id": auth.workspace_id,
                    "connector_account_id": account_id,
                    "run_type": payload.run_type,
                    "started_at": now,
                    "actor_id": auth.user_id,
                },
            )
        except IntegrityError as exc:
            # `uq_sync_runs_running_per_account` (migration 0046): another
            # sync for this account is still `running`. This INSERT is the
            # only place that row can be created, so this is also what
            # closes the lost-cursor-update and idempotency-replay races a
            # bare `FOR UPDATE` lock can no longer prevent by itself once
            # this handler releases its connection before phase 2's slow
            # adapter call -- see this function's own docstring.
            raise HTTPException(status_code=409, detail="CONNECTOR_SYNC_IN_PROGRESS") from exc

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

    # Phase 1's transaction has committed. Release the connection back to
    # the pool before the (potentially slow) adapter call in phase 2 --
    # the actual pool-exhaustion fix; see this function's own docstring.
    session.close()

    # --- Phase 2: the adapter call -- no pooled connection held here -----
    context = ConnectorAccountContext(
        workspace_id=auth.workspace_id,
        connector_account_id=account_id,
        external_account_id=account.external_account_id,
        credential=credential,
    )
    adapter_failed = False
    outcome: SyncOutcome | None = None
    failure_summary: str | None = None
    try:
        if payload.run_type == "backfill":
            outcome = adapter.backfill(context, payload.resource_type)
        else:
            outcome = adapter.incremental_sync(context, payload.resource_type, prior_cursor)
    except Exception as exc:  # noqa: BLE001 -- classified as a failed sync run, not a crash
        adapter_failed = True
        failure_summary = _sanitize_adapter_error(str(exc))

    # --- Phase 3: record the outcome, on a fresh connection ---------------
    completed_at = datetime.now(UTC)
    with SessionFactory() as outcome_session, outcome_session.begin():
        if adapter_failed:
            # `AND status = 'running'`: see the success-path UPDATE's
            # identical comment below -- must not silently overwrite a
            # reaper's already-recorded terminal status.
            outcome_session.execute(
                text(
                    "UPDATE sync_runs SET status = 'failed', error_summary = :error, "
                    "completed_at = :completed_at WHERE id = :id AND status = 'running'"
                ),
                {"error": failure_summary, "completed_at": completed_at, "id": run_id},
            )
            audit_version = _finalize_account_version(
                outcome_session,
                account_id,
                update_sql=(
                    "UPDATE connector_accounts SET status = 'error', last_error = :error, "
                    "updated_at = :now, updated_by = :actor_id, version = version + 1 "
                    "WHERE id = :id AND status != 'disconnected' "
                    "RETURNING version"
                ),
                params={
                    "error": failure_summary,
                    "now": completed_at,
                    "actor_id": auth.user_id,
                    "id": account_id,
                },
            )
            run_row = (
                outcome_session.execute(
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
                outcome_session,
                auth,
                request,
                event_type="connector_account.sync_failed",
                aggregate_id=account_id,
                version=audit_version,
                now=completed_at,
            )
            _store_idempotency(
                outcome_session,
                auth,
                idempotency_key,
                request_hash,
                response.model_dump(mode="json"),
                now,
            )
            return response

        assert outcome is not None
        # `outcome.error_summary` is adapter-returned, untrusted content
        # (same reasoning as `_sanitize_adapter_error`'s own docstring) --
        # sanitized once here and reused below rather than persisted raw.
        error_summary = (
            _sanitize_adapter_error(outcome.error_summary)
            if outcome.error_summary is not None
            else None
        )
        # `AND status = 'running'`: if this call's own `sync_runs` row was
        # already reaped as stale (`_STALE_RUNNING_SYNC_THRESHOLD`) by a
        # later request before this adapter call finally returned, this
        # UPDATE must not silently overwrite the reaper's terminal status
        # with a late-arriving outcome -- that would mask the overlap this
        # row's `failed`/reaped status exists to record. The `SELECT`
        # below reads whichever status actually won (this update's own, or
        # the reaper's), so the response returned always matches the
        # database rather than trusting the locally-held `outcome`.
        outcome_session.execute(
            text(
                "UPDATE sync_runs SET status = :status, items_processed = :items_processed, "
                "error_summary = :error_summary, completed_at = :completed_at "
                "WHERE id = :id AND status = 'running'"
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
            outcome_session.execute(
                text(
                    """
                    INSERT INTO sync_cursors (
                        id, workspace_id, connector_account_id, resource_type,
                        cursor_value, updated_at, owner_id, visibility
                    ) VALUES (
                        :id, :workspace_id, :connector_account_id, :resource_type,
                        :cursor_value, :now, :actor_id, 'workspace'
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
                    "actor_id": auth.user_id,
                },
            )
        audit_version = _finalize_account_version(
            outcome_session,
            account_id,
            update_sql=(
                "UPDATE connector_accounts SET last_synced_at = :now, last_error = :error, "
                "updated_at = :now, updated_by = :actor_id, version = version + 1 "
                "WHERE id = :id AND status != 'disconnected' "
                "RETURNING version"
            ),
            params={
                "now": completed_at,
                "error": error_summary,
                "actor_id": auth.user_id,
                "id": account_id,
            },
        )

        run_row = (
            outcome_session.execute(
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
            outcome_session,
            auth,
            request,
            event_type="connector_account.synced",
            aggregate_id=account_id,
            version=audit_version,
            now=completed_at,
        )
        _store_idempotency(
            outcome_session,
            auth,
            idempotency_key,
            request_hash,
            response.model_dump(mode="json"),
            now,
        )

    # Phase 10 Task 5: proactive Gmail action detection, run only after
    # phase 3's own transaction has committed -- `detect_actions_since`
    # opens its own short, per-message sessions/transactions, matching
    # `gmail_adapter._resolve_or_create_person`'s "keep this independent of
    # whatever transaction writes the triggering row" discipline, not
    # `outcome_session`'s. Duck-typed via `getattr`, not a shared Protocol
    # member (`gmail_adapter.py`'s own module docstring: Gmail-specific
    # surface like `is_account_allowed` is deliberately not part of either
    # Protocol) -- this also avoids `connector_accounts.py` (engineering
    # domain) importing `gmail_adapter.py` (personal domain) at module
    # level, which would be a domain-layering back-reference (engineering
    # depending on personal, on top of personal already depending on
    # engineering's own `connectors` module for its Protocol types) rather
    # than a clean one-directional dependency. Round 9 review: not an
    # actual circular import -- `ecc.domains.engineering.connectors`
    # itself imports neither `connector_accounts.py` nor `gmail_adapter.py`
    # (verified against its own import list), so this specific `getattr`
    # would not raise `ImportError` even at module level. Kept duck-typed
    # regardless, for the layering reason above and to keep
    # `connector_accounts.py` provider-agnostic the way it already is for
    # GitHub/GitLab/Jira/Datadog. Best-effort:
    # any failure here (a transient Gmail API error, a malformed body) is
    # swallowed rather than failing a sync response that has already fully
    # succeeded and been recorded -- the next sync call's own newly-
    # inserted messages are unaffected, and a skipped message is simply
    # never proactively scanned, not silently corrupted.
    #
    # Not gated on `outcome.items_processed > 0` -- round 4 review found
    # that gate silently defeated `detect_actions_since`'s own "a backlog
    # beyond one call's own bound is worked down over subsequent calls"
    # guarantee (that method's own docstring, added by round 3's fix):
    # `items_processed` counts messages *this* `backfill`/`incremental_
    # sync` call fetched (new or duplicate), so on any ordinary poll where
    # Gmail reports no activity -- ordinary steady-state for a normal-
    # cadence connector, not an edge case -- it stays `0` and the hook
    # never ran at all, leaving any prior over-budget backlog untouched
    # indefinitely rather than "worked down." `detect_actions_since`
    # itself is cheap to call when there is nothing to do (its own
    # eligibility query returning zero rows, then an immediate return), so
    # nothing is saved by skipping the call -- only correctness was lost.
    detect_actions = getattr(adapter, "detect_actions_since", None)
    if callable(detect_actions):
        try:
            detect_actions(context, since=now)
        except Exception:  # noqa: BLE001 -- best-effort, never fails the sync response
            pass

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
    if not authz.authorize(
        session, auth, resource_type="connector_accounts", resource_id=account_id, action="read"
    ):
        session.rollback()
        raise HTTPException(status_code=404, detail="CONNECTOR_NOT_FOUND")
    if not authz.authorize(
        session, auth, resource_type="connector_accounts", resource_id=account_id, action="write"
    ):
        session.rollback()
        raise HTTPException(status_code=403, detail="INSUFFICIENT_ROLE")
    # See sync_connector_endpoint's identical comment -- these pre-checks
    # must be rolled back before phase 1's own `with session.begin():`.
    session.rollback()

    request_hash = _request_hash(_EmptyBody(), f"disable:{account_id}")
    now = datetime.now(UTC)
    # Round 23 review: `adapter.disconnect(...)` used to be called from
    # *inside* this transaction, while still holding the `FOR UPDATE` lock
    # taken below -- fine for every adapter registered up to this point
    # (an in-process no-op), but `GmailAdapter.disconnect()` is the first
    # `disconnect()` in this registry that makes a real, blocking outbound
    # HTTPS call (Google's `/revoke`, up to `timeout_seconds=10.0`
    # default), so disabling a `gmail`-provider account held a pooled
    # connection *and* a row lock across that call -- dynamically proven
    # via a real Postgres `LockNotAvailable`/`pg_stat_activity: idle in
    # transaction` repro. `pending_revoke` below defers the actual network
    # call to after the transaction commits and `session` releases its
    # connection, mirroring `sync_connector_endpoint`'s own established
    # phase split for the identical class of risk.
    pending_revoke: tuple[ConnectorAdapter, ConnectorAccountContext] | None = None
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
            if account.provider == "gmail" and _personal_email_domain_exists(
                session, auth.workspace_id, account_id
            ):
                # Loop 2 round 1 review (PR #128): this endpoint is a
                # generic, provider-agnostic connector-level disable -- it
                # only marks the row `disconnected` and clears synced
                # projections, and never touches `personal_domains`/
                # `domain_consents` or purges the owner's Gmail-derived
                # data (`gmail_revocation.py`'s cascade). For every other
                # provider that is correct (disable retains previously-
                # synced data, `CONNECTOR-CONTRACT.md`); for `gmail`
                # specifically it would leave a real "disconnected but
                # data (and domain consent) remains" state reachable
                # through this endpoint -- exactly what `gmail_revocation.
                # py`'s own module docstring says is unreachable "through
                # this path", except this was a different path. Route
                # callers to the domain-level endpoint instead, which
                # reaches the cascade via `domains.py:_disable_domain`.
                # Gated on `status != "disconnected"`, not checked
                # unconditionally above: an already-`disconnected` gmail
                # account has nothing left to bypass, and every other
                # provider's own established contract is that disabling
                # an already-disconnected connector is an idempotent 200
                # no-op (`test_disable_...` tests below) -- round 2 review
                # found the unconditional version silently broke that same
                # contract for `gmail` specifically, with no test covering
                # the no-op case for this provider.
                #
                # Also gated on `_personal_email_domain_exists` (Loop 2
                # round 25 review finding, MEDIUM): this gate has no
                # dependency of its own on `personal_domains` -- an
                # allowlisted user who completes the Gmail OAuth flow
                # (`gmail_oauth.py`) without ever calling `POST /domains`
                # or `POST /consents` for `email` gets an `active`
                # `connector_accounts` row with no matching domain row at
                # all. Before this check, this generic endpoint
                # unconditionally redirected every non-disconnected
                # `gmail` account to the domain-level endpoint, which
                # 404s `DOMAIN_NOT_FOUND` for exactly that owner -- a
                # connector with no HTTP-reachable way to disconnect it,
                # a regression this same round-1 fix introduced (before
                # it, this endpoint could still disconnect any provider
                # unconditionally). No cascade-bypass risk in this
                # specific case: with no `personal_domains`/`domain_
                # consents` row, there is nothing for the cascade to
                # purge or revoke beyond what this generic path already
                # does, so falling through to the same disconnect every
                # other provider gets is correct, not merely convenient.
                raise HTTPException(
                    status_code=409, detail="GMAIL_DISABLE_REQUIRES_DOMAIN_ENDPOINT"
                )
            adapter = connector_registry.get(account.provider)
            if adapter is not None:
                # Decryption failure is folded into the same best-effort
                # treatment as revocation itself: a revocation attempt
                # must never leave a connector stuck unable to disconnect.
                # Concretely: if `ECC_CONNECTOR_TOKEN_ENCRYPTION_KEY` was
                # rotated without re-encrypting stored credentials,
                # `decrypt_credential` raises `cryptography.fernet.
                # InvalidToken` -- previously that propagated unhandled
                # out of this block (decrypt happened *outside* the try/
                # except), 500ing the request and leaving the connector
                # permanently stuck in its prior status with no way
                # through this API to disconnect it. "Cannot even attempt
                # revocation" degrades exactly like "attempted revocation
                # and it failed" -- both still disconnect. The actual
                # `adapter.disconnect(...)` call itself is deferred (see
                # above); only decryption -- a fast, local, no-network
                # operation -- runs here, inside the transaction.
                try:
                    encrypted = _get_encrypted_credential(session, auth.workspace_id, account_id)
                    pending_revoke = (
                        adapter,
                        ConnectorAccountContext(
                            workspace_id=auth.workspace_id,
                            connector_account_id=account_id,
                            external_account_id=account.external_account_id,
                            credential=decrypt_credential(encrypted),
                        ),
                    )
                except Exception:  # noqa: BLE001 -- best-effort, never blocks disconnect
                    pending_revoke = None

            session.execute(
                text(
                    "UPDATE connector_accounts SET status = 'disconnected', "
                    "disconnected_at = :now, updated_at = :now, updated_by = :actor_id, "
                    "version = version + 1 WHERE id = :id"
                ),
                {"now": now, "actor_id": auth.user_id, "id": account_id},
            )
            # `CONNECTOR-CONTRACT.md`: disconnect retains previously-synced
            # projections, visible not deleted, with a `disconnected`
            # freshness state -- a real gap the final Phase 6 review found:
            # this cascade never existed, so every already-synced row kept
            # reporting `freshness_state = 'fresh'` forever after disable.
            # All seven projection tables carry the identical `workspace_id`/
            # `connector_account_id`/`freshness_state` shape (migrations
            # `0045`/`0047`/`0048`/`0051`) -- a round-2 re-review of this same
            # fix found `changes`/`reviews` had been left off the first pass
            # (which omitted only `repositories`/`engineering_work_items`),
            # and PR #85's own security review found the three Datadog
            # tables left off this round in turn -- the identical gap, a
            # third time, closed here rather than repeated a fourth.
            for table in (
                "repositories",
                "engineering_work_items",
                "changes",
                "reviews",
                "datadog_monitors",
                "datadog_service_definitions",
                "datadog_dashboards",
            ):
                session.execute(
                    text(  # noqa: S608 -- table name is one of seven fixed literals, never user input
                        f"UPDATE {table} SET freshness_state = 'disconnected', "
                        "updated_at = :now WHERE workspace_id = :workspace_id "
                        "AND connector_account_id = :account_id "
                        "AND freshness_state != 'disconnected'"
                    ),
                    {"now": now, "workspace_id": auth.workspace_id, "account_id": account_id},
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

    # Transaction above has committed. Release `session`'s pooled
    # connection before `adapter.disconnect()`'s potentially slow,
    # blocking network call -- see this function's own module-level
    # comment above for why (round 23 review).
    session.close()
    if pending_revoke is not None:
        adapter, context = pending_revoke
        try:
            adapter.disconnect(context)
        except Exception:  # noqa: BLE001 -- best-effort revocation, never blocks disconnect
            pass
    return response


@router.get("/sync-runs", response_model=SyncRunListResponse)
def list_sync_runs_endpoint(
    auth: AuthDep,
    session: SessionDep,
    connector_account_id: Annotated[UUID | None, Query()] = None,
) -> SyncRunListResponse:
    visibility_sql, visibility_params = authz.visible_resource_filter_sql(
        session, auth, resource_type="sync_runs", action="read", table_alias="sync_runs"
    )
    clause = "AND connector_account_id = :connector_account_id" if connector_account_id else ""
    params: dict[str, Any] = {"workspace_id": auth.workspace_id, **visibility_params}
    if connector_account_id:
        params["connector_account_id"] = connector_account_id
    rows = (
        session.execute(
            text(
                "SELECT id, connector_account_id, run_type, status, items_processed, "
                "error_summary, started_at, completed_at FROM sync_runs "
                f"WHERE workspace_id = :workspace_id AND ({visibility_sql}) "  # noqa: S608
                f"{clause} ORDER BY started_at DESC"
            ),
            params,
        )
        .mappings()
        .all()
    )
    return SyncRunListResponse(sync_runs=[SyncRunResponse(**dict(row)) for row in rows])


@router.get("/metrics", response_model=MetricsListResponse)
def get_metrics_endpoint(auth: AuthDep, session: SessionDep, _csrf: CsrfDep) -> MetricsListResponse:
    """Computes all seven `DELIVERY-INTELLIGENCE-CONTRACT.md` metrics for
    the caller's own workspace and persists each as a new, immutable
    `delivery_metric_snapshots` row -- **a deliberate, disclosed
    departure from pure REST `GET` semantics.** This phase has no
    periodic sync/computation scheduler yet (`metrics.py`'s own module
    docstring); without one, a purely read-only `GET` returning only
    already-stored snapshots would return nothing at all for a workspace
    whose metrics have never been computed, or a stale number for one
    that hasn't been recomputed recently. Making the `GET` itself the
    trigger -- mirroring `POST /connectors/{id}/sync`'s own identical
    "manual trigger only" reality since Task 1 -- means this endpoint
    always reflects the current state of already-synced data, at the
    cost of writing a new snapshot row on every call. Each snapshot row
    is still itself immutable once written (never updated or deleted),
    so this is additive history, not a mutation any caller needs to
    reason about differently from a pure read.

    **`CsrfDep`, despite being a `GET`.** Review found that a state-
    changing `GET` (this one writes seven rows every call) authenticated
    only by a `SameSite=Lax` session cookie is reachable by a top-level
    cross-site navigation (`window.location = ".../metrics"`), which
    still attaches the cookie -- exactly the write-amplification/data-
    pollution risk `CsrfDep` exists to close on every other state-
    changing endpoint in this file. `require_csrf` checks a custom
    `X-CSRF-Token` header, not the HTTP method, so applying it here is
    the same mechanism, not a special case: a plain cross-site navigation
    cannot set a custom header, so it now gets a 403 instead of a write.
    """
    authz.require_role_action(session, auth, "write")
    snapshots = compute_and_store_metrics(session, workspace_id=auth.workspace_id)
    session.commit()
    return MetricsListResponse(
        metrics=[MetricSnapshotResponse(**snapshot) for snapshot in snapshots]
    )


@router.get("/repositories", response_model=RepositoryListResponse)
def list_repositories_endpoint(
    auth: AuthDep,
    session: SessionDep,
    connector_account_id: Annotated[UUID | None, Query()] = None,
    team_entity_id: Annotated[UUID | None, Query()] = None,
) -> RepositoryListResponse:
    """Task 8's own disclosed addition -- see `RepositoryResponse`'s own
    comment above for why. Mirrors `list_sync_runs_endpoint`'s shape
    exactly: workspace-scoped, an optional `connector_account_id` filter,
    no pagination (matching every other list endpoint in this file, none
    of which paginate at this activation's expected data volume).
    `team_entity_id` (migration `0050_phase6_team_linkage.py`) is the
    team-scoped-view filter that addition exists to enable -- a plain
    equality match against the confirmed link, never the suggestion.
    """
    visibility_sql, visibility_params = authz.visible_resource_filter_sql(
        session, auth, resource_type="repositories", action="read", table_alias="repositories"
    )
    clauses = []
    params: dict[str, Any] = {"workspace_id": auth.workspace_id, **visibility_params}
    if connector_account_id:
        clauses.append("AND connector_account_id = :connector_account_id")
        params["connector_account_id"] = connector_account_id
    if team_entity_id:
        clauses.append("AND team_entity_id = :team_entity_id")
        params["team_entity_id"] = team_entity_id
    rows = (
        session.execute(
            text(
                "SELECT id, connector_account_id, provider, external_id, name, source_url, "
                "default_branch, permission_state, freshness_state, provider_updated_at, "
                "observed_at, created_at, updated_at, team_entity_id, suggested_team_name, "
                "team_assignment_version, team_assignment_updated_by "
                "FROM repositories "
                f"WHERE workspace_id = :workspace_id AND ({visibility_sql}) "  # noqa: S608
                f"{' '.join(clauses)} ORDER BY name ASC"
            ),
            params,
        )
        .mappings()
        .all()
    )
    return RepositoryListResponse(repositories=[RepositoryResponse(**dict(row)) for row in rows])


@router.get("/work-items", response_model=WorkItemListResponse)
def list_work_items_endpoint(
    auth: AuthDep,
    session: SessionDep,
    connector_account_id: Annotated[UUID | None, Query()] = None,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    team_entity_id: Annotated[UUID | None, Query()] = None,
) -> WorkItemListResponse:
    """Same disclosed-addition reasoning as `list_repositories_endpoint`
    above -- `engineering_work_items` has been synced since Task 4 with
    no query surface until now. `status_filter` is a plain string
    equality match, not a `Literal`, since `engineering_work_items.status`
    carries no CHECK constraint (unlike `incidents.status`'s closed
    enum): it is provider-specific free text, and GitHub/GitLab/Jira each
    use a different vocabulary for "open"/"done" that this activation
    cannot honestly enumerate as a closed set. `team_entity_id` mirrors
    `list_repositories_endpoint`'s identical filter.
    """
    visibility_sql, visibility_params = authz.visible_resource_filter_sql(
        session,
        auth,
        resource_type="engineering_work_items",
        action="read",
        table_alias="engineering_work_items",
    )
    clauses = []
    params: dict[str, Any] = {"workspace_id": auth.workspace_id, **visibility_params}
    if connector_account_id:
        clauses.append("AND connector_account_id = :connector_account_id")
        params["connector_account_id"] = connector_account_id
    if status_filter:
        clauses.append("AND status = :status_filter")
        params["status_filter"] = status_filter
    if team_entity_id:
        clauses.append("AND team_entity_id = :team_entity_id")
        params["team_entity_id"] = team_entity_id
    rows = (
        session.execute(
            text(
                "SELECT id, connector_account_id, provider, external_id, title, source_url, "
                "item_type, status, reporter_external_id, assignee_external_id, "
                "permission_state, freshness_state, provider_created_at, observed_at, "
                "created_at, updated_at, team_entity_id, suggested_team_name, "
                "team_assignment_version, team_assignment_updated_by "
                "FROM engineering_work_items "
                f"WHERE workspace_id = :workspace_id AND ({visibility_sql}) "  # noqa: S608
                f"{' '.join(clauses)} ORDER BY title ASC"
            ),
            params,
        )
        .mappings()
        .all()
    )
    return WorkItemListResponse(work_items=[WorkItemResponse(**dict(row)) for row in rows])


@router.get("/team-suggestions", response_model=TeamSuggestionListResponse)
def list_team_suggestions_endpoint(
    auth: AuthDep, session: SessionDep
) -> TeamSuggestionListResponse:
    """Grouped, read-only view of every unconfirmed, undismissed
    `suggested_team_name` across `repositories` and `engineering_work_
    items` -- see the design doc's "Backend endpoints" section. Runs two
    separate visibility-filtered queries (one per table) rather than a
    single `UNION ALL`, because `authz.visible_resource_filter_sql` binds
    its own fixed parameter names (`__authz_resource_type` etc.) per call
    -- merging two calls' params into one query text would make both
    subqueries silently share whichever `resource_type` value won the
    dict merge. Grouping by `suggested_team_name` happens in Python
    instead, over what are already small, pre-filtered row sets.
    """
    visibility_sql_repo, visibility_params_repo = authz.visible_resource_filter_sql(
        session, auth, resource_type="repositories", action="read", table_alias="repositories"
    )
    repo_rows = (
        session.execute(
            text(
                "SELECT id, name, suggested_team_name FROM repositories "
                f"WHERE workspace_id = :workspace_id AND ({visibility_sql_repo}) "  # noqa: S608
                "AND team_entity_id IS NULL AND team_suggestion_dismissed_at IS NULL "
                "AND suggested_team_name IS NOT NULL"
            ),
            {"workspace_id": auth.workspace_id, **visibility_params_repo},
        )
        .mappings()
        .all()
    )

    visibility_sql_wi, visibility_params_wi = authz.visible_resource_filter_sql(
        session,
        auth,
        resource_type="engineering_work_items",
        action="read",
        table_alias="engineering_work_items",
    )
    work_item_rows = (
        session.execute(
            text(
                "SELECT id, title AS name, suggested_team_name FROM engineering_work_items "
                f"WHERE workspace_id = :workspace_id AND ({visibility_sql_wi}) "  # noqa: S608
                "AND team_entity_id IS NULL AND team_suggestion_dismissed_at IS NULL "
                "AND suggested_team_name IS NOT NULL"
            ),
            {"workspace_id": auth.workspace_id, **visibility_params_wi},
        )
        .mappings()
        .all()
    )

    groups: dict[str, dict[str, Any]] = {}
    for row in repo_rows:
        group = groups.setdefault(
            row["suggested_team_name"],
            {"repository_count": 0, "work_item_count": 0, "sample_items": []},
        )
        group["repository_count"] += 1
        if len(group["sample_items"]) < _TEAM_SUGGESTION_SAMPLE_CAP:
            group["sample_items"].append(
                {"id": row["id"], "resource_type": "repository", "name": row["name"]}
            )
    for row in work_item_rows:
        group = groups.setdefault(
            row["suggested_team_name"],
            {"repository_count": 0, "work_item_count": 0, "sample_items": []},
        )
        group["work_item_count"] += 1
        if len(group["sample_items"]) < _TEAM_SUGGESTION_SAMPLE_CAP:
            group["sample_items"].append(
                {"id": row["id"], "resource_type": "work_item", "name": row["name"]}
            )

    items = [
        TeamSuggestionGroup(suggested_team_name=name, **data)
        for name, data in sorted(
            groups.items(),
            key=lambda kv: kv[1]["repository_count"] + kv[1]["work_item_count"],
            reverse=True,
        )
    ]
    return TeamSuggestionListResponse(items=items)


def _lock_and_authorize_suggestion_candidates(
    session: Session,
    auth: AuthContext,
    *,
    table: Literal["repositories", "engineering_work_items"],
    suggested_team_name: str,
) -> tuple[list[UUID], list[UUID]]:
    """Locks every row in `table` still eligible for a team-suggestion
    bulk action (unconfirmed, undismissed, matching `suggested_team_
    name`), then authorizes each individually for `action="write"`.
    Returns `(authorized_ids, skipped_unauthorized_ids)`. Locking before
    authorizing matches the single-item endpoints' own `FOR UPDATE`
    timing, so a concurrent sync or another bulk action can't change a
    row out from under this one mid-check. `table` is always one of the
    two hardcoded literals below -- never request-derived.

    The candidate `SELECT` itself is restricted to `action="read"`-visible
    rows via `authz.visible_resource_filter_sql` (the same filter `list_
    team_suggestions_endpoint` applies) -- see `authz.authorize`'s
    docstring: a row invisible to the caller must never even be selected
    or locked, let alone surfaced in `skipped_unauthorized`, or an
    unauthorized caller could learn a private row's id exists by noticing
    it show up there. The subsequent per-row `action="write"` check is
    then only ever run against rows already known to be visible, matching
    `_validate_team_entity`/`assign_repository_team_endpoint`'s read-then-
    write precedent.
    """
    visibility_sql, visibility_params = authz.visible_resource_filter_sql(
        session, auth, resource_type=table, action="read", table_alias=table
    )
    rows = session.execute(
        text(
            f"SELECT id FROM {table} WHERE workspace_id = :workspace_id "  # noqa: S608
            f"AND ({visibility_sql}) "
            "AND suggested_team_name = :suggested_team_name "
            "AND team_entity_id IS NULL AND team_suggestion_dismissed_at IS NULL FOR UPDATE"
        ),
        {
            "workspace_id": auth.workspace_id,
            "suggested_team_name": suggested_team_name,
            **visibility_params,
        },
    ).all()
    authorized: list[UUID] = []
    skipped: list[UUID] = []
    for (row_id,) in rows:
        if authz.authorize(session, auth, resource_type=table, resource_id=row_id, action="write"):
            authorized.append(row_id)
        else:
            skipped.append(row_id)
    return authorized, skipped


_TEAM_SUGGESTION_TABLES: tuple[
    tuple[Literal["repositories", "engineering_work_items"], str, str], ...
] = (
    ("repositories", "repository", "repository.team_assigned"),
    ("engineering_work_items", "engineering_work_item", "engineering_work_item.team_assigned"),
)


@router.post("/team-suggestions/confirm", response_model=TeamSuggestionActionResponse)
def confirm_team_suggestion_endpoint(
    payload: TeamSuggestionConfirmRequest,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> TeamSuggestionActionResponse:
    """Bulk sibling of `assign_repository_team_endpoint`/`assign_work_
    item_team_endpoint`: confirms every currently-unconfirmed, undismissed
    `repositories`/`engineering_work_items` row sharing one `suggested_
    team_name`, in one transaction. No `expected_version` -- see the
    design doc's "Backend endpoints" section for why a per-row version
    doesn't apply to a set-based confirm.
    """
    request_hash = _request_hash(payload, "confirm_team_suggestion")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return TeamSuggestionActionResponse.model_validate(cached)

        _validate_team_entity(session, auth, payload.team_entity_id)

        updated: list[UUID] = []
        skipped: list[UUID] = []
        for table, aggregate_type, event_type in _TEAM_SUGGESTION_TABLES:
            authorized_ids, skipped_ids = _lock_and_authorize_suggestion_candidates(
                session, auth, table=table, suggested_team_name=payload.suggested_team_name
            )
            skipped.extend(skipped_ids)
            for row_id in authorized_ids:
                new_version = session.execute(
                    text(
                        f"UPDATE {table} SET team_entity_id = :team_entity_id, "  # noqa: S608
                        "team_assignment_version = team_assignment_version + 1, "
                        "team_assignment_updated_by = :actor_id, updated_at = :now "
                        "WHERE workspace_id = :workspace_id AND id = :id "
                        "AND team_entity_id IS NULL AND team_suggestion_dismissed_at IS NULL "
                        "RETURNING team_assignment_version"
                    ),
                    {
                        "team_entity_id": payload.team_entity_id,
                        "actor_id": auth.user_id,
                        "now": now,
                        "workspace_id": auth.workspace_id,
                        "id": row_id,
                    },
                ).scalar_one()
                _write_team_assignment_side_effects(
                    session,
                    auth,
                    request,
                    aggregate_type=aggregate_type,
                    event_type=event_type,
                    aggregate_id=row_id,
                    version=new_version,
                    now=now,
                )
                updated.append(row_id)

        response = TeamSuggestionActionResponse(updated=updated, skipped_unauthorized=skipped)
        _store_idempotency(
            session, auth, idempotency_key, request_hash, response.model_dump(mode="json"), now
        )
        return response


@router.post("/team-suggestions/dismiss", response_model=TeamSuggestionActionResponse)
def dismiss_team_suggestion_endpoint(
    payload: TeamSuggestionDismissRequest,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> TeamSuggestionActionResponse:
    """Bulk-hides every currently-unconfirmed, undismissed row sharing one
    `suggested_team_name`, without assigning any team. No audit event --
    unlike confirm, dismissing writes no durable link a future reader
    needs an audit trail for; the adapter-side reset rule (Tasks 2-4) is
    what keeps this from permanently suppressing a since-changed
    suggestion.
    """
    request_hash = _request_hash(payload, "dismiss_team_suggestion")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return TeamSuggestionActionResponse.model_validate(cached)

        updated: list[UUID] = []
        skipped: list[UUID] = []
        for table, _aggregate_type, _event_type in _TEAM_SUGGESTION_TABLES:
            authorized_ids, skipped_ids = _lock_and_authorize_suggestion_candidates(
                session, auth, table=table, suggested_team_name=payload.suggested_team_name
            )
            skipped.extend(skipped_ids)
            for row_id in authorized_ids:
                session.execute(
                    text(
                        f"UPDATE {table} SET team_suggestion_dismissed_at = :now "  # noqa: S608
                        "WHERE workspace_id = :workspace_id AND id = :id "
                        "AND team_entity_id IS NULL AND team_suggestion_dismissed_at IS NULL"
                    ),
                    {"now": now, "workspace_id": auth.workspace_id, "id": row_id},
                )
                updated.append(row_id)

        response = TeamSuggestionActionResponse(updated=updated, skipped_unauthorized=skipped)
        _store_idempotency(
            session, auth, idempotency_key, request_hash, response.model_dump(mode="json"), now
        )
        return response


@router.get("/monitors", response_model=MonitorListResponse)
def list_monitors_endpoint(
    auth: AuthDep,
    session: SessionDep,
    connector_account_id: Annotated[UUID | None, Query()] = None,
    team_entity_id: Annotated[UUID | None, Query()] = None,
) -> MonitorListResponse:
    """Mirrors `list_repositories_endpoint`'s shape exactly, for the
    Datadog connector's `datadog_monitors` projection (migration
    `0051_phase6_datadog_connector.py`).
    """
    visibility_sql, visibility_params = authz.visible_resource_filter_sql(
        session,
        auth,
        resource_type="datadog_monitors",
        action="read",
        table_alias="datadog_monitors",
    )
    clauses = []
    params: dict[str, Any] = {"workspace_id": auth.workspace_id, **visibility_params}
    if connector_account_id:
        clauses.append("AND connector_account_id = :connector_account_id")
        params["connector_account_id"] = connector_account_id
    if team_entity_id:
        clauses.append("AND team_entity_id = :team_entity_id")
        params["team_entity_id"] = team_entity_id
    rows = (
        session.execute(
            text(
                "SELECT id, connector_account_id, provider, external_id, source_url, "
                "name, monitor_type, query, overall_state, permission_state, "
                "freshness_state, provider_updated_at, observed_at, created_at, "
                "updated_at, team_entity_id, suggested_team_name "
                "FROM datadog_monitors "
                f"WHERE workspace_id = :workspace_id AND ({visibility_sql}) "  # noqa: S608
                f"{' '.join(clauses)} ORDER BY name ASC"
            ),
            params,
        )
        .mappings()
        .all()
    )
    return MonitorListResponse(monitors=[MonitorResponse(**dict(row)) for row in rows])


@router.get("/service-definitions", response_model=ServiceDefinitionListResponse)
def list_service_definitions_endpoint(
    auth: AuthDep,
    session: SessionDep,
    connector_account_id: Annotated[UUID | None, Query()] = None,
    team_entity_id: Annotated[UUID | None, Query()] = None,
) -> ServiceDefinitionListResponse:
    """Mirrors `list_repositories_endpoint`'s shape exactly, for the
    Datadog connector's `datadog_service_definitions` projection.
    """
    visibility_sql, visibility_params = authz.visible_resource_filter_sql(
        session,
        auth,
        resource_type="datadog_service_definitions",
        action="read",
        table_alias="datadog_service_definitions",
    )
    clauses = []
    params: dict[str, Any] = {"workspace_id": auth.workspace_id, **visibility_params}
    if connector_account_id:
        clauses.append("AND connector_account_id = :connector_account_id")
        params["connector_account_id"] = connector_account_id
    if team_entity_id:
        clauses.append("AND team_entity_id = :team_entity_id")
        params["team_entity_id"] = team_entity_id
    rows = (
        session.execute(
            text(
                "SELECT id, connector_account_id, provider, external_id, source_url, "
                "name, team, tier, description, permission_state, freshness_state, "
                "observed_at, created_at, updated_at, team_entity_id, suggested_team_name "
                "FROM datadog_service_definitions "
                f"WHERE workspace_id = :workspace_id AND ({visibility_sql}) "  # noqa: S608
                f"{' '.join(clauses)} ORDER BY name ASC"
            ),
            params,
        )
        .mappings()
        .all()
    )
    return ServiceDefinitionListResponse(
        service_definitions=[ServiceDefinitionResponse(**dict(row)) for row in rows]
    )


@router.get("/dashboards", response_model=DashboardListResponse)
def list_dashboards_endpoint(
    auth: AuthDep,
    session: SessionDep,
    connector_account_id: Annotated[UUID | None, Query()] = None,
    team_entity_id: Annotated[UUID | None, Query()] = None,
) -> DashboardListResponse:
    """Mirrors `list_repositories_endpoint`'s shape exactly, for the
    Datadog connector's `datadog_dashboards` projection.
    """
    visibility_sql, visibility_params = authz.visible_resource_filter_sql(
        session,
        auth,
        resource_type="datadog_dashboards",
        action="read",
        table_alias="datadog_dashboards",
    )
    clauses = []
    params: dict[str, Any] = {"workspace_id": auth.workspace_id, **visibility_params}
    if connector_account_id:
        clauses.append("AND connector_account_id = :connector_account_id")
        params["connector_account_id"] = connector_account_id
    if team_entity_id:
        clauses.append("AND team_entity_id = :team_entity_id")
        params["team_entity_id"] = team_entity_id
    rows = (
        session.execute(
            text(
                "SELECT id, connector_account_id, provider, external_id, source_url, "
                "title, description, permission_state, freshness_state, "
                "provider_updated_at, observed_at, created_at, updated_at, "
                "team_entity_id, suggested_team_name "
                "FROM datadog_dashboards "
                f"WHERE workspace_id = :workspace_id AND ({visibility_sql}) "  # noqa: S608
                f"{' '.join(clauses)} ORDER BY title ASC"
            ),
            params,
        )
        .mappings()
        .all()
    )
    return DashboardListResponse(dashboards=[DashboardResponse(**dict(row)) for row in rows])


def _validate_team_entity(session: Session, auth: AuthContext, team_entity_id: UUID | None) -> None:
    """Mirrors `waiting.py`'s `_counterparty_node_type` precedent: an
    existence + kind check against `pkos_nodes` at write time, not a
    schema-level FK alone (the FK only proves the row exists in *some*
    workspace at commit time via the composite constraint -- it cannot
    express "and its `node_type` must be `'team'`"). `None` is valid and
    means "clear the assignment," so this only runs the check when a real
    id was supplied.

    Found missing in the third whole-phase review: the existence check alone
    let a caller confirm a `pkos_nodes` row's id and assign a repository/work-
    item to it regardless of that node's own visibility -- `waiting.py`'s own
    precedent this docstring already claims to mirror re-checks
    `authz.authorize(..., action="read")` immediately after the raw existence
    query and folds a denial into the same 404 an outright-missing id
    returns, so a narrowed-visibility node is indistinguishable from a
    nonexistent one either way. This was the one call site that dropped that
    second check while copying the rest of the pattern.
    """
    if team_entity_id is None:
        return
    row = session.execute(
        text(
            "SELECT node_type FROM pkos_nodes "
            "WHERE workspace_id = :workspace_id AND id = :id AND status = 'active'"
        ),
        {"workspace_id": auth.workspace_id, "id": team_entity_id},
    ).one_or_none()
    if row is None:
        raise HTTPException(status_code=404, detail="TEAM_ENTITY_NOT_FOUND")
    if not authz.authorize(
        session,
        auth,
        resource_type="pkos_nodes",
        resource_id=team_entity_id,
        action="read",
    ):
        raise HTTPException(status_code=404, detail="TEAM_ENTITY_NOT_FOUND")
    if row[0] != "team":
        raise HTTPException(status_code=422, detail="TEAM_ENTITY_KIND_MISMATCH")


def _write_team_assignment_side_effects(
    session: Session,
    auth: AuthContext,
    request: Request,
    *,
    aggregate_type: str,
    event_type: str,
    aggregate_id: UUID,
    version: int,
    now: datetime,
) -> None:
    """`repositories`/`engineering_work_items`-scoped sibling of this
    module's own `_write_side_effects` (used by `create_connector_
    endpoint`/`sync_connector_endpoint`/`disable_connector_endpoint`) --
    kept separate rather than adding an `aggregate_type` parameter to that
    existing function, since its `record_audit_outbox_failure`/`queue_
    lifecycle_event` calls are hardcoded to the `"engineering_connector_
    account"` metric name those three call sites all share, and widening
    an already-tested helper's signature for one new caller family risks
    touching behavior outside this endpoint's own scope.
    """
    request_id, correlation_id = _request_ids(request)
    try:
        session.execute(
            text(
                """
                INSERT INTO audit_events (
                    id, workspace_id, event_type, aggregate_type, aggregate_id,
                    aggregate_version, actor_id, request_id, correlation_id,
                    changed_fields, authorization_result, source, metadata, occurred_at,
                    owner_id, visibility
                ) VALUES (
                    :id, :workspace_id, :event_type, :aggregate_type, :aggregate_id,
                    :aggregate_version, :actor_id, :request_id, :correlation_id,
                    ARRAY['team_entity_id'], 'allowed', 'user', '{}'::jsonb, :occurred_at,
                    :actor_id, 'workspace'
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": auth.workspace_id,
                "event_type": event_type,
                "aggregate_type": aggregate_type,
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
        record_audit_outbox_failure(aggregate_type)
        raise
    queue_lifecycle_event(session, aggregate_type, event_type, "allowed")


@router.post("/repositories/{repository_id}/team", response_model=RepositoryResponse)
def assign_repository_team_endpoint(
    repository_id: UUID,
    payload: TeamAssignmentRequest,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> RepositoryResponse:
    """The "human confirms" half of migration `0050_phase6_team_linkage.
    py`'s hybrid design for `repositories`. Mirrors `entities_mutations.
    py`'s `update_entity` in full after review found the first draft of
    this endpoint had none of `update_entity`'s optimistic-concurrency,
    idempotency, or audit-trail discipline, unlike every other mutating
    endpoint in this codebase: `expected_version` checked against `team_
    assignment_version` (409 `VERSION_CONFLICT` on mismatch, the same
    field-scoped counter migration `0050`'s own docstring explains --
    never the whole row's, since sync keeps refreshing every other
    column), `Idempotency-Key` with the same lock/cache/store pattern
    `create_connector_endpoint`/`sync_connector_endpoint`/`disable_
    connector_endpoint` already use, and an `audit_events`/`event_outbox`
    write recording who confirmed or cleared the link and when.
    """
    if not authz.authorize(
        session, auth, resource_type="repositories", resource_id=repository_id, action="read"
    ):
        session.rollback()
        raise HTTPException(status_code=404, detail="REPOSITORY_NOT_FOUND")
    if not authz.authorize(
        session, auth, resource_type="repositories", resource_id=repository_id, action="write"
    ):
        session.rollback()
        raise HTTPException(status_code=403, detail="INSUFFICIENT_ROLE")
    # See sync_connector_endpoint's identical comment -- these pre-checks
    # must be rolled back before this endpoint's own `with session.begin():`.
    session.rollback()

    request_hash = _request_hash(payload, f"assign_repository_team:{repository_id}")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return RepositoryResponse.model_validate(cached)

        _validate_team_entity(session, auth, payload.team_entity_id)

        current = session.execute(
            text(
                "SELECT team_assignment_version FROM repositories "
                "WHERE workspace_id = :workspace_id AND id = :id FOR UPDATE"
            ),
            {"workspace_id": auth.workspace_id, "id": repository_id},
        ).one_or_none()
        if current is None:
            raise HTTPException(status_code=404, detail="REPOSITORY_NOT_FOUND")
        if current[0] != payload.expected_version:
            raise HTTPException(status_code=409, detail="VERSION_CONFLICT")

        row = (
            session.execute(
                text(
                    """
                UPDATE repositories SET team_entity_id = :team_entity_id,
                    team_assignment_version = team_assignment_version + 1,
                    team_assignment_updated_by = :actor_id,
                    updated_at = :now
                WHERE workspace_id = :workspace_id AND id = :id
                RETURNING id, connector_account_id, provider, external_id, name, source_url,
                    default_branch, permission_state, freshness_state, provider_updated_at,
                    observed_at, created_at, updated_at, team_entity_id, suggested_team_name,
                    team_assignment_version, team_assignment_updated_by
                """
                ),
                {
                    "team_entity_id": payload.team_entity_id,
                    "actor_id": auth.user_id,
                    "now": now,
                    "workspace_id": auth.workspace_id,
                    "id": repository_id,
                },
            )
            .mappings()
            .one()
        )
        response = RepositoryResponse(**dict(row))
        event_type = (
            "repository.team_cleared"
            if payload.team_entity_id is None
            else "repository.team_assigned"
        )
        _write_team_assignment_side_effects(
            session,
            auth,
            request,
            aggregate_type="repository",
            event_type=event_type,
            aggregate_id=repository_id,
            version=response.team_assignment_version,
            now=now,
        )
        _store_idempotency(
            session, auth, idempotency_key, request_hash, response.model_dump(mode="json"), now
        )
        return response


@router.post("/work-items/{work_item_id}/team", response_model=WorkItemResponse)
def assign_work_item_team_endpoint(
    work_item_id: UUID,
    payload: TeamAssignmentRequest,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> WorkItemResponse:
    """Identical shape and reasoning to `assign_repository_team_endpoint`
    above, for `engineering_work_items`.
    """
    if not authz.authorize(
        session,
        auth,
        resource_type="engineering_work_items",
        resource_id=work_item_id,
        action="read",
    ):
        session.rollback()
        raise HTTPException(status_code=404, detail="WORK_ITEM_NOT_FOUND")
    if not authz.authorize(
        session,
        auth,
        resource_type="engineering_work_items",
        resource_id=work_item_id,
        action="write",
    ):
        session.rollback()
        raise HTTPException(status_code=403, detail="INSUFFICIENT_ROLE")
    # See sync_connector_endpoint's identical comment -- these pre-checks
    # must be rolled back before this endpoint's own `with session.begin():`.
    session.rollback()

    request_hash = _request_hash(payload, f"assign_work_item_team:{work_item_id}")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return WorkItemResponse.model_validate(cached)

        _validate_team_entity(session, auth, payload.team_entity_id)

        current = session.execute(
            text(
                "SELECT team_assignment_version FROM engineering_work_items "
                "WHERE workspace_id = :workspace_id AND id = :id FOR UPDATE"
            ),
            {"workspace_id": auth.workspace_id, "id": work_item_id},
        ).one_or_none()
        if current is None:
            raise HTTPException(status_code=404, detail="WORK_ITEM_NOT_FOUND")
        if current[0] != payload.expected_version:
            raise HTTPException(status_code=409, detail="VERSION_CONFLICT")

        row = (
            session.execute(
                text(
                    """
                UPDATE engineering_work_items SET team_entity_id = :team_entity_id,
                    team_assignment_version = team_assignment_version + 1,
                    team_assignment_updated_by = :actor_id,
                    updated_at = :now
                WHERE workspace_id = :workspace_id AND id = :id
                RETURNING id, connector_account_id, provider, external_id, title, source_url,
                    item_type, status, reporter_external_id, assignee_external_id,
                    permission_state, freshness_state, provider_created_at, observed_at,
                    created_at, updated_at, team_entity_id, suggested_team_name,
                    team_assignment_version, team_assignment_updated_by
                """
                ),
                {
                    "team_entity_id": payload.team_entity_id,
                    "actor_id": auth.user_id,
                    "now": now,
                    "workspace_id": auth.workspace_id,
                    "id": work_item_id,
                },
            )
            .mappings()
            .one()
        )
        response = WorkItemResponse(**dict(row))
        event_type = (
            "engineering_work_item.team_cleared"
            if payload.team_entity_id is None
            else "engineering_work_item.team_assigned"
        )
        _write_team_assignment_side_effects(
            session,
            auth,
            request,
            aggregate_type="engineering_work_item",
            event_type=event_type,
            aggregate_id=work_item_id,
            version=response.team_assignment_version,
            now=now,
        )
        _store_idempotency(
            session, auth, idempotency_key, request_hash, response.model_dump(mode="json"), now
        )
        return response
