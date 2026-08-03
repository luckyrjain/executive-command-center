"""Trigger CRUD (`triggers`) -- design doc Decision 7 / `DATA-MODEL.md`.

**Task 7: `GET /api/v1/automations/triggers` added below.** Every prior
task through Task 7a deliberately left this module router-less (see the
"Task 1-7a" paragraph retained below for the original, still-accurate
reasoning for *why* -- no trigger endpoint was in scope). Task 7's own
frontend work (`PHASE-005-automation.md`'s Frontend changes: "... schedule
controls ...") found a genuine gap building against that contract: a
workflow's `GET .../workflows/{id}` response exposes only `trigger_refs:
list[str]` (bare reference strings, e.g. `"manual"` or a trigger's own
`str(id)`) -- never a trigger's own `schedule_expression`/`timezone`/
`skip_missed`/`last_fired_at`, so a "schedule controls" UI has no real data
to render a workflow's own configured trigger(s) or next-fire information
against. Mirroring `kill_switches.py`'s own Task 7a precedent (`GET
.../kill_switch`, a small, disclosed, additive read-only endpoint closing
a UI-blocking backend gap found while building the thing that needs it,
not a separate task or PR) -- see this module's own new `router` section
below for the endpoint itself. Deliberately **read-only**: no CRUD, no
mutation, no new HTTP surface for `create_trigger` -- this activation's
"schedule controls" need only *display* a workflow's own already-configured
trigger(s), never author new ones from the browser (workflow authoring
happens via `POST /automations/workflows`'s own `trigger_refs` field,
unchanged by this task).

**Task 1-7a: no `APIRouter` in this module** (historical context for the
addition above) -- `docs/phases/phase-005/API-SCHEMAS.md`'s Task-1-scoped
surface (`GET|POST /automations/workflows`, `GET /automations/
workflows/{id}`, `POST .../publish|disable`, `GET|POST /automations/
policies`, `POST /automations/policies/{id}/revoke`) named no trigger
endpoint; the design doc's own scheduler/event-subscriber (Decision 7) was
later worker work (Task 2+) that task did not build. This mirrored `ecc.
domains.ai_runtime.tools`'s shape: a pure data-layer module with no HTTP
surface of its own, whose functions exist for a later task (and, here,
that task's own tests) to call directly. Task 7 above is the first task to
add a router to this module -- a narrow, disclosed, additive change to that
precedent, not a reversal of it.

Every *workspace-scoped* function here is workspace-scoped the same way
`workflows.py`/`policy.py` are -- no such function accepts anything that
could substitute for the caller's own `workspace_id`. **One exception,
added by Task 4:** `list_schedule_triggers` is deliberately *not*
workspace-scoped -- it is the scheduler tick's own read (`ecc.domains.
automation.scheduler.run_scheduler_once`), an internal background-worker
loop, not a per-request HTTP handler acting on behalf of one caller's
session. This mirrors `worker.claim_next_run`'s identical, already-
established precedent: a worker polls across every workspace's claimable
rows, not one workspace at a time, because there is no "caller" to scope
to in a background loop. `mark_trigger_fired` (also Task 4) *is*
workspace-scoped, like every mutating function in this package, since it
still writes a specific row this task's own caller (the scheduler,
resolving `workspace_id` from the very row `list_schedule_triggers` just
read, never from external input) must not be able to target cross-
workspace by accident.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import CursorResult, text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep
from ecc.database import get_session
from ecc.platform import authz

TriggerType = Literal["manual", "event", "schedule"]

_TRIGGER_FIELDS = """
    id, workspace_id, workflow_id, trigger_type, event_type_filter,
    schedule_expression, timezone, skip_missed, last_fired_at, created_by,
    updated_by, created_at, updated_at
"""


@dataclass(frozen=True, slots=True)
class Trigger:
    id: UUID
    workspace_id: UUID
    workflow_id: str
    trigger_type: TriggerType
    event_type_filter: str | None
    schedule_expression: str | None
    timezone: str | None
    skip_missed: bool
    last_fired_at: datetime | None
    created_by: UUID
    updated_by: UUID
    created_at: datetime
    updated_at: datetime


def _row_to_trigger(row: dict[str, Any]) -> Trigger:
    return Trigger(
        id=row["id"],
        workspace_id=row["workspace_id"],
        workflow_id=row["workflow_id"],
        trigger_type=row["trigger_type"],
        event_type_filter=row["event_type_filter"],
        schedule_expression=row["schedule_expression"],
        timezone=row["timezone"],
        skip_missed=row["skip_missed"],
        last_fired_at=row["last_fired_at"],
        created_by=row["created_by"],
        updated_by=row["updated_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def get_trigger(session: Session, workspace_id: UUID, trigger_id: UUID) -> Trigger | None:
    row = (
        session.execute(
            text(
                f"SELECT {_TRIGGER_FIELDS} FROM triggers "
                "WHERE workspace_id = :workspace_id AND id = :id"
            ),
            {"workspace_id": workspace_id, "id": trigger_id},
        )
        .mappings()
        .one_or_none()
    )
    return _row_to_trigger(dict(row)) if row is not None else None


def list_triggers(
    session: Session, auth: AuthContext, workspace_id: UUID, *, workflow_id: str | None = None
) -> list[Trigger]:
    visibility_sql, visibility_params = authz.visible_resource_filter_sql(
        session, auth, resource_type="triggers", action="read", table_alias="triggers"
    )
    clause = "AND workflow_id = :workflow_id" if workflow_id is not None else ""
    params: dict[str, Any] = {"workspace_id": workspace_id, **visibility_params}
    if workflow_id is not None:
        params["workflow_id"] = workflow_id
    rows = (
        session.execute(
            text(
                f"SELECT {_TRIGGER_FIELDS} FROM triggers "
                f"WHERE workspace_id = :workspace_id AND ({visibility_sql}) "
                f"{clause} ORDER BY created_at ASC"
            ),
            params,
        )
        .mappings()
        .all()
    )
    session.rollback()
    return [_row_to_trigger(dict(row)) for row in rows]


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


def create_trigger(
    session: Session,
    workspace_id: UUID,
    actor_id: UUID,
    *,
    workflow_id: str,
    trigger_type: TriggerType,
    event_type_filter: str | None = None,
    schedule_expression: str | None = None,
    timezone: str | None = None,
    skip_missed: bool = False,
) -> Trigger:
    """Raw insert -- the `ck_triggers_schedule_requires_expression_and_
    timezone`/`ck_triggers_event_requires_type_filter` CHECK constraints
    (migration `0038_phase5_workflow_schema.py`) are the authoritative
    enforcement of "a `schedule` trigger always names its timezone
    explicitly" (design doc Decision 7); this function does not duplicate
    that validation in Python, matching how `workflows.py`'s immutability
    guarantee is enforced by a trigger, not re-checked here. A caller
    passing a malformed combination gets a `CheckViolation` (a subclass of
    `sqlalchemy.exc.IntegrityError`) directly from Postgres.
    """
    now = datetime.now(UTC)
    trigger_id = uuid4()
    session.execute(
        text(
            """
            INSERT INTO triggers (
                id, workspace_id, workflow_id, trigger_type, event_type_filter,
                schedule_expression, timezone, skip_missed, created_by, updated_by,
                created_at, updated_at, owner_id, visibility
            ) VALUES (
                :id, :workspace_id, :workflow_id, :trigger_type, :event_type_filter,
                :schedule_expression, :timezone, :skip_missed, :created_by, :updated_by,
                :now, :now, :created_by, 'workspace'
            )
            """
        ),
        {
            "id": trigger_id,
            "workspace_id": workspace_id,
            "workflow_id": workflow_id,
            "trigger_type": trigger_type,
            "event_type_filter": event_type_filter,
            "schedule_expression": schedule_expression,
            "timezone": timezone,
            "skip_missed": skip_missed,
            "created_by": actor_id,
            "updated_by": actor_id,
            "now": now,
        },
    )
    result = get_trigger(session, workspace_id, trigger_id)
    assert result is not None
    return result


# ---------------------------------------------------------------------------
# Task 4: the scheduler tick's own reads/writes (module docstring's
# "one exception" note).
# ---------------------------------------------------------------------------


def list_schedule_triggers(session: Session) -> list[Trigger]:
    """Every `trigger_type = 'schedule'` row, across every workspace --
    deliberately unscoped (module docstring). `ecc.domains.automation.
    scheduler.run_scheduler_once` is the only caller; it evaluates each
    returned row's own `schedule_expression`/`timezone`/`skip_missed`/
    `last_fired_at` independently, so there is no meaningful "one
    workspace at a time" shape to this read the way an HTTP list endpoint
    would need. `event`/`manual` triggers are never returned here -- the
    mechanical enforcement of this task's own explicit non-scope (event
    triggers are not auto-fired by this activation, `scheduler.py`'s
    module docstring).
    """
    rows = (
        session.execute(
            text(
                f"SELECT {_TRIGGER_FIELDS} FROM triggers "
                "WHERE trigger_type = 'schedule' ORDER BY created_at ASC"
            )
        )
        .mappings()
        .all()
    )
    return [_row_to_trigger(dict(row)) for row in rows]


def mark_trigger_fired(
    session: Session,
    workspace_id: UUID,
    trigger_id: UUID,
    fired_at: datetime,
    *,
    expected_last_fired_at: datetime | None,
) -> bool:
    """Sets `last_fired_at`, the catch-up-once anchor `scheduler.py`'s own
    misfire logic reads on the next tick. Workspace-scoped like every
    mutating function in this package (module docstring) -- the caller
    (`scheduler.run_scheduler_once`) always resolves `workspace_id` from
    the same `Trigger` row `list_schedule_triggers` just read, never from
    external input. No commit here -- caller-committed, exactly like
    `create_trigger` above; `scheduler.py`'s own module docstring explains
    why its call site commits this together with the `workflow_runs`
    INSERT it fires alongside, in one atomic transaction.

    **Compare-and-swap, not an unconditional write** -- `WHERE ... AND
    last_fired_at IS NOT DISTINCT FROM :expected_last_fired_at` (`IS NOT
    DISTINCT FROM` rather than `=` so a still-`NULL` anchor, a trigger's
    own pre-first-fire state, compares correctly instead of every `NULL =
    NULL` silently evaluating to unknown/false in ordinary SQL). This is
    the exact `UPDATE ... WHERE <the state this caller read> RETURNING
    ...` compare-and-swap idiom `worker.claim_next_run` already
    establishes for the identical "two independent processes acting on
    the same row without a `SELECT ... FOR UPDATE` lock" shape -- two
    concurrent scheduler ticks (e.g. two `scripts/run_automation_worker.py`
    replicas) racing the same trigger must not both fire it. Returns
    `True` if this call's own view of `last_fired_at` was still current at
    write time (this caller won the race, or there was no race at all --
    the ordinary case); `False` if another writer already advanced the
    anchor first (this caller lost the race and must not also enqueue a
    run). `scheduler.run_scheduler_once` checks this return value *before*
    calling `worker.enqueue_run`, not after -- see that function's own
    docstring.
    """
    result = session.execute(
        text(
            "UPDATE triggers SET last_fired_at = :fired_at, updated_at = :fired_at "
            "WHERE workspace_id = :workspace_id AND id = :id "
            "AND last_fired_at IS NOT DISTINCT FROM :expected_last_fired_at"
        ),
        {
            "fired_at": fired_at,
            "workspace_id": workspace_id,
            "id": trigger_id,
            "expected_last_fired_at": expected_last_fired_at,
        },
    )
    # session.execute() against a text() UPDATE always returns a real
    # CursorResult at runtime (it has a real rowcount from the DBAPI
    # cursor) -- mypy's stubs type Session.execute()'s return as the
    # broader Result[Any] regardless of statement kind, so this narrows
    # explicitly rather than silencing the check.
    return cast("CursorResult[Any]", result).rowcount > 0


# ---------------------------------------------------------------------------
# Task 7: GET /api/v1/automations/triggers -- a small, disclosed, additive
# read surface closing the "schedule controls" gap named in this module's
# own docstring above. Mirrors kill_switches.py's `workflow_kill_switch_
# status_endpoint` for router/auth/response-model conventions: a pure read,
# no `CsrfDep`/`Idempotency-Key` (nothing is written), workspace-scoped via
# `auth.workspace_id`, optionally filtered by `workflow_id` (`list_triggers`
# already supports this filter -- unused by any caller until now).
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1/automations", tags=["automation"])
SessionDep = Annotated[Session, Depends(get_session)]


class TriggerResponse(BaseModel):
    id: UUID
    workflow_id: str
    trigger_type: TriggerType
    event_type_filter: str | None
    schedule_expression: str | None
    timezone: str | None
    skip_missed: bool
    last_fired_at: datetime | None
    created_at: datetime
    updated_at: datetime


class TriggerListResponse(BaseModel):
    triggers: list[TriggerResponse]


def _to_response(trigger: Trigger) -> TriggerResponse:
    return TriggerResponse(
        id=trigger.id,
        workflow_id=trigger.workflow_id,
        trigger_type=trigger.trigger_type,
        event_type_filter=trigger.event_type_filter,
        schedule_expression=trigger.schedule_expression,
        timezone=trigger.timezone,
        skip_missed=trigger.skip_missed,
        last_fired_at=trigger.last_fired_at,
        created_at=trigger.created_at,
        updated_at=trigger.updated_at,
    )


@router.get("/triggers", response_model=TriggerListResponse)
def list_triggers_endpoint(
    auth: AuthDep,
    session: SessionDep,
    workflow_id: Annotated[str | None, Query(max_length=200)] = None,
) -> TriggerListResponse:
    """Every trigger (`manual`/`event`/`schedule`) configured for the
    caller's workspace, optionally narrowed to one `workflow_id` -- the read
    a "schedule controls" view needs to show a workflow's own configured
    trigger(s) and next-fire-relevant fields (`schedule_expression`/
    `timezone`/`skip_missed`/`last_fired_at`) before a user attempts a new
    run, matching `UX-STATES.md`'s own "shows ... side effects ... before
    approval" spirit extended to schedule visibility. Does not compute a
    projected next-fire timestamp (that is `scheduler._next_occurrence_
    after`'s own pure function, not exposed here) -- `last_fired_at` plus
    the trigger's own `schedule_expression`/`timezone`/`skip_missed` is
    what this task's own scope needs a human to read and reason about
    manually; a later task adding a computed "next fire" field would be a
    natural, small extension of this same endpoint.
    """
    triggers = list_triggers(session, auth, auth.workspace_id, workflow_id=workflow_id)
    return TriggerListResponse(triggers=[_to_response(trigger) for trigger in triggers])
