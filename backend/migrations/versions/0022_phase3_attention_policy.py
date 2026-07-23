"""Extend attention_items for Phase 3's versioned policy; add attention_feedback.

Reconciles phase-003/DATA-MODEL.md's proposed attention_items/attention_overrides
schema with the already-shipped Phase 1 attention_items table (migration
0006) rather than forking new tables -- per the repository owner's Task 0
decision (docs/superpowers/specs/2026-07-22-phase-3-human-attention-engine-design.md,
Open decision 1). policy_version defaults to 1 so every pre-Phase-3 row is
implicitly policy v1 with no backfill ambiguity.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0022_phase3_attention_policy"
down_revision = "0021_phase2_drop_observed_at"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.add_column(
        "attention_items",
        sa.Column("policy_version", sa.SmallInteger(), nullable=False, server_default="1"),
    )
    op.add_column("attention_items", sa.Column("override_reason", sa.Text(), nullable=True))

    op.create_table(
        "attention_feedback",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("target_type", sa.String(32), nullable=False),
        sa.Column("target_id", uuid, nullable=False),
        sa.Column("label", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("actor_id", uuid, nullable=False),
        sa.Column("policy_version", sa.SmallInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "label IN ('useful','not_useful','incorrect')", name="ck_attention_feedback_label"
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "actor_id"], ["users.workspace_id", "users.id"], ondelete="RESTRICT"
        ),
    )
    op.create_index(
        "ix_attention_feedback_workspace_target",
        "attention_feedback",
        ["workspace_id", "target_type", "target_id"],
    )
    op.create_index(
        "ix_attention_feedback_workspace_created",
        "attention_feedback",
        ["workspace_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_attention_feedback_workspace_created", table_name="attention_feedback")
    op.drop_index("ix_attention_feedback_workspace_target", table_name="attention_feedback")
    op.drop_table("attention_feedback")
    op.drop_column("attention_items", "override_reason")
    op.drop_column("attention_items", "policy_version")
