"""Add the Phase 1 notes table."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_phase1_notes"
down_revision = "0003_phase1_commitments"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.create_table(
        "notes",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("owner_id", uuid, nullable=False),
        sa.Column("title", sa.String(500)),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("note_type", sa.String(32), nullable=False, server_default="general"),
        sa.Column("meeting_id", uuid),
        sa.Column("source_type", sa.String(32), nullable=False, server_default="local"),
        sa.Column("source_ref", sa.Text()),
        sa.Column(
            "search_document",
            postgresql.TSVECTOR(),
            sa.Computed(
                "setweight(to_tsvector('simple', coalesce(title, '')), 'A') || "
                "setweight(to_tsvector('simple', body), 'B')",
                persisted=True,
            ),
        ),
        sa.Column("created_by", uuid, nullable=False),
        sa.Column("updated_by", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column("pre_archive_status", sa.String(32)),
        sa.CheckConstraint(
            "note_type IN ('general','meeting','decision','journal')",
            name="ck_notes_note_type",
        ),
        sa.CheckConstraint(
            "source_type IN ('local','meeting')",
            name="ck_notes_source_type",
        ),
        sa.CheckConstraint(
            "char_length(body) BETWEEN 1 AND 100000",
            name="ck_notes_body_length",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "owner_id"],
            ["users.workspace_id", "users.id"],
            name="fk_notes_workspace_owner",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "created_by"],
            ["users.workspace_id", "users.id"],
            name="fk_notes_workspace_created_by",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "updated_by"],
            ["users.workspace_id", "users.id"],
            name="fk_notes_workspace_updated_by",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_notes_workspace_id_id"),
    )

    op.create_index(
        "ix_notes_workspace_updated_at",
        "notes",
        ["workspace_id", sa.text("updated_at DESC")],
    )
    op.create_index(
        "ix_notes_search_document",
        "notes",
        ["search_document"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_notes_search_document", table_name="notes")
    op.drop_index("ix_notes_workspace_updated_at", table_name="notes")
    op.drop_table("notes")
