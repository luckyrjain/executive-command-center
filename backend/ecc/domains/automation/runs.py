"""The `/api/v1/automations/runs` HTTP surface (`docs/phases/phase-005/
API-SCHEMAS.md`): `GET|POST /automations/runs`, `GET /automations/
runs/{id}`, `POST /automations/runs/{id}/pause|resume|cancel`.

**A new module, not folded into `worker.py`.** `worker.py` is deliberately
router-less (its own module docstring: an internally-invokable component,
not a per-request HTTP layer) -- this mirrors how `approvals.py`/
`policy.py` each own their own table's HTTP surface while the underlying
mutation/read functions those routers call can live in whichever module
owns the table (`pause_run`/`resume_run`/`list_runs` all live in
`worker.py` itself, next to `cancel_run`/`get_run`/`list_run_steps`,
following this package's own "reads and writes against a table live in
that table's owning module" convention -- see `worker.list_runs`'s own
docstring). This module is the thin HTTP-shape layer on top: request/
response Pydantic models, auth/CSRF/idempotency wiring, and the audit/
outbox side-effect writes -- identical division of labor to every other
router in this package.

**`POST /automations/runs` is Decision 7's "Manual" trigger.** "a
user-initiated run, subject to the same policy/approval evaluation as any
other trigger source (no manual-trigger bypass of authority checks)" --
mechanically true here because this endpoint calls the exact same `worker.
enqueue_run` the scheduler tick (`scheduler.run_scheduler_once`) calls; a
manually-triggered run pins the workflow's own `active` version and
`policy_ref` exactly like a scheduled one, and is dispatched through the
identical `run_step` policy/approval gate. The request body names only
`workflow_id` -- never a caller-supplied `policy_id` (`ManualRunRequest`'s
`model_config = ConfigDict(extra="forbid")` rejects any extra field,
including a `policy_id` a caller might try to smuggle in, with a `422`
rather than silently ignoring or honoring it) -- matching every other
Phase 5 endpoint's confused-deputy protection (`API-SCHEMAS.md`: "no
endpoint accepts a caller-supplied `policy_id` override"). The policy
actually in force is always the one `enqueue_run` resolves server-side
from the workflow's own currently-`active` `workflow_versions.policy_ref`.

**`GET /automations/runs/{id}` includes step detail, already redacted.**
`worker._redact_payload` runs at *write* time (`run_step`'s own `INSERT`/
`UPDATE` statements), never at read time -- `RunDetailResponse` embeds
`worker.list_run_steps`'s own return values as-is, with no additional
redaction pass in this module, since there is nothing left to redact by
the time a step row reaches this endpoint (`worker.py`'s own module
docstring: "any dict key ... has its value replaced before either ...
column is ever written").

**Task 7a: `RunDetailResponse` also embeds `compensation_steps`.** `worker.
list_compensation_steps` (existing since Task 6, never wired into this
router before now) -- `UX-STATES.md`'s own named gap for "partially
compensated" being visible via a real endpoint, not only a direct DB read
(`docs.superpowers/specs/2026-07-25-phase-5-automation-design.md`'s "shows
steps, permissions, side effects, approval points and irreversibility"
extends naturally to the compensation ledger too). `compensation_steps`
carries no `input`/`output` field at all (unlike `workflow_run_steps`), so
there is nothing for this endpoint to redact there either.

**Required error codes in scope here** (`API-SCHEMAS.md`): `schema_invalid`
(mechanically enforced by every request model's own Pydantic validation --
FastAPI's `422` response for a malformed body) and `workflow_not_active`
(`POST /automations/runs`, `worker.enqueue_run` returning `WorkflowNotActive`
for a workflow with no active version -- also covers "workflow does not
exist at all," which resolves to the identical `None` from `get_active_
workflow_version` either way, matching `workflows.py`'s own established
precedent of not distinguishing the two). `rate_limited` (`RATE_LIMITED`,
409) is handled here too, as of the `preview_only`/`rate_limit` docs-vs-code
reconciliation: `worker.enqueue_run` rejects a run that would exceed its
authorizing policy's own `rate_limit.runs_per_workflow_per_hour` ceiling
before creating any `workflow_runs` row at all (`APPROVAL-POLICY.md`: "Next
run past the limit rejected at enqueue with `rate_limited`"), and this
endpoint maps that outcome to the required error code -- the same shape
`workflow_not_active`/`kill_switch_active` already use. `policy_expired`/
`policy_revoked`/`digest_mismatch`/`approval_expired`/`simulation_only` are
not surfaced by this router directly -- they are properties of a run's own
step-dispatch outcome (`needs_review`/`waiting_approval`, already fully
handled inside `worker.run_step`/`approvals.py`) visible through this
router's own `GET` responses rather than raised as HTTP errors by a `POST`
here. `kill_switch_active` (`KILL_SWITCH_ACTIVE`, 409) is now handled here
too (Task 6, "Compensation, observability and kill switches") -- `worker.
enqueue_run` rejects a killed workflow (`WorkflowKilled`) before creating
any `workflow_runs` row at all; this endpoint simply maps that outcome to
the required error code, the same shape `workflow_not_active` already
uses.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session
from ecc.observability import queue_lifecycle_event
from ecc.platform import audit_outbox, authz
from ecc.platform.idempotency import load_cached, lock_idempotency, request_hash, store_idempotency

from . import worker as worker_module
from . import workflows as workflows_module
from .worker import (
    RunStatus,
    WorkflowRun,
    WorkflowRunNotFound,
    WorkflowRunNotPaused,
    WorkflowRunStep,
)

router = APIRouter(prefix="/api/v1/automations", tags=["automation"])
SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]


# ---------------------------------------------------------------------------
# Request/response models.
# ---------------------------------------------------------------------------


class ManualRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    # Names the workflow only -- never a policy. See module docstring's
    # confused-deputy section: extra="forbid" rejects a caller-supplied
    # `policy_id` (or any other field) with a 422 rather than silently
    # ignoring or, worse, honoring it.
    workflow_id: str = Field(min_length=1, max_length=200)


class RunStepResponse(BaseModel):
    step_index: int
    step_type: str
    status: str
    action_digest: str | None
    # Task 7 addition: `workflow_run_steps.attempt_count` (Task 6) was never
    # wired into this response before now -- a real, small, disclosed gap
    # found while building the "retrying" UX-STATES.md state (`status ==
    # "retrying"` alone tells a caller a step is retrying, but not which
    # attempt, out of `MAX_RETRY_ATTEMPTS`, it's on). `worker.
    # WorkflowRunStep` already carries this field; this is a pure response-
    # shape addition, no new query.
    attempt_count: int
    # Found during my own independent review of this task's PR: `UX-
    # STATES.md`'s own approval-decision requirement ("the exact target ...
    # shown before the approve action is reachable") was not actually met --
    # this response carried `step_type` (a generic `'action'`/`'approval_
    # gate'`/...) and the raw redacted `input`, but never *which* adapter/
    # action a step invokes, so an approver had no way to see the actual
    # target of what they were authorizing. `workflow_run_steps` itself has
    # no `action_ref` column (only `workflow_versions.graph` does, resolved
    # at dispatch time and never persisted onto the row) -- resolved here at
    # read time instead, from the run's own pinned `(workflow_id,
    # workflow_version)` graph, no migration needed. `None` for a step whose
    # `step_type` has no `action_ref` at all (`approval_gate`/`condition`) or
    # whose pinned version could not be resolved (should not happen for any
    # run that ever dispatched a step, since dispatch itself requires the
    # version to exist -- a defensive `None`, not an assumed invariant).
    action_ref: str | None
    input: dict[str, Any]
    output: dict[str, Any] | None
    started_at: datetime | None
    finished_at: datetime | None
    error_class: str | None


class RunResponse(BaseModel):
    id: UUID
    workflow_id: str
    workflow_version: int
    policy_id: UUID | None
    trigger_ref: str | None
    status: RunStatus
    current_step_index: int
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CompensationStepResponse(BaseModel):
    """Task 7a's addition -- `UX-STATES.md`'s own named gap: "'Partially
    compensated' is realized as a run in `compensation_failed` whose
    `compensation_steps` ledger (`GET /automations/runs/{id}`, once a
    future task exposes it ...) shows a mix of `succeeded`/`failed` rows."
    Mirrors `RunStepResponse`'s own shape for the sibling table exactly --
    the same fields `worker.CompensationStep` itself carries, minus
    `id`/`workspace_id`/`run_id` (implicit from the containing response),
    matching `RunStepResponse`'s own identical omissions.
    """

    compensates_step_index: int
    status: str
    action_digest: str | None
    started_at: datetime | None
    finished_at: datetime | None
    error_class: str | None


class RunDetailResponse(RunResponse):
    steps: list[RunStepResponse]
    compensation_steps: list[CompensationStepResponse]


class RunListResponse(BaseModel):
    runs: list[RunResponse]


class _EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _to_response(run: WorkflowRun) -> RunResponse:
    return RunResponse(
        id=run.id,
        workflow_id=run.workflow_id,
        workflow_version=run.workflow_version,
        policy_id=run.policy_id,
        trigger_ref=run.trigger_ref,
        status=run.status,
        current_step_index=run.current_step_index,
        queued_at=run.queued_at,
        started_at=run.started_at,
        finished_at=run.finished_at,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _step_to_response(
    step: WorkflowRunStep, action_ref_by_index: dict[int, str]
) -> RunStepResponse:
    return RunStepResponse(
        step_index=step.step_index,
        step_type=step.step_type,
        status=step.status,
        action_digest=step.action_digest,
        attempt_count=step.attempt_count,
        action_ref=action_ref_by_index.get(step.step_index),
        input=step.input,
        output=step.output,
        started_at=step.started_at,
        finished_at=step.finished_at,
        error_class=step.error_class,
    )


def _action_ref_by_step_index(session: Session, run: WorkflowRun) -> dict[int, str]:
    """Resolves every step's own `action_ref` from the run's pinned
    `(workflow_id, workflow_version)` graph -- the same immutable graph
    `worker.run_step` itself dispatches against, so this is a genuine
    reflection of what each step actually invokes, not a guess. Returns an
    empty map (never raises) if the pinned version cannot be resolved --
    `RunStepResponse.action_ref` degrades to `None` per its own docstring
    rather than failing the whole run-detail request over a display-only
    field.
    """
    version = workflows_module.get_workflow_version(
        session, run.workspace_id, run.workflow_id, run.workflow_version
    )
    if version is None:
        return {}
    return {
        index: step["action_ref"]
        for index, step in enumerate(version.graph.get("steps", []))
        if step.get("action_ref")
    }


def _compensation_step_to_response(
    step: worker_module.CompensationStep,
) -> CompensationStepResponse:
    return CompensationStepResponse(
        compensates_step_index=step.compensates_step_index,
        status=step.status,
        action_digest=step.action_digest,
        started_at=step.started_at,
        finished_at=step.finished_at,
        error_class=step.error_class,
    )


def _to_detail_response(
    run: WorkflowRun,
    steps: list[WorkflowRunStep],
    compensation_steps: list[worker_module.CompensationStep],
    action_ref_by_index: dict[int, str],
) -> RunDetailResponse:
    base = _to_response(run)
    return RunDetailResponse(
        **base.model_dump(),
        steps=[_step_to_response(s, action_ref_by_index) for s in steps],
        compensation_steps=[_compensation_step_to_response(c) for c in compensation_steps],
    )


# ---------------------------------------------------------------------------
# Idempotency-lock/audit/outbox helpers -- lock/cache/store delegate to
# ecc.platform.idempotency (domain="workflow_run"); identical pattern to
# workflows.py/policy.py/approvals.py. Audit/outbox writes delegate to
# ecc.platform.audit_outbox.write_audit_and_outbox.
# ---------------------------------------------------------------------------


def _write_side_effects(
    session: Session,
    auth: AuthContext,
    request: Request,
    *,
    event_type: str,
    aggregate_id: UUID,
    now: datetime,
) -> None:
    audit_outbox.write_audit_and_outbox(
        session,
        auth,
        request,
        event_type=event_type,
        aggregate_type="workflow_run",
        aggregate_id=aggregate_id,
        aggregate_version=1,
        changed_fields=["*"],
        payload={"aggregate_id": str(aggregate_id)},
        now=now,
        domain="workflow_run",
    )
    queue_lifecycle_event(session, "workflow_run", event_type, "allowed")


# ---------------------------------------------------------------------------
# Endpoints.
# ---------------------------------------------------------------------------


@router.get("/runs", response_model=RunListResponse)
def list_runs_endpoint(
    auth: AuthDep,
    session: SessionDep,
    run_status: Annotated[RunStatus | None, Query(alias="status")] = None,
) -> RunListResponse:
    runs = worker_module.list_runs(session, auth, auth.workspace_id, status_filter=run_status)
    session.rollback()
    return RunListResponse(runs=[_to_response(r) for r in runs])


@router.get("/runs/{run_id}", response_model=RunDetailResponse)
def get_run_endpoint(run_id: UUID, auth: AuthDep, session: SessionDep) -> RunDetailResponse:
    visible = authz.authorize(
        session, auth, resource_type="workflow_runs", resource_id=run_id, action="read"
    )
    session.rollback()
    if not visible:
        raise HTTPException(status_code=404, detail="RUN_NOT_FOUND")
    run = worker_module.get_run(session, auth.workspace_id, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="RUN_NOT_FOUND")
    steps = worker_module.list_run_steps(session, auth.workspace_id, run_id)
    compensation_steps = worker_module.list_compensation_steps(session, auth.workspace_id, run_id)
    action_ref_by_index = _action_ref_by_step_index(session, run)
    return _to_detail_response(run, steps, compensation_steps, action_ref_by_index)


@router.post("/runs", response_model=RunResponse, status_code=status.HTTP_201_CREATED)
def create_run_endpoint(
    payload: ManualRunRequest,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> RunResponse:
    authz.require_role_action(session, auth, "write")
    req_hash = request_hash(payload, "create_run")
    now = datetime.now(UTC)
    with session.begin():
        lock_idempotency(session, auth, idempotency_key)
        cached = load_cached(
            session,
            auth,
            idempotency_key,
            req_hash,
            domain="workflow_run",
            response_model=RunResponse,
        )
        if cached is not None:
            return cached

        # Decision 7's "Manual" trigger: a user-initiated run, subject to
        # the identical policy/approval evaluation as any other trigger
        # source. trigger_ref names the actor, never a caller-supplied
        # policy -- worker.enqueue_run resolves the workflow's own active
        # policy_ref server-side (module docstring's confused-deputy
        # section).
        result = worker_module.enqueue_run(
            session,
            auth.workspace_id,
            auth.user_id,
            workflow_id=payload.workflow_id,
            trigger_ref=f"manual:{auth.user_id}",
        )
        if isinstance(result, worker_module.WorkflowNotActive):
            raise HTTPException(
                status_code=409,
                detail={"code": "WORKFLOW_NOT_ACTIVE", "workflow_id": result.workflow_id},
            )
        if isinstance(result, worker_module.RunRateLimited):
            # API-SCHEMAS.md's required `rate_limited` error code, real for
            # the first time (see worker.py's own "`rate_limit` is enforced
            # at enqueue time" docstring section for the full docs-vs-code
            # history). 409, matching WORKFLOW_NOT_ACTIVE/KILL_SWITCH_ACTIVE:
            # like both of those, this is a conflict with the target's own
            # current state (this workflow has already used its hourly
            # allowance), not a malformed payload -- and, like both, it is
            # raised only after `enqueue_run` has already declined to write
            # any `workflow_runs` row, so the rejection leaves no partial
            # state behind for a caller to clean up.
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "RATE_LIMITED",
                    "workflow_id": result.workflow_id,
                    "limit": result.limit,
                    "runs_in_window": result.runs_in_window,
                },
            )
        if isinstance(result, worker_module.WorkflowKilled):
            # Task 6: API-SCHEMAS.md's kill_switch_active error code, for
            # this call site -- `enqueue_run` is the single choke point
            # both this endpoint and the scheduler's own fire path go
            # through (worker.py's own "Task 6: kill switches" docstring
            # section).
            raise HTTPException(
                status_code=409,
                detail={"code": "KILL_SWITCH_ACTIVE", "workflow_id": result.workflow_id},
            )
        if isinstance(result, worker_module.ActorMembershipInactive):
            # Defensive only, not a real reachable path here: `auth.user_id`
            # already passed `authz.require_role_action`'s own active-
            # membership check a few lines above, immediately upstream of
            # this same `enqueue_run` call, so this branch exists purely to
            # keep every member of `enqueue_run`'s own return-type union
            # handled explicitly at this call site (`ActorMembershipInactive`
            # was added in the third whole-phase review specifically for
            # `scheduler.py`'s fire path -- see that dataclass's docstring).
            raise HTTPException(status_code=409, detail={"code": "ACTOR_MEMBERSHIP_INACTIVE"})

        response = _to_response(result)
        _write_side_effects(
            session,
            auth,
            request,
            event_type="workflow_run.enqueued",
            aggregate_id=result.id,
            now=now,
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


MutateFn = Callable[[Session, UUID, UUID], WorkflowRun | WorkflowRunNotFound | WorkflowRunNotPaused]


def _mutate_run(
    run_id: UUID,
    request: Request,
    auth: AuthContext,
    session: Session,
    idempotency_key: str,
    *,
    action: str,
    event_type: str,
    mutate: MutateFn,
) -> RunResponse:
    """Shared shape for `cancel`/`pause`/`resume` -- each is a thin,
    argument-free (beyond `run_id`) mutation over an existing run, so the
    idempotency-lock/audit/outbox boilerplate is identical for all three;
    only the underlying `worker` function (`mutate`) and the event-type
    label differ.
    """
    req_hash = request_hash(_EmptyBody(), f"{action}:{run_id}")
    now = datetime.now(UTC)
    with session.begin():
        lock_idempotency(session, auth, idempotency_key)
        cached = load_cached(
            session,
            auth,
            idempotency_key,
            req_hash,
            domain="workflow_run",
            response_model=RunResponse,
        )
        if cached is not None:
            return cached

        if not authz.authorize(
            session, auth, resource_type="workflow_runs", resource_id=run_id, action="read"
        ):
            raise HTTPException(status_code=404, detail="RUN_NOT_FOUND")
        if not authz.authorize(
            session, auth, resource_type="workflow_runs", resource_id=run_id, action="write"
        ):
            raise HTTPException(status_code=403, detail="INSUFFICIENT_ROLE")

        result = mutate(session, auth.workspace_id, run_id)
        if isinstance(result, WorkflowRunNotFound):
            raise HTTPException(status_code=404, detail="RUN_NOT_FOUND")
        if isinstance(result, WorkflowRunNotPaused):
            raise HTTPException(
                status_code=409,
                detail={"code": "RUN_NOT_PAUSED", "status": result.status},
            )

        response = _to_response(result)
        _write_side_effects(
            session, auth, request, event_type=event_type, aggregate_id=result.id, now=now
        )
        store_idempotency(
            session, auth, idempotency_key, req_hash, response.model_dump(mode="json"), now
        )
        return response


@router.post("/runs/{run_id}/cancel", response_model=RunResponse)
def cancel_run_endpoint(
    run_id: UUID,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> RunResponse:
    return _mutate_run(
        run_id,
        request,
        auth,
        session,
        idempotency_key,
        action="cancel",
        event_type="workflow_run.cancelled",
        mutate=worker_module.cancel_run,
    )


@router.post("/runs/{run_id}/pause", response_model=RunResponse)
def pause_run_endpoint(
    run_id: UUID,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> RunResponse:
    return _mutate_run(
        run_id,
        request,
        auth,
        session,
        idempotency_key,
        action="pause",
        event_type="workflow_run.paused",
        mutate=worker_module.pause_run,
    )


@router.post("/runs/{run_id}/resume", response_model=RunResponse)
def resume_run_endpoint(
    run_id: UUID,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> RunResponse:
    return _mutate_run(
        run_id,
        request,
        auth,
        session,
        idempotency_key,
        action="resume",
        event_type="workflow_run.resumed",
        mutate=worker_module.resume_run,
    )
