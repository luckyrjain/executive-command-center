"""Add the Phase 1 task, audit, and idempotency foundation."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0002_phase1_task_foundation"
down_revision = "0001_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.add_column(
        "workspaces",
        sa.Column(
            "timezone",
            sa.String(64),
            nullable=False,
            server_default="UTC",
        ),
    )

    op.create_table(
        "tasks",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("owner_id", uuid, nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("status", sa.String(32), nullable=False, server_default="captured"),
        sa.Column("manual_priority", sa.String(16), nullable=False, server_default="medium"),
        sa.Column("due_date", sa.Date()),
        sa.Column("due_at", sa.DateTime(timezone=True)),
        sa.Column("blocked_reason", sa.Text()),
        sa.Column("blocked_on_person_id", uuid),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source_type", sa.String(32), nullable=False, server_default="local"),
        sa.Column("source_ref", sa.Text()),
        sa.Column("created_by", uuid, nullable=False),
        sa.Column("updated_by", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column("pre_archive_status", sa.String(32)),
        sa.CheckConstraint("due_date IS NULL OR due_at IS NULL", name="ck_tasks_one_due_precision"),
        sa.CheckConstraint(
            "status IN ('captured','planned','in_progress','blocked','completed','cancelled')",
            name="ck_tasks_status",
        ),
        sa.CheckConstraint(
            "manual_priority IN ('low','medium','high','critical')",
            name="ck_tasks_priority",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "owner_id"],
            ["users.workspace_id", "users.id"],
            name="fk_tasks_workspace_owner",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "created_by"],
            ["users.workspace_id", "users.id"],
            name="fk_tasks_workspace_created_by",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "updated_by"],
            ["users.workspace_id", "users.id"],
            name="fk_tasks_workspace_updated_by",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_tasks_workspace_id_id"),
    )
    op.create_index("ix_tasks_workspace_status", "tasks", ["workspace_id", "status"])
    op.create_index("ix_tasks_workspace_due_date", "tasks", ["workspace_id", "due_date"])
    op.create_index("ix_tasks_workspace_due_at", "tasks", ["workspace_id", "due_at"])
    op.create_index("ix_tasks_workspace_priority", "tasks", ["workspace_id", "manual_priority"])
    op.create_index("ix_tasks_workspace_pinned", "tasks", ["workspace_id", "pinned"])

    op.create_table(
        "audit_events",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("event_type", sa.String(200), nullable=False),
        sa.Column("aggregate_type", sa.String(100), nullable=False),
        sa.Column("aggregate_id", uuid, nullable=False),
        sa.Column("aggregate_version", sa.BigInteger(), nullable=False),
        sa.Column("actor_id", uuid),
        sa.Column("request_id", uuid, nullable=False),
        sa.Column("correlation_id", uuid, nullable=False),
        sa.Column("idempotency_key_hash", sa.String(64)),
        sa.Column("before", postgresql.JSONB()),
        sa.Column("after", postgresql.JSONB()),
        sa.Column(
            "changed_fields",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default="{}",
        ),
        sa.Column(
            "authorization_result",
            sa.String(32),
            nullable=False,
            server_default="allowed",
        ),
        sa.Column("source", sa.String(16), nullable=False, server_default="user"),
        sa.Column("failure_code", sa.String(100)),
        sa.Column(
            "metadata",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "actor_id"],
            ["users.workspace_id", "users.id"],
            name="fk_audit_workspace_actor",
        ),
    )
    op.create_index(
        "ix_audit_workspace_aggregate_time",
        "audit_events",
        ["workspace_id", "aggregate_type", "aggregate_id", sa.text("occurred_at DESC")],
    )

    op.create_table(
        "idempotency_records",
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("actor_id", uuid, nullable=False),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=False),
        sa.Column("response_body", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("workspace_id", "actor_id", "key"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "actor_id"],
            ["users.workspace_id", "users.id"],
            name="fk_idempotency_workspace_actor",
            ondelete="CASCADE",
        ),
    )


def downgrade() -> None:
    op.drop_table("idempotency_records")
    op.drop_index("ix_audit_workspace_aggregate_time", table_name="audit_events")
    op.drop_table("audit_events")
    op.drop_index("ix_tasks_workspace_pinned", table_name="tasks")
    op.drop_index("ix_tasks_workspace_priority", table_name="tasks")
    op.drop_index("ix_tasks_workspace_due_at", table_name="tasks")
    op.drop_index("ix_tasks_workspace_due_date", table_name="tasks")
    op.drop_index("ix_tasks_workspace_status", table_name="tasks")
    op.drop_table("tasks")
    op.drop_column("workspaces", "timezone")
