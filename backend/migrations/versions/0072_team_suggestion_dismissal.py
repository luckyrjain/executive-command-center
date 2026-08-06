"""Team suggestion dismissal (team suggestions review page).

Adds `team_suggestion_dismissed_at` (nullable `TIMESTAMPTZ`) to both
`repositories` and `engineering_work_items`, alongside `0050_phase6_
team_linkage.py`'s existing `team_entity_id`/`suggested_team_name` pair.
Sync-silent like `team_entity_id` -- no adapter's `ON CONFLICT ... DO
UPDATE` ever *sets* it, and only ever *clears* it when the incoming
`suggested_team_name` differs from the row's current stored value (see
`docs/superpowers/specs/2026-08-06-team-suggestions-review-page-design.md`'s
"Data model" section for the full reasoning). A human dismisses a
suggestion via the new `POST /api/v1/engineering/team-suggestions/dismiss`
endpoint; no sync ever sets this column.
"""

import sqlalchemy as sa
from alembic import op

revision = "0072_team_suggestion_dismissal"
down_revision = "0071_phase10_recs_create_path"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("repositories", "engineering_work_items"):
        op.add_column(
            table,
            sa.Column("team_suggestion_dismissed_at", sa.DateTime(timezone=True), nullable=True),
        )


def downgrade() -> None:
    for table in ("repositories", "engineering_work_items"):
        op.drop_column(table, "team_suggestion_dismissed_at")
