"""Add Phase 1 recommendations and feedback."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_phase1_recommendations"
down_revision = "0008_phase1_morning_briefs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.create_table(
        "recommendations",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("recommendation_type", sa.String(100), nullable=False),
        sa.Column("target_type", sa.String(100), nullable=False),
        sa.Column("target_id", uuid),
        sa.Column("proposed_action", postgresql.JSONB(), nullable=False),
        sa.Column("expected_version", sa.BigInteger()),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Numeric(5, 4), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="proposed"),
        sa.Column("evidence_ids", postgresql.ARRAY(uuid), nullable=False, server_default="{}"),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("confirmed_by", uuid),
        sa.Column("confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("execution_result", postgresql.JSONB()),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("deferred_until", sa.DateTime(timezone=True)),
        sa.Column("created_by", uuid, nullable=False),
        sa.Column("updated_by", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column("pre_archive_status", sa.String(32)),
        sa.UniqueConstraint("workspace_id", "id", name="uq_recommendations_workspace_id"),
        sa.CheckConstraint(
            "status IN ('proposed','pending_confirmation','accepted','rejected',"
            "'expired','superseded','executed','failed')",
            name="ck_recommendations_status",
        ),
        sa.CheckConstraint(
            "source IN ('rule','ai')",
            name="ck_recommendations_source",
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_recommendations_confidence",
        ),
        sa.CheckConstraint("version >= 1", name="ck_recommendations_version"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "created_by"],
            ["users.workspace_id", "users.id"],
            name="fk_recommendations_workspace_created_by",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "updated_by"],
            ["users.workspace_id", "users.id"],
            name="fk_recommendations_workspace_updated_by",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "confirmed_by"],
            ["users.workspace_id", "users.id"],
            name="fk_recommendations_workspace_confirmed_by",
        ),
    )
    op.create_index(
        "ix_recommendations_workspace_status",
        "recommendations",
        ["workspace_id", "status", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_recommendations_workspace_target",
        "recommendations",
        ["workspace_id", "target_type", "target_id"],
    )
    op.create_index(
        "ix_recommendations_workspace_expiry",
        "recommendations",
        ["workspace_id", "expires_at"],
    )

    op.create_table(
        "recommendation_feedback",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("recommendation_id", uuid, nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text()),
        sa.Column("defer_until", sa.DateTime(timezone=True)),
        sa.Column("actor_id", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "action IN ('dismiss','defer','pin','accept','reject')",
            name="ck_recommendation_feedback_action",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "recommendation_id"],
            ["recommendations.workspace_id", "recommendations.id"],
            name="fk_recommendation_feedback_recommendation",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "actor_id"],
            ["users.workspace_id", "users.id"],
            name="fk_recommendation_feedback_actor",
        ),
    )
    op.create_index(
        "ix_recommendation_feedback_recommendation",
        "recommendation_feedback",
        ["workspace_id", "recommendation_id", sa.text("created_at DESC")],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_recommendation_feedback_recommendation",
        table_name="recommendation_feedback",
    )
    op.drop_table("recommendation_feedback")
    op.drop_index("ix_recommendations_workspace_expiry", table_name="recommendations")
    op.drop_index("ix_recommendations_workspace_target", table_name="recommendations")
    op.drop_index("ix_recommendations_workspace_status", table_name="recommendations")
    op.drop_table("recommendations")
