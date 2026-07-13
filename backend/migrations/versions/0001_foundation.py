"""Create Phase 0 foundation schema."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001_foundation"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    op.create_table(
        "workspaces",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "users",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, sa.ForeignKey("workspaces.id"), nullable=False, index=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_users_workspace_id_id"),
        sa.UniqueConstraint("workspace_id", "email", name="uq_users_workspace_email"),
    )
    op.create_table(
        "sessions",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, sa.ForeignKey("workspaces.id"), nullable=False, index=True),
        sa.Column("user_id", uuid, nullable=False, index=True),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.ForeignKeyConstraint(
            ["workspace_id", "user_id"],
            ["users.workspace_id", "users.id"],
            name="fk_sessions_workspace_user",
            ondelete="CASCADE",
        ),
    )
    op.create_table(
        "pkos_nodes",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, sa.ForeignKey("workspaces.id"), nullable=False, index=True),
        sa.Column("node_type", sa.String(100), nullable=False),
        sa.Column("canonical_name", sa.Text(), nullable=False),
        sa.Column(
            "attributes",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("workspace_id", "id", name="uq_pkos_nodes_workspace_id_id"),
    )
    op.create_table(
        "pkos_edges",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, sa.ForeignKey("workspaces.id"), nullable=False, index=True),
        sa.Column("source_node_id", uuid, nullable=False),
        sa.Column("target_node_id", uuid, nullable=False),
        sa.Column("edge_type", sa.String(100), nullable=False),
        sa.Column(
            "attributes",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "source_node_id"],
            ["pkos_nodes.workspace_id", "pkos_nodes.id"],
            name="fk_pkos_edges_workspace_source",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "target_node_id"],
            ["pkos_nodes.workspace_id", "pkos_nodes.id"],
            name="fk_pkos_edges_workspace_target",
            ondelete="CASCADE",
        ),
    )
    op.create_table(
        "pkos_evidence",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, sa.ForeignKey("workspaces.id"), nullable=False, index=True),
        sa.Column("node_id", uuid, nullable=False),
        sa.Column("source_type", sa.String(100), nullable=False),
        sa.Column("source_ref", sa.Text(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id", "node_id"],
            ["pkos_nodes.workspace_id", "pkos_nodes.id"],
            name="fk_pkos_evidence_workspace_node",
            ondelete="CASCADE",
        ),
    )
    op.create_table(
        "event_outbox",
        sa.Column("event_id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, sa.ForeignKey("workspaces.id"), nullable=False, index=True),
        sa.Column("event_type", sa.String(200), nullable=False),
        sa.Column("event_version", sa.Integer(), nullable=False),
        sa.Column("correlation_id", uuid, nullable=False),
        sa.Column("causation_id", uuid),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "event_inbox",
        sa.Column("consumer", sa.String(200), primary_key=True),
        sa.Column("event_id", uuid, primary_key=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "event_dead_letters",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("event_id", uuid, nullable=False, index=True),
        sa.Column("consumer", sa.String(200), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    for table in [
        "event_dead_letters",
        "event_inbox",
        "event_outbox",
        "pkos_evidence",
        "pkos_edges",
        "pkos_nodes",
        "sessions",
        "users",
        "workspaces",
    ]:
        op.drop_table(table)
