"""Add team linkage to repositories and engineering_work_items (Phase 6
follow-up: "connect the `team` EntityKind across all integrations").

Numbered ``0050`` -- next-in-chain after ``0049_phase6_decisions_incidents.py``.

**Why this exists.** `0011_phase2_knowledge_entities.py`'s `EntityKind`
enum gained a `"team"` value in a prior, narrower change (`backend/ecc/
domains/knowledge/entities.py`), but that change deliberately stopped at
the `Literal` itself -- no `repository`/`engineering_work_item`/`change`/
`review` row had any way to say which team owns it, and `delivery_metric_
snapshots.aggregation_scope`'s `'team'` enum value (migration `0048`) has
been unreachable ever since for the identical reason: the entity existed,
nothing pointed at it. This migration closes that gap for the two tables
where "which team owns this" is a real, useful question with no existing
FK to lean on: `repositories` and `engineering_work_items`. `changes`
already has a real FK to `repositories` (`repository_id`) and `reviews` to
`changes` (`change_id`) -- both inherit their owning team transitively
through that existing chain, so neither needs its own copy of this column
(duplicating it there would just be two more places a manual reassignment
has to remember to update).

**Two columns per table, not one, following the "hybrid: auto-suggest,
human confirms" design the change linking `team` to the connector
integrations settled on:**

- `suggested_team_name` (plain text, sync-writable): a provider adapter's
  own best-effort guess at the owning org/group/project name --
  `github_adapter.py` now writes the repository's `owner.login`,
  `gitlab_adapter.py` the project's `namespace.name`, `jira_adapter.py`
  the issue's `fields.project.name`. Refreshed on every sync call, exactly
  like every other provider-sourced column on these tables. **Not a link
  to anything** -- a free-text hint a human reads, never a foreign key,
  since a raw provider org/group/project name is not itself a `pkos_nodes`
  row and auto-creating one from it would silently manufacture an entity
  from unverified sync data (this codebase's identity-resolution design
  never auto-creates or auto-merges entities from ambiguous evidence --
  `resolution_candidates` exists precisely because "looks like the same
  team" needs a human's confirmation, not an assumption).
- `team_entity_id` (composite FK to `pkos_nodes`, nullable, sync-silent):
  the confirmed link, set only through the two new `POST .../team`
  endpoints (`connector_accounts.py`) that require a human-supplied
  `team_entity_id` and validate it against a real, active,
  `kind='team'` `pkos_nodes` row in the caller's own workspace -- the
  identical "manual, API-driven, existence/kind-checked" precedent
  `commitments.counterparty_person_id`/`waiting_links.counterparty_
  entity_id`/`meeting_participants.entity_id` already established
  (`DATA-MODEL.md`'s own reconciliation section). No sync/adapter code
  path ever writes this column -- a provider's own upsert `ON CONFLICT
  ... DO UPDATE SET` clause updates `suggested_team_name` on every
  re-sync but never mentions `team_entity_id`, so a confirmed assignment
  survives indefinitely across re-syncs even if the provider's own
  org/group/project name later changes or disappears from the payload.

**Composite FK shape mirrors `commitments.counterparty_person_id`'s own
precedent exactly**: nullable, `(workspace_id, team_entity_id) ->
(pkos_nodes.workspace_id, pkos_nodes.id)`, no explicit `ondelete` override
(defaults to `NO ACTION`) -- `pkos_nodes` rows are never actually deleted
(`DATA-MODEL.md`'s own invariant: "Confirmed merges redirect old IDs but
never reuse or delete them"), so an `ON DELETE` behavior never fires in
practice; matching the nullable/optional-link precedent rather than
`waiting_links`/`meeting_participants`' `NOT NULL, RESTRICT` shape, since
an unassigned team is the normal, expected state for most repositories
and work items today, not an error condition.

**Still deferred, disclosed, not silently expanded into this migration:**
`delivery_metric_snapshots` gains no `team_entity_id` column here -- the
metrics-computation engine (`metrics.py`) does not yet read either new
column or group any query by team, so a `team_entity_id` column on that
table would be exactly the same kind of unreachable-value column
`aggregation_scope`'s own `'team'` enum value already was before this
migration. Wiring the engine itself is real, separate follow-up work; see
`docs/phases/phase-006/DELIVERY-INTELLIGENCE-CONTRACT.md`'s updated
"Task 5 status" section.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0050_phase6_team_linkage"
down_revision = "0049_phase6_decisions_incidents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    for table in ("repositories", "engineering_work_items"):
        op.add_column(table, sa.Column("team_entity_id", uuid, nullable=True))
        op.add_column(table, sa.Column("suggested_team_name", sa.String(500), nullable=True))
        op.create_foreign_key(
            f"fk_{table}_workspace_team_entity",
            table,
            "pkos_nodes",
            ["workspace_id", "team_entity_id"],
            ["workspace_id", "id"],
        )
        op.create_index(f"ix_{table}_team_entity", table, ["workspace_id", "team_entity_id"])


def downgrade() -> None:
    for table in ("repositories", "engineering_work_items"):
        op.drop_index(f"ix_{table}_team_entity", table_name=table)
        op.drop_constraint(f"fk_{table}_workspace_team_entity", table, type_="foreignkey")
        op.drop_column(table, "suggested_team_name")
        op.drop_column(table, "team_entity_id")
