"""Create approval_requests for Phase 5 Automation Task 3 (approval inbox
and policy-driven revocation enforcement).

Phase 5 Task 3 (`docs/superpowers/specs/2026-07-25-phase-5-automation-design.md`
Decision 5/6/Threat model, `docs/phases/phase-005/DATA-MODEL.md` v0.2.0,
`docs/phases/phase-005/APPROVAL-POLICY.md`). Numbered ``0040`` -- the true
next-in-chain after ``0039_phase5_workflow_runs.py``, per this codebase's
documented migration-numbering convention (migration ``0029``'s docstring
precedent, restated by ``0038``/``0039``'s own docstrings).

**`status` is deliberately three-valued (`pending|approved|rejected`), not
four.** `DATA-MODEL.md`'s raw field list has no `status` column at all
(only `decided_at`/`decision`) -- this migration adds one anyway (this
task's own instruction: "a `status` column is cleaner to index/query on"),
but does **not** add a stored `expired` value, even though `expires_at`
implies a lifecycle a caller will want to query by. `automation_policies`
(migration 0038) is the precedent this follows exactly: that table also has
no stored lifecycle-status column, only raw `expires_at`/`revoked_at`
fields, with `ecc.domains.automation.policy.policy_status` computing
`active|expired|revoked` live from them. `ecc.domains.automation.approvals.
approval_lifecycle_status` does the identical thing here -- a `pending` row
whose `expires_at` has passed is reported as `expired` by that function
without ever being written back to the database. This sidesteps a real
transactional problem a stored `expired` value would create: the only
place that would ever observe "this pending row's expiry window has
elapsed" is `ecc.domains.automation.approvals.decide_approval`, called from
inside the `POST /approvals/{id}/approve|reject` router's own `with
session.begin():` block (`workflows.py`/`policy.py`'s established
per-request-transaction pattern) -- and that block *rolls back* on the
`HTTPException` a rejected decision raises, which would silently discard
any "lazily flip status to `expired`" write attempted in the same request,
requiring a second, separately-committed bare-session round trip to do
correctly (`worker.py`'s own commit-placement discipline, restated in this
task's `approvals.py`). Treating expiry as computed, exactly like policy
expiry already is, avoids that complexity entirely rather than reinventing
it. `decided_at`/`decision`/`decided_by` stay `NULL` for a since-expired
row -- no human ever decided it, matching `DATA-MODEL.md` verbatim ("never
implicitly approved").

**`(workspace_id, run_id)` composite FK to `workflow_runs(workspace_id,
id)`, `ondelete="CASCADE"`** -- identical shape and reasoning to
`workflow_run_steps`'s own FK (migration 0039): the target's `UNIQUE
(workspace_id, id)` constraint (`uq_workflow_runs_workspace_id`,
migration 0039) is what makes a composite FK on the *pair* possible at
all, and is what makes a cross-workspace `approval_requests` row
referencing another workspace's `run_id` impossible at the database layer,
not only by convention. No FK against `workflow_run_steps` itself (by
`run_id`+`step_index`): an approval request is deliberately created
*before* any `workflow_run_steps` row exists for the step it gates
(`worker.py`'s own module docstring, this task's addition -- the digest is
computed and the approval gate evaluated *before* the `'dispatched'` row
is ever inserted, precisely so a step paused for approval never leaves an
ambiguous `'dispatched'`-with-no-outcome row a later crash-recovery pass
could misclassify as `unknown`); a `workflow_run_steps` row for the same
`(run_id, step_index)` may not exist yet at the moment this table's own
row is inserted.

**`(workspace_id, decided_by)` composite FK to `users(workspace_id, id)`,
`ondelete="RESTRICT"`, despite `decided_by` being nullable.** Postgres's
default `MATCH SIMPLE` composite-FK semantics skip enforcement entirely
when *any* referencing column is `NULL` -- a `pending` row (`decided_by
IS NULL`) is therefore never checked against this constraint at all, while
a decided row's `decided_by` is enforced exactly like every other actor
reference in this codebase (`created_by`/`updated_by` throughout Phase
1-5). This is standard Postgres behavior, not a special case requiring
extra migration machinery.

**`high_impact_categories` is `ARRAY(TEXT)`, matching `automation_policies.
action_types`/`data_classes`'s own array-not-JSONB convention** (migration
0038) -- a small, fixed-shape list of strings from
`docs.superpowers.specs...design.md` Decision 5's seven-category closed
enumeration, not a document that benefits from JSONB's schema flexibility.

**A partial unique index, `uq_approval_requests_pending_per_run_step`
(`WHERE status = 'pending'`), enforces at most one open request per
`(run_id, step_index)`** -- mirrors `workflow_versions`'s own
`uq_workflow_versions_active_per_workflow` partial-unique-index mechanism
(migration 0038) for the identical shape of invariant ("at most one row in
this specific lifecycle state per key"), at the database layer, not only
by `ecc.domains.automation.approvals.create_approval_request`'s own
check-then-insert discipline under a `SELECT ... FOR UPDATE` lock (belt
and suspenders against a hypothetical future caller that skips that
lock).

**Two `CHECK` constraints enforce decision/status consistency directly**,
mirroring `triggers`'s own two `CHECK` constraints (migration 0038) for
the identical "a database-level backstop on a cross-column invariant, not
only an application-layer one" reasoning: a `pending` row must have
`decided_at`/`decision`/`decided_by` all `NULL` (nothing has been decided
yet); an `approved`/`rejected` row must have all three set, with
`decision` matching `status` exactly (there is no row where `status` and
`decision` could ever silently disagree).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0040_phase5_approval_requests"
down_revision = "0039_phase5_workflow_runs"
branch_labels = None
depends_on = None

_STATUSES = ("pending", "approved", "rejected")
_DECISIONS = ("approved", "rejected")


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.create_table(
        "approval_requests",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("run_id", uuid, nullable=False),
        sa.Column("step_index", sa.Integer(), nullable=False),
        sa.Column("action_digest", sa.String(64), nullable=False),
        sa.Column(
            "high_impact_categories",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        # No server_default: 24-hours-from-requested_at is computed in
        # application code (`ecc.domains.automation.approvals.
        # create_approval_request`), per this task's own instruction --
        # matches `automation_policies.expires_at`'s identical precedent
        # (migration 0038) exactly, for the identical reason (a DB-level
        # default would be a second, silently-divergent place this number
        # could live).
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision", sa.String(20), nullable=True),
        sa.Column("decided_by", uuid, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in _STATUSES) + ")",
            name="ck_approval_requests_status",
        ),
        sa.CheckConstraint(
            "decision IS NULL OR decision IN (" + ", ".join(f"'{d}'" for d in _DECISIONS) + ")",
            name="ck_approval_requests_decision",
        ),
        sa.CheckConstraint("step_index >= 0", name="ck_approval_requests_step_index_nonneg"),
        sa.CheckConstraint(
            "status <> 'pending' OR "
            "(decided_at IS NULL AND decision IS NULL AND decided_by IS NULL)",
            name="ck_approval_requests_pending_undecided",
        ),
        sa.CheckConstraint(
            "status NOT IN ('approved', 'rejected') OR "
            "(decided_at IS NOT NULL AND decision = status AND decided_by IS NOT NULL)",
            name="ck_approval_requests_decided_matches_decision",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["workflow_runs.workspace_id", "workflow_runs.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "decided_by"],
            ["users.workspace_id", "users.id"],
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_approval_requests_workspace_run", "approval_requests", ["workspace_id", "run_id"]
    )
    op.create_index(
        "ix_approval_requests_workspace_status", "approval_requests", ["workspace_id", "status"]
    )
    op.create_index(
        "uq_approval_requests_pending_per_run_step",
        "approval_requests",
        ["run_id", "step_index"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index("uq_approval_requests_pending_per_run_step", table_name="approval_requests")
    op.drop_index("ix_approval_requests_workspace_status", table_name="approval_requests")
    op.drop_index("ix_approval_requests_workspace_run", table_name="approval_requests")
    op.drop_table("approval_requests")
