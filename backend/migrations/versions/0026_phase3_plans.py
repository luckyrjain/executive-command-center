"""Add plans and plan_blocks for Phase 3 deterministic planning.

Phase 3 Task 5 (propose, this migration) and Task 6 (accept/supersede/edit,
same table -- no new migration needed since a plan snapshot is immutable
once created: editing produces a brand-new plans row with the old one
marked 'superseded', mirroring waiting_links'/knowledge_claims' supersede
pattern, rather than mutating plan_blocks in place). plan_blocks therefore
carries no version column of its own -- the parent plan is the versioned
unit (DATA-MODEL.md: "plans | Versioned daily/weekly plan snapshot").
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0026_phase3_plans"
down_revision = "0025_phase3_capacity_planning"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.create_table(
        "plans",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("user_id", uuid, nullable=False),
        sa.Column("period_start", sa.Date(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="proposed"),
        sa.Column("policy_version", sa.SmallInteger(), nullable=False),
        sa.Column("capacity_minutes", sa.Integer(), nullable=False),
        sa.Column(
            "source_versions",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "conflicts", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column(
            "unscheduled",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("superseded_by", uuid, nullable=True),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", uuid, nullable=False),
        sa.Column("updated_by", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.CheckConstraint(
            "status IN ('draft','proposed','accepted','completed','superseded')",
            name="ck_plans_status",
        ),
        sa.CheckConstraint("period_start <= period_end", name="ck_plans_period_order"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "user_id"], ["users.workspace_id", "users.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_plans_workspace_id"),
    )
    op.create_index(
        "ix_plans_workspace_user_status_created",
        "plans",
        ["workspace_id", "user_id", "status", "created_at"],
    )

    op.create_table(
        "plan_blocks",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("plan_id", uuid, nullable=False),
        sa.Column("source_type", sa.String(32), nullable=False),
        sa.Column("source_id", uuid, nullable=True),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="proposed"),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("is_default_effort", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_type IN ('task','commitment','waiting_link','constraint','calendar_event')",
            name="ck_plan_blocks_source_type",
        ),
        sa.CheckConstraint("status IN ('proposed','accepted')", name="ck_plan_blocks_status"),
        sa.CheckConstraint("starts_at < ends_at", name="ck_plan_blocks_time_order"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "plan_id"], ["plans.workspace_id", "plans.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_plan_blocks_workspace_plan_starts",
        "plan_blocks",
        ["workspace_id", "plan_id", "starts_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_plan_blocks_workspace_plan_starts", table_name="plan_blocks")
    op.drop_table("plan_blocks")
    op.drop_index("ix_plans_workspace_user_status_created", table_name="plans")
    op.drop_table("plans")
