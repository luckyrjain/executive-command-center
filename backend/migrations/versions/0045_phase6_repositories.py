"""Create repositories for Phase 6 Engineering Workspace Task 2 (GitHub
read sync -- the first real, non-sandbox connector adapter).

Phase 6 Task 2 (`docs/superpowers/plans/2026-07-27-phase-6-engineering-
workspace.md`, `docs/phases/phase-006/DATA-MODEL.md`). Numbered ``0045``
-- the true next-in-chain after ``0044_phase6_connector_platform.py``, per
this codebase's own documented migration-numbering convention.

**Only `repositories` lands in this migration, not every Task 2 resource
type.** `engineering_work_items`/`changes`/`reviews`/`deployments`
(`DATA-MODEL.md`'s remaining projection tables) are deferred to a Task 2
follow-up -- this slice delivers GitHub repository backfill/incremental
sync end to end (the first real network-calling adapter, replacing
Task 1's sandbox fake) rather than a partially-wired set of five tables.
Matches migration `0044`'s own precedent of landing exactly what the
current task's own code populates, not the full data model upfront.

**Shape mirrors `connector_accounts` closely**, since this is the first of
`DATA-MODEL.md`'s "synced-content projection" tables (as opposed to Task
1's connector-platform bookkeeping tables): `provider`/`external_id`/
`source_url`/`permission_state`/`freshness_state`/`content_hash` per
`DATA-MODEL.md`'s "every projection stores..." rule, plus `provider_
updated_at` (the source's own last-modified timestamp, distinct from this
table's own `observed_at`/`updated_at` -- `provider_updated_at` is what
`GitHubAdapter`'s incremental-sync cursor advances against, since GitHub's
repository-list endpoint has no native `since` parameter; sorting by
`updated_at` descending and stopping once a page's rows fall at or before
the prior cursor is the resumable strategy `github_adapter.py` implements).

Workspace-scoped, standard actor-less shape (unlike `connector_accounts`,
no `created_by`/`updated_by` -- every row here is written by a connector
adapter's own sync call, never a human-authored mutation, so there is no
"which user touched this" audit question the way there is for a
human-editable table).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0045_phase6_repositories"
down_revision = "0044_phase6_connector_platform"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.create_table(
        "repositories",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("connector_account_id", uuid, nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("name", sa.String(500), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("default_branch", sa.String(255), nullable=True),
        sa.Column("permission_state", sa.String(20), nullable=False, server_default="active"),
        sa.Column("freshness_state", sa.String(20), nullable=False, server_default="fresh"),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("provider_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "provider IN ('github', 'gitlab', 'jira', 'sandbox')",
            name="ck_repositories_provider",
        ),
        sa.CheckConstraint(
            "permission_state IN ('active', 'permission_lost', 'deleted')",
            name="ck_repositories_permission_state",
        ),
        sa.CheckConstraint(
            "freshness_state IN ('fresh', 'stale', 'disconnected')",
            name="ck_repositories_freshness_state",
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
            name="uq_repositories_account_external_id",
        ),
    )
    op.create_index(
        "ix_repositories_workspace_account",
        "repositories",
        ["workspace_id", "connector_account_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_repositories_workspace_account", table_name="repositories")
    op.drop_table("repositories")
