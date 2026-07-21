"""Add Phase 2 timeline_entries table."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012_phase2_timeline"
down_revision = "0011_phase2_knowledge_entities"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.create_table(
        "timeline_entries",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, sa.ForeignKey("workspaces.id"), nullable=False, index=True),
        sa.Column("entity_id", uuid, nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event_type", sa.String(100), nullable=False),
        sa.Column("source_id", uuid),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "entity_id"],
            ["pkos_nodes.workspace_id", "pkos_nodes.id"],
            name="fk_timeline_entries_workspace_entity",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_id"],
            ["pkos_evidence.workspace_id", "pkos_evidence.id"],
            name="fk_timeline_entries_workspace_source",
        ),
    )
    op.create_index(
        "ix_timeline_entries_workspace_entity_order",
        "timeline_entries",
        ["workspace_id", "entity_id", sa.text("effective_at DESC"), sa.text("recorded_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_timeline_entries_workspace_entity_order", table_name="timeline_entries")
    op.drop_table("timeline_entries")
