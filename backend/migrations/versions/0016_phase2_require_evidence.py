"""Require pkos_edges.evidence_id (relationships), closing a gap an audit of
the shipped Phase 2 code found against phase-002/DATA-MODEL.md's invariant
"a claim or relationship has at least one source reference" -- knowledge_claims
already enforced this in the DB, pkos_edges (relationships) did not.
"""

from alembic import op

revision = "0016_phase2_require_evidence"
down_revision = "0015_phase2_embeddings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column("pkos_edges", "evidence_id", nullable=False)


def downgrade() -> None:
    op.alter_column("pkos_edges", "evidence_id", nullable=True)
