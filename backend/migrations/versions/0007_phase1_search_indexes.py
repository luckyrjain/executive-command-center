"""Add PostgreSQL indexes for Phase 1 global search."""

import sqlalchemy as sa
from alembic import op

revision = "0007_phase1_search_indexes"
down_revision = "0006_phase1_risks_priority"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    for table, column, name in (
        ("tasks", "title", "ix_tasks_search_title_trgm"),
        ("commitments", "summary", "ix_commitments_search_summary_trgm"),
        ("notes", "title", "ix_notes_search_title_trgm"),
        ("meetings", "title", "ix_meetings_search_title_trgm"),
        ("calendar_events", "title", "ix_calendar_events_search_title_trgm"),
        ("risks", "description", "ix_risks_search_description_trgm"),
    ):
        op.create_index(
            name,
            table,
            [sa.text(f"lower(coalesce({column}, '')) gin_trgm_ops")],
            postgresql_using="gin",
        )

    op.create_index(
        "ix_tasks_search_document",
        "tasks",
        [
            sa.text(
                "(setweight(to_tsvector('simple', coalesce(title, '')), 'A') || "
                "setweight(to_tsvector('simple', coalesce(description, '')), 'B'))"
            )
        ],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_commitments_search_document",
        "commitments",
        [
            sa.text(
                "(setweight(to_tsvector('simple', coalesce(summary, '')), 'A') || "
                "setweight(to_tsvector('simple', coalesce(description, '')), 'B'))"
            )
        ],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_meetings_search_document",
        "meetings",
        [
            sa.text(
                "(setweight(to_tsvector('simple', coalesce(title, '')), 'A') || "
                "setweight(to_tsvector('simple', coalesce(agenda, '') || ' ' || "
                "coalesce(preparation, '') || ' ' || coalesce(notes_summary, '')), 'B'))"
            )
        ],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_calendar_events_search_document",
        "calendar_events",
        [
            sa.text(
                "(setweight(to_tsvector('simple', coalesce(title, '')), 'A') || "
                "setweight(to_tsvector('simple', coalesce(description, '') || ' ' || "
                "coalesce(location, '')), 'B'))"
            )
        ],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_risks_search_document",
        "risks",
        [
            sa.text(
                "(setweight(to_tsvector('simple', coalesce(description, '')), 'A') || "
                "setweight(to_tsvector('simple', coalesce(mitigation, '') || ' ' || "
                "coalesce(trigger, '')), 'B'))"
            )
        ],
        postgresql_using="gin",
    )


def downgrade() -> None:
    for name, table in (
        ("ix_risks_search_document", "risks"),
        ("ix_calendar_events_search_document", "calendar_events"),
        ("ix_meetings_search_document", "meetings"),
        ("ix_commitments_search_document", "commitments"),
        ("ix_tasks_search_document", "tasks"),
        ("ix_risks_search_description_trgm", "risks"),
        ("ix_calendar_events_search_title_trgm", "calendar_events"),
        ("ix_meetings_search_title_trgm", "meetings"),
        ("ix_notes_search_title_trgm", "notes"),
        ("ix_commitments_search_summary_trgm", "commitments"),
        ("ix_tasks_search_title_trgm", "tasks"),
    ):
        op.drop_index(name, table_name=table)
