"""Add a partial unique index serializing concurrent syncs per connector
account (Phase 6 Task 2 review-fix).

Numbered ``0046`` -- next-in-chain after ``0045_phase6_repositories.py``.

**Why this is needed now, not in migration 0044.** `sync_connector_
endpoint` (`connector_accounts.py`) was restructured in this same task to
run in three phases across two pooled connections, so a real adapter's
slow HTTP call (`github_adapter.GitHubAdapter`) never holds a pooled
connection hostage (Task 1's own disclosed pool-exhaustion blocker). That
restructuring means the `FOR UPDATE` row lock `get_connector_account`
takes on `connector_accounts` -- previously enough, on its own, to
serialize two concurrent `/sync` calls for the same account for the
*whole* handler -- now only spans phase 1's short bookkeeping
transaction. A second `/sync` call (or idempotent retry with a
*different* Idempotency-Key value, or arriving before the first call's
own idempotency record exists) can begin its own phase 1 the instant the
first call's phase 1 commits, i.e. while the first call's slow phase 2
adapter call is still in flight -- reading the same prior cursor the
first call already read, then both landing conflicting `sync_cursors`
UPSERTs in phase 3 (a lost-update race), and a genuine same-key retry
racing an unhandled `idempotency_records` primary-key collision instead
of returning the cached response.

**The fix: make "start a sync" itself atomic and self-serializing**,
rather than trying to extend a held lock across the slow phase. `UNIQUE
(workspace_id, connector_account_id) WHERE status = 'running'` means the
phase-1 `INSERT INTO sync_runs (...status='running'...)` itself cannot
succeed while another run for the same account is still in flight --
mirroring `uq_workflow_versions_active_per_workflow` (migration 0038) and
`uq_prompt_versions_active_per_prompt_id` (migration 0029)'s identical
"partial unique index enforces at-most-one, without needing an
application-held lock across a request" idiom. `connector_accounts.py`
catches the resulting `IntegrityError` and returns `409
CONNECTOR_SYNC_IN_PROGRESS` -- a second call (whether a true concurrent
sync or a retry racing ahead of the first call's idempotency record)
never reaches phase 3's cursor UPSERT until the first call's own phase 3
has already flipped its `sync_runs` row out of `running`, closing the
lost-cursor-update race and the double-execution race in the same
mechanism.
"""

import sqlalchemy as sa
from alembic import op

revision = "0046_phase6_active_sync_guard"
down_revision = "0045_phase6_repositories"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "uq_sync_runs_running_per_account",
        "sync_runs",
        ["workspace_id", "connector_account_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    op.drop_index("uq_sync_runs_running_per_account", table_name="sync_runs")
