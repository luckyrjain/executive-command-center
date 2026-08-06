"""Phase 10 Task 4: recommendations create-path extension.

Adds `proposed_fields` (nullable JSONB) to `recommendations`, holding the
`TaskCreate`/`CommitmentCreate`/`RiskCreate`-shaped payload for a
`operation="create"` recommendation -- distinct from `proposed_action`
(reused unchanged: `{"operation": "create", "value": None}` for this new
operation, matching every existing operation's own two-key shape).
`target_id`/`expected_version` are already nullable on this table (no
`nullable=False` in migration 0009), and `target_type` has no CHECK
constraint restricting it -- so neither needs altering for a create-type
row (`target_id`/`expected_version` are simply `NULL`, `target_type` is
still `task`/`commitment`/`risk`, just describing what gets created rather
than what gets updated).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0071_phase10_recs_create_path"
down_revision = "0070_gmail_sync_cursor_type"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "recommendations",
        sa.Column("proposed_fields", postgresql.JSONB()),
    )


def downgrade() -> None:
    op.drop_column("recommendations", "proposed_fields")
