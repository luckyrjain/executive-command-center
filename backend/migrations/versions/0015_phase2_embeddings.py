"""Add pgvector extension and Phase 2 embedding_projections table."""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "0015_phase2_embeddings"
down_revision = "0014_phase2_retrieval"
branch_labels = None
depends_on = None

# sentence-transformers/all-MiniLM-L6-v2's output dimensionality -- see
# backend/ecc/domains/knowledge/embeddings.py's EMBEDDING_DIMENSIONS.
_DIMENSIONS = 384


def upgrade() -> None:
    # Must run before op.create_table below: the vector column type and the
    # HNSW access method it's indexed with both come from this extension.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    uuid = postgresql.UUID(as_uuid=True)

    # retrieval_documents (migration 0014) was never referenced by another
    # table's composite FK before now, so it never needed this -- pkos_nodes
    # (migration 0001) already carries the equivalent
    # uq_pkos_nodes_workspace_id_id for the same reason: a composite FK
    # target's referenced columns must be covered by a unique constraint.
    op.create_unique_constraint(
        "uq_retrieval_documents_workspace_id_id", "retrieval_documents", ["workspace_id", "id"]
    )

    op.create_table(
        "embedding_projections",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, sa.ForeignKey("workspaces.id"), nullable=False, index=True),
        sa.Column("document_id", uuid, nullable=False),
        sa.Column("model_id", sa.String(200), nullable=False),
        sa.Column("model_version", sa.String(50), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("embedding", Vector(_DIMENSIONS), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "document_id"],
            ["retrieval_documents.workspace_id", "retrieval_documents.id"],
            name="fk_embedding_projections_workspace_document",
            ondelete="CASCADE",
        ),
        # One embedding row per (document, model): queue_embedding() upserts
        # on this constraint. A future model migration adds new rows under a
        # new model_id rather than overwriting the old ones in place, so
        # retrieval can keep serving the old model's vectors until a rebuild
        # completes the new one -- DATA-MODEL.md's "derived projections are
        # rebuildable, never a second source of truth" framing.
        sa.UniqueConstraint(
            "workspace_id",
            "document_id",
            "model_id",
            name="uq_embedding_projections_workspace_document_model",
        ),
    )
    op.execute(
        "CREATE INDEX ix_embedding_projections_embedding_hnsw "
        "ON embedding_projections USING hnsw (embedding vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_embedding_projections_embedding_hnsw")
    op.drop_table("embedding_projections")
    op.drop_constraint(
        "uq_retrieval_documents_workspace_id_id", "retrieval_documents", type_="unique"
    )
    # Deliberately does not DROP EXTENSION vector: another table created
    # after this migration ran could depend on it, and dropping a shared
    # extension as part of one table's downgrade is exactly the kind of
    # wide-blast-radius mistake PHASE-1-DEPLOYMENT.md's rollback section
    # warns against for migration downgrades in general.
