"""The approval inbox: requirement evaluation, request creation, human
decision, and the `/api/v1/automations/approvals` HTTP surface
(`docs/superpowers/specs/2026-07-25-phase-5-automation-design.md` Decision
5/6, `docs/phases/phase-005/APPROVAL-POLICY.md`,
`docs/phases/phase-005/DATA-MODEL.md`, `docs/phases/phase-005/
API-SCHEMAS.md`).

**`evaluate_approval_requirement` is the "does this step need a fresh
human approval" decision, made once, in one place.** Pure (no session, no
I/O) so it is directly unit-testable against every combination of adapter
category / policy mode / count without a database -- `worker.py`'s
dispatch gate is the only caller that needs a live session, to resolve the
policy and the run's own already-dispatched-step count first. Fail-closed
throughout: an adapter with any non-empty `high_impact_categories` always
requires approval regardless of `approval_mode` (Decision 5 verbatim,
"always requires per-run approval regardless of policy mode" --
`APPROVAL-POLICY.md`); an unrecognized `approval_mode` (should be
unreachable -- the migration 0038 `CHECK` constraint and `PolicyCreate
Request`'s own `Literal` both already restrict it to three values) also
defaults to requiring approval rather than silently permitting dispatch.

**`preview_only` requires approval for every step here, exactly like
`per_run` -- and a step under it can still never reach `execute()`, which
this function is deliberately not the place that enforces.** Two readings of
`APPROVAL-POLICY.md`'s named-but-underspecified `preview_only` mode were
available: (a) `preview_only` behaves exactly like `per_run` (approval gates
every step, and an approved step then dispatches for real) or (b)
`preview_only` additionally blocks *even an approved* step from ever
reaching `execute()`, so a policy in this mode never produces a real side
effect at all. **Reading (b) is what is implemented**, across two modules,
and the split is deliberate:

- *This* function keeps returning `True` for every `preview_only` step, and
  that is not a formality. It is what makes the mode's real purpose work:
  an operator in `docs/runbooks/PHASE-5-DOGFOOD.md`'s Stage 1 practises the
  genuine approval flow -- a real `approval_requests` row, a real digest to
  echo, a real `/approve` or `/reject` decision, a real run transition out
  of `waiting_approval` -- against a policy that cannot cause a side effect.
  Short-circuiting `preview_only` to "blocked" *here* would be simpler code
  and would gut the mode, leaving nothing to rehearse.
- `worker._evaluate_dispatch_gate` is where (b)'s actual guarantee lives:
  after this function's requirement (if any) is satisfied for the live
  digest, that gate checks the resolved policy's `approval_mode` on its own
  single "cleared to dispatch" exit and returns `worker.StepBlockedBy
  PreviewOnlyPolicy` for `preview_only`, so there is no reachable path from
  a policy in this mode to `adapter.execute()`. `process_claimed_run` maps
  that outcome to the terminal `'preview_blocked'` run status (migration
  `0043`) -- not `needs_review`: nothing is broken and no human action could
  let the run proceed. That function's own docstring carries the full
  mechanism, including why the check sits on that one exit specifically and
  why an unregistered `action_ref` is blocked there too.

**Why the earlier reading (a) was wrong, recorded rather than quietly
replaced.** The original argument for (a) was that `POST /automations/
workflows/{id}/simulate` already cannot reach `execute()` by construction
(Decision 4, `adapters.py`'s module docstring), so a second enforcement
mechanism for `preview_only` specifically looked redundant. It is not
redundant, because the two mechanisms guard different things: `/simulate`
guarantees that *a simulation request* has no path to `execute()`, while
`preview_only` is a property of *a policy* -- and a `preview_only` policy is
reachable from `POST /automations/runs` and from a schedule trigger, neither
of which goes anywhere near `/simulate`. Under (a), the sequence "start a
real run against a `preview_only` policy, approve the step it pauses on,
watch the adapter execute for real" was fully available, which is precisely
the sequence `APPROVAL-POLICY.md` and `PHASE-5-DOGFOOD.md`'s Stage 1
threshold ("zero real side effects of any kind ... mechanically guaranteed")
told an operator was impossible. (a) satisfied "nothing executes unattended"
but not "this mode never executes," and the documents promise the second.

**`policy-limit-exceeding` is evaluated using `count_limit` only.**
`value_limit` has no adapter-declared monetary field to compare against in
this activation (`docs/superpowers/specs/...design.md` Decision 5's own
table: "No connector in this activation has monetary side effects" --
identical reasoning to why `value_limit` itself has no system-wide
default), so there is nothing to sum against it -- see `policy.py`'s own
"which policy scope fields are actually enforced" docstring section for the
honest, per-field status of `action_types`/`data_classes`/`value_limit`,
none of which any dispatching code compares an adapter against. `rate_limit`
(runs per workflow per hour) is an *enqueue-time* concern per
`APPROVAL-POLICY.md`'s own table ("the next run past the limit rejected at
enqueue with `rate_limited`"), not a per-step dispatch-time concern, and it
*is* now enforced -- at that enqueue-time choke point, by
`worker.enqueue_run`/`worker.RunRateLimited`, not here. `count_limit` is
therefore evaluated as: has this run already dispatched (attempted -- a
`workflow_run_steps` row of any status exists) `count_limit` or more
action steps? If so, the next one is `policy-limit-exceeding` and requires
approval even under `bounded_recurring` for an otherwise-`bounded`
adapter. `worker.py`'s dispatch gate computes this count (a single
`COUNT(*)` query against the run's own `workflow_run_steps`) and passes it
in -- kept out of this module so `evaluate_approval_requirement` stays a
pure function.

**Approval is bound to the exact `action_digest` it names -- mechanically,
not by convention.** `decide_approval` requires the caller (the `/approve`
router endpoint; a test calling it directly) to pass `current_action_
digest`, the value the caller is asserting this decision applies to, and
rejects a mismatch as `ApprovalDigestMismatch` (`API-SCHEMAS.md`'s
`digest_mismatch`) rather than silently deciding against whatever digest
happens to be stored. This module's own responsibility ends at "the
decision the caller asked for matches the request the caller named";
`worker.py`'s dispatch gate is the second, independent half of this
guarantee (Decision 3's Threat model: "Replay ... A replayed or reused
approval for a different digest ... is rejected") -- it never trusts an
`approved` row on faith, it re-looks-up an `approved` row keyed by the
*live, freshly recomputed* `action_digest` at the exact moment of dispatch,
so a request approved for one digest can never authorize execution under a
different one even if this module's own check were somehow bypassed.

**`status` is deliberately three-valued (`pending|approved|rejected`), not
four -- `expired` is computed, never stored.** `approval_lifecycle_status`
mirrors `policy.policy_status` exactly (the same "no stored lifecycle
column, only raw timestamp fields, a pure function derives the label"
convention that table already established) -- see migration
`0040_phase5_approval_requests.py`'s own docstring for the full reasoning,
including the transactional problem this sidesteps (a lazy `expired`
write from inside `decide_approval`, called from a router's own `with
session.begin():` block that rolls back on the `HTTPException` an expired
decision raises, would need a second, separately-committed round trip to
persist correctly -- not worth building when the value is trivially
derivable from `expires_at` on every read instead, identical to how policy
expiry already works).

**No commit inside `create_approval_request`/`decide_approval` -- caller-
committed, like `policy.revoke_policy`/`worker.enqueue_run`/`worker.
cancel_run`.** Neither performs a risky external call before its own
write completes (`worker.py`'s own commit-placement discipline is
specifically about protecting a write that must be durable *before* an
adapter's `execute()` runs); `worker.py`'s dispatch gate is the one call
site that pairs `create_approval_request` with pausing the run to
`waiting_approval` and explicitly commits that pair together on a bare
session, for the identical durability reason `run_step`'s own digest write
does (a crash between the two must never leave the run silently advancing
past a step no human ever approved).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from json import dumps
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session
from ecc.observability import (
    queue_lifecycle_event,
    record_audit_outbox_failure,
    record_idempotency_conflict,
)

from .adapters import ActionAdapter
from .policy import AutomationPolicy

Decision = Literal["approved", "rejected"]
StoredStatus = Literal["pending", "approved", "rejected"]
LifecycleStatus = Literal["pending", "approved", "rejected", "expired"]

_APPROVAL_EXPIRY_HOURS = 24

# The `workflow_run_steps.error_class` a human rejection writes onto the
# step it declined (`_advance_run_after_decision`'s own docstring). Named
# rather than inlined so `worker.py`'s own `'failed'`-outcome handling and
# this module's tests refer to one definition -- the same "exactly one place
# this value lives" discipline `worker.py` applies to its own constants.
_REJECTED_STEP_ERROR_CLASS = "ApprovalRejected"

_APPROVAL_FIELDS = """
    id, workspace_id, run_id, step_index, action_digest, high_impact_categories,
    status, requested_at, expires_at, decided_at, decision, decided_by,
    created_at, updated_at
"""


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    id: UUID
    workspace_id: UUID
    run_id: UUID
    step_index: int
    action_digest: str
    high_impact_categories: tuple[str, ...]
    status: StoredStatus
    requested_at: datetime
    expires_at: datetime
    decided_at: datetime | None
    decision: Decision | None
    decided_by: UUID | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class ApprovalNotFound:
    pass


@dataclass(frozen=True, slots=True)
class ApprovalDigestMismatch:
    expected_digest: str
    provided_digest: str | None


@dataclass(frozen=True, slots=True)
class ApprovalAlreadyDecided:
    decision: Decision
    decided_at: datetime


@dataclass(frozen=True, slots=True)
class ApprovalExpired:
    expires_at: datetime


def approval_lifecycle_status(
    approval: ApprovalRequest, *, now: datetime | None = None
) -> LifecycleStatus:
    """`policy.policy_status`'s exact analogue for this table (module
    docstring): a `pending` row whose `expires_at` has passed reports as
    `expired` without that ever being written back to the database; a
    decided row reports its own stored `status` unchanged.
    """
    moment = now if now is not None else datetime.now(UTC)
    if approval.status == "pending" and approval.expires_at <= moment:
        return "expired"
    return approval.status


def evaluate_approval_requirement(
    adapter: ActionAdapter,
    policy: AutomationPolicy,
    *,
    action_step_count_so_far: int,
) -> bool:
    """Whether the step about to be dispatched against `adapter`, under
    `policy`, needs a fresh, digest-bound human approval before `worker.
    run_step` may call `adapter.execute()`. Pure -- see module docstring.

    `action_step_count_so_far` is the number of `workflow_run_steps` rows
    (any status) this run has already written *before* the step under
    evaluation -- i.e. how many action steps this run has already
    attempted. Required, not optional, so a caller cannot forget to
    compute it and accidentally fall through to `0` (which would silently
    under-count and never trigger `policy-limit-exceeding`) -- fail closed
    on a missing count the same way this function fails closed on every
    other ambiguity.
    """
    if adapter.high_impact_categories:
        return True
    if policy.approval_mode in ("preview_only", "per_run"):
        # `preview_only` answers this question identically to `per_run` on
        # purpose -- it is *also* blocked from real dispatch afterwards, by
        # `worker._evaluate_dispatch_gate`, never here (module docstring's
        # own "`preview_only` requires approval for every step" section: this
        # function returning `True` is what gives an operator a real
        # approval flow to rehearse under the mode).
        return True
    if policy.approval_mode != "bounded_recurring":
        # Unreachable under the current schema (CHECK constraint / Literal
        # both restrict approval_mode to the three known values) -- fail
        # closed rather than silently permitting dispatch under a mode
        # this function does not recognize.
        return True
    return action_step_count_so_far >= policy.count_limit


def _row_to_approval(row: dict[str, Any]) -> ApprovalRequest:
    return ApprovalRequest(
        id=row["id"],
        workspace_id=row["workspace_id"],
        run_id=row["run_id"],
        step_index=row["step_index"],
        action_digest=row["action_digest"],
        high_impact_categories=tuple(row["high_impact_categories"]),
        status=row["status"],
        requested_at=row["requested_at"],
        expires_at=row["expires_at"],
        decided_at=row["decided_at"],
        decision=row["decision"],
        decided_by=row["decided_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def get_approval(session: Session, workspace_id: UUID, approval_id: UUID) -> ApprovalRequest | None:
    row = (
        session.execute(
            text(
                f"SELECT {_APPROVAL_FIELDS} FROM approval_requests "
                "WHERE workspace_id = :workspace_id AND id = :id"
            ),
            {"workspace_id": workspace_id, "id": approval_id},
        )
        .mappings()
        .one_or_none()
    )
    return _row_to_approval(dict(row)) if row is not None else None


def list_approvals(
    session: Session, workspace_id: UUID, *, status_filter: StoredStatus | None = None
) -> list[ApprovalRequest]:
    clause = "AND status = :status_filter" if status_filter is not None else ""
    params: dict[str, Any] = {"workspace_id": workspace_id}
    if status_filter is not None:
        params["status_filter"] = status_filter
    rows = (
        session.execute(
            text(
                f"SELECT {_APPROVAL_FIELDS} FROM approval_requests "
                f"WHERE workspace_id = :workspace_id {clause} ORDER BY requested_at ASC"
            ),
            params,
        )
        .mappings()
        .all()
    )
    return [_row_to_approval(dict(row)) for row in rows]


def get_pending_approval(
    session: Session, workspace_id: UUID, run_id: UUID, step_index: int
) -> ApprovalRequest | None:
    """The open (`pending`) request for this exact step, if any -- at most
    one can exist at a time (`uq_approval_requests_pending_per_run_step`,
    migration 0040).
    """
    row = (
        session.execute(
            text(
                f"SELECT {_APPROVAL_FIELDS} FROM approval_requests "
                "WHERE workspace_id = :workspace_id AND run_id = :run_id "
                "AND step_index = :step_index AND status = 'pending'"
            ),
            {"workspace_id": workspace_id, "run_id": run_id, "step_index": step_index},
        )
        .mappings()
        .one_or_none()
    )
    return _row_to_approval(dict(row)) if row is not None else None


def get_approved_request(
    session: Session, workspace_id: UUID, run_id: UUID, step_index: int, action_digest: str
) -> ApprovalRequest | None:
    """An `approved` request for this exact `(run_id, step_index,
    action_digest)` -- the only thing that clears `worker.py`'s dispatch
    gate once a step has been determined to require approval. Bound to the
    live digest by the caller (`worker.py` recomputes it at every dispatch
    attempt, never trusts a previously-stored value) -- a digest that
    changed between request and dispatch never matches here, mechanically
    enforcing "changed or replayed approvals are rejected" (Threat model).
    """
    row = (
        session.execute(
            text(
                f"SELECT {_APPROVAL_FIELDS} FROM approval_requests "
                "WHERE workspace_id = :workspace_id AND run_id = :run_id "
                "AND step_index = :step_index AND action_digest = :action_digest "
                "AND status = 'approved' ORDER BY decided_at DESC LIMIT 1"
            ),
            {
                "workspace_id": workspace_id,
                "run_id": run_id,
                "step_index": step_index,
                "action_digest": action_digest,
            },
        )
        .mappings()
        .one_or_none()
    )
    return _row_to_approval(dict(row)) if row is not None else None


def create_approval_request(
    session: Session,
    workspace_id: UUID,
    run_id: UUID,
    step_index: int,
    action_digest: str,
    high_impact_categories: frozenset[str] | tuple[str, ...],
) -> ApprovalRequest:
    """Creates a `pending` row, `expires_at = requested_at + 24h` (Decision
    6, computed here in application code -- no DB default, matching
    `policy.create_policy`'s identical `expires_at` precedent exactly).
    Caller-committed -- see module docstring.
    """
    now = datetime.now(UTC)
    approval_id = uuid4()
    session.execute(
        text(
            """
            INSERT INTO approval_requests (
                id, workspace_id, run_id, step_index, action_digest,
                high_impact_categories, status, requested_at, expires_at,
                created_at, updated_at
            ) VALUES (
                :id, :workspace_id, :run_id, :step_index, :action_digest,
                :high_impact_categories, 'pending', :now, :expires_at, :now, :now
            )
            """
        ),
        {
            "id": approval_id,
            "workspace_id": workspace_id,
            "run_id": run_id,
            "step_index": step_index,
            "action_digest": action_digest,
            "high_impact_categories": sorted(high_impact_categories),
            "now": now,
            "expires_at": now + timedelta(hours=_APPROVAL_EXPIRY_HOURS),
        },
    )
    result = get_approval(session, workspace_id, approval_id)
    assert result is not None  # just inserted, in the same transaction
    return result


def decide_approval(
    session: Session,
    workspace_id: UUID,
    actor_id: UUID,
    approval_id: UUID,
    decision: Decision,
    *,
    current_action_digest: str | None,
) -> (
    ApprovalRequest
    | ApprovalNotFound
    | ApprovalDigestMismatch
    | ApprovalAlreadyDecided
    | ApprovalExpired
):
    """Records a human decision. `current_action_digest` is required for
    `decision == "approved"` (echoed by the caller -- `API-SCHEMAS.md`:
    "requires the caller to echo the exact `action_digest`") and checked
    against the stored `action_digest` verbatim; a mismatch (including a
    caller that omitted it) is rejected, never silently accepted (module
    docstring). Not required for `"rejected"` -- declining an action is
    safe regardless of whether the caller also knows its exact digest, and
    `API-SCHEMAS.md` only names the echo requirement for `/approve`.

    A row already decided is `ApprovalAlreadyDecided` (a second decision on
    the same request is rejected, not silently accepted or overwritten --
    matches `policy.revoke_policy`'s identical precedent for an
    already-revoked policy). A `pending` row whose `expires_at` has passed
    is `ApprovalExpired` -- computed live (`approval_lifecycle_status`),
    never decided either way, matching `DATA-MODEL.md` verbatim ("never
    implicitly approved").
    """
    row = (
        session.execute(
            text(
                f"SELECT {_APPROVAL_FIELDS} FROM approval_requests "
                "WHERE workspace_id = :workspace_id AND id = :id FOR UPDATE"
            ),
            {"workspace_id": workspace_id, "id": approval_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return ApprovalNotFound()

    approval = _row_to_approval(dict(row))
    if approval.status != "pending":
        assert approval.decision is not None and approval.decided_at is not None
        return ApprovalAlreadyDecided(decision=approval.decision, decided_at=approval.decided_at)

    now = datetime.now(UTC)
    if approval_lifecycle_status(approval, now=now) == "expired":
        return ApprovalExpired(expires_at=approval.expires_at)

    if decision == "approved" and (
        current_action_digest is None or current_action_digest != approval.action_digest
    ):
        return ApprovalDigestMismatch(
            expected_digest=approval.action_digest, provided_digest=current_action_digest
        )

    session.execute(
        text(
            "UPDATE approval_requests SET status = :decision, decision = :decision, "
            "decided_at = :now, decided_by = :actor_id, updated_at = :now WHERE id = :id"
        ),
        {"decision": decision, "now": now, "actor_id": actor_id, "id": approval_id},
    )
    # Unblocks (approved) or fails-with-rollback (rejected) the run this
    # request gates, in the same uncommitted unit of work as the decision
    # itself -- folded into decide_approval (rather than left for each call
    # site to remember) so every caller, HTTP or direct, gets correct run-
    # advancement for free. See _advance_run_after_decision's own docstring
    # for why this is a run_id/step_index-scoped raw-SQL helper rather than
    # a call into worker.py (circular import), and for why a rejection
    # re-queues the run instead of terminating it here.
    _advance_run_after_decision(
        session,
        workspace_id,
        approval.run_id,
        approval.step_index,
        approval.action_digest,
        decision,
    )
    result = get_approval(session, workspace_id, approval_id)
    assert result is not None
    return result


# --- GET /api/v1/automations/approvals, POST .../{id}/approve|reject ------

router = APIRouter(prefix="/api/v1/automations", tags=["automation"])
SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]


class ApprovalResponse(BaseModel):
    id: UUID
    run_id: UUID
    step_index: int
    action_digest: str
    high_impact_categories: list[str]
    status: LifecycleStatus
    requested_at: datetime
    expires_at: datetime
    decided_at: datetime | None
    decision: Decision | None
    decided_by: UUID | None
    created_at: datetime
    updated_at: datetime


class ApprovalListResponse(BaseModel):
    approvals: list[ApprovalResponse]


class ApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # sha256 hex digest -- worker.compute_action_digest's own output shape.
    action_digest: str = Field(min_length=64, max_length=64)


class _EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _to_response(approval: ApprovalRequest) -> ApprovalResponse:
    return ApprovalResponse(
        id=approval.id,
        run_id=approval.run_id,
        step_index=approval.step_index,
        action_digest=approval.action_digest,
        high_impact_categories=list(approval.high_impact_categories),
        status=approval_lifecycle_status(approval),
        requested_at=approval.requested_at,
        expires_at=approval.expires_at,
        decided_at=approval.decided_at,
        decision=approval.decision,
        decided_by=approval.decided_by,
        created_at=approval.created_at,
        updated_at=approval.updated_at,
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
) -> ApprovalResponse | None:
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
        record_idempotency_conflict("approval_request")
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
    return ApprovalResponse.model_validate(row["response_body"])


def _store_idempotency(
    session: Session,
    auth: AuthContext,
    key: str,
    request_hash: str,
    response: ApprovalResponse,
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


def _write_side_effects(
    session: Session,
    auth: AuthContext,
    request: Request,
    *,
    event_type: str,
    aggregate_id: UUID,
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
                    :id, :workspace_id, :event_type, 'approval_request', :aggregate_id,
                    1, :actor_id, :request_id, :correlation_id,
                    ARRAY['*'], 'allowed', 'user', '{}'::jsonb, :occurred_at
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": auth.workspace_id,
                "event_type": event_type,
                "aggregate_id": aggregate_id,
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
                "payload": dumps({"aggregate_id": str(aggregate_id)}),
                "occurred_at": now,
            },
        )
    except SQLAlchemyError:
        record_audit_outbox_failure("approval_request")
        raise
    queue_lifecycle_event(session, "approval_request", event_type, "allowed")


def _advance_run_after_decision(
    session: Session,
    workspace_id: UUID,
    run_id: UUID,
    step_index: int,
    action_digest: str,
    decision: Decision,
) -> None:
    """Unblocks (or fails, with rollback) the run this decision applies to.
    Written as its own small raw-SQL helper directly against
    `workflow_runs`/`workflow_run_steps`, rather than importing `worker.py`'s
    own row-mutating functions, to avoid a circular import: `worker.py`
    already imports this module (its dispatch gate calls `evaluate_approval_
    requirement`/`create_approval_request`/`get_pending_approval`/`get_
    approved_request`), so this module cannot import `worker.py` back. The
    SQL here is intentionally minimal and does not need `worker.py`'s
    `WorkflowRun` dataclass or its read helpers.

    Only acts if the run is still exactly `waiting_approval` **and** still
    paused at this exact `step_index` -- an approval decided for a step the
    run has already moved past (cancelled in the meantime, or -- not
    reachable in this task's own scope, since a run only ever pauses on
    its own `current_step_index` -- somehow advanced independently) is a
    no-op here, never a stale resurrection of a run that moved on for a
    different reason.

    **Both decisions send the run back to `'queued'`, and neither
    terminates it here.** `approved` always did: `claim_next_run`'s ordinary
    claim predicate picks it up on the next poll cycle, `run_step`'s gate
    re-evaluates for the same step, finds the now-`approved` row matching
    the live digest, and dispatches -- reusing the existing claim/poll
    machinery entirely, with no resume-specific code path.

    `rejected` now does the same thing, and this is the fix to a real
    Task 3 x Task 6 seam bug an adversarial review found. This branch used
    to write `status = 'failed'` directly, justified by "matching how an
    adapter's own raised exception also produces `failed`" -- a
    justification Task 6 silently invalidated when it routed
    adapter-exception failures through `worker._qualifying_compensations`/
    `_mark_compensating`/`_run_compensation_sequence` instead. The result
    was that an identical step failing by accident (an adapter raised) got
    its already-succeeded predecessors compensated, while the same step
    failing because **a human explicitly said no** did not -- the more
    consequential case getting strictly worse handling, and no rollback of
    real side effects an operator had just declined to build on.

    Rather than duplicating Task 6's compensation-qualifying logic here (or
    importing it, which the circular import above forbids, or extracting it
    to a third module, which would split one cohesive mechanic across two
    files for one call site), this writes the *one* fact `worker.py` needs
    in order to reach that logic through its own existing, single code
    path, and hands the run back to the poll loop:

    1. A `workflow_run_steps` row for the rejected `step_index` at
       `status='failed'`, `error_class='ApprovalRejected'`, carrying the
       approval's own `action_digest`. (The gate deliberately writes no row
       for a step it pauses -- `worker.py`'s "Task 3's approval/policy gate"
       section -- so this is normally an `INSERT`; `ON CONFLICT` makes it
       idempotent against any row that somehow already exists.) `input` is
       left `'{}'` deliberately: no adapter input was ever resolved or sent
       for this step, and this row records the rejection, not a dispatch
       attempt.
    2. `status = 'queued'` on the run.

    `run_step`'s existing-row branch then returns that `'failed'` outcome
    *without ever calling `execute()`* (a `'failed'` row short-circuits
    exactly like a `'succeeded'` one, and the gate never re-runs because a
    row now exists -- so no second approval request is ever created and no
    dispatch loop is possible), and `process_claimed_run`'s own `'failed'`
    branch takes it from there: compensations for qualifying
    already-succeeded steps if any are declared, ending
    `'compensated'`/`'compensation_failed'`, or the unchanged
    `_finish_run(..., 'failed')` if none are. A rejected run in a workflow
    with no `compensate_ref` anywhere therefore still ends `'failed'`,
    exactly as before -- just one poll cycle later, and now through the
    same seam every other failure goes through. Parking a run back in
    `'queued'` to let the ordinary claim/poll machinery continue it is this
    module's and `worker.py`'s established pattern for precisely this
    situation, not a new one (`approved` above; `resume_run`; the bounded-
    retry path's own `next_attempt_at` re-queue).
    """
    row = (
        session.execute(
            text(
                "SELECT status, current_step_index FROM workflow_runs "
                "WHERE workspace_id = :workspace_id AND id = :id FOR UPDATE"
            ),
            {"workspace_id": workspace_id, "id": run_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return
    if row["status"] != "waiting_approval" or row["current_step_index"] != step_index:
        return

    now = datetime.now(UTC)
    if decision == "rejected":
        session.execute(
            text(
                """
                INSERT INTO workflow_run_steps (
                    id, workspace_id, run_id, step_index, step_type, status,
                    action_digest, input, started_at, finished_at, error_class,
                    created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, :run_id, :step_index, 'action', 'failed',
                    :action_digest, '{}'::jsonb, :now, :now, :error_class, :now, :now
                )
                ON CONFLICT ON CONSTRAINT uq_workflow_run_steps_run_step_index DO UPDATE SET
                    status = 'failed', error_class = :error_class,
                    finished_at = :now, updated_at = :now
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "run_id": run_id,
                "step_index": step_index,
                "action_digest": action_digest,
                "error_class": _REJECTED_STEP_ERROR_CLASS,
                "now": now,
            },
        )
    session.execute(
        text(
            "UPDATE workflow_runs SET status = 'queued', queued_at = :now, "
            "leased_by = NULL, leased_until = NULL, updated_at = :now WHERE id = :id"
        ),
        {"now": now, "id": run_id},
    )


@router.get("/approvals", response_model=ApprovalListResponse)
def list_approvals_endpoint(
    auth: AuthDep,
    session: SessionDep,
    approval_status: Annotated[StoredStatus | None, Query(alias="status")] = None,
) -> ApprovalListResponse:
    approvals = list_approvals(session, auth.workspace_id, status_filter=approval_status)
    return ApprovalListResponse(approvals=[_to_response(a) for a in approvals])


@router.post("/approvals/{approval_id}/approve", response_model=ApprovalResponse)
def approve_endpoint(
    approval_id: UUID,
    payload: ApproveRequest,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> ApprovalResponse:
    request_hash = _request_hash(payload, f"approve:{approval_id}")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached

        result = decide_approval(
            session,
            auth.workspace_id,
            auth.user_id,
            approval_id,
            "approved",
            current_action_digest=payload.action_digest,
        )
        if isinstance(result, ApprovalNotFound):
            raise HTTPException(status_code=404, detail="APPROVAL_NOT_FOUND")
        if isinstance(result, ApprovalDigestMismatch):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "DIGEST_MISMATCH",
                    "expected_digest": result.expected_digest,
                    "provided_digest": result.provided_digest,
                },
            )
        if isinstance(result, ApprovalAlreadyDecided):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "APPROVAL_ALREADY_DECIDED",
                    "decision": result.decision,
                    "decided_at": result.decided_at.isoformat(),
                },
            )
        if isinstance(result, ApprovalExpired):
            raise HTTPException(
                status_code=409,
                detail={"code": "APPROVAL_EXPIRED", "expires_at": result.expires_at.isoformat()},
            )

        response = _to_response(result)
        _write_side_effects(
            session,
            auth,
            request,
            event_type="approval_request.approved",
            aggregate_id=result.id,
            now=now,
        )
        _store_idempotency(session, auth, idempotency_key, request_hash, response, now)
        return response


@router.post("/approvals/{approval_id}/reject", response_model=ApprovalResponse)
def reject_endpoint(
    approval_id: UUID,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> ApprovalResponse:
    request_hash = _request_hash(_EmptyBody(), f"reject:{approval_id}")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached

        result = decide_approval(
            session,
            auth.workspace_id,
            auth.user_id,
            approval_id,
            "rejected",
            current_action_digest=None,
        )
        if isinstance(result, ApprovalNotFound):
            raise HTTPException(status_code=404, detail="APPROVAL_NOT_FOUND")
        if isinstance(result, ApprovalDigestMismatch):
            # Unreachable for decision="rejected" (decide_approval only
            # checks the digest when approving) -- handled anyway so this
            # endpoint's own type checking stays exhaustive.
            raise HTTPException(status_code=409, detail={"code": "DIGEST_MISMATCH"})
        if isinstance(result, ApprovalAlreadyDecided):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "APPROVAL_ALREADY_DECIDED",
                    "decision": result.decision,
                    "decided_at": result.decided_at.isoformat(),
                },
            )
        if isinstance(result, ApprovalExpired):
            raise HTTPException(
                status_code=409,
                detail={"code": "APPROVAL_EXPIRED", "expires_at": result.expires_at.isoformat()},
            )

        response = _to_response(result)
        _write_side_effects(
            session,
            auth,
            request,
            event_type="approval_request.rejected",
            aggregate_id=result.id,
            now=now,
        )
        _store_idempotency(session, auth, idempotency_key, request_hash, response, now)
        return response
