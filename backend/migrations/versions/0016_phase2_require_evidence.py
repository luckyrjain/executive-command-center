"""Require pkos_edges.evidence_id (relationships), closing a gap an audit of
the shipped Phase 2 code found against phase-002/DATA-MODEL.md's invariant
"a claim or relationship has at least one source reference" -- knowledge_claims
already enforced this in the DB, pkos_edges (relationships) did not.

evidence_id was added nullable by migration 0010 with no default, so any
edge row written before this migration (Phase 1 relationships predate the
column entirely; Phase 2 relationships created before the app layer
started requiring evidence_id, per Task 16 in the completeness-audit fix
branch) can carry evidence_id IS NULL. Setting the column NOT NULL without
backfilling those rows first would make this migration fail outright on
any environment carrying such data, so upgrade() backfills each orphaned
edge with a placeholder pkos_evidence row (evidence_state='missing', since
it documents that the edge's real source was never captured, not that one
now exists) before tightening the constraint.
"""

from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

from alembic import op
from sqlalchemy import text

revision = "0016_phase2_require_evidence"
down_revision = "0015_phase2_embeddings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    orphaned_edges = (
        bind.execute(
            text(
                "SELECT id, workspace_id, source_node_id FROM pkos_edges WHERE evidence_id IS NULL"
            )
        )
        .mappings()
        .all()
    )
    now = datetime.now(UTC)
    for edge in orphaned_edges:
        evidence_id = uuid4()
        source_ref = f"legacy-backfill:pkos_edges:{edge['id']}"
        bind.execute(
            text(
                """
                INSERT INTO pkos_evidence (
                    id, workspace_id, node_id, source_type, source_ref, sha256,
                    captured_at, evidence_state
                ) VALUES (
                    :id, :workspace_id, :node_id, 'legacy_backfill', :source_ref, :sha256,
                    :captured_at, 'missing'
                )
                """
            ),
            {
                "id": evidence_id,
                "workspace_id": edge["workspace_id"],
                "node_id": edge["source_node_id"],
                "source_ref": source_ref,
                "sha256": sha256(source_ref.encode()).hexdigest(),
                "captured_at": now,
            },
        )
        bind.execute(
            text("UPDATE pkos_edges SET evidence_id = :evidence_id WHERE id = :id"),
            {"evidence_id": evidence_id, "id": edge["id"]},
        )
    op.alter_column("pkos_edges", "evidence_id", nullable=False)


def downgrade() -> None:
    op.alter_column("pkos_edges", "evidence_id", nullable=True)
