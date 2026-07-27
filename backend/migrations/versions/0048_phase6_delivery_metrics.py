"""Create changes, reviews and delivery_metric_snapshots for Phase 6
Engineering Workspace Task 5 (delivery and reliability metrics).

Numbered ``0048`` -- next-in-chain after ``0047_phase6_work_items.py``.

**Task 5's own scope, not every remaining `DATA-MODEL.md` table.**
`deployments`/`incidents`/`engineering_decisions`/`service_links`/
`source_tombstones` still do not exist -- this migration lands only what
Task 5's own code populates: `changes` and `reviews` (via
`github_adapter.py`'s new `_sync_changes`/`_sync_reviews`, GitHub only
this task -- see that module's own docstring for why GitLab changes/
reviews sync is deferred to a Task 5 follow-up) and
`delivery_metric_snapshots` (the metric-computation engine's output).
Matches every prior Phase 6 migration's identical "land exactly what the
current task populates" precedent.

**Also adds `engineering_work_items.provider_created_at`**, one column on
an existing table rather than a table of its own -- Task 5's own `work_
ageing` metric needs each item's real creation date (Jira's `fields.
created`), a genuine gap Task 4 left: `engineering_work_items` stored
`provider_updated_at` but never the issue's own opening date, since no
Task 4 code path needed it. Approximating age from this row's own
`observed_at`/`created_at` (when *this system* first saw the issue,
possibly long after it was actually opened) instead of fixing the real
gap would make `work_ageing` silently wrong for every issue backfilled
after it was already old -- exactly the "never fabricate a number that
looks plausible but isn't backed by real data" failure mode this
codebase's whole design consistently refuses elsewhere (`jira_adapter.py`
now also populates this column for both new writes and re-synced
existing rows; see that module's own docstring).

**`changes` and `reviews` mirror `repositories`'/`engineering_work_items`'
shape** (`DATA-MODEL.md`'s "every projection stores provider, external
ID, source URL, observed/updated times, permission/freshness state and
raw-content hash" rule), each extended with the fields specific to what
they represent:

- `changes.repository_id` is a real FK to `repositories.id` (not an
  unresolved raw external identifier, unlike `engineering_work_items`'
  `reporter_external_id`/`assignee_external_id`) -- a change's owning
  repository is never ambiguous the way a Jira reporter's identity is;
  `github_adapter.py`'s own sync already holds the repository row it
  fanned out from, so storing the real FK costs nothing extra and is
  strictly more useful than a raw string.
- `changes.author_external_id` is left as a raw, unresolved provider
  identifier (a GitHub user id), matching `engineering_work_items`'
  established "ambiguous external identity, Task 6 resolution scope"
  precedent for the identical reason -- resolving it against a Phase 2
  `Person` entity is not this task's concern.
- `changes.provider_number` is GitHub's human-visible PR `number`
  (distinct from `external_id`, which stores the immutable `id` --
  `jira_adapter.py`'s established "id, not the human-visible/renamable
  key, is the real identity" precedent applies equally here: a PR's
  `number` is stable in practice but is not what this schema treats as
  identity). `_sync_reviews` needs it to build `GET /repos/{owner}/
  {repo}/pulls/{number}/reviews`'s URL, which addresses by `number`, not
  `id` -- stored at write time since `_sync_changes` already has it,
  rather than making `_sync_reviews` re-derive it.
- `reviews.change_id` is a real FK to `changes.id`, for the same reason
  `changes.repository_id` is real: never ambiguous, always known at
  write time.
- `reviews.reviewer_external_id` is likewise a raw, unresolved identifier.
- `reviews.review_state` is the provider's own review-state vocabulary
  (`approved`/`changes_requested`/`commented`/`dismissed`/`pending` for
  GitHub -- see `github_adapter.py`'s own docstring for the exact
  mapping), not a value this schema itself constrains via CHECK, since a
  future GitLab reviews implementation's vocabulary genuinely differs
  (approvals only, no `changes_requested` concept) and forcing a shared
  enum now would either reject real GitLab values later or silently
  coerce them -- stored as free text instead, deliberately.

**`delivery_metric_snapshots`' column set is `DELIVERY-INTELLIGENCE-
CONTRACT.md`'s own field list made concrete**: `metric_definition_id`/
`version` (split here into `metric_key` + `definition_version`, since a
later redefinition of one metric's computation creates a new
`(metric_key, definition_version)` pair and never rewrites a historical
snapshot -- immutable, insert-only, no update/delete path from any
endpoint), `window`, `numerator`, `denominator`, `population` and
`coverage` (split into `coverage_status` -- the closed `complete`/
`partial`/`insufficient_coverage` enum -- plus `coverage_percentage` and
`coverage_gap_description`, the disclosed-gap text
`DELIVERY-INTELLIGENCE-CONTRACT.md` requires displayed alongside a
`partial` number). `value` is the metric's own final computed number
(a rate, a duration in seconds, a count) -- `NULL` when `coverage_status
= 'insufficient_coverage'`, per the contract's "no numeric value" rule.
`details_json` holds a small JSON-encoded object for a metric whose full
shape genuinely doesn't reduce to one scalar `value` -- work ageing's own
contract definition is "distribution... bucketed," not a single number;
`value` still carries a convenient scalar summary (the bucketed
distribution's median) alongside the full bucket counts in
`details_json`, rather than adding a second table for the one metric
that needs more than a scalar.

**`aggregation_scope` is a closed enum of exactly `system`/`team`/
`workstream` per Decision 3**, but this task's own engine only ever
writes `system` -- no `Team`/`Workstream` entity exists anywhere in this
codebase yet (confirmed by repo survey) for a `team`/`workstream`-scoped
snapshot to group by. Accepted limitation, disclosed in
`docs/phases/phase-006/DELIVERY-INTELLIGENCE-CONTRACT.md`'s own "Task 5
status" section, not silently narrowed at the schema level -- the column
itself still permits all three values for whichever later task adds
team/workstream modeling.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0048_phase6_delivery_metrics"
down_revision = "0047_phase6_work_items"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.create_table(
        "changes",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("connector_account_id", uuid, nullable=False),
        sa.Column("repository_id", uuid, nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("provider_number", sa.String(50), nullable=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("status", sa.String(50), nullable=True),
        sa.Column("author_external_id", sa.String(255), nullable=True),
        sa.Column("provider_created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("merged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("permission_state", sa.String(20), nullable=False, server_default="active"),
        sa.Column("freshness_state", sa.String(20), nullable=False, server_default="fresh"),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("provider_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "provider IN ('github', 'gitlab', 'jira', 'sandbox')",
            name="ck_changes_provider",
        ),
        sa.CheckConstraint(
            "permission_state IN ('active', 'permission_lost', 'deleted')",
            name="ck_changes_permission_state",
        ),
        sa.CheckConstraint(
            "freshness_state IN ('fresh', 'stale', 'disconnected')",
            name="ck_changes_freshness_state",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "connector_account_id"],
            ["connector_accounts.workspace_id", "connector_accounts.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["repository_id"], ["repositories.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "workspace_id",
            "connector_account_id",
            "external_id",
            name="uq_changes_account_external_id",
        ),
    )
    op.create_index(
        "ix_changes_workspace_account", "changes", ["workspace_id", "connector_account_id"]
    )
    op.create_index("ix_changes_repository", "changes", ["repository_id"])

    op.create_table(
        "reviews",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("connector_account_id", uuid, nullable=False),
        sa.Column("change_id", uuid, nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("external_id", sa.String(255), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("reviewer_external_id", sa.String(255), nullable=True),
        sa.Column("review_state", sa.String(50), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("permission_state", sa.String(20), nullable=False, server_default="active"),
        sa.Column("freshness_state", sa.String(20), nullable=False, server_default="fresh"),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("provider_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "provider IN ('github', 'gitlab', 'jira', 'sandbox')",
            name="ck_reviews_provider",
        ),
        sa.CheckConstraint(
            "permission_state IN ('active', 'permission_lost', 'deleted')",
            name="ck_reviews_permission_state",
        ),
        sa.CheckConstraint(
            "freshness_state IN ('fresh', 'stale', 'disconnected')",
            name="ck_reviews_freshness_state",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "connector_account_id"],
            ["connector_accounts.workspace_id", "connector_accounts.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["change_id"], ["changes.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "workspace_id",
            "connector_account_id",
            "external_id",
            name="uq_reviews_account_external_id",
        ),
    )
    op.create_index(
        "ix_reviews_workspace_account", "reviews", ["workspace_id", "connector_account_id"]
    )
    op.create_index("ix_reviews_change", "reviews", ["change_id"])

    op.create_table(
        "delivery_metric_snapshots",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("metric_key", sa.String(50), nullable=False),
        sa.Column("definition_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("aggregation_scope", sa.String(20), nullable=False, server_default="system"),
        sa.Column("window_label", sa.String(20), nullable=False),
        sa.Column("window_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("window_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("population", sa.Integer(), nullable=False),
        sa.Column("numerator", sa.Numeric(), nullable=True),
        sa.Column("denominator", sa.Numeric(), nullable=True),
        sa.Column("value", sa.Numeric(), nullable=True),
        sa.Column("details_json", sa.Text(), nullable=True),
        sa.Column("coverage_status", sa.String(30), nullable=False),
        sa.Column("coverage_percentage", sa.Numeric(), nullable=False),
        sa.Column("coverage_gap_description", sa.Text(), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "metric_key IN ('delivery_frequency', 'lead_time_for_changes', "
            "'change_failure_rate', 'time_to_restore', 'work_ageing', "
            "'blocked_work', 'review_latency')",
            name="ck_delivery_metric_snapshots_metric_key",
        ),
        sa.CheckConstraint(
            "aggregation_scope IN ('system', 'team', 'workstream')",
            name="ck_delivery_metric_snapshots_aggregation_scope",
        ),
        sa.CheckConstraint(
            "coverage_status IN ('complete', 'partial', 'insufficient_coverage')",
            name="ck_delivery_metric_snapshots_coverage_status",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_delivery_metric_snapshots_workspace_metric",
        "delivery_metric_snapshots",
        ["workspace_id", "metric_key", "computed_at"],
    )

    op.add_column(
        "engineering_work_items",
        sa.Column("provider_created_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("engineering_work_items", "provider_created_at")
    op.drop_index(
        "ix_delivery_metric_snapshots_workspace_metric",
        table_name="delivery_metric_snapshots",
    )
    op.drop_table("delivery_metric_snapshots")
    op.drop_index("ix_reviews_change", table_name="reviews")
    op.drop_index("ix_reviews_workspace_account", table_name="reviews")
    op.drop_table("reviews")
    op.drop_index("ix_changes_repository", table_name="changes")
    op.drop_index("ix_changes_workspace_account", table_name="changes")
    op.drop_table("changes")
