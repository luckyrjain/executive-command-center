"""Global and per-workflow kill switches (`automation_kill_switches`) --
`docs/phases/PHASE-005-automation.md`'s Rollback plan ("Global/workflow
kill switches stop new runs"), `docs/runbooks/PHASE-5-RECOVERY.md`'s "Kill
switches and their recovery interaction" section.

**A new module, one concern per file** -- matching this package's own
established convention (`workflows.py`/`policy.py`/`approvals.py`/
`worker.py`/`scheduler.py`/`triggers.py` each own exactly one table/
concept). This module owns `automation_kill_switches` end to end: the
table's own reads/writes, and the small `POST /automations/workflows/{id}/
kill_switch` + `POST /automations/kill_switch` HTTP surface -- neither
endpoint existed in `API-SCHEMAS.md`'s endpoint list before this task; both
are added here as a disclosed, minimal contract addition (see that file's
own updated text), matching how prior tasks disclosed generalizations
rather than silently changing an "Approved for Implementation" doc.

**Scope semantics (migration `0042_phase5_compensation_retry_kill_switch.py`'s
own docstring has the full schema reasoning).** `workflow_id IS NULL` means
a *global* switch (blocks every workflow in the workspace); a non-`NULL`
`workflow_id` blocks only that one workflow. `is_workflow_killed` is the
single read `ecc.domains.automation.worker`'s claim predicate and dispatch
loop, and `worker.enqueue_run`, all need -- true if *either* scope has an
active row for this workspace/workflow.

**Append-only-ish history, not just current state.** `activate_kill_switch`
against an already-active exact scope is a no-op (idempotent, returns the
existing row unchanged -- matches `pause_run`'s own "already paused is a
no-op, not an error" precedent) rather than a duplicate row; deactivating
sets `active=false`/`deactivated_at`/`deactivated_by` on the existing
active row rather than deleting it, so the table itself is a real audit
trail an operator can read back later ("when was this workflow killed, by
whom, and why, across every activation/deactivation cycle"), not merely
current-state. Reactivating after a deactivation inserts a **fresh** row
(migration docstring's own reasoning: each activation window is its own
row with its own `activated_by`/`activated_at`).

**Every activate/deactivate durably commits and writes an `audit_events`
row in the same commit** (`source='automation'`, matching `local_adapters.
py`'s Task 5 precedent for an automation-package-owned audit write, not a
human-initiated one from a different domain) -- a kill switch is exactly
the kind of security-relevant action `docs/superpowers/specs/
2026-07-25-phase-5-automation-design.md`'s Threat model section cares
about being auditable.

**`docs/runbooks/PHASE-5-RECOVERY.md`'s own "Kill switches and their
recovery interaction" section is the authority on what re-enabling a
previously-killed workflow does *not* do:** "Re-enabling a previously-
killed workflow does not automatically resume its paused runs ... the
operator explicitly resolves [it] the same way an `unknown` outcome is
resolved ... not implicitly on re-enable."

**How that guarantee is actually achieved -- corrected.** This docstring
previously claimed the property held "by construction, not by an extra
check," on the grounds that a killed run lands in `needs_review` and
`needs_review` is never in `worker._CLAIMABLE_PREDICATE`'s claimable set.
An adversarial review found that reasoning covered only *one* of the ways a
run can be in flight when a switch activates -- the case where a worker
happened to be actively dispatching the run and observed the kill switch
itself (`worker.process_claimed_run`'s own per-step check). It did **not**
cover two others, and for those the documented guarantee was simply false:

1. **Runs already sitting in `'queued'`** when the switch activates.
   `worker.enqueue_run`'s kill-switch rejection only blocks *new* rows; it
   never touches rows already queued.
2. **Retry-pending runs**, which `worker.run_step`'s bounded-retry path
   deliberately parks back in `'queued'` with a `next_attempt_at`.

Both sat quietly in `'queued'` for the entire incident -- invisible as
anything needing review -- and the instant the switch was deactivated the
very next poll cycle claimed and dispatched them, with zero operator
review. `activate_kill_switch` therefore now performs one extra, explicit
step (`_park_queued_runs_for_review`): at **activation** time it transitions
every currently-`'queued'` run in the affected scope (global -> every
workflow in the workspace; per-workflow -> that one workflow) straight to
`'needs_review'`. That restores "by construction" for real, in the strong
sense: after activation there is no run left in any state a later
deactivation could silently resume from, so deactivation itself remains a
pure kill-switch-row mutation that touches no run at all. It also matches
the fail-closed, operator-visible philosophy the rest of this system uses
-- `needs_review` is the same terminal-until-human-action state an unusable
policy, an ambiguous `unknown` outcome, and a mid-dispatch kill-switch
discovery already land in, so an incident leaves one uniform queue of runs
for an operator to work through rather than two with different visibility.

Deliberately scoped to `'queued'` only. A `'leased'`/`'running'`/
`'compensating'` run is lease-bearing and actively owned by some worker:
stealing it here would violate the same lease-ownership discipline
`worker.py`'s own transitions enforce, and it is unnecessary -- that
worker's own per-step kill-switch check parks it in `needs_review` itself.
A `'paused'` run already requires an explicit `resume_run`, and
`waiting_approval`/terminal runs are not resumable by a poll cycle at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from json import dumps
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Path
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session
from ecc.observability import (
    queue_kill_switch_event,
    queue_lifecycle_event,
    queue_run_state_transition,
    record_audit_outbox_failure,
    record_idempotency_conflict,
)

_KILL_SWITCH_FIELDS = """
    id, workspace_id, workflow_id, active, reason, activated_by, activated_at,
    deactivated_by, deactivated_at, created_at, updated_at
"""


@dataclass(frozen=True, slots=True)
class KillSwitch:
    id: UUID
    workspace_id: UUID
    workflow_id: str | None
    active: bool
    reason: str | None
    activated_by: UUID
    activated_at: datetime
    deactivated_by: UUID | None
    deactivated_at: datetime | None
    created_at: datetime
    updated_at: datetime


def _scope_label(workflow_id: str | None) -> str:
    """`"global"`/`"workflow"` -- the fixed, bounded label vocabulary this
    module's own `kill_switch_events_total` metric uses (`ecc.observability`'s
    own "no PII/raw ID in a label" discipline: never the `workflow_id`
    string itself).
    """
    return "global" if workflow_id is None else "workflow"


def _row_to_kill_switch(row: dict[str, Any]) -> KillSwitch:
    return KillSwitch(
        id=row["id"],
        workspace_id=row["workspace_id"],
        workflow_id=row["workflow_id"],
        active=row["active"],
        reason=row["reason"],
        activated_by=row["activated_by"],
        activated_at=row["activated_at"],
        deactivated_by=row["deactivated_by"],
        deactivated_at=row["deactivated_at"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def is_workflow_killed(session: Session, workspace_id: UUID, workflow_id: str) -> bool:
    """True if either a global (`workflow_id IS NULL`) or this-exact-
    workflow active row exists for this workspace -- the one check `worker.
    enqueue_run` and `worker.process_claimed_run`'s per-step loop both need.
    A single query covers both scopes via `OR` rather than two round trips.
    """
    row = session.execute(
        text(
            "SELECT 1 FROM automation_kill_switches WHERE workspace_id = :workspace_id "
            "AND active AND (workflow_id IS NULL OR workflow_id = :workflow_id) LIMIT 1"
        ),
        {"workspace_id": workspace_id, "workflow_id": workflow_id},
    ).first()
    return row is not None


def get_active_kill_switch(
    session: Session, workspace_id: UUID, workflow_id: str | None
) -> KillSwitch | None:
    """The currently-active row for this *exact* scope (`workflow_id=None`
    for global) -- what `activate_kill_switch`/`deactivate_kill_switch`
    look up to decide whether to no-op, insert, or flip. Deliberately
    exact-scope, not `is_workflow_killed`'s "either scope" reasoning: a
    per-workflow deactivation must never accidentally touch a separate
    global switch, and vice versa.
    """
    clause = "workflow_id IS NULL" if workflow_id is None else "workflow_id = :workflow_id"
    params: dict[str, Any] = {"workspace_id": workspace_id}
    if workflow_id is not None:
        params["workflow_id"] = workflow_id
    row = (
        session.execute(
            text(
                f"SELECT {_KILL_SWITCH_FIELDS} FROM automation_kill_switches "
                f"WHERE workspace_id = :workspace_id AND active AND {clause}"
            ),
            params,
        )
        .mappings()
        .one_or_none()
    )
    return _row_to_kill_switch(dict(row)) if row is not None else None


def get_kill_switch(
    session: Session, workspace_id: UUID, kill_switch_id: UUID
) -> KillSwitch | None:
    row = (
        session.execute(
            text(
                f"SELECT {_KILL_SWITCH_FIELDS} FROM automation_kill_switches "
                "WHERE workspace_id = :workspace_id AND id = :id"
            ),
            {"workspace_id": workspace_id, "id": kill_switch_id},
        )
        .mappings()
        .one_or_none()
    )
    return _row_to_kill_switch(dict(row)) if row is not None else None


def get_latest_kill_switch(
    session: Session, workspace_id: UUID, workflow_id: str | None
) -> KillSwitch | None:
    """The most recently activated row for this *exact* scope, active or
    not -- what the HTTP endpoint below reads back after an activate or
    deactivate call to build its response, so both directions report the
    row's true persisted state uniformly rather than the endpoint hand-
    reconstructing a response from whichever function's own narrower
    return value it happened to call.
    """
    clause = "workflow_id IS NULL" if workflow_id is None else "workflow_id = :workflow_id"
    params: dict[str, Any] = {"workspace_id": workspace_id}
    if workflow_id is not None:
        params["workflow_id"] = workflow_id
    row = (
        session.execute(
            text(
                f"SELECT {_KILL_SWITCH_FIELDS} FROM automation_kill_switches "
                f"WHERE workspace_id = :workspace_id AND {clause} "
                "ORDER BY activated_at DESC LIMIT 1"
            ),
            params,
        )
        .mappings()
        .one_or_none()
    )
    return _row_to_kill_switch(dict(row)) if row is not None else None


def list_kill_switches(session: Session, workspace_id: UUID) -> list[KillSwitch]:
    """Full history (active and inactive rows alike) for this workspace,
    newest first -- the audit-trail read this module's own docstring
    promises, used by tests and available to a future run-history UI.
    """
    rows = (
        session.execute(
            text(
                f"SELECT {_KILL_SWITCH_FIELDS} FROM automation_kill_switches "
                "WHERE workspace_id = :workspace_id ORDER BY activated_at DESC"
            ),
            {"workspace_id": workspace_id},
        )
        .mappings()
        .all()
    )
    return [_row_to_kill_switch(dict(row)) for row in rows]


def _write_kill_switch_audit(
    session: Session,
    *,
    workspace_id: UUID,
    actor_id: UUID,
    event_type: str,
    kill_switch_id: UUID,
    workflow_id: str | None,
    reason: str | None,
    now: datetime,
) -> None:
    """`local_adapters.py`'s `_write_note_audit_and_outbox` is this
    function's precedent -- an automation-package-owned audit_events/
    event_outbox pair, `source='automation'`. `request_id`/`correlation_id`
    are freshly generated when this is called from a genuine `Request`-less
    context, but every current call site here *does* have a `Request`
    (the HTTP router below) -- freshly generating regardless keeps this
    helper reusable from a future non-HTTP caller too, matching `policy.py`/
    `approvals.py`'s own `_request_ids` fallback shape.
    """
    request_id = uuid4()
    correlation_id = uuid4()
    try:
        session.execute(
            text(
                """
                INSERT INTO audit_events (
                    id, workspace_id, event_type, aggregate_type, aggregate_id,
                    aggregate_version, actor_id, request_id, correlation_id,
                    changed_fields, authorization_result, source, metadata, occurred_at
                ) VALUES (
                    :id, :workspace_id, :event_type, 'automation_kill_switch', :aggregate_id,
                    1, :actor_id, :request_id, :correlation_id,
                    ARRAY['active'], 'allowed', 'automation', CAST(:metadata AS jsonb), :occurred_at
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "event_type": event_type,
                "aggregate_id": kill_switch_id,
                "actor_id": actor_id,
                "request_id": request_id,
                "correlation_id": correlation_id,
                "metadata": dumps({"workflow_id": workflow_id, "reason": reason}),
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
                "workspace_id": workspace_id,
                "event_type_v1": f"{event_type}.v1",
                "correlation_id": correlation_id,
                "payload": dumps(
                    {"kill_switch_id": str(kill_switch_id), "workflow_id": workflow_id}
                ),
                "occurred_at": now,
            },
        )
    except SQLAlchemyError:
        record_audit_outbox_failure("automation_kill_switch")
        raise
    queue_lifecycle_event(session, "automation_kill_switch", event_type, "allowed")
    queue_kill_switch_event(session, _scope_label(workflow_id), event_type.rsplit(".", 1)[-1])


def _park_queued_runs_for_review(
    session: Session, workspace_id: UUID, workflow_id: str | None, now: datetime
) -> list[UUID]:
    """Transitions every currently-`'queued'` `workflow_runs` row in this
    switch's scope to `'needs_review'` -- the extra, explicit step
    `activate_kill_switch` performs so that "deactivating a kill switch
    never silently auto-resumes anything" is true for *all* in-flight runs,
    not only the ones a worker happened to observe (module docstring's own
    corrected "How that guarantee is actually achieved" section has the full
    reasoning and the two cases the previous claim missed).

    Raw SQL against another module's table, deliberately, for the identical
    circular-import reason `approvals._advance_run_after_decision` already
    documents in the mirror direction: `worker.py` imports this module (its
    claim predicate and per-step loop both need `is_workflow_killed`), so
    this module cannot import `worker.py` back to reuse `_pause_run`. The
    statement is the same shape `_pause_run` writes anyway -- set the
    status, release any lease fields, never touch `finished_at`
    (`needs_review` is not terminal). `next_attempt_at` is deliberately
    left as-is on a retry-pending run: it is inert while the run is
    unclaimable, and it is real forensic information about where the run was
    when the incident hit.

    Scope mirrors `is_workflow_killed`'s own semantics exactly: a global
    switch (`workflow_id is None`) covers every workflow in the workspace, a
    per-workflow switch only its own `workflow_id`. Returns the parked run
    ids so the caller can report/count them; caller-committed, like every
    other write in this module.
    """
    scope_clause = "" if workflow_id is None else "AND workflow_id = :workflow_id"
    params: dict[str, Any] = {"workspace_id": workspace_id, "now": now}
    if workflow_id is not None:
        params["workflow_id"] = workflow_id
    parked = [
        row[0]
        for row in session.execute(
            text(
                "UPDATE workflow_runs SET status = 'needs_review', leased_by = NULL, "
                "leased_until = NULL, updated_at = :now "
                "WHERE workspace_id = :workspace_id AND status = 'queued' "
                f"{scope_clause} RETURNING id"
            ),
            params,
        ).all()
    ]
    for _ in parked:
        # Deferred rather than recorded directly -- this module is
        # caller-committed, exactly like `worker.cancel_run`'s own fast path
        # (`ecc.observability.queue_run_state_transition`'s own docstring).
        queue_run_state_transition(session, "needs_review")
    return parked


def activate_kill_switch(
    session: Session,
    workspace_id: UUID,
    workflow_id: str | None,
    reason: str | None,
    actor_id: UUID,
) -> KillSwitch:
    """Idempotent for an already-active exact scope (module docstring) --
    returns the existing row unchanged, writes no duplicate audit event and
    parks no runs (a switch that is already active has already done both,
    and `enqueue_run` has been rejecting new runs ever since).
    Otherwise inserts a fresh row and commits (caller-committed, matching
    every other Phase 5 mutation's `with session.begin():` convention --
    this function itself does not call `session.commit()`, unlike `worker.
    py`'s own bare-session discipline, since kill-switch activation has no
    risky external call to protect against the way `run_step`'s digest-
    before-execute write does).

    **Also parks every currently-`'queued'` run in this switch's scope into
    `'needs_review'`** (`_park_queued_runs_for_review`) -- in the same
    uncommitted unit of work as the switch row itself, so a switch can never
    become active without its scope's queued runs having been parked, and
    vice versa. Module docstring's "How that guarantee is actually achieved
    -- corrected" section explains why this extra step is required rather
    than the property holding for free.
    """
    existing = get_active_kill_switch(session, workspace_id, workflow_id)
    if existing is not None:
        return existing

    now = datetime.now(UTC)
    kill_switch_id = uuid4()
    session.execute(
        text(
            """
            INSERT INTO automation_kill_switches (
                id, workspace_id, workflow_id, active, reason, activated_by,
                activated_at, created_at, updated_at
            ) VALUES (
                :id, :workspace_id, :workflow_id, true, :reason, :actor_id, :now, :now, :now
            )
            """
        ),
        {
            "id": kill_switch_id,
            "workspace_id": workspace_id,
            "workflow_id": workflow_id,
            "reason": reason,
            "actor_id": actor_id,
            "now": now,
        },
    )
    _write_kill_switch_audit(
        session,
        workspace_id=workspace_id,
        actor_id=actor_id,
        event_type="automation_kill_switch.activated",
        kill_switch_id=kill_switch_id,
        workflow_id=workflow_id,
        reason=reason,
        now=now,
    )
    _park_queued_runs_for_review(session, workspace_id, workflow_id, now)
    result = get_kill_switch(session, workspace_id, kill_switch_id)
    assert result is not None
    return result


def deactivate_kill_switch(
    session: Session, workspace_id: UUID, workflow_id: str | None, actor_id: UUID
) -> bool:
    """Sets `active=false`/`deactivated_at`/`deactivated_by` on the exact-
    scope active row, if one exists (module docstring: never deletes it --
    the table stays a real history). Returns `False`, writes nothing, if
    there was no active row for this scope to begin with (idempotent
    no-op, matching `activate_kill_switch`'s identical precedent for the
    opposite direction).
    """
    existing = get_active_kill_switch(session, workspace_id, workflow_id)
    if existing is None:
        return False

    now = datetime.now(UTC)
    session.execute(
        text(
            "UPDATE automation_kill_switches SET active = false, deactivated_by = :actor_id, "
            "deactivated_at = :now, updated_at = :now WHERE id = :id"
        ),
        {"actor_id": actor_id, "now": now, "id": existing.id},
    )
    _write_kill_switch_audit(
        session,
        workspace_id=workspace_id,
        actor_id=actor_id,
        event_type="automation_kill_switch.deactivated",
        kill_switch_id=existing.id,
        workflow_id=workflow_id,
        reason=existing.reason,
        now=now,
    )
    return True


# ---------------------------------------------------------------------------
# POST /api/v1/automations/workflows/{workflow_id}/kill_switch (per-workflow)
# POST /api/v1/automations/kill_switch (global)
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1/automations", tags=["automation"])
SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]


class KillSwitchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active: bool
    reason: str | None = Field(default=None, max_length=2000)


class KillSwitchResponse(BaseModel):
    workflow_id: str | None
    active: bool
    reason: str | None
    activated_by: UUID | None
    activated_at: datetime | None
    deactivated_by: UUID | None
    deactivated_at: datetime | None


def _to_response(workflow_id: str | None, switch: KillSwitch | None) -> KillSwitchResponse:
    """`switch is None` -- `deactivate_kill_switch` found nothing to
    deactivate (this scope was already inactive) -- reported as an
    already-inactive response, not an error (matches `pause_run`'s own
    "already in that state is a no-op" precedent restated throughout this
    module).
    """
    if switch is None:
        return KillSwitchResponse(
            workflow_id=workflow_id,
            active=False,
            reason=None,
            activated_by=None,
            activated_at=None,
            deactivated_by=None,
            deactivated_at=None,
        )
    return KillSwitchResponse(
        workflow_id=switch.workflow_id,
        active=switch.active,
        reason=switch.reason,
        activated_by=switch.activated_by,
        activated_at=switch.activated_at,
        deactivated_by=switch.deactivated_by,
        deactivated_at=switch.deactivated_at,
    )


def _request_hash(payload: BaseModel, action: str) -> str:
    material = {"action": action, "payload": payload.model_dump(mode="json")}
    return sha256(dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _lock_idempotency(session: Session, auth: AuthContext, key: str) -> None:
    lock_key = f"{auth.workspace_id}:{auth.user_id}:{key}"
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": lock_key},
    )


def _load_cached(
    session: Session, auth: AuthContext, key: str, request_hash: str
) -> KillSwitchResponse | None:
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
        record_idempotency_conflict("automation_kill_switch")
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
    return KillSwitchResponse.model_validate(row["response_body"])


def _store_idempotency(
    session: Session,
    auth: AuthContext,
    key: str,
    request_hash: str,
    response: KillSwitchResponse,
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


def _handle_kill_switch_request(
    session: Session,
    auth: AuthContext,
    workflow_id: str | None,
    payload: KillSwitchRequest,
    idempotency_key: str,
    request_hash: str,
) -> KillSwitchResponse:
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached

        if payload.active:
            activate_kill_switch(
                session, auth.workspace_id, workflow_id, payload.reason, auth.user_id
            )
        else:
            deactivate_kill_switch(session, auth.workspace_id, workflow_id, auth.user_id)

        latest = get_latest_kill_switch(session, auth.workspace_id, workflow_id)
        response = _to_response(workflow_id, latest)
        _store_idempotency(session, auth, idempotency_key, request_hash, response, now)
        return response


class KillSwitchStatusResponse(BaseModel):
    """Task 7a's addition -- `UX-STATES.md`'s own named gap: "a future UI
    surfaces the workflow's own current kill-switch state ... so an
    operator sees *why* new runs are rejected before attempting one, not
    only after." Wraps `list_kill_switches`/`get_active_kill_switch`,
    interpreted for one specific workflow rather than reused as-is
    (`KillSwitchResponse` alone cannot express "killed by which scope,"
    since a global and a per-workflow switch can each be independently
    active or not) -- a documented choice, not a mechanical reuse of the
    existing response model. `killed` is `True` if *either* scope has an
    active row, mirroring `is_workflow_killed`'s own "either scope" OR
    exactly. `history` is this workflow's own per-workflow-scope rows only
    (`workflow_id = workflow_id`, never the global scope's own separate
    history) -- a global switch's activation history is not specific to any
    one workflow and would be a confusing, unbounded list to embed in a
    per-workflow response; its *current* effective state is still fully
    visible via `active_global`.
    """

    workflow_id: str
    killed: bool
    active_global: KillSwitchResponse | None
    active_workflow: KillSwitchResponse | None
    history: list[KillSwitchResponse]


@router.get("/workflows/{workflow_id}/kill_switch", response_model=KillSwitchStatusResponse)
def workflow_kill_switch_status_endpoint(
    workflow_id: Annotated[str, Path(max_length=200)],
    auth: AuthDep,
    session: SessionDep,
) -> KillSwitchStatusResponse:
    """A pure read -- no `CsrfDep`/`Idempotency-Key`, matching every other
    `GET` in this package (`workflows.list_workflows_endpoint`, `runs.
    list_runs_endpoint`). Workspace-scoped via `auth.workspace_id`; does not
    itself validate that `workflow_id` names a real `workflow_definitions`
    row (neither do the two `POST` endpoints below -- a kill switch's own
    scope is an opaque string, matching how `workflow_runs.workflow_id`/
    `automation_policies.workflow_id` are already opaque, no-FK references
    elsewhere in this package), so this simply reports "no switch, of either
    scope, for this workflow_id" for a workflow_id that happens not to
    exist, the same as it would for one that exists but was never killed.

    `Path(max_length=200)` mirrors `policy.list_policies_endpoint`/`triggers.
    list_triggers_endpoint`'s own `Query(max_length=200)` on the identical
    field, and matches `automation_kill_switches.workflow_id`'s real column
    type (`sa.String(200)`, migration `0042_phase5_compensation_retry_kill_
    switch.py`). Added by security audit batch C: opaque-but-unbounded meant
    an over-long `workflow_id` reached the database and surfaced as an
    uncaught `DataError`/500 rather than an ordinary `422` validation
    response -- a client's malformed input reported as a server fault.
    """
    active_global = get_active_kill_switch(session, auth.workspace_id, None)
    active_workflow = get_active_kill_switch(session, auth.workspace_id, workflow_id)
    history = [
        switch
        for switch in list_kill_switches(session, auth.workspace_id)
        if switch.workflow_id == workflow_id
    ]
    return KillSwitchStatusResponse(
        workflow_id=workflow_id,
        killed=active_global is not None or active_workflow is not None,
        active_global=_to_response(None, active_global) if active_global is not None else None,
        active_workflow=(
            _to_response(workflow_id, active_workflow) if active_workflow is not None else None
        ),
        history=[_to_response(workflow_id, switch) for switch in history],
    )


@router.post("/workflows/{workflow_id}/kill_switch", response_model=KillSwitchResponse)
def workflow_kill_switch_endpoint(
    workflow_id: Annotated[str, Path(max_length=200)],
    payload: KillSwitchRequest,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> KillSwitchResponse:
    """`Path(max_length=200)` -- see `workflow_kill_switch_status_endpoint`'s
    own docstring for the reasoning; this is the route where an over-long
    `workflow_id` actually reached an `INSERT` against
    `automation_kill_switches.workflow_id` (`sa.String(200)`) and produced a
    `DataError`/500 instead of a `422`.
    """
    request_hash = _request_hash(payload, f"kill_switch:{workflow_id}")
    return _handle_kill_switch_request(
        session, auth, workflow_id, payload, idempotency_key, request_hash
    )


@router.post("/kill_switch", response_model=KillSwitchResponse)
def global_kill_switch_endpoint(
    payload: KillSwitchRequest,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> KillSwitchResponse:
    request_hash = _request_hash(payload, "kill_switch:global")
    return _handle_kill_switch_request(session, auth, None, payload, idempotency_key, request_hash)
