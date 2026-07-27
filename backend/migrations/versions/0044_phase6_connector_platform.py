"""Create connector_accounts/sync_cursors/sync_runs for Phase 6 Engineering
Workspace Task 1 (connector framework and source projections -- the
platform layer only, no provider-specific projection tables yet).

Phase 6 Task 1 (`docs/superpowers/specs/2026-07-27-phase-6-engineering-
workspace-design.md`, `docs/phases/phase-006/DATA-MODEL.md` v0.1.0).
Numbered ``0044`` -- the true next-in-chain after
``0043_phase5_preview_blocked_status.py``, per this codebase's own
documented convention that migration file numbers match actual
implementation/chain order, not a plan's nominal numbering (migration
``0029``'s own docstring states this precedent explicitly).

**Only the connector-platform tables land in this migration, not the full
`DATA-MODEL.md` table list.** `repositories`/`engineering_work_items`/
`changes`/`reviews`/`deployments`/`incidents`/`engineering_decisions`/
`service_links`/`delivery_metric_snapshots`/`source_tombstones` are each
added by the task that first populates them (Task 2 onward) -- mirroring
Phase 5 Task 1's own precedent exactly: migration ``0038`` created
``workflow_definitions``/``workflow_versions``/``automation_policies``/
``triggers`` only, and ``workflow_runs`` waited for Task 2's own migration
(``0039_phase5_workflow_runs.py``). Building every projection table now,
before any adapter exists to populate them, would be schema speculation
this repository's own precedent explicitly avoids.

**Workspace-scoped, standard actor/audit shape**, identical to every
Phase 1-5 domain table (`waiting_links`, migration 0023;
`automation_policies`, migration 0038): every table carries `workspace_id`
FK'd to `workspaces.id` (`ondelete="CASCADE"`), and every `created_by`/
`updated_by` actor reference is a composite FK to `users.(workspace_id,
id)` (`ondelete="RESTRICT"`).

**`connector_accounts.encrypted_credentials` is `bytea`, not `text`.**
`ecc.domains.engineering.crypto.encrypt_credential` returns Fernet's own
URL-safe-base64 token as `bytes` (design doc Decision 2) -- stored as raw
bytes rather than round-tripped through a text encoding step this schema
does not need. No column anywhere on this table holds a plaintext
credential; `POST /engineering/connectors` and `GET /engineering/
connectors` (`connector_accounts.py`) never select this column into a
response model.

**`connector_accounts` needs `UNIQUE(workspace_id, id)`, not just its own
primary key,** so `sync_cursors`/`sync_runs` can each hold a genuine
workspace-scoped composite FK against it -- identical reasoning and
mechanism to `waiting_links`' own `uq_waiting_links_workspace_id`
(migration 0023) and `workflow_definitions`' own `UNIQUE(workspace_id,
workflow_id)` (migration 0038): a composite FK target must itself be
unique on the composite key, which a bare single-column primary key does
not by itself provide to a *different* table's composite FK declaration.

**`sync_cursors` is keyed `(workspace_id, connector_account_id,
resource_type)`, not one row per connector account.** `CONNECTOR-CONTRACT.md`:
sync is incremental per resource kind (a connector backfills/incrementally
syncs repositories, work items, changes, etc. as independent streams, each
needing its own resumable position) -- one shared cursor column per
account would force every resource type's sync progress to collapse into a
single position, which is not how any of GitHub/GitLab/Jira's own
pagination/webhook-delivery cursors actually work per resource kind.

**No immutability trigger on any of these three tables** (unlike migration
0038's `workflow_versions`) -- none of them is a versioned, once-published
artifact whose past state other rows pin against; `connector_accounts.
status`/`sync_cursors.cursor_value`/`sync_runs.status` are all expected to
be updated in place as sync actually progresses, the ordinary mutable-row
shape most Phase 1-3 tables already use (e.g. `waiting_links.status`).

**No seed data.** Every row in these three tables is created exclusively
through `ecc.domains.engineering.connector_accounts`'s own write paths
(`POST /engineering/connectors`, `POST .../{id}/sync`) -- there is nothing
this migration itself should own the identity of.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0044_phase6_connector_platform"
down_revision = "0043_phase5_preview_blocked"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    # --- connector_accounts --------------------------------------------------

    op.create_table(
        "connector_accounts",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("external_account_id", sa.String(255), nullable=False),
        sa.Column("display_name", sa.String(255), nullable=False),
        sa.Column(
            "granted_scopes",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        # Fernet ciphertext bytes -- never a plaintext credential. See
        # module docstring's "bytea, not text" section.
        sa.Column("encrypted_credentials", sa.LargeBinary(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("status_detail", sa.Text(), nullable=True),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("disconnected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_by", uuid, nullable=False),
        sa.Column("updated_by", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "provider IN ('github', 'gitlab', 'jira', 'sandbox')",
            name="ck_connector_accounts_provider",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'active', 'permission_lost', 'rate_limited', "
            "'disconnected', 'error')",
            name="ck_connector_accounts_status",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "created_by"], ["users.workspace_id", "users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "updated_by"], ["users.workspace_id", "users.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "provider",
            "external_account_id",
            name="uq_connector_accounts_workspace_provider_external_id",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_connector_accounts_workspace_id"),
    )
    op.create_index(
        "ix_connector_accounts_workspace_status",
        "connector_accounts",
        ["workspace_id", "status"],
    )

    # --- sync_cursors ---------------------------------------------------------

    op.create_table(
        "sync_cursors",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("connector_account_id", uuid, nullable=False),
        sa.Column("resource_type", sa.String(30), nullable=False),
        sa.Column("cursor_value", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "resource_type IN ('repository', 'work_item', 'change', 'review', "
            "'deployment', 'incident')",
            name="ck_sync_cursors_resource_type",
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
            "resource_type",
            name="uq_sync_cursors_account_resource_type",
        ),
    )

    # --- sync_runs --------------------------------------------------------------

    op.create_table(
        "sync_runs",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("connector_account_id", uuid, nullable=False),
        sa.Column("run_type", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        sa.Column("items_processed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_summary", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "run_type IN ('backfill', 'incremental', 'webhook')",
            name="ck_sync_runs_run_type",
        ),
        sa.CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'partial')",
            name="ck_sync_runs_status",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "connector_account_id"],
            ["connector_accounts.workspace_id", "connector_accounts.id"],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_sync_runs_workspace_account_started",
        "sync_runs",
        ["workspace_id", "connector_account_id", "started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_sync_runs_workspace_account_started", table_name="sync_runs")
    op.drop_table("sync_runs")
    op.drop_table("sync_cursors")
    op.drop_index("ix_connector_accounts_workspace_status", table_name="connector_accounts")
    op.drop_table("connector_accounts")
