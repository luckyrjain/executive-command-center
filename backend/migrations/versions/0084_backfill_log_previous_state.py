"""Security remediation Spec A (plan task FX2, deep review round 3): add
`personal_visibility_backfill_log.previous_state`, a nullable JSONB snapshot
of the row as the personal-visibility backfill found it.

`scripts/backfill_personal_visibility.py` writes it when it changes a row:
for tasks/commitments/risks their `version` and `updated_at` (plus whether
the backfill bumped `version`, and the member fields of their attention
items); for email attention items those member fields -- `dismissed_at`,
`deferred_until`, and a SHA-256 digest of `override_reason`. The log holds
ids, owners, versions, timestamps and that digest, never member-written
text itself. `--restore` then puts a row back only
while those values are exactly what the backfill left -- an exact,
clock-free check that replaces the earlier `updated_at` stamp and
audit-time lookup. Log rows written before this revision keep a NULL
snapshot and are restored by the owner/visibility compare-and-set alone.

Operational note: `ADD COLUMN ... NULL` with no default is a metadata-only
change in PostgreSQL -- instant, no table rewrite, only a brief ACCESS
EXCLUSIVE lock on this ops-only table (which no application request reads).
`downgrade()` drops the column and with it every snapshot: back the table
up first (`pg_dump -t personal_visibility_backfill_log`) if a restore may
still be needed.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0084_backfill_log_previous_state"
down_revision = "0083_email_derived_target_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "personal_visibility_backfill_log",
        sa.Column("previous_state", postgresql.JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("personal_visibility_backfill_log", "previous_state")
