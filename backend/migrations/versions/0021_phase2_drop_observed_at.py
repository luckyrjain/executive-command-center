"""Drop pkos_evidence.observed_at, closing a gap an audit of the shipped
Phase 2 code found: migration 0010 added this column (nullable, no default)
because PKOS-SCHEMA.md's evidence contract names an `observed_at` field
alongside `captured_at`, but no code path in the shipped implementation
ever writes or reads it -- evidence.py's create/resolve/delete handlers list
their column sets explicitly and never include it, and no SELECT in the
knowledge domain projects it either. Every row's observed_at has always
been and will always be NULL; the same class of dead column migration
0020 already dropped for pkos_nodes.entity_id.
"""
import sqlalchemy as sa
from alembic import op

revision = "0021_phase2_drop_observed_at"
down_revision = "0020_phase2_drop_dead_entity_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("pkos_evidence", "observed_at")


def downgrade() -> None:
    op.add_column(
        "pkos_evidence", sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True)
    )
