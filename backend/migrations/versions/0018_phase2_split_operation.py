"""Widen entity_operations.operation_type to allow 'split', closing a gap
an audit of the shipped Phase 2 code found: DATA-MODEL.md's invariant
"Split operations restore traceable descendants and invalidate obsolete
projections" and ENTITY-RESOLUTION-CONTRACT.md's "reversal restores prior
identities unless a later dependent operation requires manual split" both
name split as a real operation, but the CHECK constraint only ever allowed
merge/reverse -- split was never implemented, and this constraint would
have needed a migration to add it even if the code had been written.
"""

from alembic import op

revision = "0018_phase2_split_operation"
down_revision = "0017_phase2_resolution_defer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_entity_operations_type", "entity_operations", type_="check")
    op.create_check_constraint(
        "ck_entity_operations_type",
        "entity_operations",
        "operation_type IN ('merge', 'reverse', 'split')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_entity_operations_type", "entity_operations", type_="check")
    op.create_check_constraint(
        "ck_entity_operations_type",
        "entity_operations",
        "operation_type IN ('merge', 'reverse')",
    )
