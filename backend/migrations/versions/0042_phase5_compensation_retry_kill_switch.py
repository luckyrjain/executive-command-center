"""Create compensation_steps/automation_kill_switches and alter
workflow_run_steps/workflow_runs for Phase 5 Automation Task 6
(compensation, observability and kill switches).

Phase 5 Task 6 (`docs/superpowers/specs/2026-07-25-phase-5-automation-design.md`
Decision 9, `docs/phases/phase-005/EXECUTION-CONTRACT.md`'s retry/
compensation section, `docs/phases/phase-005/DATA-MODEL.md` v0.2.0,
`docs/runbooks/PHASE-5-RECOVERY.md`'s kill-switch section). Numbered
``0042`` -- the true next-in-chain after
``0041_phase5_scheduler_and_pause.py``, per this codebase's documented
migration-numbering convention (migration ``0029``'s docstring precedent,
restated by ``0038``/``0039``/``0040``/``0041``'s own docstrings).

**`compensation_steps` -- the explicit compensation ledger `DATA-MODEL.md`
already names** (`run_id`, `compensates_step_index`, `action_digest`,
`status`), never populated before this task. `(workspace_id, run_id)`
composite-FK's to `workflow_runs(workspace_id, id)`, `ondelete="CASCADE"` --
identical shape and reasoning to `workflow_run_steps`/`approval_requests`'s
own FK (migrations 0039/0040): `uq_workflow_runs_workspace_id` is what
makes a cross-workspace `compensation_steps` row referencing another
workspace's `run_id` impossible at the database layer, not only by
convention. This table is a *ledger*, deliberately separate from
`workflow_run_steps`'s own `step_type='compensation'` rows (`DATA-MODEL.md`:
"`step_type='compensation'` rows are distinct rows from the step they
compensate") -- `workflow_run_steps` records the compensation *step's own*
dispatch (its `step_index`, its own `input`/`output`), while
`compensation_steps` records the *relationship* (which earlier step,
identified by `compensates_step_index`, this compensation is undoing) plus
a redundant, independently-queryable `status`/`action_digest` pair a
reviewer or a future UI can read without joining back through the graph to
figure out which `workflow_run_steps` row is a compensation for which.

**`workflow_run_steps.attempt_count` -- the bounded-retry counter
`EXECUTION-CONTRACT.md` requires** ("Retries use bounded exponential
backoff only for classified transient failures ... never for a step whose
side effect may have already partially occurred"). `NOT NULL DEFAULT 0` --
every existing row (and every row this task's own code never retries)
keeps the value a fresh dispatch already implies. `ck_workflow_run_steps_
status` (migration 0039) is dropped and recreated widened to include
`'retrying'` -- `DATA-MODEL.md`'s own step-status vocabulary did not
include this value; this task adds it as the mechanical record of "this
step raised a classified-transient error and is scheduled for another
attempt" (`UX-STATES.md`'s required "retrying" UI state is realized at the
*step* level this way, not by a new run-level status -- see `workflow_runs.
next_attempt_at` below).

**`workflow_runs.next_attempt_at` -- when a retry-pending run becomes
claimable again.** A step that raises a classified-transient error releases
the run's lease back to `'queued'` (exactly like `waiting_approval`/
`needs_review` already do via `_pause_run`) rather than blocking the worker
in-process for the backoff interval -- `next_attempt_at` is the timestamp
before which `claim_next_run`'s own predicate must not reclaim this run,
so a retry actually waits its exponential-backoff interval instead of being
immediately re-picked-up by the very next 2-second poll. `NULL` for every
run that has never hit a transient failure (the ordinary case) --
`claim_next_run`'s widened predicate (`ecc.domains.automation.worker`, this
task) treats `NULL` as "no retry gate, claimable under the ordinary rules."
There is **no** new `workflow_runs.status` value for "retrying" --
`DATA-MODEL.md`'s run-state vocabulary already covers the retry-pending
run adequately as an ordinary `'queued'` row (this task's own instruction:
"There is no `retrying` run-status and none should be added"), so no
`ck_workflow_runs_status` change is needed for retries.

**`automation_kill_switches` -- global or per-workflow, an append-only-ish
audit trail, not just current state.** `workflow_id` nullable (`NULL`
means global, matching `docs/runbooks/PHASE-5-RECOVERY.md`'s "A global or
per-workflow kill switch" language verbatim); `active` boolean; `reason`
free text; `activated_by`/`activated_at` always set (a switch is always
activated *by* someone, at some point); `deactivated_by`/`deactivated_at`
both `NULL` until deactivation. Deactivating an existing active row sets
`active=false`/`deactivated_at`/`deactivated_by` on that *same* row rather
than deleting it (so the table itself remains a real history of every
activation/deactivation, mirroring `automation_policies.revoked_at`'s own
"never delete, only mark" precedent) -- reactivating after a deactivation
inserts a **fresh** row rather than flipping the old one back to active, so
each activation window is its own row with its own `activated_by`/
`activated_at`, not a single row silently overwritten across multiple
activate/deactivate cycles.

**Two partial unique indexes enforce "at most one currently-active switch
per scope," mirroring `workflow_versions`'s own `uq_workflow_versions_
active_per_workflow` partial-unique-index mechanism (migration 0038)
exactly** for the identical "at most one row in this specific lifecycle
state per key" shape: `uq_automation_kill_switches_active_global` (`WHERE
workflow_id IS NULL AND active`, unique on `workspace_id` alone -- at most
one active *global* switch per workspace) and `uq_automation_kill_switches_
active_per_workflow` (`WHERE active`, unique on `(workspace_id,
workflow_id)` -- at most one active switch per specific workflow; a `NULL`
`workflow_id` row is excluded from this second index's own uniqueness
scope by construction, since Postgres never treats two `NULL`s as equal
for a plain, non-partial unique constraint, but this index is partial and
scoped by `WHERE active` regardless, so the two indexes are independent,
non-overlapping guarantees -- the global-scope one and the per-workflow-
scope one -- not a redundant pair).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# NOTE: the revision id below is *not* this file's full basename (unlike
# every prior Phase 5 migration's exact-basename-minus-`.py` convention) --
# `alembic_version.version_num` is a plain `VARCHAR(32)` (alembic's own
# default, unconfigured in this repository), and this file's fully
# descriptive basename (`0042_phase5_compensation_retry_kill_switch`, 43
# characters) does not fit. `0041_phase5_scheduler_and_pause` (32
# characters exactly) was already this convention's practical ceiling; this
# migration genuinely needs a wider label. Confirmed directly, not assumed:
# `alembic upgrade head` raised `psycopg.errors.StringDataRightTruncation`
# with the full name before this shortening. Resolved by abbreviating to
# `comp`/`kill` while keeping the file's own basename fully descriptive --
# a disclosed, mechanical divergence from the naming convention, not a
# silent one.
revision = "0042_phase5_comp_retry_kill"
down_revision = "0041_phase5_scheduler_and_pause"
branch_labels = None
depends_on = None

_STEP_STATUSES = ("pending", "dispatched", "succeeded", "failed", "unknown", "skipped", "retrying")
_COMPENSATION_STATUSES = ("pending", "dispatched", "succeeded", "failed", "unknown")


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    # --- workflow_run_steps: attempt_count + widened status CHECK --------

    op.add_column(
        "workflow_run_steps",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.drop_constraint("ck_workflow_run_steps_status", "workflow_run_steps", type_="check")
    op.create_check_constraint(
        "ck_workflow_run_steps_status",
        "workflow_run_steps",
        "status IN (" + ", ".join(f"'{s}'" for s in _STEP_STATUSES) + ")",
    )
    op.create_check_constraint(
        "ck_workflow_run_steps_attempt_count_nonneg",
        "workflow_run_steps",
        "attempt_count >= 0",
    )

    # --- workflow_runs: next_attempt_at (retry-gate, no new status) ------

    op.add_column(
        "workflow_runs",
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )

    # --- compensation_steps: the explicit compensation ledger -------------

    op.create_table(
        "compensation_steps",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("run_id", uuid, nullable=False),
        sa.Column("compensates_step_index", sa.Integer(), nullable=False),
        sa.Column("action_digest", sa.String(64), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_class", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in _COMPENSATION_STATUSES) + ")",
            name="ck_compensation_steps_status",
        ),
        sa.CheckConstraint(
            "compensates_step_index >= 0", name="ck_compensation_steps_index_nonneg"
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["workflow_runs.workspace_id", "workflow_runs.id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_compensation_steps_workspace_run", "compensation_steps", ["workspace_id", "run_id"]
    )

    # --- automation_kill_switches: global/per-workflow, audit-trail shape -

    op.create_table(
        "automation_kill_switches",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("workflow_id", sa.String(200), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("activated_by", uuid, nullable=False),
        sa.Column("activated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deactivated_by", uuid, nullable=True),
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "activated_by"],
            ["users.workspace_id", "users.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "deactivated_by"],
            ["users.workspace_id", "users.id"],
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_automation_kill_switches_workspace_workflow",
        "automation_kill_switches",
        ["workspace_id", "workflow_id"],
    )
    op.create_index(
        "uq_automation_kill_switches_active_global",
        "automation_kill_switches",
        ["workspace_id"],
        unique=True,
        postgresql_where=sa.text("workflow_id IS NULL AND active"),
    )
    op.create_index(
        "uq_automation_kill_switches_active_per_workflow",
        "automation_kill_switches",
        ["workspace_id", "workflow_id"],
        unique=True,
        postgresql_where=sa.text("active"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_automation_kill_switches_active_per_workflow", table_name="automation_kill_switches"
    )
    op.drop_index(
        "uq_automation_kill_switches_active_global", table_name="automation_kill_switches"
    )
    op.drop_index(
        "ix_automation_kill_switches_workspace_workflow", table_name="automation_kill_switches"
    )
    op.drop_table("automation_kill_switches")

    op.drop_index("ix_compensation_steps_workspace_run", table_name="compensation_steps")
    op.drop_table("compensation_steps")

    op.drop_column("workflow_runs", "next_attempt_at")

    op.drop_constraint(
        "ck_workflow_run_steps_attempt_count_nonneg", "workflow_run_steps", type_="check"
    )
    op.drop_constraint("ck_workflow_run_steps_status", "workflow_run_steps", type_="check")
    op.create_check_constraint(
        "ck_workflow_run_steps_status",
        "workflow_run_steps",
        "status IN ('pending', 'dispatched', 'succeeded', 'failed', 'unknown', 'skipped')",
    )
    op.drop_column("workflow_run_steps", "attempt_count")
