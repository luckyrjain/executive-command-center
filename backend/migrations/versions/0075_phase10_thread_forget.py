"""Phase 10 Task 6: widen `deletion_jobs` for a thread-scoped "forget this"
action (`docs/superpowers/plans/2026-08-04-phase-10-gmail-connector.md`,
Task 6: "Per-thread 'forget this' action extending Phase 7's existing
deletion granularity").

Phase 7's `deletion_jobs` (migration `0054_phase7_personal_domains.py`) was
built for exactly one granularity: `scope` is `CHECK`-constrained to the
single literal `'domain'`, and the table has no column identifying *which*
resource within a domain a job targeted -- correct for Task 6's own
predecessors, since every deletion before this task has been domain-wide.
Task 6's own per-thread deletion is the first sub-domain-granularity
deletion this codebase performs, and needs both an audit trail (matching
every other mutating personal-domain endpoint's own "record what changed
and when" convention) and a way to record *which* thread, neither of which
`deletion_jobs`'s current shape can express.

Two additive, backward-compatible changes, both leaving every existing
`'domain'`-scope row and every existing caller of `export_deletion.py`'s
own domain-wide delete completely unaffected:

1. Widens `ck_deletion_jobs_scope` from `scope IN ('domain')` to `scope IN
   ('domain', 'thread')` -- an in-place `CHECK` constraint replacement
   (drop, re-create with the wider list), not a new constraint, so there is
   exactly one scope check at any time, matching this table's own existing
   single-constraint shape.
2. Adds `resource_id UUID`, nullable (a `'domain'`-scope row has no
   resource narrower than the domain itself to name, so this column stays
   `NULL` for every row `export_deletion.py` writes -- only Task 6's new
   `POST .../gmail/threads/{thread_id}/forget` endpoint ever populates it,
   with the thread's own `email_threads.id`). No FK to `email_threads`:
   a `deletion_jobs` row is a permanent audit record of an action that
   happened, and Phase 10's own Task 6 design intentionally does not delete
   the `email_threads` row itself (only that thread's cached message
   content) -- but a future task could still delete threads outright, and
   an audit row for a past deletion must not itself be deleted (or block
   deletion) via an FK it would otherwise force.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0075_phase10_thread_forget"
down_revision = "0074_phase10_email_detect_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "deletion_jobs",
        sa.Column("resource_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.drop_constraint("ck_deletion_jobs_scope", "deletion_jobs", type_="check")
    op.create_check_constraint(
        "ck_deletion_jobs_scope",
        "deletion_jobs",
        "scope IN ('domain', 'thread')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_deletion_jobs_scope", "deletion_jobs", type_="check")
    op.create_check_constraint(
        "ck_deletion_jobs_scope",
        "deletion_jobs",
        "scope IN ('domain')",
    )
    op.drop_column("deletion_jobs", "resource_id")
