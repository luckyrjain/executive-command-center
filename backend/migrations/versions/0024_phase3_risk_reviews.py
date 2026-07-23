"""Add risk_reviews: append-only review and escalation history.

Phase 3 Task 3. Recording a review updates risks.review_at and risks.version
transactionally with the risk_reviews insert, matching every existing
dual-write pattern in this codebase. risks.py's own CRUD and its
review_overdue/review_due_soon scoring factors are unmodified by this task.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0024_phase3_risk_reviews"
down_revision = "0023_phase3_waiting"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.create_table(
        "risk_reviews",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("risk_id", uuid, nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        # Free-text reference strings (URLs, document names, evidence IDs
        # quoted as text) rather than a FK-enforced array into pkos_evidence
        # -- risks predate Phase 2's evidence model and not every review
        # will cite Phase 2 evidence specifically.
        sa.Column(
            "evidence_refs",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_review_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actor_id", uuid, nullable=False),
        sa.CheckConstraint(
            "outcome IN ('no_change','escalated','de_escalated','mitigated','closed')",
            name="ck_risk_reviews_outcome",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        # RESTRICT, not CASCADE: this table's own docstring says it is
        # append-only review and escalation history -- deleting a risk must
        # not silently delete that history out from under it (finding #14).
        # A risk that still has review history must be archived, not
        # hard-deleted, if that history is to be preserved.
        sa.ForeignKeyConstraint(
            ["workspace_id", "risk_id"], ["risks.workspace_id", "risks.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "actor_id"], ["users.workspace_id", "users.id"], ondelete="RESTRICT"
        ),
    )
    op.create_index(
        "ix_risk_reviews_workspace_risk_reviewed",
        "risk_reviews",
        ["workspace_id", "risk_id", "reviewed_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_risk_reviews_workspace_risk_reviewed", table_name="risk_reviews")
    op.drop_table("risk_reviews")
