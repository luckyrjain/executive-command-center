"""Add waiting_links: directional obligation/dependency records.

Phase 3 Task 2. Direction changes append a new row rather than mutate one
in place (ATTENTION-MODEL.md: "Direction changes create history; they do
not overwrite the original obligation") -- mirrors Phase 2's
knowledge_claims supersede pattern via the self-referencing
``superseded_by`` column, rather than a separate history table.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0023_phase3_waiting"
down_revision = "0022_phase3_attention_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.create_table(
        "waiting_links",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("subject_type", sa.String(32), nullable=False),
        sa.Column("subject_id", uuid, nullable=False),
        sa.Column("counterparty_entity_id", uuid, nullable=False),
        sa.Column("direction", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="open"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("since_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("superseded_by", uuid, nullable=True),
        sa.Column("created_by", uuid, nullable=False),
        sa.Column("updated_by", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.CheckConstraint(
            "subject_type IN ('task','commitment','knowledge_entity')",
            name="ck_waiting_links_subject_type",
        ),
        sa.CheckConstraint(
            "direction IN ('waiting_on_me','waiting_on_them','blocked_by','delegated')",
            name="ck_waiting_links_direction",
        ),
        sa.CheckConstraint(
            "status IN ('open','fulfilled','cancelled','superseded')",
            name="ck_waiting_links_status",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "counterparty_entity_id"],
            ["pkos_nodes.workspace_id", "pkos_nodes.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "created_by"], ["users.workspace_id", "users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "updated_by"], ["users.workspace_id", "users.id"], ondelete="RESTRICT"
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_waiting_links_workspace_id"),
    )
    op.create_index(
        "ix_waiting_links_workspace_subject",
        "waiting_links",
        ["workspace_id", "subject_type", "subject_id"],
    )
    op.create_index(
        "ix_waiting_links_workspace_status_created",
        "waiting_links",
        ["workspace_id", "status", "created_at"],
    )
    op.create_index(
        "ix_waiting_links_workspace_counterparty",
        "waiting_links",
        ["workspace_id", "counterparty_entity_id"],
    )
    # Cycle detection (Step 2 of Task 2) walks blocked_by edges keyed by
    # (subject_id, counterparty_entity_id) for knowledge_entity subjects
    # only -- this partial index keeps that bounded graph walk cheap
    # without indexing every row.
    op.create_index(
        "ix_waiting_links_blocked_by_graph",
        "waiting_links",
        ["workspace_id", "subject_id", "counterparty_entity_id"],
        postgresql_where=sa.text(
            "direction = 'blocked_by' AND subject_type = 'knowledge_entity' AND status = 'open'"
        ),
    )


def downgrade() -> None:
    op.drop_index("ix_waiting_links_blocked_by_graph", table_name="waiting_links")
    op.drop_index("ix_waiting_links_workspace_counterparty", table_name="waiting_links")
    op.drop_index("ix_waiting_links_workspace_status_created", table_name="waiting_links")
    op.drop_index("ix_waiting_links_workspace_subject", table_name="waiting_links")
    op.drop_table("waiting_links")
