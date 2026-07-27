"""Create incidents, engineering_decisions and their change-correlation
join tables for Phase 6 Engineering Workspace Task 6 (decisions,
incidents and knowledge linking).

Numbered ``0049`` -- next-in-chain after ``0048_phase6_delivery_metrics.py``.

**Task 6's own scope, not every remaining `DATA-MODEL.md` table.**
`deployments`/`service_links`/`source_tombstones` still do not exist --
`deployments` in particular has no task assigned to it at all
(`DATA-MODEL.md`'s own note), which is why `incidents` here has no
correlation to it: `change_failure_rate`'s own contract population
("deployments followed by a linked incident within 24h") stays
`insufficient_coverage` even after this migration, since it needs
`deployments` too, not just `incidents`. Only `time_to_restore` (needs
only resolved incidents) becomes computable.

**`incidents` and `engineering_decisions` are NOT synced-content
projections** -- unlike every other Phase 6 table so far (`repositories`,
`engineering_work_items`, `changes`, `reviews`), neither is written by a
connector adapter's sync call. Both are workspace-authored records (an
operator captures "we had an incident" / "we decided X"), the identical
shape Phase 2's own `pkos_nodes`-adjacent knowledge-capture tables
(notes, claims) already use: no `provider`/`external_id`/`source_url`/
`content_hash` columns (there is no external source to dedupe against),
but the same `version`/`created_by`/`updated_by` optimistic-concurrency
and audit shape `connector_accounts` (migration `0044`) established,
since these rows ARE human-editable, unlike a sync-only projection row.

**Correlation to `changes` is a real join table, not an array column.**
`incident_changes`/`decision_changes` each hold `(workspace_id,
incident_id/decision_id, change_id)` with real FK constraints (`ON
DELETE CASCADE`) -- consistent with `changes.repository_id`'s own
precedent of a real FK wherever the linked row's identity is
unambiguous, rather than an unenforceable `uuid[]` array column that
couldn't uphold referential integrity across a `changes` row being
deleted. The `incident_id`/`decision_id` side of each join table uses a
genuine composite `(workspace_id, id)` FK -- `incidents`/`engineering_
decisions` each add their own `UNIQUE(workspace_id, id)` constraint for
exactly that (the same "a composite FK target must itself be unique on
the composite key" reasoning migration `0044`'s own docstring already
explains for `connector_accounts`). The `change_id` side is a plain FK
to `changes.id` only, matching `reviews.change_id`'s own identical,
already-accepted precedent -- `changes` itself has no `UNIQUE(workspace_
id, id)` constraint for a composite FK to target (migration `0048` never
added one), so workspace scoping for that one relationship is enforced
at the query layer, not the schema layer, same as `reviews`.

**Work-item correlation is deferred, disclosed.** Only `changes` are
linked in this migration -- an incident/decision citing a work item
(e.g. "resolved by JIRA-123") is equally real and valuable, but adding a
second pair of join tables (`incident_work_items`/`decision_work_items`)
without a concrete Task 6 caller populating them yet is speculative
scope; deferred to a follow-up once real usage need is established.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0049_phase6_decisions_incidents"
down_revision = "0048_phase6_delivery_metrics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.create_table(
        "incidents",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("severity", sa.String(20), nullable=False, server_default="medium"),
        sa.Column("status", sa.String(20), nullable=False, server_default="open"),
        sa.Column("detected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_by", uuid, nullable=False),
        sa.Column("updated_by", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "severity IN ('low', 'medium', 'high', 'critical')",
            name="ck_incidents_severity",
        ),
        sa.CheckConstraint(
            "status IN ('open', 'resolved')",
            name="ck_incidents_status",
        ),
        sa.CheckConstraint(
            "(status = 'open' AND resolved_at IS NULL) OR "
            "(status = 'resolved' AND resolved_at IS NOT NULL)",
            name="ck_incidents_resolved_at_matches_status",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "created_by"], ["users.workspace_id", "users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "updated_by"], ["users.workspace_id", "users.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_incidents_workspace_id"),
    )
    op.create_index("ix_incidents_workspace_detected", "incidents", ["workspace_id", "detected_at"])

    op.create_table(
        "engineering_decisions",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="proposed"),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_by", uuid, nullable=False),
        sa.Column("updated_by", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('proposed', 'decided', 'superseded')",
            name="ck_engineering_decisions_status",
        ),
        sa.CheckConstraint(
            "(status = 'proposed' AND decided_at IS NULL) OR "
            "(status IN ('decided', 'superseded') AND decided_at IS NOT NULL)",
            name="ck_engineering_decisions_decided_at_matches_status",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "created_by"], ["users.workspace_id", "users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "updated_by"], ["users.workspace_id", "users.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_engineering_decisions_workspace_id"),
    )
    op.create_index(
        "ix_engineering_decisions_workspace_status",
        "engineering_decisions",
        ["workspace_id", "status"],
    )

    op.create_table(
        "incident_changes",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("incident_id", uuid, nullable=False),
        sa.Column("change_id", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "incident_id"],
            ["incidents.workspace_id", "incidents.id"],
            ondelete="CASCADE",
        ),
        # `change_id` -> `changes.id` only (not a composite `(workspace_id,
        # change_id)` FK) -- `changes` itself has no `UNIQUE(workspace_id,
        # id)` constraint for a composite FK to target (migration `0048`
        # never added one), matching `reviews.change_id`'s own identical,
        # already-accepted precedent (a plain FK, with workspace scoping
        # enforced at the query layer, not the schema layer, for this one
        # relationship -- see that migration's own review history).
        sa.ForeignKeyConstraint(["change_id"], ["changes.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "workspace_id", "incident_id", "change_id", name="uq_incident_changes_pair"
        ),
    )
    op.create_index("ix_incident_changes_incident", "incident_changes", ["incident_id"])
    op.create_index("ix_incident_changes_change", "incident_changes", ["change_id"])

    op.create_table(
        "decision_changes",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("decision_id", uuid, nullable=False),
        sa.Column("change_id", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "decision_id"],
            ["engineering_decisions.workspace_id", "engineering_decisions.id"],
            ondelete="CASCADE",
        ),
        # See `incident_changes`'s own identical comment above.
        sa.ForeignKeyConstraint(["change_id"], ["changes.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "workspace_id", "decision_id", "change_id", name="uq_decision_changes_pair"
        ),
    )
    op.create_index("ix_decision_changes_decision", "decision_changes", ["decision_id"])
    op.create_index("ix_decision_changes_change", "decision_changes", ["change_id"])


def downgrade() -> None:
    op.drop_index("ix_decision_changes_change", table_name="decision_changes")
    op.drop_index("ix_decision_changes_decision", table_name="decision_changes")
    op.drop_table("decision_changes")
    op.drop_index("ix_incident_changes_change", table_name="incident_changes")
    op.drop_index("ix_incident_changes_incident", table_name="incident_changes")
    op.drop_table("incident_changes")
    op.drop_index("ix_engineering_decisions_workspace_status", table_name="engineering_decisions")
    op.drop_table("engineering_decisions")
    op.drop_index("ix_incidents_workspace_detected", table_name="incidents")
    op.drop_table("incidents")
