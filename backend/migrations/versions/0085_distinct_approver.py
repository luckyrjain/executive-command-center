"""Add `workspaces.require_distinct_approver`: when true, a member may not
approve an `approval_requests` row for a run they started
(`workflow_runs.created_by`) -- a second member must. Rejecting stays
allowed for anyone who may decide the request.

Opt-in (`DEFAULT false`), so every existing workspace -- including every
single-member one, which has no second member who could ever approve --
keeps today's behaviour. Only an owner can change it
(`identity.accounts.patch_workspace_endpoint`).

Operational note: `ADD COLUMN ... NOT NULL DEFAULT false` with a constant
default is a metadata-only change in PostgreSQL 11+ -- no table rewrite.
"""

import sqlalchemy as sa
from alembic import op

revision = "0085_distinct_approver"
down_revision = "0084_backfill_log_previous_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "workspaces",
        sa.Column(
            "require_distinct_approver",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )


def downgrade() -> None:
    op.drop_column("workspaces", "require_distinct_approver")
