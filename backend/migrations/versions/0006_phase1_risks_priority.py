"""Add the Phase 1 risks and deterministic attention-item projection."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0006_phase1_risks_priority"
down_revision = "0005_phase1_calendar_meetings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.create_table(
        "risks",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("probability", sa.SmallInteger(), nullable=False),
        sa.Column("impact", sa.SmallInteger(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="identified"),
        sa.Column("owner_id", uuid, nullable=False),
        sa.Column("mitigation", sa.Text()),
        sa.Column("trigger", sa.Text()),
        sa.Column("review_at", sa.DateTime(timezone=True)),
        sa.Column("project_id", uuid),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by", uuid, nullable=False),
        sa.Column("updated_by", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column("pre_archive_status", sa.String(32)),
        sa.CheckConstraint(
            "char_length(description) BETWEEN 1 AND 5000", name="ck_risks_description"
        ),
        sa.CheckConstraint("probability BETWEEN 1 AND 5", name="ck_risks_probability"),
        sa.CheckConstraint("impact BETWEEN 1 AND 5", name="ck_risks_impact"),
        sa.CheckConstraint(
            "status IN ('identified','assessed','monitoring','mitigating','materialized','closed')",
            name="ck_risks_status",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "owner_id"], ["users.workspace_id", "users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "created_by"], ["users.workspace_id", "users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "updated_by"], ["users.workspace_id", "users.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_risks_workspace_id"),
    )
    op.create_index("ix_risks_workspace_status", "risks", ["workspace_id", "status"])
    op.create_index("ix_risks_workspace_review", "risks", ["workspace_id", "review_at"])
    op.create_index("ix_risks_workspace_owner", "risks", ["workspace_id", "owner_id"])
    op.create_index("ix_risks_workspace_pinned", "risks", ["workspace_id", "pinned"])

    op.create_table(
        "attention_items",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("entity_type", sa.String(32), nullable=False),
        sa.Column("entity_id", uuid, nullable=False),
        sa.Column("source_entity_version", sa.BigInteger(), nullable=False),
        sa.Column("score", sa.SmallInteger(), nullable=False),
        sa.Column("confidence", sa.Numeric(4, 3), nullable=False),
        sa.Column(
            "factors", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("explanation", sa.Text(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("dismissed_at", sa.DateTime(timezone=True)),
        sa.Column("dismissed_entity_version", sa.BigInteger()),
        sa.Column("deferred_until", sa.DateTime(timezone=True)),
        sa.CheckConstraint("score BETWEEN 0 AND 100", name="ck_attention_items_score"),
        sa.CheckConstraint("confidence BETWEEN 0 AND 1", name="ck_attention_items_confidence"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "workspace_id", "entity_type", "entity_id", name="uq_attention_items_entity"
        ),
    )
    op.create_index(
        "ix_attention_items_workspace_rank",
        "attention_items",
        ["workspace_id", sa.text("score DESC"), sa.text("generated_at DESC")],
    )
    op.create_index(
        "ix_attention_items_workspace_expiry",
        "attention_items",
        ["workspace_id", "expires_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_attention_items_workspace_expiry", table_name="attention_items")
    op.drop_index("ix_attention_items_workspace_rank", table_name="attention_items")
    op.drop_table("attention_items")
    op.drop_index("ix_risks_workspace_pinned", table_name="risks")
    op.drop_index("ix_risks_workspace_owner", table_name="risks")
    op.drop_index("ix_risks_workspace_review", table_name="risks")
    op.drop_index("ix_risks_workspace_status", table_name="risks")
    op.drop_table("risks")
