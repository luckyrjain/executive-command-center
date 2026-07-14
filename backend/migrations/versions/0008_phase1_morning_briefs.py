"""Add persisted Phase 1 morning briefs."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_phase1_morning_briefs"
down_revision = "0007_phase1_search_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.create_table(
        "morning_briefs",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("user_id", uuid, nullable=False),
        sa.Column("briefing_date", sa.Date(), nullable=False),
        sa.Column("generation_version", sa.Integer(), nullable=False),
        sa.Column("sections", postgresql.JSONB(), nullable=False),
        sa.Column(
            "source_versions",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "evidence_ids",
            postgresql.ARRAY(uuid),
            nullable=False,
            server_default="{}",
        ),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("algorithm_version", sa.String(64), nullable=False),
        sa.Column("ai_status", sa.String(32), nullable=False, server_default="disabled"),
        sa.Column("stale_reason", sa.String(100)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["users.workspace_id", "users.id"],
            name="fk_morning_briefs_workspace_user",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "user_id",
            "briefing_date",
            "generation_version",
            name="uq_morning_briefs_workspace_user_date_version",
        ),
        sa.CheckConstraint(
            "generation_version >= 1",
            name="ck_morning_briefs_generation_version",
        ),
        sa.CheckConstraint(
            "ai_status IN ('disabled', 'available', 'unavailable')",
            name="ck_morning_briefs_ai_status",
        ),
    )
    op.create_index(
        "ix_morning_briefs_current",
        "morning_briefs",
        [
            "workspace_id",
            "user_id",
            "briefing_date",
            sa.text("generation_version DESC"),
        ],
    )
    op.create_index(
        "ix_morning_briefs_generated_at",
        "morning_briefs",
        ["workspace_id", "generated_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_morning_briefs_generated_at", table_name="morning_briefs")
    op.drop_index("ix_morning_briefs_current", table_name="morning_briefs")
    op.drop_table("morning_briefs")
