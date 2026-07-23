"""Add meeting_participants and meeting_packs for Phase 3 meeting preparation.

Phase 3 Task 7. `meeting_participants` is a genuinely new join table (the
design doc's Open decision 2): Phase 1's `meetings`/`calendar_events` have no
structured attendee list today, only free-text `title`/`description`. It
links a `meetings` row (not `calendar_event_id` -- the design doc's Open
decision 2 was written before accounting for the fact that `meetings` is
already Phase 1's first-class, independently-keyed row and the whole Phase 3
API surface addresses meetings as `/meetings/{id}/prep`; linking to the
meeting itself, not its optional calendar event, is the consistent choice
and matches `notes.meeting_id`'s existing FK target) to Phase 2's `pkos_nodes`
person entities, per API-SCHEMAS.md's "attendees as knowledge entity refs".

`meeting_packs` snapshots a generated preparation pack. Lifecycle per
DATA-MODEL.md: `fresh -> stale -> refreshed|archived`. `stale_at` is a
generation-time TTL threshold (`MEETING-PREP-CONTRACT.md`: "A pack stores
... generation time and stale threshold") in addition to the material-change
staleness check `meeting_prep.py` performs by fingerprint comparison; either
condition marks the pack `stale` at read time. Refresh always creates a new
row (never rewrites an existing snapshot in place) so prior packs remain
available as history, mirroring `plans`'/`waiting_links`' supersede pattern.

`meeting_packs.content` persists the fully-rendered pack body (participants,
timeline, commitments, decisions, notes, risks, dependencies, evidence gaps)
exactly as generated -- a real frozen snapshot per MEETING-PREP-CONTRACT.md,
not re-derived from live tables on every GET (a subsequent GET returns this
stored JSON verbatim; only `POST .../prep/refresh` re-derives and persists a
new one). `uq_meeting_packs_active_per_meeting` enforces at most one
fresh-or-stale pack per meeting at the database level, closing the
duplicate-pack race a plain existence-check-then-insert can't close on its
own under concurrent requests.

Also adds `notes.restricted` (`BOOLEAN NOT NULL DEFAULT false`).
`MEETING-PREP-CONTRACT.md`'s Safety section requires private/restricted
notes to be excluded from a generated pack, and `TEST-PLAN.md` names this as
a distinct required test -- but no visibility/privacy signal exists
anywhere in the shipped `notes` schema to hook that exclusion on. This is
the minimal additive column to make that named, existing contract
requirement satisfiable, matching how Task 1 added `attention_items.
override_reason` directly rather than a new table.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0027_phase3_meetings"
down_revision = "0026_phase3_plans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.add_column(
        "notes",
        sa.Column("restricted", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    op.create_table(
        "meeting_participants",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("meeting_id", uuid, nullable=False),
        sa.Column("entity_id", uuid, nullable=False),
        sa.Column("role", sa.String(100), nullable=False, server_default="attendee"),
        sa.Column("created_by", uuid, nullable=False),
        sa.Column("updated_by", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "meeting_id"],
            ["meetings.workspace_id", "meetings.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "entity_id"],
            ["pkos_nodes.workspace_id", "pkos_nodes.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "created_by"], ["users.workspace_id", "users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "updated_by"], ["users.workspace_id", "users.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint(
            "workspace_id", "meeting_id", "entity_id", name="uq_meeting_participants_link"
        ),
    )
    op.create_index(
        "ix_meeting_participants_workspace_meeting",
        "meeting_participants",
        ["workspace_id", "meeting_id"],
    )

    op.create_table(
        "meeting_packs",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("meeting_id", uuid, nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="fresh"),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stale_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "source_versions",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        # The frozen, fully-rendered pack body -- see module docstring.
        sa.Column(
            "content",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_by", uuid, nullable=False),
        sa.Column("updated_by", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.CheckConstraint(
            "status IN ('fresh','stale','refreshed','archived')",
            name="ck_meeting_packs_status",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "meeting_id"],
            ["meetings.workspace_id", "meetings.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "created_by"], ["users.workspace_id", "users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "updated_by"], ["users.workspace_id", "users.id"], ondelete="RESTRICT"
        ),
    )
    op.create_index(
        "ix_meeting_packs_workspace_meeting_generated",
        "meeting_packs",
        ["workspace_id", "meeting_id", "generated_at"],
    )
    # At most one active (fresh or stale) pack per meeting -- a database
    # constraint, not just an application-level existence check, so two
    # concurrent "generate pack" requests can't both pass the check and
    # both insert (or leave a real duplicate uncaught).
    op.create_index(
        "uq_meeting_packs_active_per_meeting",
        "meeting_packs",
        ["workspace_id", "meeting_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('fresh', 'stale')"),
    )

    # meeting_prep.py's _fetch_notes filters/orders by (workspace_id,
    # meeting_id, created_at) and _fetch_commitments filters by
    # (workspace_id, counterparty_person_id) -- both hot paths on every
    # prep-pack generation/refresh, neither previously indexed.
    op.create_index(
        "ix_notes_workspace_meeting_created",
        "notes",
        ["workspace_id", "meeting_id", "created_at"],
    )
    op.create_index(
        "ix_commitments_workspace_counterparty",
        "commitments",
        ["workspace_id", "counterparty_person_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_commitments_workspace_counterparty", table_name="commitments")
    op.drop_index("ix_notes_workspace_meeting_created", table_name="notes")
    op.drop_index("uq_meeting_packs_active_per_meeting", table_name="meeting_packs")
    op.drop_index("ix_meeting_packs_workspace_meeting_generated", table_name="meeting_packs")
    op.drop_table("meeting_packs")
    op.drop_index("ix_meeting_participants_workspace_meeting", table_name="meeting_participants")
    op.drop_table("meeting_participants")
    op.drop_column("notes", "restricted")
