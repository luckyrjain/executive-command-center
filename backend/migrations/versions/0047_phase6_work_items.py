"""Create engineering_work_items for Phase 6 Engineering Workspace Task 4
(Jira work-item sync -- the third real, non-sandbox connector adapter).

Numbered ``0047`` -- next-in-chain after ``0046_phase6_active_sync_guard.py``.

**Only `engineering_work_items` lands in this migration**, matching
`0045_phase6_repositories.py`'s own precedent of landing exactly what the
current task's own code populates (`DATA-MODEL.md`'s remaining
`changes`/`reviews`/`deployments`/`incidents`/`engineering_decisions`/
`service_links`/`delivery_metric_snapshots`/`source_tombstones` wait for
the task that first populates each).

**Shape mirrors `repositories` closely** (`DATA-MODEL.md`'s "every
projection stores provider, external ID, source URL, observed/updated
times, permission/freshness state and raw-content hash" rule), extended
with the work-item-specific fields a Jira issue actually carries:
`item_type` (Jira's own issue-type name -- Bug/Story/Task/Epic/...),
`status` (Jira's own workflow-status name), and `reporter_external_id`/
`assignee_external_id` -- raw Jira `accountId` strings, deliberately
**not** resolved against Phase 2's `Person` entities here. Ambiguous-
identity resolution is explicit Task 6 scope (`docs/superpowers/specs/
2026-07-27-phase-6-engineering-workspace-design.md`'s "why this isn't a
green field" section reuses Phase 2's existing `resolution_candidates`/
`merge_entities` machinery, not a second resolution mechanism built here)
-- storing an unresolved raw identifier now, rather than guessing at a
resolution, is the correct intermediate state.

**`external_id` is Jira's issue `id` (the immutable internal numeric
identifier), not its `key`** (e.g. `PROJ-123`, which can change if an
issue moves between projects) -- `jira_adapter.py`'s own module docstring
has the full reasoning. `key` is not discarded; it is embedded in
`source_url` (`https://{site}/browse/{key}`) and folded into
`content_hash`, so a rename is still detected as a content change even
though it does not change the row's own identity.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0047_phase6_work_items"
down_revision = "0046_phase6_active_sync_guard"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.create_table(
        "engineering_work_items",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("connector_account_id", uuid, nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("item_type", sa.String(100), nullable=True),
        sa.Column("status", sa.String(100), nullable=True),
        sa.Column("reporter_external_id", sa.String(255), nullable=True),
        sa.Column("assignee_external_id", sa.String(255), nullable=True),
        sa.Column("permission_state", sa.String(20), nullable=False, server_default="active"),
        sa.Column("freshness_state", sa.String(20), nullable=False, server_default="fresh"),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("provider_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "provider IN ('github', 'gitlab', 'jira', 'sandbox')",
            name="ck_engineering_work_items_provider",
        ),
        sa.CheckConstraint(
            "permission_state IN ('active', 'permission_lost', 'deleted')",
            name="ck_engineering_work_items_permission_state",
        ),
        sa.CheckConstraint(
            "freshness_state IN ('fresh', 'stale', 'disconnected')",
            name="ck_engineering_work_items_freshness_state",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "connector_account_id"],
            ["connector_accounts.workspace_id", "connector_accounts.id"],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "connector_account_id",
            "external_id",
            name="uq_engineering_work_items_account_external_id",
        ),
    )
    op.create_index(
        "ix_engineering_work_items_workspace_account",
        "engineering_work_items",
        ["workspace_id", "connector_account_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_engineering_work_items_workspace_account", table_name="engineering_work_items"
    )
    op.drop_table("engineering_work_items")
