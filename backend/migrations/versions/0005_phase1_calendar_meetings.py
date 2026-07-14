"""Add Phase 1 calendar event and meeting tables."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_phase1_calendar_meetings"
down_revision = "0004_phase1_notes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.create_table(
        "calendar_events",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("external_source", sa.String(32), nullable=False, server_default="local"),
        sa.Column("external_id", sa.Text()),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("all_day", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("timezone", sa.String(128), nullable=False),
        sa.Column("location", sa.Text()),
        sa.Column("description", sa.Text()),
        sa.Column("status", sa.String(32), nullable=False, server_default="confirmed"),
        sa.Column(
            "source_authoritative",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("created_by", uuid, nullable=False),
        sa.Column("updated_by", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column("pre_archive_status", sa.String(32)),
        sa.CheckConstraint("external_source = 'local'", name="ck_calendar_events_local_source"),
        sa.CheckConstraint(
            "status IN ('confirmed','tentative','cancelled')",
            name="ck_calendar_events_status",
        ),
        sa.CheckConstraint("ends_at > starts_at", name="ck_calendar_events_time_order"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "created_by"],
            ["users.workspace_id", "users.id"],
            name="fk_calendar_events_workspace_created_by",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "updated_by"],
            ["users.workspace_id", "users.id"],
            name="fk_calendar_events_workspace_updated_by",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "id",
            name="uq_calendar_events_workspace_id_id",
        ),
    )
    op.create_index(
        "uq_calendar_events_external_identity",
        "calendar_events",
        ["workspace_id", "external_source", "external_id"],
        unique=True,
        postgresql_where=sa.text("external_id IS NOT NULL"),
    )
    op.create_index(
        "ix_calendar_events_workspace_starts_at",
        "calendar_events",
        ["workspace_id", "starts_at"],
    )
    op.create_index(
        "ix_calendar_events_workspace_status",
        "calendar_events",
        ["workspace_id", "status"],
    )

    op.create_table(
        "meetings",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("calendar_event_id", uuid),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("standalone_starts_at", sa.DateTime(timezone=True)),
        sa.Column("standalone_ends_at", sa.DateTime(timezone=True)),
        sa.Column("standalone_timezone", sa.String(128)),
        sa.Column("status", sa.String(32), nullable=False, server_default="planned"),
        sa.Column("agenda", sa.Text()),
        sa.Column("preparation", sa.Text()),
        sa.Column("notes_summary", sa.Text()),
        sa.Column("created_by", uuid, nullable=False),
        sa.Column("updated_by", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column("pre_archive_status", sa.String(32)),
        sa.CheckConstraint(
            "status IN ('planned','in_progress','completed','cancelled')",
            name="ck_meetings_status",
        ),
        sa.CheckConstraint(
            "(calendar_event_id IS NOT NULL AND standalone_starts_at IS NULL "
            "AND standalone_ends_at IS NULL AND standalone_timezone IS NULL) OR "
            "(calendar_event_id IS NULL AND standalone_starts_at IS NOT NULL "
            "AND standalone_ends_at IS NOT NULL AND standalone_timezone IS NOT NULL)",
            name="ck_meetings_linked_or_standalone_timing",
        ),
        sa.CheckConstraint(
            "standalone_ends_at IS NULL OR standalone_starts_at IS NULL "
            "OR standalone_ends_at > standalone_starts_at",
            name="ck_meetings_standalone_time_order",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "calendar_event_id"],
            ["calendar_events.workspace_id", "calendar_events.id"],
            name="fk_meetings_workspace_calendar_event",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "created_by"],
            ["users.workspace_id", "users.id"],
            name="fk_meetings_workspace_created_by",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "updated_by"],
            ["users.workspace_id", "users.id"],
            name="fk_meetings_workspace_updated_by",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_meetings_workspace_id_id"),
        sa.UniqueConstraint(
            "workspace_id",
            "calendar_event_id",
            name="uq_meetings_workspace_calendar_event",
        ),
    )
    op.create_index(
        "ix_meetings_workspace_status",
        "meetings",
        ["workspace_id", "status"],
    )
    op.create_index(
        "ix_meetings_workspace_standalone_starts_at",
        "meetings",
        ["workspace_id", "standalone_starts_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_meetings_workspace_standalone_starts_at", table_name="meetings")
    op.drop_index("ix_meetings_workspace_status", table_name="meetings")
    op.drop_table("meetings")
    op.drop_index("ix_calendar_events_workspace_status", table_name="calendar_events")
    op.drop_index("ix_calendar_events_workspace_starts_at", table_name="calendar_events")
    op.drop_index("uq_calendar_events_external_identity", table_name="calendar_events")
    op.drop_table("calendar_events")
