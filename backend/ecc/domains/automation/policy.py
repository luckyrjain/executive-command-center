"""Automation policy reads/CRUD/revocation (`automation_policies`), plus
`GET|POST /api/v1/automations/policies` and `POST /api/v1/automations/
policies/{id}/revoke` (`docs/phases/phase-005/API-SCHEMAS.md`,
`docs/phases/phase-005/APPROVAL-POLICY.md`).

`value_limit`/`count_limit` are required, non-nullable fields with no
system-wide default (`APPROVAL-POLICY.md`'s resolved decision, design doc
Decision 6: "a numeric default with no concrete connector behind it would
be an arbitrary number, not a considered one") -- a policy author must set
both explicitly on every `POST`; migration `0038_phase5_workflow_schema.py`
declares both columns with no `server_default` for the identical reason.
`expires_at` defaults to 90 days from creation, computed here in
application code rather than a DB `server_default` (per this task's own
instruction) so the number is visible and testable at one call site rather
than split across a migration and this module.

`policy_status`/`is_policy_usable` are the "expiry-check helper" this
task's own package layout calls for -- pure functions over an already-
loaded `AutomationPolicy`, with no HTTP surface of their own, reusable by
whatever later task builds the run-dispatch path that actually needs to
gate on them (Decision 6: an expired or revoked policy blocks *future*
runs; this task builds no run dispatch, so nothing beyond `revoke_policy`
below currently calls them for an HTTP effect).

## Which policy scope fields are enforced

This module stores and returns all eight scope/limit fields
`APPROVAL-POLICY.md` names. How each one is enforced:

- **`approval_mode` -- enforced.** `approvals.evaluate_approval_requirement`
  (per-step approval requirement) plus `worker._evaluate_dispatch_gate`'s
  `preview_only` dispatch block.
- **`expires_at`/`revoked_at` -- enforced.** `is_policy_usable`, called by
  `worker._evaluate_dispatch_gate` before every not-yet-started step.
- **`count_limit` -- enforced.** `evaluate_approval_requirement`'s
  `policy-limit-exceeding` check, per run.
- **`value_limit` -- enforced.** Same check, per run: the run's summed
  `workflow_run_steps.dispatch_value` plus this step's
  (`adapter_contract.dispatch_value`) must not exceed it. Every adapter
  registered today moves value 0. A per-day window is deferred.
- **`action_types`/`data_classes` -- enforced on `scope_enforced` rows.**
  `approvals.evaluate_policy_scope`: the adapter's `action_type` must be
  listed and its `data_class` must rank at or below the highest listed
  class (`adapter_contract.DATA_CLASSES`, an ordinal ceiling). Checked at
  publish (`ACTION_REF_OUTSIDE_POLICY_SCOPE`), dispatch (blocks to
  `needs_review`, not an approval), retry-resume, compensation and
  `/simulate`. `create_policy` refuses an empty or unknown scope
  (`validate_policy_scope` -> `POLICY_SCOPE_EMPTY`/
  `POLICY_SCOPE_UNKNOWN_VALUE`) and always writes `scope_enforced = true`.
  A legacy row (`scope_enforced = false`, created before migration 0086)
  is not checked and ages out within 90 days.
- **`rate_limit` (`runs_per_workflow_per_hour`) -- enforced.**
  `worker.enqueue_run` rejects the next run past the ceiling
  (`worker.RunRateLimited` -> `rate_limited`).
- **`schedule` -- not a control on this table.** Schedule authority lives on
  `triggers` (`trigger_type='schedule'` and its own `schedule_expression`/
  `timezone`), which is what `scheduler.py` actually evaluates; this column
  is descriptive metadata about the authorized cadence, never a second
  scheduler input.

Full rules: `APPROVAL-POLICY.md`'s "Scope enforcement" section and
`docs/superpowers/specs/2026-10-01-automation-policy-scope-enforcement-
design.md`.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from json import dumps
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
from ecc.platform.request_models import EmptyBody as _EmptyBody

from .adapter_contract import ACTION_TYPES, DATA_CLASSES

ApprovalMode = Literal["preview_only", "per_run", "bounded_recurring"]
PolicyLifecycleStatus = Literal["active", "expired", "revoked"]

_POLICY_EXPIRY_DAYS = 90
_DEFAULT_RATE_LIMIT: dict[str, Any] = {"runs_per_workflow_per_hour": 10}

_POLICY_FIELDS = """
    id, workspace_id, workflow_id, action_types, data_classes, value_limit,
    count_limit, rate_limit, schedule, approval_mode, expires_at, revoked_at,
    version, created_by, updated_by, created_at, updated_at, scope_enforced
"""


@dataclass(frozen=True, slots=True)
class AutomationPolicy:
    id: UUID
    workspace_id: UUID
    workflow_id: str
    action_types: tuple[str, ...]
    data_classes: tuple[str, ...]
    value_limit: Decimal
    count_limit: int
    rate_limit: dict[str, Any]
    schedule: str | None
    approval_mode: ApprovalMode
    expires_at: datetime
    revoked_at: datetime | None
    version: int
    created_by: UUID
    updated_by: UUID
    created_at: datetime
    updated_at: datetime
    # False for a legacy row (created before migration 0086, or by a
    # pre-0086 app instance): its scope fields are not enforced. Every
    # policy `create_policy` writes is enforced.
    scope_enforced: bool


@dataclass(frozen=True, slots=True)
class PolicyNotFound:
    pass


@dataclass(frozen=True, slots=True)
class PolicyAlreadyRevoked:
    revoked_at: datetime


@dataclass(frozen=True, slots=True)
class PolicyAlreadyExpired:
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class PolicyScopeInvalid:
    """`create_policy` refused the requested scope: `code` is
    `POLICY_SCOPE_EMPTY` (no action type or no data class -- an empty scope
    would authorize nothing) or `POLICY_SCOPE_UNKNOWN_VALUE` (a value
    outside the closed vocabulary)."""

    code: Literal["POLICY_SCOPE_EMPTY", "POLICY_SCOPE_UNKNOWN_VALUE"]
    field: Literal["action_types", "data_classes"]
    values: tuple[str, ...]
    allowed: tuple[str, ...]


def validate_policy_scope(
    action_types: list[str], data_classes: list[str]
) -> PolicyScopeInvalid | None:
    """The scope a new policy may be created with (scope-enforcement
    design, Decision 3 item 3): at least one action type and one data
    class, each from the closed vocabularies. Lives in the domain function,
    not a Pydantic validator, so direct callers are covered and the API
    answers `POLICY_SCOPE_*` rather than a generic validation error."""
    checks: tuple[
        tuple[Literal["action_types", "data_classes"], list[str], tuple[str, ...]], ...
    ] = (
        ("action_types", action_types, tuple(sorted(ACTION_TYPES))),
        ("data_classes", data_classes, DATA_CLASSES),
    )
    for field, values, allowed in checks:
        if not values:
            return PolicyScopeInvalid("POLICY_SCOPE_EMPTY", field, (), allowed)
        unknown = tuple(sorted({v for v in values if v not in allowed}))
        if unknown:
            return PolicyScopeInvalid("POLICY_SCOPE_UNKNOWN_VALUE", field, unknown, allowed)
    return None


def policy_status(
    policy: AutomationPolicy, *, now: datetime | None = None
) -> PolicyLifecycleStatus:
    """Design doc Decision 6 / `APPROVAL-POLICY.md`: a policy is `revoked`
    if `revoked_at` is set (revocation is permanent and immediate for any
    not-yet-started step), else `expired` if `expires_at` has passed, else
    `active`. Revocation takes precedence over expiry when both happen to
    be true, since revocation is the more specific, human-initiated action.
    """
    moment = now if now is not None else datetime.now(UTC)
    if policy.revoked_at is not None:
        return "revoked"
    if policy.expires_at <= moment:
        return "expired"
    return "active"


def is_policy_usable(policy: AutomationPolicy, *, now: datetime | None = None) -> bool:
    """Whether this policy currently authorizes anything -- `True` only for
    `policy_status(...) == "active"` (design doc Decision 6's "expired policy
    blocks future runs"). The worker's dispatch gate, publish-time scope
    check and `/simulate` all use it.
    """
    return policy_status(policy, now=now) == "active"


def _row_to_policy(row: dict[str, Any]) -> AutomationPolicy:
    return AutomationPolicy(
        id=row["id"],
        workspace_id=row["workspace_id"],
        workflow_id=row["workflow_id"],
        action_types=tuple(row["action_types"]),
        data_classes=tuple(row["data_classes"]),
        value_limit=row["value_limit"],
        count_limit=row["count_limit"],
        rate_limit=row["rate_limit"],
        schedule=row["schedule"],
        approval_mode=row["approval_mode"],
        expires_at=row["expires_at"],
        revoked_at=row["revoked_at"],
        version=row["version"],
        created_by=row["created_by"],
        updated_by=row["updated_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        scope_enforced=row["scope_enforced"],
    )


def get_policy(session: Session, workspace_id: UUID, policy_id: UUID) -> AutomationPolicy | None:
    row = (
        session.execute(
            text(
                f"SELECT {_POLICY_FIELDS} FROM automation_policies "
                "WHERE workspace_id = :workspace_id AND id = :id"
            ),
            {"workspace_id": workspace_id, "id": policy_id},
        )
        .mappings()
        .one_or_none()
    )
    return _row_to_policy(dict(row)) if row is not None else None


def get_policy_for_workflow(
    session: Session, workspace_id: UUID, policy_id: UUID, workflow_id: str
) -> AutomationPolicy | None:
    """The policy `policy_id` names, only if it governs `workflow_id`.

    A policy is bound to one workflow family (`workflow_id`). Every
    server-side lookup of a version's or run's authority goes through here,
    so a `policy_ref` naming another workflow's policy (written before
    drafts were checked, or by a direct caller) resolves to
    no policy at all and fails closed -- a run of workflow Y never borrows
    the authority of a policy written for workflow X (`API-SCHEMAS.md`'s
    confused-deputy rule).
    """
    policy = get_policy(session, workspace_id, policy_id)
    if policy is None or policy.workflow_id != workflow_id:
        return None
    return policy


def list_policies(
    session: Session, auth: AuthContext, *, workflow_id: str | None = None
) -> list[AutomationPolicy]:
    extra_clauses = []
    extra_params: dict[str, Any] = {}
    if workflow_id is not None:
        extra_clauses.append("workflow_id = :workflow_id")
        extra_params["workflow_id"] = workflow_id
    rows = authz.list_visible_resources(
        session,
        auth,
        resource_type="automation_policies",
        columns=_POLICY_FIELDS,
        order_by="created_at ASC",
        extra_clauses=extra_clauses,
        extra_params=extra_params,
    )
    session.rollback()
    return [_row_to_policy(dict(row)) for row in rows]


def workflow_family_exists(session: Session, workspace_id: UUID, workflow_id: str) -> bool:
    return (
        session.execute(
            text(
                "SELECT 1 FROM workflow_definitions WHERE workspace_id = :workspace_id "
                "AND workflow_id = :workflow_id LIMIT 1"
            ),
            {"workspace_id": workspace_id, "workflow_id": workflow_id},
        ).first()
        is not None
    )


def create_policy(
    session: Session,
    workspace_id: UUID,
    actor_id: UUID,
    *,
    workflow_id: str,
    action_types: list[str],
    data_classes: list[str],
    value_limit: Decimal,
    count_limit: int,
    rate_limit: dict[str, Any] | None,
    schedule: str | None,
    approval_mode: ApprovalMode,
) -> AutomationPolicy | PolicyScopeInvalid:
    """Refuses an empty or out-of-vocabulary scope (`PolicyScopeInvalid`,
    `validate_policy_scope`), and writes every policy it creates with
    `scope_enforced = true` explicitly -- migration 0086 leaves the column's
    default false so only this code path creates enforced rows.

    `expires_at` is always `now + 90 days` -- never caller-supplied
    (design doc Decision 6's default, computed here rather than left to a
    DB `server_default` per this task's own instruction). `rate_limit`
    falls back to `APPROVAL-POLICY.md`'s resolved system-wide default (10
    runs/workflow/hour) when the caller omits it -- the one limit field
    this doc gives a concrete default for, unlike `value_limit`/
    `count_limit`.
    """
    invalid = validate_policy_scope(action_types, data_classes)
    if invalid is not None:
        return invalid
    now = datetime.now(UTC)
    policy_id = uuid4()
    session.execute(
        text(
            """
            INSERT INTO automation_policies (
                id, workspace_id, workflow_id, action_types, data_classes,
                value_limit, count_limit, rate_limit, schedule, approval_mode,
                expires_at, revoked_at, version, created_by, updated_by,
                created_at, updated_at, owner_id, visibility, scope_enforced
            ) VALUES (
                :id, :workspace_id, :workflow_id, :action_types, :data_classes,
                :value_limit, :count_limit, CAST(:rate_limit AS jsonb), :schedule,
                :approval_mode, :expires_at, NULL, 1, :created_by, :updated_by,
                :now, :now, :created_by, 'workspace', true
            )
            """
        ),
        {
            "id": policy_id,
            "workspace_id": workspace_id,
            "workflow_id": workflow_id,
            "action_types": action_types,
            "data_classes": data_classes,
            "value_limit": value_limit,
            "count_limit": count_limit,
            "rate_limit": dumps(rate_limit if rate_limit is not None else _DEFAULT_RATE_LIMIT),
            "schedule": schedule,
            "approval_mode": approval_mode,
            "expires_at": now + timedelta(days=_POLICY_EXPIRY_DAYS),
            "created_by": actor_id,
            "updated_by": actor_id,
            "now": now,
        },
    )
    result = get_policy(session, workspace_id, policy_id)
    assert result is not None
    return result


def revoke_policy(
    session: Session, workspace_id: UUID, actor_id: UUID, policy_id: UUID
) -> AutomationPolicy | PolicyNotFound | PolicyAlreadyRevoked | PolicyAlreadyExpired:
    """Sets `revoked_at = now()` and bumps `version` (this table's own
    `expected_version`-shaped optimistic-concurrency column, Phase 3's
    established idiom the design doc cites throughout) -- takes effect
    immediately per `APPROVAL-POLICY.md` verbatim ("Revocation takes effect
    immediately for any not-yet-started step"). Revoking an already-revoked
    or already-expired policy is rejected rather than silently accepted as
    a no-op: both are terminal states a `POST .../revoke` cannot
    meaningfully act on further, and surfacing that distinctly (`POLICY_
    REVOKED`/`POLICY_EXPIRED`, `API-SCHEMAS.md`'s required error codes)
    is more informative to a caller than a quiet 200.
    """
    row = (
        session.execute(
            text(
                f"SELECT {_POLICY_FIELDS} FROM automation_policies "
                "WHERE workspace_id = :workspace_id AND id = :id FOR UPDATE"
            ),
            {"workspace_id": workspace_id, "id": policy_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return PolicyNotFound()

    policy = _row_to_policy(dict(row))
    if policy.revoked_at is not None:
        return PolicyAlreadyRevoked(revoked_at=policy.revoked_at)

    now = datetime.now(UTC)
    if policy.expires_at <= now:
        return PolicyAlreadyExpired(expires_at=policy.expires_at)

    session.execute(
        text(
            "UPDATE automation_policies SET revoked_at = :now, updated_at = :now, "
            "updated_by = :actor_id, version = version + 1 WHERE id = :id"
        ),
        {"now": now, "actor_id": actor_id, "id": policy.id},
    )
    result = get_policy(session, workspace_id, policy_id)
    assert result is not None
    return result


# --- GET|POST /api/v1/automations/policies, POST .../{id}/revoke ----------

router = APIRouter(prefix="/api/v1/automations", tags=["automation"])
SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]


class PolicyCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow_id: str = Field(min_length=1, max_length=200)
    action_types: list[str] = Field(default_factory=list, max_length=50)
    data_classes: list[str] = Field(default_factory=list, max_length=20)
    # Required, no default (APPROVAL-POLICY.md's resolved decision) --
    # ge=0 matches the migration's ck_automation_policies_*_nonneg checks.
    value_limit: Decimal = Field(ge=0)
    count_limit: int = Field(ge=0)
    rate_limit: dict[str, Any] | None = None
    schedule: str | None = Field(default=None, max_length=500)
    approval_mode: ApprovalMode


class PolicyResponse(BaseModel):
    id: UUID
    workflow_id: str
    action_types: list[str]
    data_classes: list[str]
    value_limit: Decimal
    count_limit: int
    rate_limit: dict[str, Any]
    schedule: str | None
    approval_mode: ApprovalMode
    expires_at: datetime
    revoked_at: datetime | None
    status: PolicyLifecycleStatus
    version: int
    created_at: datetime
    updated_at: datetime
    # Defaulted, not required: idempotent replays (`load_cached`, kept up to
    # a year) re-validate response bodies cached before this field existed,
    # and every such policy is legacy anyway.
    scope_enforced: bool = False


class PolicyListResponse(BaseModel):
    policies: list[PolicyResponse]


def _to_response(policy: AutomationPolicy) -> PolicyResponse:
    return PolicyResponse(
        id=policy.id,
        workflow_id=policy.workflow_id,
        action_types=list(policy.action_types),
        data_classes=list(policy.data_classes),
        value_limit=policy.value_limit,
        count_limit=policy.count_limit,
        rate_limit=policy.rate_limit,
        schedule=policy.schedule,
        approval_mode=policy.approval_mode,
        expires_at=policy.expires_at,
        revoked_at=policy.revoked_at,
        status=policy_status(policy),
        version=policy.version,
        created_at=policy.created_at,
        updated_at=policy.updated_at,
        scope_enforced=policy.scope_enforced,
    )


@router.get("/policies", response_model=PolicyListResponse)
def list_policies_endpoint(
    auth: AuthDep,
    session: SessionDep,
    workflow_id: Annotated[str | None, Query(max_length=200)] = None,
) -> PolicyListResponse:
    policies = list_policies(session, auth, workflow_id=workflow_id)
    return PolicyListResponse(policies=[_to_response(policy) for policy in policies])


@router.post("/policies", response_model=PolicyResponse, status_code=status.HTTP_201_CREATED)
def create_policy_endpoint(
    payload: PolicyCreateRequest,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> PolicyResponse:
    authz.require_role_action(session, auth, "write")
    req_hash = request_hash(payload, "create_policy")
    now = datetime.now(UTC)
    with session.begin():
        authz.lock_membership_for_write(session, auth, role_action="write")
        lock_idempotency(session, auth, idempotency_key)
        cached = load_cached(
            session,
            auth,
            idempotency_key,
            req_hash,
            domain="automation_policy",
            response_model=PolicyResponse,
        )
        if cached is not None:
            return cached

        if not workflow_family_exists(session, auth.workspace_id, payload.workflow_id):
            raise HTTPException(status_code=404, detail="WORKFLOW_NOT_FOUND")

        created = create_policy(
            session,
            auth.workspace_id,
            auth.user_id,
            workflow_id=payload.workflow_id,
            action_types=payload.action_types,
            data_classes=payload.data_classes,
            value_limit=payload.value_limit,
            count_limit=payload.count_limit,
            rate_limit=payload.rate_limit,
            schedule=payload.schedule,
            approval_mode=payload.approval_mode,
        )
        if isinstance(created, PolicyScopeInvalid):
            # Raised inside the transaction: nothing was written, and the
            # idempotency record below is never stored for a refusal.
            raise HTTPException(
                status_code=422,
                detail={
                    "code": created.code,
                    "field": created.field,
                    "values": list(created.values),
                    "allowed": list(created.allowed),
                },
            )
        response = _to_response(created)
        audit_outbox.write_audit_and_outbox(
            session,
            auth,
            request,
            event_type="automation_policy.created",
            aggregate_type="automation_policy",
            aggregate_id=created.id,
            aggregate_version=created.version,
            changed_fields=["*"],
            payload={"aggregate_id": str(created.id), "version": created.version},
            now=now,
            domain="automation_policy",
        )
        queue_lifecycle_event(session, "automation_policy", "automation_policy.created", "allowed")
        store_idempotency(
            session,
            auth,
            idempotency_key,
            req_hash,
            response.model_dump(mode="json"),
            now,
            status.HTTP_201_CREATED,
        )
        return response


@router.post("/policies/{policy_id}/revoke", response_model=PolicyResponse)
def revoke_policy_endpoint(
    policy_id: UUID,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> PolicyResponse:
    req_hash = request_hash(_EmptyBody(), f"revoke:{policy_id}")
    now = datetime.now(UTC)
    with session.begin():
        authz.lock_membership_for_write(session, auth)
        lock_idempotency(session, auth, idempotency_key)

        # Lock before authorizing: an ownership transfer that commits while
        # this request waits on the row lock must be seen by the checks below
        # (READ COMMITTED: each later statement reads the committed row), not
        # by checks that ran against the pre-transfer row. Ahead of the
        # idempotency cache too: it is read only after these checks pass, so
        # a caller who has since lost access (removed, suspended, demoted, or
        # no longer able to see the row) never has a cached success replayed.
        # (`revoke_policy` re-selects this row FOR UPDATE below:
        # a no-op re-lock within this transaction.)
        locked = session.execute(
            text(
                "SELECT id FROM automation_policies "
                "WHERE workspace_id = :workspace_id AND id = :id FOR UPDATE"
            ),
            {"workspace_id": auth.workspace_id, "id": policy_id},
        ).one_or_none()
        if locked is None:
            raise HTTPException(status_code=404, detail="POLICY_NOT_FOUND")
        if not authz.authorize(
            session, auth, resource_type="automation_policies", resource_id=policy_id, action="read"
        ):
            raise HTTPException(status_code=404, detail="POLICY_NOT_FOUND")
        if not authz.authorize(
            session,
            auth,
            resource_type="automation_policies",
            resource_id=policy_id,
            action="write",
        ):
            raise HTTPException(status_code=403, detail="INSUFFICIENT_ROLE")

        # After authz, before the state checks in the helper below: a
        # same-key replay of a successful call finds the row already
        # transitioned and must get the cached 200, not a 409.
        cached = load_cached(
            session,
            auth,
            idempotency_key,
            req_hash,
            domain="automation_policy",
            response_model=PolicyResponse,
        )
        if cached is not None:
            return cached

        result = revoke_policy(session, auth.workspace_id, auth.user_id, policy_id)
        if isinstance(result, PolicyNotFound):
            raise HTTPException(status_code=404, detail="POLICY_NOT_FOUND")
        if isinstance(result, PolicyAlreadyRevoked):
            raise HTTPException(
                status_code=409,
                detail={"code": "POLICY_REVOKED", "revoked_at": result.revoked_at.isoformat()},
            )
        if isinstance(result, PolicyAlreadyExpired):
            raise HTTPException(
                status_code=409,
                detail={"code": "POLICY_EXPIRED", "expires_at": result.expires_at.isoformat()},
            )

        response = _to_response(result)
        audit_outbox.write_audit_and_outbox(
            session,
            auth,
            request,
            event_type="automation_policy.revoked",
            aggregate_type="automation_policy",
            aggregate_id=result.id,
            aggregate_version=result.version,
            changed_fields=["*"],
            payload={"aggregate_id": str(result.id), "version": result.version},
            now=now,
            domain="automation_policy",
        )
        queue_lifecycle_event(session, "automation_policy", "automation_policy.revoked", "allowed")
        store_idempotency(
            session, auth, idempotency_key, req_hash, response.model_dump(mode="json"), now
        )
        return response
