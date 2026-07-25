"""Phase 5 Automation Task 4: the schedule-trigger evaluation tick
(`docs/superpowers/specs/2026-07-25-phase-5-automation-design.md`
Decision 7, `ecc.domains.automation.scheduler`).

Covers, per this task's own required minimum:

1. A `schedule` trigger whose cron expression has already elapsed fires
   exactly once per tick.
2. Timezone-aware next-fire-time computation in the trigger's own
   declared timezone, not server-local/naive-UTC time -- both a direct
   IANA-offset proof (`Asia/Kolkata`) and a real DST-transition proof
   (`America/New_York`'s 2026-11-01 fall-back).
3. Misfire/catch-up-once: a trigger whose fire time elapsed several
   fire-intervals ago (the scheduler "was down") fires exactly once, not
   once per missed window.
4. `skip_missed=true`: the identical backlog does NOT catch up, but an
   ordinary on-time fire still happens once the schedule catches up to
   the present on its own.
5. An `event`-type trigger is never auto-fired by the scheduler tick
   (`triggers.list_schedule_triggers` never returns one) -- proves the
   explicit non-implementation of event-trigger firing.
6. Firing enqueues a `workflow_runs` row with `trigger_ref` set to
   `schedule:<trigger id>`.
7. Durability: a crash (simulated via fault injection, since the two
   writes are wrapped in one atomic transaction/commit -- module
   docstring's own reasoning for why this activation needs only one
   commit, not `run_step`'s two) between "anchor advanced" and "run
   enqueued" leaves *both* writes rolled back, never a partial
   (duplicate-prone or anchor-lost) state -- reasoned about the same way
   Task 2's `DigestVisibilityProbeAdapter` proved a real durability
   property directly, not merely the absence of an exception.
8. Concurrency: two `run_scheduler_once` calls racing the same due
   trigger under real concurrent database connections (not mocked) --
   exactly one fires it, mirroring `worker.py`'s own `test_two_workers_
   racing_the_same_run_exactly_one_claims_it` precedent for `triggers.
   mark_trigger_fired`'s compare-and-swap `UPDATE`.
"""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.automation import scheduler as automation_scheduler
from ecc.domains.automation import triggers as automation_triggers
from ecc.domains.automation import worker as automation_worker
from ecc.domains.automation import workflows as automation_workflows

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


# ---------------------------------------------------------------------------
# 1. Pure schedule math -- no session, no database, directly unit-testable
# (module docstring / scheduler.py's own "mirrors approvals.evaluate_
# approval_requirement" precedent).
# ---------------------------------------------------------------------------


def test_next_occurrence_after_is_timezone_aware_not_naive_utc() -> None:
    """The exact class of bug design doc Decision 7 names by name (Phase
    3's `MeetingPrep.tsx` timezone-mislabeling bug): midnight in
    `Asia/Kolkata` (UTC+5:30) is *not* midnight UTC -- a naive
    server-local/UTC-as-if-local evaluation would answer
    `2026-01-01 00:00:00+00:00`; the real, correct answer is
    `2025-12-31 18:30:00+00:00` (midnight IST minus 5:30).
    """
    anchor = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    occurrence = automation_scheduler._next_occurrence_after(  # noqa: SLF001
        "0 0 * * *", "Asia/Kolkata", anchor
    )
    # Confirmed directly: the correct IST-midnight-in-UTC instant.
    assert occurrence == datetime(2026, 1, 1, 18, 30, tzinfo=UTC)
    # And explicitly not the naive-UTC-as-if-local wrong answer.
    assert occurrence != datetime(2026, 1, 2, 0, 0, tzinfo=UTC)


def test_next_occurrence_after_handles_dst_fall_back_transition() -> None:
    """A real DST transition: `America/New_York` falls back from EDT
    (UTC-4) to EST (UTC-5) at 2026-11-01 02:00 local time (confirmed
    directly against `zoneinfo` before writing this test). A daily
    9am-local schedule's UTC delta between consecutive occurrences is
    exactly 24h on every ordinary day, but 25h on the specific day that
    contains the fall-back -- a fixed-24h-interval (i.e. DST-naive)
    computation would get this specific day wrong (it would compute 8am
    local, not 9am local, the day after the transition). This is the
    concrete, reproducible proof this module's own zoneinfo+croniter
    composition is DST-correct, not merely same-timezone-round-trip
    correct.
    """
    before_transition = datetime(2026, 10, 30, 12, 0, tzinfo=UTC)
    occ1 = automation_scheduler._next_occurrence_after(  # noqa: SLF001
        "0 9 * * *", "America/New_York", before_transition
    )
    occ2 = automation_scheduler._next_occurrence_after(  # noqa: SLF001
        "0 9 * * *", "America/New_York", occ1
    )
    occ3 = automation_scheduler._next_occurrence_after(  # noqa: SLF001
        "0 9 * * *", "America/New_York", occ2
    )
    assert occ1 == datetime(2026, 10, 30, 13, 0, tzinfo=UTC)  # 9am EDT (-4)
    assert occ2 == datetime(2026, 10, 31, 13, 0, tzinfo=UTC)  # 9am EDT (-4)
    assert occ2 - occ1 == timedelta(hours=24)  # ordinary day, no transition
    assert occ3 == datetime(2026, 11, 1, 14, 0, tzinfo=UTC)  # 9am EST (-5)
    # The transition day: 25h in UTC terms, not 24h -- the DST-naive wrong
    # answer would be occ3 == 2026-11-01 13:00 UTC (8am EST, not 9am EST).
    assert occ3 - occ2 == timedelta(hours=25)


def test_evaluate_schedule_not_due_before_first_occurrence() -> None:
    anchor = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
    now = datetime(2026, 3, 1, 0, 30, tzinfo=UTC)  # cron fires hourly, not yet
    decision = automation_scheduler._evaluate_schedule(  # noqa: SLF001
        schedule_expression="0 * * * *",
        timezone_name="UTC",
        anchor=anchor,
        skip_missed=False,
        now=now,
    )
    assert isinstance(decision, automation_scheduler._NotDue)  # noqa: SLF001


def test_evaluate_schedule_fires_once_for_a_single_ordinary_elapsed_occurrence() -> None:
    """The ordinary steady state -- exactly one occurrence elapsed since
    the anchor -- always fires, regardless of `skip_missed`, since nothing
    was genuinely missed (this is what makes `skip_missed=true` still able
    to fire at all; see the misfire-vs-skip tests below for the backlog
    case this is deliberately distinct from).
    """
    anchor = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
    now = datetime(2026, 3, 1, 1, 5, tzinfo=UTC)  # one hourly occurrence elapsed
    for skip_missed in (False, True):
        decision = automation_scheduler._evaluate_schedule(  # noqa: SLF001
            schedule_expression="0 * * * *",
            timezone_name="UTC",
            anchor=anchor,
            skip_missed=skip_missed,
            now=now,
        )
        assert isinstance(decision, automation_scheduler._Due)  # noqa: SLF001
        assert decision.new_anchor == now


def test_evaluate_schedule_catch_up_once_for_a_multi_window_backlog() -> None:
    """The scheduler "was down" for hours: many hourly occurrences have
    elapsed since the anchor. `skip_missed=False` (the default) still
    fires exactly once -- `_Due`, not repeated -- never once per missed
    window (there is no mechanism in this module that could even express
    "fire N times"; `_evaluate_schedule` returns a single decision).
    """
    anchor = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
    now = datetime(2026, 3, 1, 6, 30, tzinfo=UTC)  # 6 hourly occurrences missed
    decision = automation_scheduler._evaluate_schedule(  # noqa: SLF001
        schedule_expression="0 * * * *",
        timezone_name="UTC",
        anchor=anchor,
        skip_missed=False,
        now=now,
    )
    assert isinstance(decision, automation_scheduler._Due)  # noqa: SLF001
    assert decision.new_anchor == now


def test_evaluate_schedule_skip_missed_true_skips_the_same_backlog() -> None:
    anchor = datetime(2026, 3, 1, 0, 0, tzinfo=UTC)
    now = datetime(2026, 3, 1, 6, 30, tzinfo=UTC)
    decision = automation_scheduler._evaluate_schedule(  # noqa: SLF001
        schedule_expression="0 * * * *",
        timezone_name="UTC",
        anchor=anchor,
        skip_missed=True,
        now=now,
    )
    assert isinstance(decision, automation_scheduler._MisfireSkip)  # noqa: SLF001
    assert decision.new_anchor == now


# ---------------------------------------------------------------------------
# Shared workspace/user/workflow fixtures -- self-contained, mirroring
# test_automation_worker_postgres.py's own fixture shape.
# ---------------------------------------------------------------------------


@pytest.fixture
def scheduler_test_context() -> Iterator[tuple[UUID, UUID]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Automation Scheduler Test', 'Asia/Kolkata', :now)"
            ),
            {"id": workspace_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO users (id, workspace_id, email, password_hash, created_at) "
                "VALUES (:id, :workspace_id, :email, 'test-password-hash', :now)"
            ),
            {
                "id": user_id,
                "workspace_id": workspace_id,
                "email": f"{user_id}@example.test",
                "now": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                "expires_at, last_seen_at) "
                "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :now)"
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "user_id": user_id,
                "token_hash": sha256(token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "now": now,
            },
        )

    try:
        yield workspace_id, user_id
    finally:
        _cleanup_workspace(workspace_id)


def _cleanup_workspace(workspace_id: UUID) -> None:
    with engine.begin() as connection:
        for table in (
            "approval_requests",
            "workflow_run_steps",
            "workflow_runs",
            "triggers",
            "automation_policies",
            "workflow_versions",
            "workflow_definitions",
            "event_outbox",
            "audit_events",
            "idempotency_records",
            "sessions",
            "users",
        ):
            connection.execute(
                text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                {"workspace_id": workspace_id},
            )
        connection.execute(
            text("DELETE FROM workspaces WHERE id = :workspace_id"), {"workspace_id": workspace_id}
        )


def _publish_workflow(
    workspace_id: UUID, user_id: UUID, workflow_id: str
) -> automation_workflows.WorkflowVersion:
    """A minimal active workflow -- scheduler tests only ever assert on
    `enqueue_run`'s own enqueue-time behavior (trigger_ref, status,
    pinned version), never on step dispatch, so no policy and a
    single-step graph pointing at an adapter no test here ever runs is
    enough.
    """
    graph = {
        "steps": [
            {
                "step_id": "s1",
                "step_type": "action",
                "action_ref": "test.unused",
                "input_mapping": {},
                "on_success": "succeeded",
                "on_failure": "failed",
            }
        ]
    }
    with SessionFactory() as session, session.begin():
        draft = automation_workflows.create_workflow_draft(
            session,
            workspace_id,
            user_id,
            workflow_id=workflow_id,
            graph=graph,
            trigger_refs=[],
            policy_ref=None,
        )
        activated = automation_workflows.activate_workflow_version(session, workspace_id, draft.id)
    assert isinstance(activated, automation_workflows.WorkflowVersion)
    return activated


def _create_schedule_trigger(
    workspace_id: UUID,
    user_id: UUID,
    workflow_id: str,
    *,
    schedule_expression: str,
    timezone: str = "UTC",
    skip_missed: bool = False,
) -> automation_triggers.Trigger:
    with SessionFactory() as session, session.begin():
        return automation_triggers.create_trigger(
            session,
            workspace_id,
            user_id,
            workflow_id=workflow_id,
            trigger_type="schedule",
            schedule_expression=schedule_expression,
            timezone=timezone,
            skip_missed=skip_missed,
        )


def _set_last_fired_at(trigger_id: UUID, value: datetime | None) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE triggers SET last_fired_at = :value WHERE id = :id"),
            {"value": value, "id": trigger_id},
        )


def _set_created_at(trigger_id: UUID, value: datetime) -> None:
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE triggers SET created_at = :value WHERE id = :id"),
            {"value": value, "id": trigger_id},
        )


# ---------------------------------------------------------------------------
# 2. Full DB-backed tick: fires exactly once, sets trigger_ref, respects
#    timezone, respects misfire/skip_missed policy, ignores event/manual
#    triggers.
# ---------------------------------------------------------------------------


def test_due_schedule_trigger_fires_exactly_once_and_sets_trigger_ref(
    scheduler_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = scheduler_test_context
    workflow_id = f"test.sched-fire.{uuid4().hex}"
    _publish_workflow(workspace_id, user_id, workflow_id)
    trigger = _create_schedule_trigger(
        workspace_id, user_id, workflow_id, schedule_expression="0 * * * *", timezone="UTC"
    )
    # created_at defaults to "now" (fixture insert time) -- push it well
    # into the past so the trigger's first hourly occurrence has already
    # elapsed relative to the tick's own `now` below.
    _set_created_at(trigger.id, datetime(2026, 3, 1, 0, 0, tzinfo=UTC))

    tick_now = datetime(2026, 3, 1, 1, 30, tzinfo=UTC)
    outcomes = automation_scheduler.run_scheduler_once(SessionFactory, now=tick_now)
    fired = [o for o in outcomes if isinstance(o, automation_scheduler.TriggerFired)]
    assert any(o.trigger_id == trigger.id for o in fired)

    with SessionFactory() as session, session.begin():
        runs = automation_worker.list_runs(session, workspace_id)
    matching = [r for r in runs if r.trigger_ref == f"schedule:{trigger.id}"]
    assert len(matching) == 1
    assert matching[0].status == "queued"
    assert matching[0].workflow_id == workflow_id

    # A second tick at the *same* now must not re-fire -- the anchor
    # already advanced.
    outcomes_again = automation_scheduler.run_scheduler_once(SessionFactory, now=tick_now)
    fired_again = [o for o in outcomes_again if isinstance(o, automation_scheduler.TriggerFired)]
    assert not any(o.trigger_id == trigger.id for o in fired_again)
    with SessionFactory() as session, session.begin():
        runs_after = automation_worker.list_runs(session, workspace_id)
    matching_after = [r for r in runs_after if r.trigger_ref == f"schedule:{trigger.id}"]
    assert len(matching_after) == 1  # still exactly one, not two


def test_schedule_trigger_evaluated_in_its_own_declared_timezone(
    scheduler_test_context: tuple[UUID, UUID],
) -> None:
    """Two triggers with identical UTC clock-times but different declared
    IANA timezones fire at different real moments -- proves the tick
    reads each trigger's own `timezone` column, not a single shared
    server-local assumption.
    """
    workspace_id, user_id = scheduler_test_context
    workflow_id_ist = f"test.sched-tz-ist.{uuid4().hex}"
    workflow_id_utc = f"test.sched-tz-utc.{uuid4().hex}"
    _publish_workflow(workspace_id, user_id, workflow_id_ist)
    _publish_workflow(workspace_id, user_id, workflow_id_utc)

    # Daily at midnight, one trigger declared Asia/Kolkata (UTC+5:30), one
    # declared UTC. Midnight IST is 18:30 UTC the *previous* day.
    trigger_ist = _create_schedule_trigger(
        workspace_id,
        user_id,
        workflow_id_ist,
        schedule_expression="0 0 * * *",
        timezone="Asia/Kolkata",
    )
    trigger_utc = _create_schedule_trigger(
        workspace_id, user_id, workflow_id_utc, schedule_expression="0 0 * * *", timezone="UTC"
    )
    anchor = datetime(2025, 12, 31, 0, 0, tzinfo=UTC)
    _set_created_at(trigger_ist.id, anchor)
    _set_created_at(trigger_utc.id, anchor)

    # 2025-12-31 20:00 UTC: past midnight IST (18:30 UTC) but before
    # midnight UTC itself.
    tick_now = datetime(2025, 12, 31, 20, 0, tzinfo=UTC)
    outcomes = automation_scheduler.run_scheduler_once(SessionFactory, now=tick_now)
    fired_ids = {o.trigger_id for o in outcomes if isinstance(o, automation_scheduler.TriggerFired)}
    not_due_ids = {
        o.trigger_id for o in outcomes if isinstance(o, automation_scheduler.TriggerNotDue)
    }

    assert trigger_ist.id in fired_ids  # midnight IST already passed
    assert trigger_utc.id in not_due_ids  # midnight UTC has not arrived yet


def test_misfire_backlog_fires_exactly_once_not_once_per_missed_window(
    scheduler_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = scheduler_test_context
    workflow_id = f"test.sched-misfire.{uuid4().hex}"
    _publish_workflow(workspace_id, user_id, workflow_id)
    trigger = _create_schedule_trigger(
        workspace_id, user_id, workflow_id, schedule_expression="0 * * * *", timezone="UTC"
    )
    # Simulate "the scheduler was down": last_fired_at several hourly
    # windows in the past relative to the tick's own now.
    _set_last_fired_at(trigger.id, datetime(2026, 3, 1, 0, 0, tzinfo=UTC))

    tick_now = datetime(2026, 3, 1, 6, 30, tzinfo=UTC)  # 6 hourly windows elapsed
    outcomes = automation_scheduler.run_scheduler_once(SessionFactory, now=tick_now)
    fired = [o for o in outcomes if isinstance(o, automation_scheduler.TriggerFired)]
    assert len([o for o in fired if o.trigger_id == trigger.id]) == 1

    with SessionFactory() as session, session.begin():
        runs = automation_worker.list_runs(session, workspace_id)
    matching = [r for r in runs if r.trigger_ref == f"schedule:{trigger.id}"]
    assert len(matching) == 1  # exactly one run, not six


def test_skip_missed_true_does_not_catch_up_the_backlog(
    scheduler_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = scheduler_test_context
    workflow_id = f"test.sched-skip-missed.{uuid4().hex}"
    _publish_workflow(workspace_id, user_id, workflow_id)
    trigger = _create_schedule_trigger(
        workspace_id,
        user_id,
        workflow_id,
        schedule_expression="0 * * * *",
        timezone="UTC",
        skip_missed=True,
    )
    _set_last_fired_at(trigger.id, datetime(2026, 3, 1, 0, 0, tzinfo=UTC))

    tick_now = datetime(2026, 3, 1, 6, 30, tzinfo=UTC)  # 6 hourly windows elapsed
    outcomes = automation_scheduler.run_scheduler_once(SessionFactory, now=tick_now)
    skipped = [o for o in outcomes if isinstance(o, automation_scheduler.TriggerMisfireSkipped)]
    assert any(o.trigger_id == trigger.id for o in skipped)

    with SessionFactory() as session, session.begin():
        runs = automation_worker.list_runs(session, workspace_id)
    matching = [r for r in runs if r.trigger_ref == f"schedule:{trigger.id}"]
    assert matching == []  # no catch-up fire at all

    # But an ordinary, on-time future occurrence still fires normally --
    # skip_missed suppresses backlog catch-up, not the trigger entirely.
    next_tick_now = datetime(2026, 3, 1, 7, 5, tzinfo=UTC)  # one ordinary hour later
    outcomes_next = automation_scheduler.run_scheduler_once(SessionFactory, now=next_tick_now)
    fired_next = [o for o in outcomes_next if isinstance(o, automation_scheduler.TriggerFired)]
    assert any(o.trigger_id == trigger.id for o in fired_next)
    with SessionFactory() as session, session.begin():
        runs_after = automation_worker.list_runs(session, workspace_id)
    matching_after = [r for r in runs_after if r.trigger_ref == f"schedule:{trigger.id}"]
    assert len(matching_after) == 1


def test_event_trigger_never_auto_fired_by_scheduler_tick(
    scheduler_test_context: tuple[UUID, UUID],
) -> None:
    """Proves the explicit non-implementation of event-trigger firing
    (module docstring's own scope boundary): an `event` trigger is simply
    never returned by `triggers.list_schedule_triggers`, so it can never
    appear in a tick's outcomes at all, regardless of how long ago it was
    created or how "due" a naive reading of its own fields might suggest.
    """
    workspace_id, user_id = scheduler_test_context
    workflow_id = f"test.sched-event-ignored.{uuid4().hex}"
    _publish_workflow(workspace_id, user_id, workflow_id)
    with SessionFactory() as session, session.begin():
        event_trigger = automation_triggers.create_trigger(
            session,
            workspace_id,
            user_id,
            workflow_id=workflow_id,
            trigger_type="event",
            event_type_filter="some.event.v1",
        )
        manual_trigger = automation_triggers.create_trigger(
            session,
            workspace_id,
            user_id,
            workflow_id=workflow_id,
            trigger_type="manual",
        )

    outcomes = automation_scheduler.run_scheduler_once(
        SessionFactory, now=datetime(2030, 1, 1, tzinfo=UTC)
    )
    outcome_ids = {getattr(o, "trigger_id", None) for o in outcomes}
    assert event_trigger.id not in outcome_ids
    assert manual_trigger.id not in outcome_ids

    with SessionFactory() as session, session.begin():
        runs = automation_worker.list_runs(session, workspace_id)
    assert runs == []


def test_workflow_not_active_still_advances_anchor_without_enqueuing_run(
    scheduler_test_context: tuple[UUID, UUID],
) -> None:
    """A schedule trigger naming a workflow with no `active` version fires
    "logically" (its own schedule was due) but `worker.enqueue_run`
    rejects it -- the anchor still advances (module docstring: "so a
    workflow left inactive does not cause this trigger to be re-evaluated
    as due on every single tick forever").
    """
    workspace_id, user_id = scheduler_test_context
    workflow_id = f"test.sched-inactive.{uuid4().hex}"
    graph = {
        "steps": [
            {
                "step_id": "s1",
                "step_type": "action",
                "action_ref": "test.unused",
                "input_mapping": {},
                "on_success": "succeeded",
                "on_failure": "failed",
            }
        ]
    }
    with SessionFactory() as session, session.begin():
        # A draft only -- never activated, so get_active_workflow_version
        # finds nothing.
        automation_workflows.create_workflow_draft(
            session,
            workspace_id,
            user_id,
            workflow_id=workflow_id,
            graph=graph,
            trigger_refs=[],
            policy_ref=None,
        )
    trigger = _create_schedule_trigger(
        workspace_id, user_id, workflow_id, schedule_expression="0 * * * *", timezone="UTC"
    )
    _set_created_at(trigger.id, datetime(2026, 3, 1, 0, 0, tzinfo=UTC))

    tick_now = datetime(2026, 3, 1, 1, 30, tzinfo=UTC)
    outcomes = automation_scheduler.run_scheduler_once(SessionFactory, now=tick_now)
    failed = [
        o
        for o in outcomes
        if isinstance(o, automation_scheduler.TriggerFireFailedWorkflowNotActive)
    ]
    assert any(o.trigger_id == trigger.id for o in failed)

    with SessionFactory() as session, session.begin():
        refreshed = automation_triggers.get_trigger(session, workspace_id, trigger.id)
    assert refreshed is not None
    assert refreshed.last_fired_at == tick_now

    # No repeated attempt on the very next tick at the same instant.
    outcomes_again = automation_scheduler.run_scheduler_once(SessionFactory, now=tick_now)
    failed_again = [
        o
        for o in outcomes_again
        if isinstance(o, automation_scheduler.TriggerFireFailedWorkflowNotActive)
        and o.trigger_id == trigger.id
    ]
    assert failed_again == []


# ---------------------------------------------------------------------------
# 3. Durability: a crash between the two writes leaves both rolled back.
# ---------------------------------------------------------------------------


def test_crash_between_enqueue_and_anchor_advance_rolls_back_atomically(
    scheduler_test_context: tuple[UUID, UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Proves the real property this module's own docstring depends on --
    not merely that no exception propagates, but that a failure between
    the two writes (`triggers.mark_trigger_fired`'s compare-and-swap
    `UPDATE`, which runs *first* per this module's concurrency-safety
    design, then `worker.enqueue_run`'s `INSERT`) leaves *neither* one
    durably visible, exactly like `DigestVisibilityProbeAdapter` proved a
    positive visibility property for `worker.run_step` in Task 2. Here the
    equivalent proof is negative: fault-inject a failure *after* `mark_
    trigger_fired`'s `UPDATE` has executed (simulating a process crash at
    that exact point -- the stronger proof, since it confirms the already-
    executed-but-not-yet-committed anchor advance itself rolls back, not
    merely that a call never reached) and confirm that after the failure,
    an independent read sees no partial state -- no orphan `workflow_runs`
    row, no advanced `last_fired_at` -- because neither write was ever
    committed (this module's own single-atomic-commit design, module
    docstring's durability section).
    """
    workspace_id, user_id = scheduler_test_context
    workflow_id = f"test.sched-crash.{uuid4().hex}"
    _publish_workflow(workspace_id, user_id, workflow_id)
    trigger = _create_schedule_trigger(
        workspace_id, user_id, workflow_id, schedule_expression="0 * * * *", timezone="UTC"
    )
    _set_created_at(trigger.id, datetime(2026, 3, 1, 0, 0, tzinfo=UTC))
    tick_now = datetime(2026, 3, 1, 1, 30, tzinfo=UTC)

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("simulated crash between mark_trigger_fired and enqueue_run")

    monkeypatch.setattr(automation_scheduler.worker_module, "enqueue_run", _boom)
    with pytest.raises(RuntimeError):
        automation_scheduler.run_scheduler_once(SessionFactory, now=tick_now)

    with SessionFactory() as session, session.begin():
        runs = automation_worker.list_runs(session, workspace_id)
        refreshed = automation_triggers.get_trigger(session, workspace_id, trigger.id)
    matching = [r for r in runs if r.trigger_ref == f"schedule:{trigger.id}"]
    assert matching == []  # the enqueue_run INSERT was rolled back too
    assert refreshed is not None
    assert refreshed.last_fired_at is None  # the anchor never advanced

    # The trigger is not permanently stuck -- a real (unpatched) tick
    # still fires it normally afterward.
    monkeypatch.undo()
    outcomes = automation_scheduler.run_scheduler_once(SessionFactory, now=tick_now)
    fired = [o for o in outcomes if isinstance(o, automation_scheduler.TriggerFired)]
    assert any(o.trigger_id == trigger.id for o in fired)
    with SessionFactory() as session, session.begin():
        runs_after = automation_worker.list_runs(session, workspace_id)
    matching_after = [r for r in runs_after if r.trigger_ref == f"schedule:{trigger.id}"]
    assert len(matching_after) == 1


def test_mark_trigger_fired_compare_and_swap_second_caller_with_stale_expectation_loses(
    scheduler_test_context: tuple[UUID, UUID],
) -> None:
    """Deterministic, non-threaded proof of the actual compare-and-swap
    mechanism `run_scheduler_once`'s concurrency safety depends on
    (`triggers.mark_trigger_fired`, module docstring's concurrency-safety
    section) -- two sequential callers both holding the trigger's
    *original* `last_fired_at` as their `expected_last_fired_at` (exactly
    the situation two concurrent scheduler ticks that both read the
    trigger row before either wrote would be in): the first call's
    `UPDATE` matches and wins (`True`); the second call's `WHERE` clause
    no longer matches the now-advanced row, so it affects zero rows and
    loses (`False`) -- without needing real thread interleaving to
    observe, which the DB-integration race test below can only encourage,
    not guarantee.
    """
    workspace_id, user_id = scheduler_test_context
    workflow_id = f"test.sched-cas.{uuid4().hex}"
    _publish_workflow(workspace_id, user_id, workflow_id)
    trigger = _create_schedule_trigger(
        workspace_id, user_id, workflow_id, schedule_expression="0 * * * *", timezone="UTC"
    )
    assert trigger.last_fired_at is None

    with SessionFactory() as session:
        first = automation_triggers.mark_trigger_fired(
            session,
            workspace_id,
            trigger.id,
            datetime(2026, 3, 1, 1, 0, tzinfo=UTC),
            expected_last_fired_at=None,
        )
        session.commit()
    assert first is True

    with SessionFactory() as session:
        second = automation_triggers.mark_trigger_fired(
            session,
            workspace_id,
            trigger.id,
            datetime(2026, 3, 1, 1, 0, tzinfo=UTC),
            expected_last_fired_at=None,  # stale -- the row already advanced
        )
        session.commit()
    assert second is False

    with SessionFactory() as session, session.begin():
        refreshed = automation_triggers.get_trigger(session, workspace_id, trigger.id)
    assert refreshed is not None
    assert refreshed.last_fired_at == datetime(2026, 3, 1, 1, 0, tzinfo=UTC)


def test_two_concurrent_ticks_racing_the_same_due_trigger_fire_exactly_once(
    scheduler_test_context: tuple[UUID, UUID],
) -> None:
    """Real concurrent database connections (not mocked) -- mirrors
    `worker.py`'s own `test_two_workers_racing_the_same_run_exactly_one_
    claims_it` precedent for the identical class of property: two
    independent processes evaluating the same due trigger at effectively
    the same instant (two `scripts/run_automation_worker.py` replicas'
    ticks landing together, or simply this same process's own next tick
    starting before the previous one's transaction committed) must not
    both enqueue a run for it.

    **Why this test tolerates either `TriggerRaceLost` or `TriggerNotDue`
    for the second thread, rather than asserting one specifically.**
    `run_scheduler_once` does its own `list_schedule_triggers` read (a
    short, separately-committed transaction) *before* reaching the
    compare-and-swap write -- if real wall-clock thread scheduling lets
    one thread's entire tick (read, decide, write, commit) finish before
    the other thread's own read even executes, the second thread's read
    already observes the first thread's committed anchor advance and
    correctly evaluates `_NotDue` on its own, never reaching the compare-
    and-swap `UPDATE` at all -- a `TriggerRaceLost` outcome only occurs
    when both threads' reads interleave closely enough that both reach the
    `UPDATE` holding the *same* stale `expected_last_fired_at`. Both
    outcomes are correct (neither ever produces a duplicate run); which
    one actually happens for the losing thread depends on real timing this
    test does not control. The property this test exists to prove --
    `test_mark_trigger_fired_compare_and_swap_second_caller_with_stale_
    expectation_loses` above proves the underlying mechanism itself,
    deterministically -- is the one asserted unconditionally below: at
    most one `TriggerFired`, at most one `workflow_runs` row, ever.
    """
    workspace_id, user_id = scheduler_test_context
    workflow_id = f"test.sched-race.{uuid4().hex}"
    _publish_workflow(workspace_id, user_id, workflow_id)
    trigger = _create_schedule_trigger(
        workspace_id, user_id, workflow_id, schedule_expression="0 * * * *", timezone="UTC"
    )
    _set_created_at(trigger.id, datetime(2026, 3, 1, 0, 0, tzinfo=UTC))
    tick_now = datetime(2026, 3, 1, 1, 30, tzinfo=UTC)

    def _tick() -> list[automation_scheduler.TriggerEvaluationOutcome]:
        return automation_scheduler.run_scheduler_once(SessionFactory, now=tick_now)

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_a = pool.submit(_tick)
        future_b = pool.submit(_tick)
        outcomes_a = future_a.result()
        outcomes_b = future_b.result()

    all_outcomes = [
        o for outcomes in (outcomes_a, outcomes_b) for o in outcomes if o.trigger_id == trigger.id
    ]
    fired = [o for o in all_outcomes if isinstance(o, automation_scheduler.TriggerFired)]
    assert len(fired) == 1
    # The other outcome is always either TriggerRaceLost or TriggerNotDue
    # (docstring above) -- never a second TriggerFired.
    assert len(all_outcomes) == 2
    assert all(
        isinstance(o, automation_scheduler.TriggerRaceLost | automation_scheduler.TriggerNotDue)
        for o in all_outcomes
        if o is not fired[0]
    )

    with SessionFactory() as session, session.begin():
        runs = automation_worker.list_runs(session, workspace_id)
    matching = [r for r in runs if r.trigger_ref == f"schedule:{trigger.id}"]
    assert len(matching) == 1  # exactly one workflow_runs row, not two


# ---------------------------------------------------------------------------
# 4. Workspace isolation for the newly-added triggers.list_schedule_
#    triggers -- deliberately unscoped by design (module docstring), so
#    this proves it correctly evaluates triggers across workspaces without
#    ever writing a run into the wrong one.
# ---------------------------------------------------------------------------


def test_scheduler_never_writes_a_run_into_the_wrong_workspace(
    scheduler_test_context: tuple[UUID, UUID],
) -> None:
    workspace_a, user_a = scheduler_test_context
    workflow_id = f"test.sched-isolation.{uuid4().hex}"
    _publish_workflow(workspace_a, user_a, workflow_id)
    trigger = _create_schedule_trigger(
        workspace_a, user_a, workflow_id, schedule_expression="0 * * * *", timezone="UTC"
    )
    _set_created_at(trigger.id, datetime(2026, 3, 1, 0, 0, tzinfo=UTC))

    workspace_b = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Scheduler Isolation Peer', 'UTC', :now)"
            ),
            {"id": workspace_b, "now": now},
        )
    try:
        tick_now = datetime(2026, 3, 1, 1, 30, tzinfo=UTC)
        automation_scheduler.run_scheduler_once(SessionFactory, now=tick_now)

        with SessionFactory() as session, session.begin():
            runs_b = automation_worker.list_runs(session, workspace_b)
        assert runs_b == []

        with SessionFactory() as session, session.begin():
            runs_a = automation_worker.list_runs(session, workspace_a)
        assert any(r.trigger_ref == f"schedule:{trigger.id}" for r in runs_a)
    finally:
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM workspaces WHERE id = :id"), {"id": workspace_b})
