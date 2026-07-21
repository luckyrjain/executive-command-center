"""Add Phase 2 retrieval_documents table."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014_phase2_retrieval"
down_revision = "0013_phase2_resolution"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.create_table(
        "retrieval_documents",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, sa.ForeignKey("workspaces.id"), nullable=False, index=True),
        sa.Column("entity_type", sa.String(50), nullable=False),
        sa.Column("entity_id", uuid, nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "search_document",
            postgresql.TSVECTOR(),
            sa.Computed(
                "setweight(to_tsvector('simple', coalesce(title, '')), 'A') || "
                "setweight(to_tsvector('simple', coalesce(body, '')), 'B')",
                persisted=True,
            ),
        ),
        sa.Column("source_version", sa.Integer(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "entity_id"],
            ["pkos_nodes.workspace_id", "pkos_nodes.id"],
            name="fk_retrieval_documents_workspace_entity",
            ondelete="CASCADE",
        ),
        # One projection row per entity: queue_retrieval_document() upserts
        # on this constraint, matching DATA-MODEL.md's "Normalized
        # searchable projection" (rebuildable, not a second source of
        # truth) framing.
        sa.UniqueConstraint(
            "workspace_id", "entity_id", name="uq_retrieval_documents_workspace_entity"
        ),
    )
    op.create_index(
        "ix_retrieval_documents_search_document",
        "retrieval_documents",
        ["search_document"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_retrieval_documents_workspace_updated_at",
        "retrieval_documents",
        ["workspace_id", sa.text("updated_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_retrieval_documents_workspace_updated_at", table_name="retrieval_documents")
    op.drop_index("ix_retrieval_documents_search_document", table_name="retrieval_documents")
    op.drop_table("retrieval_documents")
