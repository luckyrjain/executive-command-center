"""Add resolution_candidates.deferred_until, closing a gap an audit of the
shipped Phase 2 code found: UX-STATES.md names "confirm match, reject match
and defer" as the resolution review's three primary actions, but only
confirm/reject existed. Mirrors attention_items.deferred_until (Phase 1) --
defer postpones review without deciding the candidate, status stays 'open'.
"""

import sqlalchemy as sa
from alembic import op

revision = "0017_phase2_resolution_defer"
down_revision = "0016_phase2_require_evidence"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "resolution_candidates", sa.Column("deferred_until", sa.DateTime(timezone=True))
    )


def downgrade() -> None:
    op.drop_column("resolution_candidates", "deferred_until")
