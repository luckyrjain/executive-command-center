"""Phase 7 Task 5 part 2: `personal_insights.professional_referral_note`
and `personal_insight_feedback` (`docs/phases/phase-007/INSIGHT-CONTRACT.md`:
"`health`/`finance`-classified insights require a non-empty `professional_
referral_note` field" and "`POST .../feedback` never rewrites an insight's
own `kind` -- only separate feedback metadata").

**`professional_referral_note`, nullable, added to the existing `personal_
insights` table.** `NULL` for every deterministic (`habits.py`) insight and
for any AI-generated insight whose sources are all `standard`/`sensitive`
-- the *conditional* non-empty requirement for `high_stakes` sources is
enforced by `validator.py:check_personal_insight_grounding` before an
`ai_runs` row is even persisted as `completed`, not by a database
constraint here (a `CHECK` conditioned on "was any source domain `high_
stakes`" would need to know facts this table doesn't itself record).

**`personal_insight_feedback`, a new table, not a column on `personal_
insights`.** `POST /personal/insights/{id}/feedback` must be structurally
incapable of rewriting `personal_insights.kind` -- a separate table with no
`kind` column of its own makes that true by construction (there is no
column to write to), rather than relying on the endpoint's own code never
choosing to write one. `useful` (boolean) and an optional free-text
`comment` are the only feedback fields this task defines; `insight_id` is a
plain foreign key (not `UNIQUE`) -- a user may leave feedback on the same
insight more than once (e.g. revising an initial reaction), so this is an
append-only feedback log, not a single upsert-by-insight slot.

`personal_insights` gains a `UNIQUE(workspace_id, id)` constraint (mirroring
`goals`/`routines`/`domain_sources`' own identical `uq_*_workspace_id`
precedent, `0054_phase7_personal_domains.py`) purely so `personal_insight_
feedback` can foreign-key to it via the composite `(workspace_id,
insight_id)` pair every other cross-table reference in this package already
uses for tenant-isolation-at-the-database-level, not a `personal_insights.
id`-alone reference.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0059_phase7_insight_feedback"
down_revision = "0058_phase7_insight_eval"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.add_column(
        "personal_insights",
        sa.Column("professional_referral_note", sa.Text(), nullable=True),
    )
    op.create_unique_constraint(
        "uq_personal_insights_workspace_id", "personal_insights", ["workspace_id", "id"]
    )

    op.create_table(
        "personal_insight_feedback",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("owner_id", uuid, nullable=False),
        sa.Column("insight_id", uuid, nullable=False),
        sa.Column("useful", sa.Boolean(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("created_by", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "insight_id"],
            ["personal_insights.workspace_id", "personal_insights.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "created_by"], ["users.workspace_id", "users.id"], ondelete="RESTRICT"
        ),
    )
    op.create_index(
        "ix_personal_insight_feedback_owner_insight",
        "personal_insight_feedback",
        ["workspace_id", "owner_id", "insight_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_personal_insight_feedback_owner_insight", table_name="personal_insight_feedback"
    )
    op.drop_table("personal_insight_feedback")
    op.drop_constraint("uq_personal_insights_workspace_id", "personal_insights", type_="unique")
    op.drop_column("personal_insights", "professional_referral_note")
