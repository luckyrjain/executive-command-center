"""Add Phase 2 entity_aliases and knowledge_claims tables."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011_phase2_knowledge_entities"
down_revision = "0010_phase2_pkos_reconciliation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.create_table(
        "entity_aliases",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, sa.ForeignKey("workspaces.id"), nullable=False, index=True),
        sa.Column("entity_id", uuid, nullable=False),
        sa.Column("alias_type", sa.String(50), nullable=False),
        sa.Column("normalized_value", sa.Text(), nullable=False),
        sa.Column("source_id", uuid, nullable=False),
        sa.Column("confidence", sa.Numeric(3, 2), nullable=False, server_default="1.00"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_entity_aliases_confidence"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "entity_id"],
            ["pkos_nodes.workspace_id", "pkos_nodes.id"],
            name="fk_entity_aliases_workspace_entity",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_id"],
            ["pkos_evidence.workspace_id", "pkos_evidence.id"],
            name="fk_entity_aliases_workspace_source",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "alias_type",
            "normalized_value",
            name="uq_entity_aliases_workspace_type_value",
        ),
    )
    op.create_index(
        "ix_entity_aliases_workspace_entity",
        "entity_aliases",
        ["workspace_id", "entity_id"],
    )

    op.create_table(
        "knowledge_claims",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, sa.ForeignKey("workspaces.id"), nullable=False, index=True),
        sa.Column("subject_id", uuid, nullable=False),
        sa.Column("predicate", sa.String(100), nullable=False),
        sa.Column("value_json", postgresql.JSONB(), nullable=False),
        sa.Column("source_id", uuid, nullable=False),
        sa.Column("confidence", sa.Numeric(3, 2), nullable=False, server_default="1.00"),
        sa.Column("valid_from", sa.DateTime(timezone=True)),
        sa.Column("valid_to", sa.DateTime(timezone=True)),
        sa.Column("superseded_by", uuid),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_knowledge_claims_confidence"
        ),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_from IS NULL OR valid_to > valid_from",
            name="ck_knowledge_claims_valid_interval",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "subject_id"],
            ["pkos_nodes.workspace_id", "pkos_nodes.id"],
            name="fk_knowledge_claims_workspace_subject",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_id"],
            ["pkos_evidence.workspace_id", "pkos_evidence.id"],
            name="fk_knowledge_claims_workspace_source",
        ),
    )
    op.create_index(
        "ix_knowledge_claims_workspace_subject",
        "knowledge_claims",
        ["workspace_id", "subject_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_claims_workspace_subject", table_name="knowledge_claims")
    op.drop_table("knowledge_claims")
    op.drop_index("ix_entity_aliases_workspace_entity", table_name="entity_aliases")
    op.drop_table("entity_aliases")
