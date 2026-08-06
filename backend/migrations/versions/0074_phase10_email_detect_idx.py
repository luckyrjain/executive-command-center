"""Partial index supporting `detect_actions_since`'s eligibility scan (Phase 10 Task 5).

Loop 2 round 3's fix (see `docs/phases/phase-010/IMPLEMENTATION-STATUS.md`)
dropped the `created_at >= :since` filter from `GmailAdapter.detect_actions_
since`'s eligibility query (`gmail_adapter.py`) to stop a per-call `LIMIT`
from permanently losing backlog -- `body IS NULL` is now the query's *only*
filter, and it is no longer bounded by time. Round 4 review found that this
made the query's cost scale with an account's entire un-fetched-body
history rather than a recent time window, with no index to support it: the
only existing indexes on this table are `ix_email_messages_thread_sent`
(thread-scoped, not usable for a cross-thread `workspace_id`/`owner_id`/
`direction` scan) and the primary key.

This index is partial (`WHERE body IS NULL`) rather than covering the full
table, matching `gmail_adapter.py`'s own body-fetch pipeline: once a
message's body is fetched (or marked unfetchable -- round 4 finding 3),
`body` is no longer `NULL` and the row drops out of the index entirely, so
the index only ever holds the (small, steady-state) backlog of not-yet-
processed messages, not the account's full mail history.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0074_phase10_email_detect_idx"
down_revision = "0073_phase10_email_detect_eval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_email_messages_detect_action_eligible",
        "email_messages",
        ["workspace_id", "owner_id", "direction"],
        postgresql_where=sa.text("body IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_email_messages_detect_action_eligible", table_name="email_messages")
