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

## Which policy scope fields are actually enforced (accepted limitation)

This module stores and returns all eight scope/limit fields
`APPROVAL-POLICY.md` names. Only some of them are compared against anything
at dispatch or enqueue time, and the difference is disclosed here (rather
than left for a reader to discover by grepping for call sites) in the same
"state the real boundary explicitly" style
`docs/phases/phase-005/IMPLEMENTATION-STATUS.md` already uses for Phase 5's
other accepted limitations:

- **`approval_mode` -- enforced.** `approvals.evaluate_approval_requirement`
  (per-step approval requirement) plus `worker._evaluate_dispatch_gate`'s
  `preview_only` dispatch block.
- **`expires_at`/`revoked_at` -- enforced.** `is_policy_usable`, called by
  `worker._evaluate_dispatch_gate` before every not-yet-started step.
- **`count_limit` -- enforced.** `evaluate_approval_requirement`'s
  `policy-limit-exceeding` check, per run.
- **`rate_limit` (`runs_per_workflow_per_hour`) -- enforced.**
  `worker.enqueue_run` rejects the next run past the ceiling
  (`worker.RunRateLimited` -> `rate_limited`).
- **`schedule` -- not a control on this table.** Schedule authority lives on
  `triggers` (`trigger_type='schedule'` and its own `schedule_expression`/
  `timezone`), which is what `scheduler.py` actually evaluates; this column
  is descriptive metadata about the authorized cadence, never a second
  scheduler input.
- **`action_types` -- NOT enforced.** Stored, returned by
  `GET /automations/policies`, never compared against a dispatching adapter.
- **`data_classes` -- NOT enforced.** Same: stored and returned, never
  compared.
- **`value_limit` -- NOT enforced.** Same: stored and returned (and
  required/non-nullable, so an author must still choose a number), never
  summed against anything.

**The three unenforced fields, stated plainly: a policy scoped to specific
`action_types` or `data_classes` currently authorizes any registered adapter
regardless of that adapter's actual type or data classification, and a
`value_limit` of any size constrains nothing.** They are stored, returned by
the API, and intended for future enforcement -- they are not a live control
today. The reason is a missing model, not an oversight: enforcing them
requires per-adapter metadata mapping an adapter identity to its action type
and the data classes it touches (and, for `value_limit`, a monetary amount
per dispatch), and `adapters.ActionAdapter` declares no such fields --
Decision 8's contract carries `adapter_id`, `input_schema`/`output_schema`,
`reversible` and `high_impact_categories`, and nothing else. Adding that
metadata means extending the adapter protocol, backfilling it for every
registered adapter, deciding how an adapter that declines to classify itself
is treated (fail-closed, matching `high_impact_categories`' own precedent),
and threading it through both `_evaluate_dispatch_gate` and
`workflows._simulate_steps` -- a design change of its own, deliberately not
attempted as part of a docs-vs-code reconciliation. What *is* enforced
meanwhile is the part that does not need adapter metadata at all:
`high_impact_categories` (which every adapter already declares) always
forces per-run approval, `approval_mode` gates or blocks every step, and
`expires_at`/`revoked_at`/`count_limit`/`rate_limit` all bound authority by
time and volume. The gap is real but narrow: it is "this policy does not
narrow *which kinds* of registered adapter it authorizes," not "this policy
authorizes unattended execution."
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
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session
from ecc.observability import (
    queue_lifecycle_event,
    record_audit_outbox_failure,
)
from ecc.platform import authz
from ecc.platform.idempotency import load_cached, lock_idempotency, request_hash, store_idempotency

ApprovalMode = Literal["preview_only", "per_run", "bounded_recurring"]
PolicyLifecycleStatus = Literal["active", "expired", "revoked"]

_POLICY_EXPIRY_DAYS = 90
_DEFAULT_RATE_LIMIT: dict[str, Any] = {"runs_per_workflow_per_hour": 10}

_POLICY_FIELDS = """
    id, workspace_id, workflow_id, action_types, data_classes, value_limit,
    count_limit, rate_limit, schedule, approval_mode, expires_at, revoked_at,
    version, created_by, updated_by, created_at, updated_at
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


@dataclass(frozen=True, slots=True)
class PolicyNotFound:
    pass


@dataclass(frozen=True, slots=True)
class PolicyAlreadyRevoked:
    revoked_at: datetime


@dataclass(frozen=True, slots=True)
class PolicyAlreadyExpired:
    expires_at: datetime


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
    `policy_status(...) == "active"`. No caller in this task's scope
    dispatches a run, so nothing yet calls this for an HTTP-visible effect;
    it exists for the Task 2 worker's own future policy-resolution step to
    reuse rather than reimplement (design doc Decision 6's "expired policy
    blocks future runs").
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


def list_policies(
    session: Session, auth: AuthContext, workspace_id: UUID, *, workflow_id: str | None = None
) -> list[AutomationPolicy]:
    visibility_sql, visibility_params = authz.visible_resource_filter_sql(
        session,
        auth,
        resource_type="automation_policies",
        action="read",
        table_alias="automation_policies",
    )
    clause = "AND workflow_id = :workflow_id" if workflow_id is not None else ""
    params: dict[str, Any] = {"workspace_id": workspace_id, **visibility_params}
    if workflow_id is not None:
        params["workflow_id"] = workflow_id
    rows = (
        session.execute(
            text(
                f"SELECT {_POLICY_FIELDS} FROM automation_policies "
                f"WHERE workspace_id = :workspace_id AND ({visibility_sql}) "
                f"{clause} ORDER BY created_at ASC"
            ),
            params,
        )
        .mappings()
        .all()
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
) -> AutomationPolicy:
    """`expires_at` is always `now + 90 days` -- never caller-supplied
    (design doc Decision 6's default, computed here rather than left to a
    DB `server_default` per this task's own instruction). `rate_limit`
    falls back to `APPROVAL-POLICY.md`'s resolved system-wide default (10
    runs/workflow/hour) when the caller omits it -- the one limit field
    this doc gives a concrete default for, unlike `value_limit`/
    `count_limit`.
    """
    now = datetime.now(UTC)
    policy_id = uuid4()
    session.execute(
        text(
            """
            INSERT INTO automation_policies (
                id, workspace_id, workflow_id, action_types, data_classes,
                value_limit, count_limit, rate_limit, schedule, approval_mode,
                expires_at, revoked_at, version, created_by, updated_by,
                created_at, updated_at, owner_id, visibility
            ) VALUES (
                :id, :workspace_id, :workflow_id, :action_types, :data_classes,
                :value_limit, :count_limit, CAST(:rate_limit AS jsonb), :schedule,
                :approval_mode, :expires_at, NULL, 1, :created_by, :updated_by,
                :now, :now, :created_by, 'workspace'
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
    )


def _request_ids(request: Request) -> tuple[UUID, UUID]:
    try:
        return UUID(request.state.request_id), UUID(request.state.correlation_id)
    except (AttributeError, TypeError, ValueError):
        return uuid4(), uuid4()


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
                    :id, :workspace_id, :event_type, 'automation_policy', :aggregate_id,
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
        record_audit_outbox_failure("automation_policy")
        raise
    queue_lifecycle_event(session, "automation_policy", event_type, "allowed")


@router.get("/policies", response_model=PolicyListResponse)
def list_policies_endpoint(
    auth: AuthDep,
    session: SessionDep,
    workflow_id: Annotated[str | None, Query(max_length=200)] = None,
) -> PolicyListResponse:
    policies = list_policies(session, auth, auth.workspace_id, workflow_id=workflow_id)
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
        response = _to_response(created)
        _write_side_effects(
            session,
            auth,
            request,
            event_type="automation_policy.created",
            aggregate_id=created.id,
            version=created.version,
            now=now,
        )
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


class _EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


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
        _write_side_effects(
            session,
            auth,
            request,
            event_type="automation_policy.revoked",
            aggregate_id=result.id,
            version=result.version,
            now=now,
        )
        store_idempotency(
            session, auth, idempotency_key, req_hash, response.model_dump(mode="json"), now
        )
        return response
