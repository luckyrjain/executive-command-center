"""Create workflow_runs/workflow_run_steps for Phase 5 Automation Task 2
(the durable local worker and crash recovery).

Phase 5 Task 2 (`docs/superpowers/specs/2026-07-25-phase-5-automation-design.md`
Decision 3, `docs/phases/phase-005/DATA-MODEL.md` v0.2.0,
`docs/phases/phase-005/EXECUTION-CONTRACT.md`,
`docs/runbooks/PHASE-5-RECOVERY.md`). Numbered ``0039`` -- the true
next-in-chain after ``0038_phase5_workflow_schema.py``, per this codebase's
documented migration-numbering convention (migration ``0029``'s docstring
precedent, restated by ``0038``'s own docstring).

**Lease/claim mechanics live entirely in `ecc.domains.automation.worker`,
not here.** This migration only shapes the two tables the worker's
compare-and-swap `UPDATE ... WHERE status = 'queued' OR (status IN
('leased', 'running') AND leased_until < now()) RETURNING *` (design doc
Decision 3, generalized -- see that module's own docstring for why the
`WHERE` clause's expired-lease branch checks both `leased` and `running`
rather than `leased` alone as Decision 3's SQL literally shows) and the
idempotency-gated step dispatch operate against.

**`workflow_runs` pins the exact `(workflow_id, workflow_version)`
it started with**, enforced by a composite FK to
`workflow_versions(workspace_id, workflow_id, version)` (the unique
constraint `uq_workflow_versions_workspace_workflow_version`, migration
0038) -- not merely documented behavior; a `workflow_runs` row referencing
a `workflow_version` that does not exist for this workspace is rejected by
the database, matching Decision 2's "every run pins the exact version it
started with" verbatim.

**`policy_id`/`trigger_ref` carry no FK constraint**, for the identical
reason migration 0038 gives for `workflow_versions.policy_ref`: an opaque
reference recording which policy/trigger this run's author intended, not a
live relationship the database enforces -- avoids an artificial
table-creation-order constraint (a run can be enqueued manually with no
trigger at all) and matches this codebase's existing "opaque UUID
reference into a registry/config table" precedent for `action_ref`/
`compensate_ref` inside `workflow_versions.graph` itself.

**`uq_workflow_runs_workspace_id`** (a `UNIQUE(workspace_id, id)`
constraint, redundant with the `id` primary key on its own but required by
Postgres as the referenced-column shape for `workflow_run_steps`'s own
composite `(workspace_id, run_id)` FK below) is the mechanical backstop
that makes a `workflow_run_steps` row belonging to a different workspace
than its own `run_id` impossible at the database layer, not only by
convention -- this codebase's usual paranoia about cross-workspace leaks
(`docs/superpowers/specs/2026-07-25-phase-5-automation-design.md`'s Threat
model; every Phase 1-4 table's workspace-scoping precedent).

**`workflow_run_steps` is keyed `UNIQUE(run_id, step_index)`** -- one row
per step per run, not one row per attempt. `action_digest` is written
(together with `status = 'dispatched'`) in the same `INSERT` that creates
this row, and that `INSERT` is flushed to the database **before** the
worker calls the resolved adapter's `execute()` (Decision 3's "the worker
persists state before ... each side effect", made concrete). A step
already `succeeded` under this row's digest is never re-dispatched; a step
still `dispatched` (digest persisted, no outcome ever recorded) when the
worker next examines it is the exactly-once-ambiguous crash case and
surfaces as `unknown`, moving the run to `needs_review` -- never a blind
retry (`EXECUTION-CONTRACT.md` verbatim).

**Redaction is a real constraint on `input`/`output`, not just a
docstring.** `ecc.domains.automation.worker._redact_payload` strips any
value whose key name matches a secret-shaped marker (`secret`, `password`,
`token`, `credential`, `api_key`, `authorization`) before either column is
ever written -- this migration does not (and cannot) enforce that at the
database layer, matching how `workflow_versions.graph`'s own opaque
`action_ref`/`compensate_ref` references are an application-layer
contract, not a database one; `secret_references` (`DATA-MODEL.md`, not
part of this task's scope) is the eventual authoritative mechanism for
never persisting a secret value at all.

**`status`'s `CHECK` constraint allows the full run-state vocabulary
`DATA-MODEL.md` already specifies** (`waiting_approval`, `paused`,
`compensating`, `compensated`, `compensation_failed`, `rate_limited`
included), even though this task's own worker only legitimately produces
`queued|leased|running|succeeded|failed|needs_review|cancelled` --
widening the `CHECK` constraint in a later migration, once a later task's
approval-gate/compensation/rate-limit logic needs one of the remaining
states, is exactly the repeated-migration churn this task's own
instructions say to avoid.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0039_phase5_workflow_runs"
down_revision = "0038_phase5_workflow_schema"
branch_labels = None
depends_on = None

_RUN_STATUSES = (
    "queued",
    "leased",
    "running",
    "waiting_approval",
    "paused",
    "needs_review",
    "succeeded",
    "failed",
    "cancelled",
    "compensating",
    "compensated",
    "compensation_failed",
    "expired",
    "rate_limited",
)
_STEP_STATUSES = ("pending", "dispatched", "succeeded", "failed", "unknown", "skipped")
_STEP_TYPES = ("action", "approval_gate", "condition", "compensation")


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    # --- workflow_runs: durable run state, lease and correlation -----------

    op.create_table(
        "workflow_runs",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("workflow_id", sa.String(200), nullable=False),
        sa.Column("workflow_version", sa.Integer(), nullable=False),
        sa.Column("policy_id", uuid, nullable=True),
        sa.Column("trigger_ref", sa.String(300), nullable=True),
        sa.Column("status", sa.String(30), nullable=False, server_default="queued"),
        sa.Column("current_step_index", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("leased_by", sa.String(200), nullable=True),
        sa.Column("leased_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("queued_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in _RUN_STATUSES) + ")",
            name="ck_workflow_runs_status",
        ),
        sa.CheckConstraint("current_step_index >= 0", name="ck_workflow_runs_step_index_nonneg"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "workflow_id", "workflow_version"],
            [
                "workflow_versions.workspace_id",
                "workflow_versions.workflow_id",
                "workflow_versions.version",
            ],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "created_by"], ["users.workspace_id", "users.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_workflow_runs_workspace_id"),
    )
    op.create_index(
        "ix_workflow_runs_workspace_status", "workflow_runs", ["workspace_id", "status"]
    )
    op.create_index("ix_workflow_runs_claim_candidates", "workflow_runs", ["status", "queued_at"])

    # --- workflow_run_steps: attempt, idempotency key, redacted result -----

    op.create_table(
        "workflow_run_steps",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("run_id", uuid, nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("step_type", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("action_digest", sa.String(64), nullable=True),
        sa.Column(
            "input", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("output", postgresql.JSONB(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_class", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "step_type IN (" + ", ".join(f"'{s}'" for s in _STEP_TYPES) + ")",
            name="ck_workflow_run_steps_step_type",
        ),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in _STEP_STATUSES) + ")",
            name="ck_workflow_run_steps_status",
        ),
        sa.CheckConstraint("step_index >= 0", name="ck_workflow_run_steps_step_index_nonneg"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["workflow_runs.workspace_id", "workflow_runs.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("run_id", "step_index", name="uq_workflow_run_steps_run_step_index"),
    )
    op.create_index(
        "ix_workflow_run_steps_workspace_run", "workflow_run_steps", ["workspace_id", "run_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_workflow_run_steps_workspace_run", table_name="workflow_run_steps")
    op.drop_table("workflow_run_steps")
    op.drop_index("ix_workflow_runs_claim_candidates", table_name="workflow_runs")
    op.drop_index("ix_workflow_runs_workspace_status", table_name="workflow_runs")
    op.drop_table("workflow_runs")
