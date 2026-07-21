"""Add Phase 2 resolution_candidates and entity_operations tables."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013_phase2_resolution"
down_revision = "0012_phase2_timeline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.create_table(
        "resolution_candidates",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, sa.ForeignKey("workspaces.id"), nullable=False, index=True),
        sa.Column("left_entity_id", uuid, nullable=False),
        sa.Column("right_entity_id", uuid, nullable=False),
        sa.Column("score", sa.Numeric(5, 4), nullable=False),
        sa.Column("factors_json", postgresql.JSONB(), nullable=False),
        sa.Column("resolver_version", sa.String(50), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_by", uuid),
        sa.Column("reason", sa.Text()),
        sa.CheckConstraint("score >= 0 AND score <= 1", name="ck_resolution_candidates_score"),
        sa.CheckConstraint(
            "status IN ('open', 'confirmed', 'rejected', 'expired')",
            name="ck_resolution_candidates_status",
        ),
        sa.CheckConstraint(
            "left_entity_id <> right_entity_id", name="ck_resolution_candidates_distinct_pair"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "left_entity_id"],
            ["pkos_nodes.workspace_id", "pkos_nodes.id"],
            name="fk_resolution_candidates_workspace_left",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "right_entity_id"],
            ["pkos_nodes.workspace_id", "pkos_nodes.id"],
            name="fk_resolution_candidates_workspace_right",
        ),
        # Callers always normalize (left_entity_id, right_entity_id) into a
        # stable order before insert (see resolution.py's create_candidate),
        # so a plain unique index on the ordered pair is sufficient to
        # prevent duplicate rows for the same pair regardless of caller
        # argument order.
        sa.UniqueConstraint(
            "workspace_id",
            "left_entity_id",
            "right_entity_id",
            name="uq_resolution_candidates_pair",
        ),
    )

    op.create_table(
        "entity_operations",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, sa.ForeignKey("workspaces.id"), nullable=False, index=True),
        sa.Column("operation_type", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("inputs_json", postgresql.JSONB(), nullable=False),
        sa.Column("outputs_json", postgresql.JSONB(), nullable=False),
        sa.Column("actor_id", uuid, nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        # Self-FK: a 'reverse' operation row points back at the 'merge' row
        # it reverses. NULL for the 'merge' row itself. Task 5 (reversible
        # merge/split lineage) is what actually populates this column --
        # this migration only creates the shape, per this plan's "never
        # re-open an already-applied migration" rule.
        sa.Column("reverses_operation_id", uuid),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "operation_type IN ('merge', 'reverse')", name="ck_entity_operations_type"
        ),
        sa.CheckConstraint("status IN ('active', 'reversed')", name="ck_entity_operations_status"),
        sa.ForeignKeyConstraint(
            ["reverses_operation_id"],
            ["entity_operations.id"],
            name="fk_entity_operations_reverses",
        ),
    )


def downgrade() -> None:
    op.drop_table("entity_operations")
    op.drop_table("resolution_candidates")
