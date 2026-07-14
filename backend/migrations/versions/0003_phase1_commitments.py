"""Add the Phase 1 commitments table."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0003_phase1_commitments"
down_revision = "0002_phase1_task_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.create_unique_constraint(
        "uq_pkos_evidence_workspace_id_id",
        "pkos_evidence",
        ["workspace_id", "id"],
    )

    op.create_table(
        "commitments",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("owner_id", uuid, nullable=False),
        sa.Column("summary", sa.String(500), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("direction", sa.String(32), nullable=False),
        sa.Column("counterparty_person_id", uuid),
        sa.Column("counterparty_name", sa.String(500)),
        sa.Column("status", sa.String(32), nullable=False, server_default="confirmed"),
        sa.Column("due_date", sa.Date()),
        sa.Column("due_at", sa.DateTime(timezone=True)),
        sa.Column("importance", sa.String(16), nullable=False, server_default="medium"),
        sa.Column("evidence_id", uuid),
        sa.Column("confidence", sa.Numeric(4, 3)),
        sa.Column("fulfilled_at", sa.DateTime(timezone=True)),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by", uuid, nullable=False),
        sa.Column("updated_by", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column("pre_archive_status", sa.String(32)),
        sa.CheckConstraint(
            "direction IN ('made_by_me','made_to_me')",
            name="ck_commitments_direction",
        ),
        sa.CheckConstraint(
            "status IN ('detected','confirmed','active','fulfilled','broken','cancelled')",
            name="ck_commitments_status",
        ),
        sa.CheckConstraint(
            "importance IN ('low','medium','high','critical')",
            name="ck_commitments_importance",
        ),
        sa.CheckConstraint(
            "due_date IS NULL OR due_at IS NULL",
            name="ck_commitments_one_due_precision",
        ),
        sa.CheckConstraint(
            "confidence IS NULL OR (confidence >= 0 AND confidence <= 1)",
            name="ck_commitments_confidence",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "owner_id"],
            ["users.workspace_id", "users.id"],
            name="fk_commitments_workspace_owner",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "counterparty_person_id"],
            ["pkos_nodes.workspace_id", "pkos_nodes.id"],
            name="fk_commitments_workspace_counterparty",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "evidence_id"],
            ["pkos_evidence.workspace_id", "pkos_evidence.id"],
            name="fk_commitments_workspace_evidence",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "created_by"],
            ["users.workspace_id", "users.id"],
            name="fk_commitments_workspace_created_by",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "updated_by"],
            ["users.workspace_id", "users.id"],
            name="fk_commitments_workspace_updated_by",
        ),
        sa.UniqueConstraint(
            "workspace_id",
            "id",
            name="uq_commitments_workspace_id_id",
        ),
    )

    op.create_index(
        "ix_commitments_workspace_status_due_date",
        "commitments",
        ["workspace_id", "status", "due_date"],
    )
    op.create_index(
        "ix_commitments_workspace_status_due_at",
        "commitments",
        ["workspace_id", "status", "due_at"],
    )
    op.create_index(
        "ix_commitments_workspace_owner",
        "commitments",
        ["workspace_id", "owner_id"],
    )
    op.create_index(
        "ix_commitments_workspace_importance",
        "commitments",
        ["workspace_id", "importance"],
    )


def downgrade() -> None:
    op.drop_index("ix_commitments_workspace_importance", table_name="commitments")
    op.drop_index("ix_commitments_workspace_owner", table_name="commitments")
    op.drop_index("ix_commitments_workspace_status_due_at", table_name="commitments")
    op.drop_index("ix_commitments_workspace_status_due_date", table_name="commitments")
    op.drop_table("commitments")
    op.drop_constraint(
        "uq_pkos_evidence_workspace_id_id",
        "pkos_evidence",
        type_="unique",
    )
