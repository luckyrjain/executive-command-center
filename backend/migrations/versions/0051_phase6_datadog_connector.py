"""Add the Datadog connector: provider registration plus three new synced-
content projections (monitors, service definitions, dashboards).

Numbered ``0051`` -- next-in-chain after ``0050_phase6_team_linkage.py``.

**Why a fourth provider, and why these three resource types.** The
original ask behind this work was "team-specific dashboards and SRE
golden signals" -- Datadog is the first non-issue-tracker,
non-source-control connector this codebase adds, so it needed its own
research pass (real REST API, not the MCP server sketched earlier and
explicitly rejected for durable sync: remote/hosted-only, OAuth-shaped
auth that doesn't fit `ConnectorAdapter.authorize(credential: str)`'s
single-token model, and most tools have no resumable cursoring). Of the
three real candidate resources, all three were requested rather than
picking just one:

- **`datadog_monitors`** (`GET /api/v1/monitor`): the actual "golden
  signals" data -- each monitor is a real latency/error-rate/traffic/
  saturation alert with a live `OK`/`Alert`/`Warn`/`No Data`-style state.
  The best-documented of the three: confirmed pagination (`page`/
  `page_size`). `datadog_adapter.py` parses a monitor's own `team:<name>`
  tag client-side after fetching (not a server-side `monitor_tags` query
  filter, which this adapter does not use); see that module's own
  docstring for the exact mechanism.
- **`datadog_service_definitions`** (`GET /api/v2/services/definitions`):
  Datadog's own Service Catalog, with a **real, first-class `team`
  field** -- unlike every other provider this codebase connects to
  (GitHub/GitLab/Jira all only ever offer a heuristic guess at team
  ownership; see migration `0050_phase6_team_linkage.py`), Datadog's
  service definitions actually say which team owns a service. Confirmed
  pagination (`page[size]`/`page[number]`, JSON:API-style).
- **`datadog_dashboards`** (`GET /api/v1/dashboard`): the literal
  "team-specific dashboard" from the original ask. **Disclosed gap**:
  this endpoint's pagination and tag-filtering support could not be
  confirmed from Datadog's published docs during this task's own
  research pass (the API reference page did not fully materialize for
  either of those two facts) -- `datadog_adapter.py`'s own module
  docstring has the full disclosure and the defensive coding this forced
  (treating the full list response as a single unpaginated page,
  `suggested_team_name` populated only when a `tags` array is actually
  present in the response, never assumed).

**Shape mirrors every other synced-content projection** (`DATA-MODEL.md`'s
"every projection stores provider, external ID, source URL, observed/
updated times, permission/freshness state and raw-content hash" rule),
plus the identical `team_entity_id`/`suggested_team_name` pair migration
`0050` established -- **but no `team_assignment_version`/`team_
assignment_updated_by` columns here**: this task does not add `POST
.../team` confirm endpoints for these three new tables (see this
migration's own "deliberately deferred" note below), so there is no
confirm-endpoint write path that would need the optimistic-concurrency
columns migration `0050`'s own review added for `repositories`/
`engineering_work_items`. Adding them now, unused, would repeat the
identical "unreachable column" mistake that migration's own docstring
just finished disclosing and closing.

**`datadog_service_definitions.suggested_team_name` is not a heuristic
like the other three providers' -- it is Datadog's own real `team`
field, copied verbatim.** Still named `suggested_team_name` (not
`team_entity_id` directly) and still requires the identical human
confirmation step before becoming an authoritative link: a real
Datadog team name is not itself a validated, existing `pkos_nodes`
`kind="team"` row in this workspace, and this codebase's identity-
resolution design never auto-creates entities from external evidence,
however reliable that evidence is (see migration `0050`'s own docstring
for why "looks like the same team" always needs a human's confirmation,
not an assumption -- the same principle that already applies to a
Jira project name, just with much better underlying data quality here).

**Deliberately deferred, disclosed, not silently expanded into this
task**: no `POST .../datadog/{monitors,service-definitions,dashboards}/
{id}/team` confirm endpoints, and no frontend UI for these three new
tables -- this task's own scope is the connector/sync layer plus the
read-only query surface it needs to be usable at all (mirroring
`list_repositories_endpoint`'s own precedent), not the write/confirm
side or a UI. The three new `GET /engineering/monitors|service-
definitions|dashboards` endpoints this same task adds *do* include an
optional `team_entity_id` query filter (read-only, identical to
`list_repositories_endpoint`'s own filter) -- only the confirm-write
endpoint and the frontend UI are the deferred half, not the filter.
Wiring team confirmation and a frontend surface for these three tables
is real, separate follow-up work, matching the same "land exactly what
this task needs" discipline every prior Phase 6 migration in this chain
has followed.

**Two existing CHECK constraints widen, nothing else changes.**
`connector_accounts.provider` gains `'datadog'`
(`ck_connector_accounts_provider`) and `sync_cursors.resource_type` gains
`'monitor'`, `'service_definition'`, `'dashboard'`
(`ck_sync_cursors_resource_type`) -- both closed enums from migration
`0044_phase6_connector_platform.py`, widened via `ALTER TABLE ... DROP
CONSTRAINT`/`ADD CONSTRAINT` rather than a full table rewrite. No other
table's provider CHECK constraint needs touching: `repositories`/
`engineering_work_items`/`changes`/`reviews`'s own `ck_*_provider`
constraints stay exactly `('github', 'gitlab', 'jira', 'sandbox')`, since
none of Datadog's three resource types is a repository, work item,
change, or review -- they get their own new tables instead.
"""

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0051_phase6_datadog_connector"
down_revision = "0050_phase6_team_linkage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.drop_constraint("ck_connector_accounts_provider", "connector_accounts", type_="check")
    op.create_check_constraint(
        "ck_connector_accounts_provider",
        "connector_accounts",
        "provider IN ('github', 'gitlab', 'jira', 'sandbox', 'datadog')",
    )
    op.drop_constraint("ck_sync_cursors_resource_type", "sync_cursors", type_="check")
    op.create_check_constraint(
        "ck_sync_cursors_resource_type",
        "sync_cursors",
        "resource_type IN ('repository', 'work_item', 'change', 'review', "
        "'deployment', 'incident', 'monitor', 'service_definition', 'dashboard')",
    )

    def _projection_columns() -> list[sa.Column[Any]]:
        return [
            sa.Column("id", uuid, primary_key=True),
            sa.Column("workspace_id", uuid, nullable=False),
            sa.Column("connector_account_id", uuid, nullable=False),
            sa.Column("provider", sa.String(20), nullable=False),
            sa.Column("external_id", sa.String(255), nullable=False),
            sa.Column("source_url", sa.Text(), nullable=False),
            sa.Column("permission_state", sa.String(20), nullable=False, server_default="active"),
            sa.Column("freshness_state", sa.String(20), nullable=False, server_default="fresh"),
            sa.Column("content_hash", sa.String(64), nullable=True),
            sa.Column("provider_updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("team_entity_id", uuid, nullable=True),
            sa.Column("suggested_team_name", sa.String(500), nullable=True),
        ]

    op.create_table(
        "datadog_monitors",
        *_projection_columns(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("monitor_type", sa.String(100), nullable=True),
        sa.Column("query", sa.Text(), nullable=True),
        sa.Column("overall_state", sa.String(50), nullable=True),
        sa.CheckConstraint("provider IN ('datadog')", name="ck_datadog_monitors_provider"),
        sa.CheckConstraint(
            "permission_state IN ('active', 'permission_lost', 'deleted')",
            name="ck_datadog_monitors_permission_state",
        ),
        sa.CheckConstraint(
            "freshness_state IN ('fresh', 'stale', 'disconnected')",
            name="ck_datadog_monitors_freshness_state",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "connector_account_id"],
            ["connector_accounts.workspace_id", "connector_accounts.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "team_entity_id"],
            ["pkos_nodes.workspace_id", "pkos_nodes.id"],
            name="fk_datadog_monitors_workspace_team_entity",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "connector_account_id",
            "external_id",
            name="uq_datadog_monitors_account_external_id",
        ),
    )
    op.create_index(
        "ix_datadog_monitors_workspace_account",
        "datadog_monitors",
        ["workspace_id", "connector_account_id"],
    )

    op.create_table(
        "datadog_service_definitions",
        *_projection_columns(),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("team", sa.String(500), nullable=True),
        sa.Column("tier", sa.String(100), nullable=True),
        sa.Column("description", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "provider IN ('datadog')", name="ck_datadog_service_definitions_provider"
        ),
        sa.CheckConstraint(
            "permission_state IN ('active', 'permission_lost', 'deleted')",
            name="ck_datadog_service_definitions_permission_state",
        ),
        sa.CheckConstraint(
            "freshness_state IN ('fresh', 'stale', 'disconnected')",
            name="ck_datadog_service_definitions_freshness_state",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "connector_account_id"],
            ["connector_accounts.workspace_id", "connector_accounts.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "team_entity_id"],
            ["pkos_nodes.workspace_id", "pkos_nodes.id"],
            name="fk_datadog_service_definitions_workspace_team_entity",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "connector_account_id",
            "external_id",
            name="uq_datadog_service_definitions_account_external_id",
        ),
    )
    op.create_index(
        "ix_datadog_service_definitions_workspace_account",
        "datadog_service_definitions",
        ["workspace_id", "connector_account_id"],
    )

    op.create_table(
        "datadog_dashboards",
        *_projection_columns(),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.CheckConstraint("provider IN ('datadog')", name="ck_datadog_dashboards_provider"),
        sa.CheckConstraint(
            "permission_state IN ('active', 'permission_lost', 'deleted')",
            name="ck_datadog_dashboards_permission_state",
        ),
        sa.CheckConstraint(
            "freshness_state IN ('fresh', 'stale', 'disconnected')",
            name="ck_datadog_dashboards_freshness_state",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "connector_account_id"],
            ["connector_accounts.workspace_id", "connector_accounts.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "team_entity_id"],
            ["pkos_nodes.workspace_id", "pkos_nodes.id"],
            name="fk_datadog_dashboards_workspace_team_entity",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "connector_account_id",
            "external_id",
            name="uq_datadog_dashboards_account_external_id",
        ),
    )
    op.create_index(
        "ix_datadog_dashboards_workspace_account",
        "datadog_dashboards",
        ["workspace_id", "connector_account_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_datadog_dashboards_workspace_account", table_name="datadog_dashboards")
    op.drop_table("datadog_dashboards")
    op.drop_index(
        "ix_datadog_service_definitions_workspace_account",
        table_name="datadog_service_definitions",
    )
    op.drop_table("datadog_service_definitions")
    op.drop_index("ix_datadog_monitors_workspace_account", table_name="datadog_monitors")
    op.drop_table("datadog_monitors")

    op.drop_constraint("ck_sync_cursors_resource_type", "sync_cursors", type_="check")
    op.create_check_constraint(
        "ck_sync_cursors_resource_type",
        "sync_cursors",
        "resource_type IN ('repository', 'work_item', 'change', 'review', "
        "'deployment', 'incident')",
    )
    op.drop_constraint("ck_connector_accounts_provider", "connector_accounts", type_="check")
    op.create_check_constraint(
        "ck_connector_accounts_provider",
        "connector_accounts",
        "provider IN ('github', 'gitlab', 'jira', 'sandbox')",
    )
