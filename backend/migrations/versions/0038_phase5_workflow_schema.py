"""Create workflow_definitions/workflow_versions, automation_policies and
triggers for Phase 5 Automation Task 1 (data layer only).

Phase 5 Task 1 (`docs/superpowers/specs/2026-07-25-phase-5-automation-design.md`,
`docs/phases/phase-005/DATA-MODEL.md` v0.2.0). Numbered ``0038`` -- the true
next-in-chain after ``0037_phase4_meeting_timeout2.py``, per this codebase's
own documented convention that migration file numbers match actual
implementation/chain order, not a plan's nominal numbers (migration
``0029``'s own docstring states this precedent explicitly).

**Workspace-scoped, unlike Phase 4's `prompt_versions`/`tool_definitions`.**
`prompt_versions`/`tool_definitions` (migration 0029) are deliberately
*not* workspace-scoped -- they are the platform's global, shared catalog.
`workflow_definitions`/`workflow_versions`/`automation_policies`/`triggers`
are the opposite case: user-authored automations that belong to one
executive's own workspace, exactly like every Phase 1-3 domain table
(`waiting_links`, migration 0023; `risk_reviews`, migration 0024). Every
table here carries `workspace_id`, FK'd to `workspaces.id` with
`ondelete="CASCADE"` (Phase 3's own convention), and every `created_by`/
`updated_by` actor reference is a composite FK to `users.(workspace_id,
id)` with `ondelete="RESTRICT"` -- identical shape to `waiting_links`/
`risk_reviews`.

**Two tables for the workflow family, not one (a deliberate divergence
from `prompt_versions`' single-table shape).** `prompt_versions` has no
separate "prompt family" header row -- the slug (`prompt_id`) is just a
repeated string column across version rows, with no other table needing to
reference the family itself. Here, `automation_policies.workflow_id` and
`triggers.workflow_id` both need a stable, reliably-FK-able target for "the
workflow family this policy/trigger belongs to" -- a policy or trigger can
exist (and, per `DATA-MODEL.md`, is expected to exist) independently of any
one specific `workflow_versions` row's lifecycle. A composite unique
constraint on `workflow_versions(workspace_id, workflow_id)` cannot serve
as that FK target because it is deliberately *not* unique on its own (many
version rows legitimately share one `workflow_id`) -- only
`(workspace_id, workflow_id, version)` is unique on that table. So
`workflow_definitions` exists as a thin identity table (`id`, `workspace_id`,
`workflow_id`, actor/timestamp columns only) with a genuine
`UNIQUE(workspace_id, workflow_id)` constraint, which
`automation_policies`/`triggers` FK against, and `workflow_versions` also
FK's against for its own `workflow_id` column. This is the literal reading
of `DATA-MODEL.md`'s own field list ("`workflow_id` (stable slug string,
unique per workspace)") -- unique per workspace only holds if exactly one
row identifies the family, which requires this second table.

**Immutability and versioning (design doc Decision 2), mirroring migration
0029's trigger pattern exactly in style.** `workflow_versions.definition_
hash` is `sha256` over the canonical (UTF-8, sorted-object-keys, compact-
separator) JSON bytes of `{graph, trigger_refs, policy_ref}` -- computed
identically here (nothing is seeded, so there is no migration-side hash
computation to keep in sync, unlike migration 0029) and at runtime by
`ecc.domains.automation.workflows.compute_definition_hash`. Once a row's
`status` leaves `draft` (`active` or `retired`), `graph`/`trigger_refs`/
`policy_ref`/`definition_hash` -- the entire hashed identity envelope, the
same "guard the whole envelope, not just the two headline columns" choice
migration 0029's own docstring explains -- become immutable, enforced by
`trg_workflow_versions_immutability` (`BEFORE UPDATE`), never by
application code alone. `status`/`updated_at` remain freely editable at any
status; `ecc.domains.automation.workflows.activate_workflow_version`/
`disable_workflow_version` only ever write those two columns. A partial
unique index (`uq_workflow_versions_active_per_workflow`, `WHERE status =
'active'`) enforces exactly one active version per `(workspace_id,
workflow_id)`, identical mechanism to `uq_prompt_versions_active_per_
prompt_id` (migration 0029) and `uq_meeting_packs_active_per_meeting`
(migration 0027) before it.

**`workflow_versions.policy_ref` carries no FK constraint.** It is declared
input material to `definition_hash` (design doc Decision 2's `{graph,
trigger_refs, policy_ref}` triple) recording which policy a version's
author intended to run under -- an opaque UUID reference, not a live
relationship the database enforces, matching how `graph`'s own
`action_ref`/`compensate_ref` step fields are opaque references into a
future action-adapter registry rather than FK'd rows (design doc Decision
8: adapters are registered in code, not stored as rows in this activation).
Enforcing this FK would also force `automation_policies` to be created
before any `workflow_versions` row that references it, an ordering
constraint the actual authoring flow (draft a workflow, draft a policy,
either order) does not otherwise need. `ecc.domains.automation.workflows`
validates a caller-supplied `policy_ref` resolves to a same-workspace
`automation_policies` row at the application layer instead.

**No seed data.** Unlike migration 0029's platform catalog, every row in
these four tables is user-authored automation content -- there is nothing
this migration itself should own the identity of. Task 1 ships the schema;
row creation is exclusively through `ecc.domains.automation`'s own write
paths (`POST /automations/workflows`, `POST /automations/policies`).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0038_phase5_workflow_schema"
down_revision = "0037_phase4_meeting_timeout2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    # --- workflow_definitions: the workflow family identity row ------------

    op.create_table(
        "workflow_definitions",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("workflow_id", sa.String(200), nullable=False),
        sa.Column("created_by", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "created_by"], ["users.workspace_id", "users.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "workspace_id", "workflow_id", name="uq_workflow_definitions_workspace_workflow_id"
        ),
    )

    # --- workflow_versions: the immutable, versioned graph ------------------

    op.create_table(
        "workflow_versions",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("workflow_id", sa.String(200), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("graph", postgresql.JSONB(), nullable=False),
        sa.Column(
            "trigger_refs",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("policy_ref", uuid, nullable=True),
        sa.Column("definition_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("created_by", uuid, nullable=False),
        sa.Column("updated_by", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'retired')", name="ck_workflow_versions_status"
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "workflow_id"],
            ["workflow_definitions.workspace_id", "workflow_definitions.workflow_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "created_by"], ["users.workspace_id", "users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "updated_by"], ["users.workspace_id", "users.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "workflow_id",
            "version",
            name="uq_workflow_versions_workspace_workflow_version",
        ),
    )
    op.create_index(
        "ix_workflow_versions_workspace_workflow",
        "workflow_versions",
        ["workspace_id", "workflow_id", "version"],
    )
    op.create_index(
        "uq_workflow_versions_active_per_workflow",
        "workflow_versions",
        ["workspace_id", "workflow_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    # --- automation_policies -------------------------------------------------

    op.create_table(
        "automation_policies",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("workflow_id", sa.String(200), nullable=False),
        sa.Column(
            "action_types",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column(
            "data_classes",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        # Non-nullable, no system-wide default (APPROVAL-POLICY.md's
        # resolved decision / design doc Decision 6): "a numeric default
        # with no concrete connector behind it would be an arbitrary
        # number, not a considered one" -- a policy author must set both
        # explicitly. Deliberately no `server_default` on either column.
        sa.Column("value_limit", sa.Numeric(14, 2), nullable=False),
        sa.Column("count_limit", sa.Integer(), nullable=False),
        # Unlike value_limit/count_limit, the runs-per-hour rate limit *does*
        # have a resolved system-wide default (APPROVAL-POLICY.md: "10
        # (policy default, overridable per policy)") -- so this column may
        # default, matching the doc's own distinction between the two kinds
        # of limit.
        sa.Column(
            "rate_limit",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{\"runs_per_workflow_per_hour\": 10}'::jsonb"),
        ),
        sa.Column("schedule", sa.Text(), nullable=True),
        sa.Column("approval_mode", sa.String(20), nullable=False),
        # No server_default: 90-days-from-creation is computed in
        # application code (`ecc.domains.automation.policy.create_policy`),
        # per this task's own instruction -- a DB-level default would be a
        # second, silently-divergent place this number could live.
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_by", uuid, nullable=False),
        sa.Column("updated_by", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "approval_mode IN ('preview_only', 'per_run', 'bounded_recurring')",
            name="ck_automation_policies_approval_mode",
        ),
        sa.CheckConstraint("value_limit >= 0", name="ck_automation_policies_value_limit_nonneg"),
        sa.CheckConstraint("count_limit >= 0", name="ck_automation_policies_count_limit_nonneg"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "workflow_id"],
            ["workflow_definitions.workspace_id", "workflow_definitions.workflow_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "created_by"], ["users.workspace_id", "users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "updated_by"], ["users.workspace_id", "users.id"], ondelete="RESTRICT"
        ),
    )
    op.create_index(
        "ix_automation_policies_workspace_workflow",
        "automation_policies",
        ["workspace_id", "workflow_id"],
    )
    op.create_index(
        "ix_automation_policies_workspace_expires",
        "automation_policies",
        ["workspace_id", "expires_at"],
    )

    # --- triggers --------------------------------------------------------------

    op.create_table(
        "triggers",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("workflow_id", sa.String(200), nullable=False),
        sa.Column("trigger_type", sa.String(20), nullable=False),
        sa.Column("event_type_filter", sa.String(200), nullable=True),
        sa.Column("schedule_expression", sa.String(200), nullable=True),
        sa.Column("timezone", sa.String(64), nullable=True),
        sa.Column("skip_missed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by", uuid, nullable=False),
        sa.Column("updated_by", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "trigger_type IN ('manual', 'event', 'schedule')", name="ck_triggers_trigger_type"
        ),
        # design doc Decision 7 / DATA-MODEL.md: "timezone is a required
        # IANA zone name for schedule triggers -- never inferred from
        # server-local time." Enforced here, not only at the application
        # layer, per this task's own instruction ("a CHECK constraint if
        # straightforward").
        sa.CheckConstraint(
            "trigger_type <> 'schedule' OR "
            "(schedule_expression IS NOT NULL AND timezone IS NOT NULL)",
            name="ck_triggers_schedule_requires_expression_and_timezone",
        ),
        sa.CheckConstraint(
            "trigger_type <> 'event' OR event_type_filter IS NOT NULL",
            name="ck_triggers_event_requires_type_filter",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "workflow_id"],
            ["workflow_definitions.workspace_id", "workflow_definitions.workflow_id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "created_by"], ["users.workspace_id", "users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "updated_by"], ["users.workspace_id", "users.id"], ondelete="RESTRICT"
        ),
    )
    op.create_index("ix_triggers_workspace_workflow", "triggers", ["workspace_id", "workflow_id"])

    # --- Immutability trigger (design doc Decision 2) -----------------------
    #
    # Mirrors migration 0029's enforce_prompt_version_immutability exactly in
    # style: rejects an UPDATE that changes any column of the hashed identity
    # envelope (graph/trigger_refs/policy_ref/definition_hash) once the row's
    # OLD status has already left 'draft'. status/updated_at are deliberately
    # excluded -- activate_workflow_version/disable_workflow_version only
    # ever write those two columns.
    op.execute(
        """
        CREATE FUNCTION enforce_workflow_version_immutability() RETURNS trigger AS $$
        BEGIN
            IF OLD.status <> 'draft' AND (
                NEW.graph IS DISTINCT FROM OLD.graph
                OR NEW.trigger_refs IS DISTINCT FROM OLD.trigger_refs
                OR NEW.policy_ref IS DISTINCT FROM OLD.policy_ref
                OR NEW.definition_hash IS DISTINCT FROM OLD.definition_hash
            ) THEN
                RAISE EXCEPTION
                    'workflow_versions row % (workspace_id=%, workflow_id=%, version=%) is '
                    'immutable once status <> draft (current status=%)',
                    OLD.id, OLD.workspace_id, OLD.workflow_id, OLD.version, OLD.status;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_workflow_versions_immutability
        BEFORE UPDATE ON workflow_versions
        FOR EACH ROW
        EXECUTE FUNCTION enforce_workflow_version_immutability();
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_workflow_versions_immutability ON workflow_versions")
    op.execute("DROP FUNCTION IF EXISTS enforce_workflow_version_immutability()")
    op.drop_index("ix_triggers_workspace_workflow", table_name="triggers")
    op.drop_table("triggers")
    op.drop_index("ix_automation_policies_workspace_expires", table_name="automation_policies")
    op.drop_index("ix_automation_policies_workspace_workflow", table_name="automation_policies")
    op.drop_table("automation_policies")
    op.drop_index("uq_workflow_versions_active_per_workflow", table_name="workflow_versions")
    op.drop_index("ix_workflow_versions_workspace_workflow", table_name="workflow_versions")
    op.drop_table("workflow_versions")
    op.drop_table("workflow_definitions")
