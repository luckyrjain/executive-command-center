"""Adds `sync_cursors.backfill_resume_cursor` (nullable text) -- a second,
independent piece of persisted sync state alongside `cursor_value`.

Confirmed bug (see `ecc.domains.engineering.connector_accounts._run_
connector_sync` and each real adapter's own `backfill()`): `cursor_value`
is written from `SyncOutcome.next_cursor`, a "newest item seen" watermark
consumed only by `incremental_sync` -- no adapter's `backfill()` had
anywhere to persist "which page/token to resume this backfill walk from,"
so every backfill call restarted pagination from page 1 with no prior
state, bounded by each adapter's own `_MAX_PAGES_PER_CALL`. A workspace
with more real items in a resource type than that bound covers per call
could never have its older backlog reached by any sequence of calls --
confirmed live against a real Jira Cloud account with a real backlog.

This column is written/read exclusively by `run_type == "backfill"` calls
(`_run_connector_sync`'s own phase 1 read / phase 3 write) -- `incremental_
sync` never touches it, matching `cursor_value`'s own existing
single-purpose-per-column precedent on this table rather than overloading
one column with two independently-lifecycled concepts. `NULL` means
either "nothing to resume yet" or "this resource type's backfill has
genuinely, fully completed" -- the two are indistinguishable from this
column alone by design; a completed backfill's next call simply starts a
fresh walk from page 1 again, which is correct.

No CHECK constraint -- like `cursor_value`, this column's shape is
entirely adapter-defined and opaque to the platform layer (a page number
for GitHub/GitLab repositories, a per-repository JSON map for GitHub
changes, a provider-issued opaque `nextPageToken` for Jira work items).
"""

import sqlalchemy as sa
from alembic import op

revision = "0081_backfill_resume_cursor"
down_revision = "0080_automation_hot_path_indexes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "sync_cursors",
        sa.Column("backfill_resume_cursor", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("sync_cursors", "backfill_resume_cursor")
