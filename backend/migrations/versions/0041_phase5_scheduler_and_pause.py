"""Add triggers.last_fired_at and workflow_runs.pause_requested_at for
Phase 5 Automation Task 4 (scheduler/misfire tracking, user-initiated
pause/resume).

Phase 5 Task 4 (`docs/superpowers/specs/2026-07-25-phase-5-automation-design.md`
Decision 7, `docs/phases/phase-005/API-SCHEMAS.md`,
`docs/phases/phase-005/DATA-MODEL.md` v0.2.0). Numbered ``0041`` -- the true
next-in-chain after ``0040_phase5_approval_requests.py``, per this
codebase's documented migration-numbering convention (migration ``0029``'s
docstring precedent, restated by ``0038``/``0039``/``0040``'s own
docstrings).

**`triggers.last_fired_at` -- the durable anchor `ecc.domains.automation.
scheduler` uses to implement Decision 7's catch-up-once misfire policy.**
Two designs were available for tracking "when did this schedule trigger
last fire": (a) this new column, updated transactionally alongside the
`workflow_runs` INSERT the scheduler tick writes when a trigger fires, or
(b) derive it by querying the most recent `workflow_runs` row with
`trigger_ref = 'schedule:<trigger id>'`. This migration picks (a) --
`scheduler.py`'s own module docstring has the full reasoning, restated
briefly here: a dedicated column makes "did this trigger already fire for
this window" a single indexed point lookup rather than a `MAX(queued_at)`
scan of `workflow_runs` filtered by a string-prefix `trigger_ref` (no index
supports that filter shape without adding one anyway), and, more
importantly, keeps the trigger's own fire-bookkeeping colocated with the
trigger row itself rather than requiring every future run-listing/analytics
query on `workflow_runs` to know it must not be confused by "the row that
also happens to double as scheduler bookkeeping." The scheduler's own tick
writes both this column and the fired `workflow_runs` row in one atomic
transaction (single `session.commit()`), not the before/after-`execute()`
two-commit discipline `worker.run_step` needs -- see `scheduler.py`'s
module docstring for why that discipline does not apply here (there is no
risky external call between the two writes to bracket).

**`workflow_runs.pause_requested_at` -- the user-initiated-pause analogue
of `cancel_requested_at` (migration 0039).** `API-SCHEMAS.md`'s
`POST /automations/runs/{id}/pause` needs the identical "block the next
not-yet-dispatched step, do not attempt to interrupt one already
dispatched" mechanic `cancel_requested_at` already implements for
`POST .../cancel` -- reusing that exact column would conflate two
independently-resumable/terminal outcomes (a cancelled run is terminal; a
paused run is not and has a real resume path back to `'queued'`), so this
migration adds a second, parallel flag rather than overloading the
existing one. `ecc.domains.automation.worker.pause_run`/`resume_run` (this
task's own addition) are the only writers; `resume_run` clears this column
back to `NULL` on resume (unlike `cancel_requested_at`, which is one-way --
a cancelled run never un-cancels), matching this codebase's precedent of
one-way flags only where the corresponding action is genuinely one-way.
"""

import sqlalchemy as sa
from alembic import op

revision = "0041_phase5_scheduler_and_pause"
down_revision = "0040_phase5_approval_requests"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("triggers", sa.Column("last_fired_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "workflow_runs",
        sa.Column("pause_requested_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("workflow_runs", "pause_requested_at")
    op.drop_column("triggers", "last_fired_at")
