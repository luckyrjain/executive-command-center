"""Create `cross_domain_grants` for Phase 7 Task 5, part 1 (schema + grant
management endpoints -- the AI-generated `trend`/`correlation` insight
types that will consume these grants are part 2, a separate PR, per the
repository owner's own scope split for this task).

Phase 7 Task 5 (`docs/superpowers/specs/2026-07-31-phase-7-personal-
intelligence-design.md`, Decision 1 item 5; `docs/phases/phase-007/
DATA-MODEL.md`). `cross_domain_grants` was deliberately deferred out of
migration `0054_phase7_personal_domains.py` (that migration's own
docstring: "added by the task that first needs it") -- Tasks 2-4 (`learning`
/`travel`/`relationships`) never needed it, since each is a single,
independently-enabled domain with no cross-domain consumer.

**Schema, resolving `DATA-MODEL.md`'s own top-line description** ("Cross-
domain grants name source domain, target purpose, fields/categories and
expiry") **into concrete columns, since nothing more specific existed
before this task.** No `target_domain_key` column: the design doc's own
wording names a target *purpose*, not a second domain -- `purpose` (a
closed, extensible enum, one value defined this task: `'insight_
generation'`, the only named consumer in Decision 1 item 5) is what a
grant is *for*, not which other domain reads it. `granted_categories`
(JSONB array of strings) names which categories of `source_domain_key`'s
own data the grant covers (e.g. `record_type` names) -- loosely typed
since `domain_records.record_type` itself has no closed enum either
(`0054`'s own docstring: a `String(50)`, not a `CHECK` constraint).
`expires_at` is nullable (an indefinite grant is valid) but, when set,
time-bounds the grant the same way `domain_consents` are described as
"time-bound and revocable" in `DOMAIN-PRIVACY-CONTRACT.md`.

**Lifecycle mirrors `domain_consents`, not `domain_records`.** A grant is
granted once and later only ever flips `revoked_at` -- there is no
"edit a grant's own categories/purpose after creation" operation this
task's own scope needs (matching `domain_consents`'s identical "no
`version`/`updated_at`/`created_by`" shape, `0054`'s own table). `owner_id`
already identifies the granting actor, the same reasoning `domain_
consents` already relies on for single-user-owned personal data.

Only `source_domain_key` needs to already be an enabled `personal_domains`
row (the FK below) -- a grant names what data it draws from, not a second
domain it grants *to*, so there is no second FK to satisfy.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0056_phase7_cross_domain_grants"
down_revision = "0055_phase4_expl_item_prompt_v3"
branch_labels = None
depends_on = None

_DOMAIN_KEYS = "'habits', 'learning', 'travel', 'relationships', 'health', 'finance'"
_PURPOSES = "'insight_generation'"


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.create_table(
        "cross_domain_grants",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("owner_id", uuid, nullable=False),
        sa.Column("source_domain_key", sa.String(20), nullable=False),
        sa.Column("purpose", sa.String(50), nullable=False),
        sa.Column("granted_categories", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            f"source_domain_key IN ({_DOMAIN_KEYS})",
            name="ck_cross_domain_grants_source_domain_key",
        ),
        sa.CheckConstraint(f"purpose IN ({_PURPOSES})", name="ck_cross_domain_grants_purpose"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "owner_id", "source_domain_key"],
            [
                "personal_domains.workspace_id",
                "personal_domains.owner_id",
                "personal_domains.domain_key",
            ],
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_cross_domain_grants_owner_source",
        "cross_domain_grants",
        ["workspace_id", "owner_id", "source_domain_key"],
    )


def downgrade() -> None:
    op.drop_index("ix_cross_domain_grants_owner_source", table_name="cross_domain_grants")
    op.drop_table("cross_domain_grants")
