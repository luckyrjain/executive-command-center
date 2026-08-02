"""Phase 8 Task 3: schema-wide `owner_id`/`visibility` migration and the
`resource_grants` table (`docs/superpowers/specs/2026-08-01-phase-8-multi-
user-design.md` Decision 2, `docs/phases/phase-008/DATA-MODEL.md`).

`roles` (`owner|admin|member|viewer`) is a closed application-level enum,
not a database table -- see the design doc's own reasoning (a fixed,
bounded permission set per role is what makes the full role/resource/
action test matrix `TEST-PLAN.md` requires achievable as a literal finite
suite). No migration exists for it; `ecc.platform.authz` defines it as a
Python constant.

**Every workspace-scoped table gets `visibility`; only the ones that
lacked `owner_id` before this migration also get that column.** Phase 1's
four pre-existing `owner_id` tables (`tasks`, `commitments`, `notes`,
`risks`) get `visibility` only, defaulted `'workspace'`. Every Phase 7
personal-domain table already carrying `owner_id` (`personal_domains`,
`domain_consents`, `domain_sources`, `domain_records`, `goals`, `routines`,
`check_ins`, `personal_insights`, `cross_domain_grants`,
`personal_insight_feedback` -- Decision 2 item 3's own explicit
`_UNGRANTABLE_RESOURCE_TYPES` list, quoted verbatim) gets `visibility`
hardcoded `'private'`, not merely defaulted -- `ecc.platform.authz` rejects
any `resource_grants` row naming one of these `resource_type`s at write
time, so no role or grant can ever widen it.

**`deletion_jobs` is a disclosed judgment call, not an oversight.** It
already has `owner_id` (added in `0054_phase7_personal_domains.py`
alongside the ten UNGRANTABLE tables above) but is *not* one of Decision 2
item 3's explicitly-named ten -- the design doc's own enumeration leaves
it unaddressed. It is a housekeeping/audit record of a domain deletion
having occurred, not personal content itself (the underlying personal data
is already gone by the time this row exists), so it is treated the same
as Phase 1's own `owner_id` tables: `visibility` defaults `'workspace'`,
not hardcoded `'private'`.

**Every remaining `workspace_id`-scoped table without `owner_id` gets both
columns**, backfilled per `DATA-MODEL.md`'s own line ("backfilled
`owner_id` = the workspace's original sole `users` row"), split two ways:

- `morning_briefs`, `capacity_profiles`, `planning_constraints`, `plans`
  already carry a `user_id` column FK'd to `users.(workspace_id, id)` that
  means exactly what `owner_id` would -- backfilled `owner_id = user_id`
  directly rather than a fresh lookup, avoiding a second column that would
  only ever hold the same value as the first.
- Every other table is backfilled from a per-row correlated subquery
  against that row's own `workspace_id`, selecting the earliest-created
  `users` row in that workspace -- the same "sole pre-existing user
  becomes the de facto owner" backfill `0061_phase8_accounts_memberships.
  py` already used for `workspace_memberships`, applied here per-table
  instead of once.

**Six tables are deliberately excluded, not merely missed**, because none
represents a "content resource" `authorize()` would ever gate:
`users` (the owner-anchor table itself -- an owner column pointing to its
own table is incoherent), `sessions`/`workspace_memberships`/`invitations`
(identity/membership infrastructure -- the very mechanism `authorize()`
itself reads, which must not recursively depend on its own output),
`event_outbox` (system event-delivery infrastructure) and
`idempotency_records` (a request-dedup cache keyed by
`(workspace_id, actor_id, key)` with no surrogate `id` at all -- not a
content record ever independently read back through a `GET`).

**FK topology matches the direct-to-`users` pattern** every Phase 1
`owner_id` table already uses (`tasks`, `commitments`, `notes`, `risks`,
and Phase 7's `personal_domains`) -- not the indirect,
via-`personal_domains` pattern the other nine Phase 7 tables use, since
none of these ~50 new tables has an equivalent intermediate anchor table
of their own.

**No native Postgres `ENUM` type** -- `visibility` is `sa.String(20)` plus
a `CHECK (visibility IN ('private','shared_explicitly','workspace'))`,
matching every existing status/kind/classification column in this
codebase (confirmed zero uses of `sa.Enum`/`postgresql.ENUM` across all 62
prior migrations).

**`resource_grants`** (`id`, `workspace_id`, `grantee_account_id` -- FK to
`accounts.id` directly, since a grant targets a workspace-independent
identity per Decision 2's own wording, not a `users` row --
`resource_type`, `resource_id`, `actions` (non-empty text array, e.g.
`{read}` or `{read,write}` -- this codebase's `audit_events.changed_fields`
already establishes the same array-of-text-not-JSON convention),
`granted_by` (`users.id`, the granting *actor* within this workspace,
composite-FK'd to `users.(workspace_id, id)` the same way `invitations.
invited_by` already is), `expires_at`/`revoked_at` (both nullable --
"active" means neither has fired), `created_at`). `resource_type`/
`resource_id` are a polymorphic reference (no FK, deliberately -- the
referenced table varies per row), the same pattern `audit_events.
aggregate_type`/`aggregate_id` and `attention_items.entity_type`/
`entity_id` already establish in this codebase; `ecc.platform.authz`'s own
`_UNGRANTABLE_RESOURCE_TYPES` check (application-level, not a DB
constraint -- Decision 2's own wording) is what actually enforces the
personal-domain carve-out at grant-creation time.

**Why every new/widened column also gets a DB-level default, not just a
one-time backfill.** The plan's own Task 3/Task 4 split wires `authorize()`
end to end into `engineering` alone this task, deferring knowledge/
attention/automation/AI-runtime/calendar-scheduling to Task 4 ("mechanical
repetition ... across the remaining domains"). But this migration's own
`owner_id`/`visibility` columns are unavoidably schema-wide (the plan's own
Task 3 text), landing in one shot -- so between this migration and Task 4,
every one of those five still-unwired domains' *existing* `INSERT`
statements would otherwise start failing a `NOT NULL` violation the moment
this migration merges, months before that domain's own task ever touches
them. A one-time backfill only fixes rows that already exist; it does
nothing for rows a not-yet-updated domain inserts tomorrow.

`visibility` needs no correlation to the row's own other columns, so a
plain `server_default` (added via `alter_column` after the backfill, once
the value being defaulted is fixed per table) covers every future insert
everywhere, forever, with no per-callsite code required. `owner_id` cannot
use a plain column `DEFAULT` -- both backfill formulas (`t.user_id`, or the
workspace's earliest-created user) need to read another column of the very
row being inserted (`NEW.user_id`/`NEW.workspace_id`), which a `DEFAULT`
expression cannot reference. A `BEFORE INSERT` trigger can: two shared
trigger functions (`authz_default_owner_from_user_id`,
`authz_default_owner_from_workspace_original_user`) implement the exact
two backfill formulas above, attached per table in the same two groups the
backfill loop already uses. Each only fires `IF NEW.owner_id IS NULL` --
`engineering`'s own endpoints (wired this task) already set `owner_id`
explicitly on every insert, so the trigger is a pure no-op there; it exists
purely as a safety net for every domain Task 4 (and later tasks) haven't
reached yet, filling in the same value the migration's own backfill would
have used had that row existed before this migration ran. Once a later
task's own endpoint starts setting `owner_id` explicitly, the trigger
simply stops firing for that table -- no migration needed to retire it,
and no harm in leaving it in place indefinitely as a defense-in-depth
backstop.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0063_phase8_authz_visibility"
down_revision = "0062_phase8_invitations"
branch_labels = None
depends_on = None

_VISIBILITY_CHECK_SQL = "visibility IN ('private', 'shared_explicitly', 'workspace')"

# Phase 1's four pre-existing owner_id tables -- visibility only, 'workspace'.
_EXISTING_OWNER_WORKSPACE_DEFAULT = ("tasks", "commitments", "notes", "risks")

# See this module's own docstring for why `deletion_jobs` is treated as
# 'workspace', not one of the ten UNGRANTABLE tables below.
_EXISTING_OWNER_WORKSPACE_DEFAULT_PHASE7 = ("deletion_jobs",)

# Decision 2 item 3's own explicit UNGRANTABLE list, quoted verbatim.
_EXISTING_OWNER_PRIVATE_HARDCODED = (
    "personal_domains",
    "domain_consents",
    "domain_sources",
    "domain_records",
    "goals",
    "routines",
    "check_ins",
    "personal_insights",
    "cross_domain_grants",
    "personal_insight_feedback",
)

# Tables with workspace_id but no owner_id, where an existing `user_id`
# column already means what owner_id would -- backfilled owner_id = user_id.
_NEW_OWNER_FROM_USER_ID = ("morning_briefs", "capacity_profiles", "planning_constraints", "plans")

# Every other workspace_id-scoped table without owner_id -- backfilled to
# that workspace's own earliest-created users row.
_NEW_OWNER_FROM_WORKSPACE_ORIGINAL_USER = (
    "pkos_nodes",
    "pkos_edges",
    "pkos_evidence",
    "audit_events",
    "calendar_events",
    "meetings",
    "attention_items",
    "recommendations",
    "recommendation_feedback",
    "entity_aliases",
    "knowledge_claims",
    "timeline_entries",
    "resolution_candidates",
    "entity_operations",
    "retrieval_documents",
    "embedding_projections",
    "attention_feedback",
    "waiting_links",
    "risk_reviews",
    "plan_blocks",
    "meeting_participants",
    "meeting_packs",
    "ai_runs",
    "ai_run_steps",
    "evaluation_runs",
    "generated_artifacts",
    "workflow_definitions",
    "workflow_versions",
    "automation_policies",
    "triggers",
    "workflow_runs",
    "workflow_run_steps",
    "approval_requests",
    "compensation_steps",
    "automation_kill_switches",
    "connector_accounts",
    "sync_cursors",
    "sync_runs",
    "repositories",
    "engineering_work_items",
    "changes",
    "reviews",
    "delivery_metric_snapshots",
    "incidents",
    "engineering_decisions",
    "incident_changes",
    "decision_changes",
    "datadog_monitors",
    "datadog_service_definitions",
    "datadog_dashboards",
)

_ORIGINAL_WORKSPACE_USER_SUBQUERY = (
    "(SELECT u.id FROM users AS u WHERE u.workspace_id = t.workspace_id "
    "ORDER BY u.created_at ASC LIMIT 1)"
)

# Trigger-function names for the `owner_id` safety net -- see this module's
# own docstring ("Why every new/widened column also gets a DB-level
# default"). One function per backfill formula, shared across every table
# in that formula's group rather than one function per table.
_TRIGGER_FN_FROM_USER_ID = "authz_default_owner_from_user_id"
_TRIGGER_FN_FROM_WORKSPACE_ORIGINAL_USER = "authz_default_owner_from_workspace_original_user"


def _add_visibility(table: str, *, default: str) -> None:
    op.add_column(table, sa.Column("visibility", sa.String(20), nullable=True))
    op.execute(f"UPDATE {table} SET visibility = '{default}'")  # noqa: S608
    op.alter_column(table, "visibility", nullable=False, server_default=default)
    op.create_check_constraint(f"ck_{table}_visibility", table, _VISIBILITY_CHECK_SQL)


def _add_owner_and_visibility(table: str, *, backfill_sql: str, trigger_fn: str) -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.add_column(table, sa.Column("owner_id", uuid, nullable=True))
    op.execute(f"UPDATE {table} AS t SET owner_id = {backfill_sql}")  # noqa: S608
    op.alter_column(table, "owner_id", nullable=False)
    op.create_foreign_key(
        f"fk_{table}_workspace_owner",
        table,
        "users",
        ["workspace_id", "owner_id"],
        ["workspace_id", "id"],
    )
    op.execute(
        f"CREATE TRIGGER trg_{table}_default_owner BEFORE INSERT ON {table} "  # noqa: S608
        f"FOR EACH ROW EXECUTE FUNCTION {trigger_fn}()"
    )
    _add_visibility(table, default="workspace")


def _drop_visibility(table: str) -> None:
    op.drop_constraint(f"ck_{table}_visibility", table, type_="check")
    op.drop_column(table, "visibility")


def _drop_owner_and_visibility(table: str) -> None:
    _drop_visibility(table)
    op.execute(f"DROP TRIGGER trg_{table}_default_owner ON {table}")
    op.drop_constraint(f"fk_{table}_workspace_owner", table, type_="foreignkey")
    op.drop_column(table, "owner_id")


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    for table in _EXISTING_OWNER_WORKSPACE_DEFAULT + _EXISTING_OWNER_WORKSPACE_DEFAULT_PHASE7:
        _add_visibility(table, default="workspace")
    for table in _EXISTING_OWNER_PRIVATE_HARDCODED:
        _add_visibility(table, default="private")

    # See this module's own docstring ("Why every new/widened column also
    # gets a DB-level default") for why these two functions exist at all --
    # a `BEFORE INSERT` safety net for every domain Task 4+ hasn't reached
    # yet, not something either backfill loop below could do with a plain
    # column DEFAULT.
    op.execute(
        f"""
        CREATE FUNCTION {_TRIGGER_FN_FROM_USER_ID}() RETURNS trigger AS $$
        BEGIN
            IF NEW.owner_id IS NULL THEN
                NEW.owner_id := NEW.user_id;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        f"""
        CREATE FUNCTION {_TRIGGER_FN_FROM_WORKSPACE_ORIGINAL_USER}() RETURNS trigger AS $$
        BEGIN
            IF NEW.owner_id IS NULL THEN
                SELECT u.id INTO NEW.owner_id FROM users AS u
                WHERE u.workspace_id = NEW.workspace_id ORDER BY u.created_at ASC LIMIT 1;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )

    for table in _NEW_OWNER_FROM_USER_ID:
        _add_owner_and_visibility(
            table, backfill_sql="t.user_id", trigger_fn=_TRIGGER_FN_FROM_USER_ID
        )
    for table in _NEW_OWNER_FROM_WORKSPACE_ORIGINAL_USER:
        _add_owner_and_visibility(
            table,
            backfill_sql=_ORIGINAL_WORKSPACE_USER_SUBQUERY,
            trigger_fn=_TRIGGER_FN_FROM_WORKSPACE_ORIGINAL_USER,
        )

    op.create_table(
        "resource_grants",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("grantee_account_id", uuid, nullable=False),
        sa.Column("resource_type", sa.String(100), nullable=False),
        sa.Column("resource_id", uuid, nullable=False),
        sa.Column("actions", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("granted_by", uuid, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("cardinality(actions) > 0", name="ck_resource_grants_actions_nonempty"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["grantee_account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "granted_by"],
            ["users.workspace_id", "users.id"],
            name="fk_resource_grants_workspace_granted_by",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_resource_grants_workspace_resource",
        "resource_grants",
        ["workspace_id", "resource_type", "resource_id"],
    )
    op.create_index("ix_resource_grants_grantee", "resource_grants", ["grantee_account_id"])


def downgrade() -> None:
    op.drop_index("ix_resource_grants_grantee", table_name="resource_grants")
    op.drop_index("ix_resource_grants_workspace_resource", table_name="resource_grants")
    op.drop_table("resource_grants")

    for table in reversed(_NEW_OWNER_FROM_WORKSPACE_ORIGINAL_USER):
        _drop_owner_and_visibility(table)
    for table in reversed(_NEW_OWNER_FROM_USER_ID):
        _drop_owner_and_visibility(table)

    op.execute(f"DROP FUNCTION {_TRIGGER_FN_FROM_WORKSPACE_ORIGINAL_USER}()")
    op.execute(f"DROP FUNCTION {_TRIGGER_FN_FROM_USER_ID}()")

    for table in reversed(_EXISTING_OWNER_PRIVATE_HARDCODED):
        _drop_visibility(table)
    for table in reversed(
        _EXISTING_OWNER_WORKSPACE_DEFAULT + _EXISTING_OWNER_WORKSPACE_DEFAULT_PHASE7
    ):
        _drop_visibility(table)
