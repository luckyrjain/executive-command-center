"""Reconcile pkos_nodes/pkos_edges/pkos_evidence with PKOS-SCHEMA.md's
Phase 1-deferred columns, per docs/superpowers/specs/2026-07-21-phase-2-
knowledge-platform-design.md's Open decision 1 (extend PKOS rather than
fork independent knowledge_entities/relationships/source_refs tables).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_phase2_pkos_reconciliation"
down_revision = "0009_phase1_recommendations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    # pkos_nodes -> knowledge_entities
    op.add_column("pkos_nodes", sa.Column("entity_id", uuid))
    op.add_column(
        "pkos_nodes",
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
    )
    op.add_column(
        "pkos_nodes",
        sa.Column("confidence", sa.Numeric(3, 2), nullable=False, server_default="1.00"),
    )
    op.add_column(
        "pkos_nodes",
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
    )
    op.create_check_constraint(
        "ck_pkos_nodes_status",
        "pkos_nodes",
        "status IN ('active','archived','redirected')",
    )
    op.create_check_constraint(
        "ck_pkos_nodes_confidence",
        "pkos_nodes",
        "confidence >= 0 AND confidence <= 1",
    )
    op.create_check_constraint("ck_pkos_nodes_version", "pkos_nodes", "version >= 1")

    # pkos_edges -> relationships
    op.add_column(
        "pkos_edges",
        sa.Column("confidence", sa.Numeric(3, 2), nullable=False, server_default="1.00"),
    )
    op.add_column("pkos_edges", sa.Column("evidence_id", uuid))
    op.add_column("pkos_edges", sa.Column("valid_from", sa.DateTime(timezone=True)))
    op.add_column("pkos_edges", sa.Column("valid_to", sa.DateTime(timezone=True)))
    op.add_column(
        "pkos_edges",
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
    )
    op.create_check_constraint(
        "ck_pkos_edges_status",
        "pkos_edges",
        "status IN ('active','disputed','invalidated')",
    )
    op.create_check_constraint(
        "ck_pkos_edges_confidence",
        "pkos_edges",
        "confidence >= 0 AND confidence <= 1",
    )
    op.create_check_constraint(
        "ck_pkos_edges_valid_interval",
        "pkos_edges",
        "valid_to IS NULL OR valid_from IS NULL OR valid_to > valid_from",
    )
    op.create_foreign_key(
        "fk_pkos_edges_workspace_evidence",
        "pkos_edges",
        "pkos_evidence",
        ["workspace_id", "evidence_id"],
        ["workspace_id", "id"],
    )

    # pkos_evidence -> source_refs
    op.add_column(
        "pkos_evidence",
        sa.Column(
            "evidence_state",
            sa.String(32),
            nullable=False,
            server_default="available",
        ),
    )
    op.add_column("pkos_evidence", sa.Column("observed_at", sa.DateTime(timezone=True)))
    op.create_check_constraint(
        "ck_pkos_evidence_state",
        "pkos_evidence",
        "evidence_state IN ('available','missing','permission_denied','deleted')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_pkos_evidence_state", "pkos_evidence", type_="check")
    op.drop_column("pkos_evidence", "observed_at")
    op.drop_column("pkos_evidence", "evidence_state")

    op.drop_constraint("fk_pkos_edges_workspace_evidence", "pkos_edges", type_="foreignkey")
    op.drop_constraint("ck_pkos_edges_valid_interval", "pkos_edges", type_="check")
    op.drop_constraint("ck_pkos_edges_confidence", "pkos_edges", type_="check")
    op.drop_constraint("ck_pkos_edges_status", "pkos_edges", type_="check")
    op.drop_column("pkos_edges", "status")
    op.drop_column("pkos_edges", "valid_to")
    op.drop_column("pkos_edges", "valid_from")
    op.drop_column("pkos_edges", "evidence_id")
    op.drop_column("pkos_edges", "confidence")

    op.drop_constraint("ck_pkos_nodes_version", "pkos_nodes", type_="check")
    op.drop_constraint("ck_pkos_nodes_confidence", "pkos_nodes", type_="check")
    op.drop_constraint("ck_pkos_nodes_status", "pkos_nodes", type_="check")
    op.drop_column("pkos_nodes", "version")
    op.drop_column("pkos_nodes", "confidence")
    op.drop_column("pkos_nodes", "status")
    op.drop_column("pkos_nodes", "entity_id")
