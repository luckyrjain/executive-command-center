"""Create the Phase 7 domain/consent/vault framework tables for Task 1
(domain/consent/vault framework and the `habits` reference domain --
platform layer plus the one reference domain, not the full six-domain
`DATA-MODEL.md` scope).

Phase 7 Task 1 (`docs/superpowers/specs/2026-07-31-phase-7-personal-
intelligence-design.md`, `docs/phases/phase-007/DATA-MODEL.md` v0.2.0).

**Only the framework tables plus `habits`'s own two tables land here, not
every table Phase 7's `DATA-MODEL.md` eventually needs.** `cross_domain_
grants` is added by the task that first needs it (design doc Decision 1,
item 5) -- mirrors Phase 6 Task 1's own precedent (`0044_phase6_connector_
platform.py`'s docstring) of shipping only the tables this activation's
own scope populates, not the full phase's eventual table list upfront.

**Workspace *and* owner scoped, standard actor/audit shape.** Every table
carries `workspace_id` FK'd to `workspaces.id` (`ondelete="CASCADE"`) --
the tenant boundary every table in this codebase uses -- plus `owner_id`
FK'd to `users.(workspace_id, id)`, mirroring `notes.py`'s own `owner_id`
precedent (migration `0004_phase1_notes.py`) for personal, single-user-
owned content within a workspace. `created_by`/`updated_by` (composite FK
to `users.(workspace_id, id)`, `ondelete="RESTRICT"`) follow the same
established shape as every prior-phase table (`connector_accounts`,
migration `0044`; `notes`, migration `0004`).

**`personal_domains` is the enablement record, not a domain catalog.**
The set of valid `domain_key` values is a `CHECK` constraint enum (design
doc Decision 1), not a separate lookup table -- there is nothing about a
domain's own definition (its schema, its classification) that varies per
workspace/owner, so a catalog table would only ever hold six static rows
duplicated identically across every workspace. One row per
`(workspace_id, owner_id, domain_key)` records this owner's own
enablement/classification state for that domain.

**`domain_records` encryption is field-level, not column-level, and not
this migration's own concern.** Design doc Decision 3: `sensitive`/
`high_stakes`-classified narrative fields inside `payload` (JSONB) are
individually Fernet-encrypted by application code before write;
structured, low-sensitivity fields on the same record stay plaintext.
`habits` (this task's own reference domain) is `standard`-classified, so
no `domain_records` row this task actually writes needs any encrypted
field at all -- `ecc.domains.personal.crypto`'s own encryption helpers are
therefore deferred to the task that first needs them (`relationships`,
the first `sensitive`-classified domain), not built speculatively here
with no real caller. `payload` itself is a plain `JSONB` column
regardless -- encryption, when a later domain needs it, operates on
individual keys *within* the JSON document, not the column type.

**`check_ins` is append-only, no `version`/`updated_at`.** A check-in is a
discrete, immutable logged event ("I did this routine on this date"), not
a record a user edits after the fact -- unlike every other mutable table
here, it has no optimistic-concurrency column because there is no update
path to guard.

**`personal_insights` is upserted by a deterministic `insight_key`, not
inserted fresh on every computation.** Task 1 ships zero AI-generated
insights (design doc Decision 4) -- every insight here is a deterministic
computation from `routines`/`check_ins` (e.g. a check-in gap), recomputed
on each `GET /personal/insights` call. `UNIQUE(workspace_id, owner_id,
insight_key)` lets that recomputation `UPSERT` the *content* fields
(`evidence`/`limitations`/`computed_at`) while leaving `dismissed_at`
untouched on conflict -- a user's dismissal must survive the next
recomputation of the same underlying gap, not be silently un-dismissed by
it.

**No cascading soft-delete flag on `domain_records`/`goals`/`routines`/
`check_ins`.** `API-SCHEMAS.md` names no per-record delete endpoint this
task implements -- only `POST /personal/domains/{id}/delete` (a whole-
domain delete). `ecc.domains.personal.export_deletion` hard-deletes every
row across these four tables for the given `(workspace_id, owner_id,
domain_key)` in one transaction and records the event in `deletion_jobs`;
no row in any of these four tables is ever soft-deleted, so no query
anywhere else in this task needs a `WHERE deleted_at IS NULL` filter.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0054_phase7_personal_domains"
down_revision = "0053_phase4_expl_item_prompt_v2"
branch_labels = None
depends_on = None

_DOMAIN_KEYS = "'habits', 'learning', 'travel', 'relationships', 'health', 'finance'"
_CLASSIFICATIONS = "'standard', 'sensitive', 'high_stakes'"


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    # --- personal_domains -------------------------------------------------

    op.create_table(
        "personal_domains",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("owner_id", uuid, nullable=False),
        sa.Column("domain_key", sa.String(20), nullable=False),
        sa.Column("classification", sa.String(20), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("enabled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", uuid, nullable=False),
        sa.Column("updated_by", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.CheckConstraint(
            f"domain_key IN ({_DOMAIN_KEYS})", name="ck_personal_domains_domain_key"
        ),
        sa.CheckConstraint(
            f"classification IN ({_CLASSIFICATIONS})", name="ck_personal_domains_classification"
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "owner_id"], ["users.workspace_id", "users.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "created_by"], ["users.workspace_id", "users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "updated_by"], ["users.workspace_id", "users.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "workspace_id", "owner_id", "domain_key", name="uq_personal_domains_owner_domain"
        ),
    )

    # --- domain_consents ----------------------------------------------------

    op.create_table(
        "domain_consents",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("owner_id", uuid, nullable=False),
        sa.Column("domain_key", sa.String(20), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "owner_id", "domain_key"],
            [
                "personal_domains.workspace_id",
                "personal_domains.owner_id",
                "personal_domains.domain_key",
            ],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_domain_consents_owner_domain",
        "domain_consents",
        ["workspace_id", "owner_id", "domain_key"],
    )

    # --- domain_sources -------------------------------------------------------

    op.create_table(
        "domain_sources",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("owner_id", uuid, nullable=False),
        sa.Column("domain_key", sa.String(20), nullable=False),
        sa.Column("source_type", sa.String(20), nullable=False),
        sa.Column("label", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "source_type IN ('manual', 'imported_file')", name="ck_domain_sources_source_type"
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "owner_id", "domain_key"],
            [
                "personal_domains.workspace_id",
                "personal_domains.owner_id",
                "personal_domains.domain_key",
            ],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_domain_sources_workspace_id"),
    )

    # --- domain_records -------------------------------------------------------

    op.create_table(
        "domain_records",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("owner_id", uuid, nullable=False),
        sa.Column("domain_key", sa.String(20), nullable=False),
        sa.Column("record_type", sa.String(50), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("domain_source_id", uuid, nullable=True),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retention_acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", uuid, nullable=False),
        sa.Column("updated_by", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "owner_id", "domain_key"],
            [
                "personal_domains.workspace_id",
                "personal_domains.owner_id",
                "personal_domains.domain_key",
            ],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "domain_source_id"],
            ["domain_sources.workspace_id", "domain_sources.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "created_by"], ["users.workspace_id", "users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "updated_by"], ["users.workspace_id", "users.id"], ondelete="RESTRICT"
        ),
    )
    op.create_index(
        "ix_domain_records_owner_domain_effective",
        "domain_records",
        ["workspace_id", "owner_id", "domain_key", sa.text("effective_at DESC")],
    )

    # --- goals ------------------------------------------------------------------

    op.create_table(
        "goals",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("owner_id", uuid, nullable=False),
        sa.Column("domain_key", sa.String(20), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("target_count", sa.Integer(), nullable=True),
        sa.Column("target_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("created_by", uuid, nullable=False),
        sa.Column("updated_by", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.CheckConstraint(
            "status IN ('active', 'completed', 'abandoned')", name="ck_goals_status"
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "owner_id", "domain_key"],
            [
                "personal_domains.workspace_id",
                "personal_domains.owner_id",
                "personal_domains.domain_key",
            ],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "created_by"], ["users.workspace_id", "users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "updated_by"], ["users.workspace_id", "users.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_goals_workspace_id"),
    )

    # --- routines ---------------------------------------------------------------

    op.create_table(
        "routines",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("owner_id", uuid, nullable=False),
        sa.Column("domain_key", sa.String(20), nullable=False),
        sa.Column("goal_id", uuid, nullable=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("cadence", sa.String(20), nullable=False),
        sa.Column("created_by", uuid, nullable=False),
        sa.Column("updated_by", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("cadence IN ('daily', 'weekly', 'monthly')", name="ck_routines_cadence"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "owner_id", "domain_key"],
            [
                "personal_domains.workspace_id",
                "personal_domains.owner_id",
                "personal_domains.domain_key",
            ],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "goal_id"], ["goals.workspace_id", "goals.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "created_by"], ["users.workspace_id", "users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "updated_by"], ["users.workspace_id", "users.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_routines_workspace_id"),
    )

    # --- check_ins ------------------------------------------------------------

    op.create_table(
        "check_ins",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("owner_id", uuid, nullable=False),
        sa.Column("domain_key", sa.String(20), nullable=False),
        sa.Column("routine_id", uuid, nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("note", sa.String(500), nullable=True),
        sa.Column("created_by", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "owner_id", "domain_key"],
            [
                "personal_domains.workspace_id",
                "personal_domains.owner_id",
                "personal_domains.domain_key",
            ],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "routine_id"],
            ["routines.workspace_id", "routines.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "created_by"], ["users.workspace_id", "users.id"], ondelete="RESTRICT"
        ),
    )
    op.create_index(
        "ix_check_ins_routine_occurred",
        "check_ins",
        ["workspace_id", "routine_id", sa.text("occurred_at DESC")],
    )

    # --- deletion_jobs --------------------------------------------------------

    op.create_table(
        "deletion_jobs",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("owner_id", uuid, nullable=False),
        sa.Column("domain_key", sa.String(20), nullable=False),
        sa.Column("scope", sa.String(20), nullable=False, server_default="domain"),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", uuid, nullable=False),
        sa.CheckConstraint("scope IN ('domain')", name="ck_deletion_jobs_scope"),
        sa.CheckConstraint("status IN ('pending', 'completed')", name="ck_deletion_jobs_status"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "created_by"], ["users.workspace_id", "users.id"], ondelete="RESTRICT"
        ),
    )
    op.create_index(
        "ix_deletion_jobs_owner_domain",
        "deletion_jobs",
        ["workspace_id", "owner_id", "domain_key"],
    )

    # --- personal_insights ----------------------------------------------------

    op.create_table(
        "personal_insights",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("owner_id", uuid, nullable=False),
        sa.Column("domain_key", sa.String(20), nullable=False),
        sa.Column("insight_key", sa.String(255), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("source_period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("missing_data", sa.Text(), nullable=True),
        sa.Column("confidence", sa.String(10), nullable=False),
        sa.Column("limitations", sa.Text(), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('observation', 'trend', 'correlation', 'reminder', 'planning_suggestion')",
            name="ck_personal_insights_kind",
        ),
        sa.CheckConstraint(
            "confidence IN ('low', 'medium', 'high')", name="ck_personal_insights_confidence"
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "owner_id", "domain_key"],
            [
                "personal_domains.workspace_id",
                "personal_domains.owner_id",
                "personal_domains.domain_key",
            ],
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "workspace_id", "owner_id", "insight_key", name="uq_personal_insights_owner_key"
        ),
    )


def downgrade() -> None:
    op.drop_table("personal_insights")
    op.drop_index("ix_deletion_jobs_owner_domain", table_name="deletion_jobs")
    op.drop_table("deletion_jobs")
    op.drop_index("ix_check_ins_routine_occurred", table_name="check_ins")
    op.drop_table("check_ins")
    op.drop_table("routines")
    op.drop_table("goals")
    op.drop_index("ix_domain_records_owner_domain_effective", table_name="domain_records")
    op.drop_table("domain_records")
    op.drop_table("domain_sources")
    op.drop_index("ix_domain_consents_owner_domain", table_name="domain_consents")
    op.drop_table("domain_consents")
    op.drop_table("personal_domains")
