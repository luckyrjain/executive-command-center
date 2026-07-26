"""The schedule-trigger evaluation tick (`docs/superpowers/specs/
2026-07-25-phase-5-automation-design.md` Decision 7,
`docs/phases/phase-005/DATA-MODEL.md`'s `triggers` row,
`docs/phases/phase-005/API-SCHEMAS.md`'s `POST /automations/runs` --
this module is what actually fires a `schedule` trigger into a real
`workflow_runs` row via `worker.enqueue_run`).

**No HTTP surface in this module**, matching `triggers.py`/`worker.py`'s
own precedent exactly: nothing in `API-SCHEMAS.md` names a scheduler
endpoint -- `run_scheduler_once` is an internally-invokable function a
process-supervisor entrypoint (`scripts/run_automation_worker.py`, this
task's own addition -- see that script's own docstring) and this task's
own tests call directly, deterministically, one tick at a time.

**Flagged discrepancy: no pre-existing cron-parsing dependency.** The
design doc's own text reads "Python's standard library `zoneinfo` plus an
existing cron-parsing dependency already available to the backend covers
this without a new external service" -- checked directly against this
repository's `pyproject.toml`/`uv.lock` before writing this module: no
such dependency existed. This is a real discrepancy between the design-time
document and reality, not a silently-papered-over assumption. Resolved by
adding `croniter` (small, pure-Python, MIT-licensed, no native build step)
as a new pinned dependency (`pyproject.toml`) -- a small, mechanical
addition, not an architectural decision requiring its own ADR/RFC-005
amendment (it is a library choice inside an already-approved activation,
not a new activated *technology* in the RFC-005 sense Decision 1
evaluates).

**Timezone-aware evaluation (Decision 7, the load-bearing property this
module exists to get right).** `_next_occurrence_after` converts the
anchor instant into the trigger's own declared IANA timezone (`zoneinfo.
ZoneInfo(trigger.timezone)`) *before* handing it to `croniter` -- the cron
expression's fields (`"0 9 * * *"` etc.) are evaluated against that
trigger's own local wall-clock time, never server-local time and never a
naive UTC-as-if-local reading. This is the exact class of bug `docs/
superpowers/specs/2026-07-25-phase-5-automation-design.md` names by name
(Phase 3's `MeetingPrep.tsx` timezone-mislabeling bug) -- `tests/
test_automation_scheduler_postgres.py` proves it directly with a
timezone/instant pair where naive UTC evaluation and this module's own
`zoneinfo`-aware evaluation disagree, not merely with same-timezone
round-trip cases that would pass even with the bug present.

**Misfire policy (Decision 7, made mechanically concrete).** Each
`schedule` trigger's own durable anchor is `triggers.last_fired_at`
(`NULL` until its first fire, in which case `created_at` is the anchor --
a freshly-created trigger's *next* scheduled occurrence is what fires it,
never an immediate catch-up fire the instant it is created). Per tick,
per trigger:

1. `first_due = _next_occurrence_after(anchor)` -- the earliest scheduled
   occurrence strictly after the anchor. If `first_due > now`, nothing to
   do this tick (`NotDue`).
2. Otherwise at least one occurrence is due. `second_due =
   _next_occurrence_after(first_due)` -- the occurrence *after* that one.
   If `second_due <= now` too, more than one scheduled occurrence has
   elapsed since the anchor -- a genuine misfire (the scheduler, or this
   trigger's own bookkeeping, was not evaluated across an entire
   intervening window, not merely "slightly late by less than one poll
   cycle"). Exactly one occurrence elapsing (`second_due > now`) is the
   ordinary, healthy steady state (a schedule fires within one ~60s tick
   of its scheduled time) and is never treated as a misfire regardless of
   `skip_missed` -- distinguishing "on time" from "backlog" this way is
   what makes `skip_missed=true` still let an ordinary schedule fire at
   all, rather than suppressing every fire unconditionally.
3. A genuine misfire (step 2) with `skip_missed=False` (the default)
   fires **once** (`Due`) -- catch-up-once, never once per missed window.
   `skip_missed=True` does not fire at all for the backlog (`MisfireSkipped`)
   -- "no catch-up," per Decision 7 verbatim.
4. Either way, the trigger's anchor advances to `now` (the tick's own
   moment), never to `first_due` or any other in-the-past instant -- this
   is what makes the policy genuinely "at most once," not merely
   "at most once per tick": the very next tick recomputes `first_due` from
   this fresh anchor and finds nothing due until the *next* real future
   occurrence, so a trigger can never be evaluated as "still owing a fire"
   for an occurrence this tick already resolved one way or the other.

**Durability -- one atomic commit per trigger, not the before/after-two-
commit discipline `worker.run_step` needs.** `run_step`'s own module
docstring explains *why* it needs two separate, immediate commits
bracketing `execute()`: the write before `execute()` must survive a crash
that happens *during* a risky, possibly-already-side-effecting external
call, and the write after records that call's own outcome. This module's
own two writes per fired trigger (`worker.enqueue_run`'s `INSERT` into
`workflow_runs`, and `triggers.mark_trigger_fired`'s `UPDATE` of
`last_fired_at`) have no such risky call between them -- `enqueue_run`
only ever inserts a `queued` row; nothing about *that* insert can have a
real external side effect a crash could leave ambiguous. So this module
does the safer, simpler thing instead: both writes happen inside the
*same* transaction, on the *same* bare session, followed by exactly one
`session.commit()`. This avoids the split-brain either alternative
two-separate-commit ordering would risk: commit the run first (a crash
before the anchor advances would re-fire the same occurrence on the next
tick -- a real duplicate run) or commit the anchor first (a crash before
the run is enqueued would silently lose that fire forever, since the
anchor already advanced past it and catch-up-once has nothing left to
catch up). One atomic transaction has neither failure mode: a crash
before the commit leaves *both* writes rolled back (the trigger looks
exactly as if this tick never happened, and the next tick's `first_due`
computation from the unchanged `last_fired_at` anchor correctly re-derives
"this occurrence is still due" and fires it -- or, if enough time has
passed that a second occurrence is now also due, applies the identical
misfire policy again); a crash after is a normal, fully-durable fire. This
module still follows this package's "durability-critical writes commit
immediately on a bare session" discipline (`worker.py`'s own module
docstring) -- it just needs one commit per trigger, not two, because there
is no adapter `execute()` call in the middle to bracket.

**Concurrency safety -- two scheduler instances racing the same trigger
must not both fire it.** `triggers.mark_trigger_fired` is a compare-and-
swap `UPDATE ... WHERE last_fired_at IS NOT DISTINCT FROM :expected
RETURNING`-shaped write (mirroring `worker.claim_next_run`'s own,
already-established idiom for the identical "no `SELECT ... FOR UPDATE`
lock, just an atomic conditional write" shape), not an unconditional one.
`run_scheduler_once` calls it *before* `worker.enqueue_run`, still inside
the same one-commit transaction described above, and only proceeds to
`enqueue_run` if it won (`True`) -- a lost race (`False`, returned as
`TriggerRaceLost`) means a second concurrent tick already advanced this
exact trigger's anchor first, so this call must not also enqueue a
duplicate run for the same occurrence. Under Postgres's default READ
COMMITTED isolation, the losing transaction's `UPDATE` blocks on the
winner's row lock until the winner commits, then re-evaluates its own
`WHERE` clause against the now-committed (changed) `last_fired_at` and
naturally affects zero rows -- no explicit `SELECT ... FOR UPDATE` or
advisory lock needed, exactly as `claim_next_run`'s own docstring already
explains for the workflow_runs case. `tests/
test_automation_scheduler_postgres.py`'s own concurrent-tick test proves
this directly (`ThreadPoolExecutor`, two independent `SessionFactory`
sessions racing one trigger, not mocked), mirroring `worker.py`'s
`test_two_workers_racing_the_same_run_exactly_one_claims_it` precedent.

**Explicitly out of this task's scope: event-trigger firing.** `event`-type
triggers are never returned by `triggers.list_schedule_triggers` (filtered
to `trigger_type = 'schedule'` at the SQL level) and this module contains
no code path that evaluates one. This matches the design doc's own
Architecture-impact section verbatim: event triggers are "a *consumer* of
[the] eventual durable event bus," and `platform/events/bus.py` still
provides only `NonDurableInProcessEventBus` (confirmed directly against
that module before writing this one) -- "if `NonDurableInProcessEventBus`
is still the only implementation when Phase 5 implementation begins, that
is a pre-existing gap this document does not attempt to close." Building a
durable outbox-backed event bus is not this task's job, so an `event`
trigger remains exactly what Task 1 left it: creatable via `triggers.
create_trigger`, never auto-fired by anything in this package.

**Explicitly out of this task's scope: a `notifications` table or delivery
system.** `DATA-MODEL.md`'s own `notifications` row is explicit: "Existing
skeleton, retained unchanged -- notification delivery itself is not a
Phase 5 approval-gate item." The word "notifications" in this task's own
roadmap label ("Schedules, triggers and notifications") does not expand
this task's actual scope beyond what `DATA-MODEL.md`'s contract text says
-- no `notifications` row is ever written by this module or by `runs.py`.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from croniter import croniter
from sqlalchemy.orm import Session

from ecc.observability import record_schedule_lag

from . import triggers as triggers_module
from . import worker as worker_module

# Decision 7 verbatim: "60s interval -- coarser than the 2s run-claim poll,
# since schedule granularity coarser than a minute is not a real product
# requirement." Referenced by this module's own docstring/tests and by
# scripts/run_automation_worker.py's own poll-loop wiring -- one place this
# number lives, mirroring worker.py's own POLL_INTERVAL_SECONDS precedent.
SCHEDULER_TICK_INTERVAL_SECONDS = 60


# ---------------------------------------------------------------------------
# Result types -- one per trigger evaluated, mirroring worker.py's own
# dataclass-per-outcome convention rather than a raw string/enum.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TriggerNotDue:
    """This trigger's next scheduled occurrence has not yet arrived --
    the ordinary steady state for most ticks, most triggers.
    """

    trigger_id: UUID


@dataclass(frozen=True, slots=True)
class TriggerFired:
    """A `workflow_runs` row was enqueued for this trigger this tick."""

    trigger_id: UUID
    workspace_id: UUID
    workflow_id: str
    run_id: UUID


@dataclass(frozen=True, slots=True)
class TriggerFireFailedWorkflowNotActive:
    """The trigger fired (its schedule was due), but `worker.enqueue_run`
    rejected it -- the workflow it names has no `active` version right
    now. The anchor still advances to `now` (module docstring's misfire
    section) so a workflow left inactive does not cause this trigger to
    be re-evaluated as "due" on every single tick forever; it simply
    waits for its own next real scheduled occurrence, at which point this
    same outcome recurs if the workflow is still inactive, or a real
    `TriggerFired` if it has since been reactivated.
    """

    trigger_id: UUID
    workflow_id: str


@dataclass(frozen=True, slots=True)
class TriggerFireFailedWorkflowKilled:
    """Task 6: the trigger fired (its schedule was due), but `worker.
    enqueue_run` rejected it -- a global or per-workflow kill switch
    currently blocks this workflow (`PHASE-005-automation.md`'s Rollback
    plan: "Global/workflow kill switches stop new runs"). The anchor still
    advances to `now`, mirroring `TriggerFireFailedWorkflowNotActive`'s
    identical reasoning: a killed workflow does not cause this trigger to
    be re-evaluated as "due" on every tick forever.
    """

    trigger_id: UUID
    workflow_id: str


@dataclass(frozen=True, slots=True)
class TriggerMisfireSkipped:
    """More than one scheduled occurrence elapsed since this trigger's own
    anchor, and it has `skip_missed=True` -- no catch-up fire, per
    Decision 7 verbatim. The anchor still advances to `now`.
    """

    trigger_id: UUID


@dataclass(frozen=True, slots=True)
class TriggerRaceLost:
    """This trigger was due, but another concurrent scheduler tick (a
    second `scripts/run_automation_worker.py` replica, or simply this same
    process's own next tick landing before this one's transaction
    committed) already advanced `last_fired_at` first --
    `triggers.mark_trigger_fired`'s compare-and-swap `UPDATE` affected zero
    rows. Never an error: the ordinary steady state under >1 concurrent
    scheduler instance, mirroring `worker.claim_next_run` returning `None`
    when a worker loses the identical kind of race for a `workflow_runs`
    row. No `workflow_runs` row is enqueued by *this* call when this
    outcome is returned -- the instance that won the race already enqueued
    the one run this occurrence gets.
    """

    trigger_id: UUID


TriggerEvaluationOutcome = (
    TriggerNotDue
    | TriggerFired
    | TriggerFireFailedWorkflowNotActive
    | TriggerFireFailedWorkflowKilled
    | TriggerMisfireSkipped
    | TriggerRaceLost
)


# ---------------------------------------------------------------------------
# Pure schedule math -- no session, no I/O, directly unit-testable without
# a database (mirrors approvals.evaluate_approval_requirement's own
# "pure function, testable against every combination without a database"
# precedent).
# ---------------------------------------------------------------------------


def _next_occurrence_after(
    schedule_expression: str, timezone_name: str, anchor: datetime
) -> datetime:
    """The earliest cron occurrence strictly after `anchor`, computed in
    the trigger's own declared IANA timezone and returned as a
    timezone-aware UTC `datetime` (module docstring's own "timezone-aware
    evaluation" section). `anchor` may be any timezone-aware `datetime`
    (this module always passes UTC in practice, `last_fired_at`/
    `created_at` both being stored `timestamptz` columns) -- it is
    converted into `timezone_name` *before* being handed to `croniter`, so
    the cron expression's own fields are evaluated against the trigger's
    own local wall-clock time, never the anchor's original zone.
    """
    tz = ZoneInfo(timezone_name)
    anchor_local = anchor.astimezone(tz)
    next_local = croniter(schedule_expression, anchor_local).get_next(datetime)
    # croniter ships no type stubs (pyproject.toml's mypy override) -- its
    # own runtime contract guarantees get_next(datetime) returns a
    # datetime, narrowed explicitly here rather than trusting an untyped
    # Any through to this function's own declared return type.
    assert isinstance(next_local, datetime)
    return next_local.astimezone(UTC)


@dataclass(frozen=True, slots=True)
class _NotDue:
    pass


@dataclass(frozen=True, slots=True)
class _Due:
    new_anchor: datetime
    # Task 6 observability (PHASE-005-automation.md's "schedule lag/
    # misfire" line): the occurrence this tick is actually resolving --
    # `run_scheduler_once` computes `moment - first_due` from this to
    # observe into `schedule_lag_seconds`.
    first_due: datetime


@dataclass(frozen=True, slots=True)
class _MisfireSkip:
    new_anchor: datetime
    first_due: datetime


def _evaluate_schedule(
    *,
    schedule_expression: str,
    timezone_name: str,
    anchor: datetime,
    skip_missed: bool,
    now: datetime,
) -> _NotDue | _Due | _MisfireSkip:
    """The pure decision (module docstring's "Misfire policy" section,
    steps 1-4) -- given a trigger's own schedule/timezone/anchor/
    skip_missed and the tick's own `now`, whether (and how) it should
    fire this tick. Never touches a database; `run_scheduler_once` is the
    only caller that turns `_Due`/`_MisfireSkip` into real writes.
    """
    first_due = _next_occurrence_after(schedule_expression, timezone_name, anchor)
    if first_due > now:
        return _NotDue()

    second_due = _next_occurrence_after(schedule_expression, timezone_name, first_due)
    missed_a_whole_window = second_due <= now
    if missed_a_whole_window and skip_missed:
        return _MisfireSkip(new_anchor=now, first_due=first_due)
    return _Due(new_anchor=now, first_due=first_due)


# ---------------------------------------------------------------------------
# The tick itself.
# ---------------------------------------------------------------------------

SessionFactoryType = Callable[[], Session]


def run_scheduler_once(
    session_factory: SessionFactoryType, *, now: datetime | None = None
) -> list[TriggerEvaluationOutcome]:
    """One full scheduler tick: reads every `schedule` trigger (across
    every workspace -- `triggers.list_schedule_triggers`'s own deliberate
    non-scoping, mirroring `worker.claim_next_run`), evaluates each one's
    own schedule independently, and fires (`worker.enqueue_run` +
    `triggers.mark_trigger_fired`, one atomic commit per fired/skipped
    trigger -- module docstring's durability section) whichever are due.

    `now` defaults to the real current instant; a caller (this task's own
    tests) may pass a fixed value to evaluate a tick deterministically at
    an exact simulated moment, exactly like `approval_lifecycle_status`'s/
    `policy_status`'s own `now` parameter precedent elsewhere in this
    package.

    Returns one outcome per `schedule` trigger examined, in the same order
    `list_schedule_triggers` returned them -- callers that want a real
    60-second polling loop call this repeatedly (`scripts/
    run_automation_worker.py`); no such loop exists in this function
    itself, matching `worker.run_worker_once`'s identical "one iteration,
    deterministic, no sleep" shape.
    """
    moment = now if now is not None else datetime.now(UTC)

    with session_factory() as session, session.begin():
        due_candidates = triggers_module.list_schedule_triggers(session)

    outcomes: list[TriggerEvaluationOutcome] = []
    for trigger in due_candidates:
        # CHECK constraint ck_triggers_schedule_requires_expression_and_
        # timezone (migration 0038) guarantees both are non-NULL for every
        # trigger_type='schedule' row -- list_schedule_triggers filters to
        # exactly that trigger_type, so these asserts document an
        # already-enforced database invariant rather than re-validating it.
        assert trigger.schedule_expression is not None
        assert trigger.timezone is not None

        anchor = trigger.last_fired_at if trigger.last_fired_at is not None else trigger.created_at
        decision = _evaluate_schedule(
            schedule_expression=trigger.schedule_expression,
            timezone_name=trigger.timezone,
            anchor=anchor,
            skip_missed=trigger.skip_missed,
            now=moment,
        )

        if isinstance(decision, _NotDue):
            outcomes.append(TriggerNotDue(trigger_id=trigger.id))
            continue

        if isinstance(decision, _MisfireSkip):
            with session_factory() as session:
                won_race = triggers_module.mark_trigger_fired(
                    session,
                    trigger.workspace_id,
                    trigger.id,
                    decision.new_anchor,
                    expected_last_fired_at=trigger.last_fired_at,
                )
                session.commit()
            if won_race:
                # Task 6 observability: schedule lag (PHASE-005-automation.
                # md's "schedule lag/misfire" line) -- how late this tick
                # noticed the (skipped) backlog occurrence.
                record_schedule_lag((moment - decision.first_due).total_seconds())
            outcomes.append(
                TriggerMisfireSkipped(trigger_id=trigger.id)
                if won_race
                else TriggerRaceLost(trigger_id=trigger.id)
            )
            continue

        # _Due -- fire. The anchor-advance compare-and-swap runs *first* and
        # is checked *before* enqueue_run, not after (module docstring's
        # concurrency-safety note on `mark_trigger_fired`) -- a lost race
        # means another scheduler instance already fired this exact
        # occurrence, so this instance must not also enqueue a second run.
        # Both writes still land in the same transaction/commit as before
        # when this instance does win, preserving the original durability
        # guarantee (module docstring's durability section).
        with session_factory() as session:
            won_race = triggers_module.mark_trigger_fired(
                session,
                trigger.workspace_id,
                trigger.id,
                decision.new_anchor,
                expected_last_fired_at=trigger.last_fired_at,
            )
            if not won_race:
                session.commit()
                outcomes.append(TriggerRaceLost(trigger_id=trigger.id))
                continue

            run = worker_module.enqueue_run(
                session,
                trigger.workspace_id,
                trigger.created_by,
                workflow_id=trigger.workflow_id,
                trigger_ref=f"schedule:{trigger.id}",
            )
            session.commit()

        # Task 6 observability: schedule lag -- how late this tick actually
        # fired relative to the occurrence it resolves, regardless of
        # whether enqueue_run itself then rejected it (WorkflowNotActive/
        # WorkflowKilled below still measure "the tick's own lag" honestly
        # -- the trigger's own anchor still advanced, per this branch's
        # comment above).
        record_schedule_lag((moment - decision.first_due).total_seconds())

        if isinstance(run, worker_module.WorkflowNotActive):
            outcomes.append(
                TriggerFireFailedWorkflowNotActive(
                    trigger_id=trigger.id, workflow_id=trigger.workflow_id
                )
            )
        elif isinstance(run, worker_module.WorkflowKilled):
            # Task 6: a global or per-workflow kill switch blocked this
            # otherwise-due fire -- PHASE-005-automation.md's Rollback plan
            # ("Global/workflow kill switches stop new runs") applies to
            # the scheduler's own fire path exactly like it does to
            # runs.py's POST /automations/runs, both going through this
            # same enqueue_run choke point (module docstring's "Task 6:
            # kill switches" section).
            outcomes.append(
                TriggerFireFailedWorkflowKilled(
                    trigger_id=trigger.id, workflow_id=trigger.workflow_id
                )
            )
        else:
            outcomes.append(
                TriggerFired(
                    trigger_id=trigger.id,
                    workspace_id=trigger.workspace_id,
                    workflow_id=trigger.workflow_id,
                    run_id=run.id,
                )
            )

    return outcomes
