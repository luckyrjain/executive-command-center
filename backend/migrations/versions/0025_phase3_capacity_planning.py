"""Add capacity_profiles and planning_constraints for Phase 3 planning.

Phase 3 Task 4. capacity_profiles is one row per weekday (0=Monday..6=Sunday)
per user; the whole 7-row set is managed as a single versioned unit through
GET|PUT /api/v1/planning/capacity (see capacity.py's module docstring for
the "profile version" derivation). planning_constraints has no dedicated
public endpoint in this task -- per phase-003/API-SCHEMAS.md's published
surface, only /planning/capacity is public; constraints are plan-scoped
input that Task 5's POST /plans persists here, not independently CRUD'd.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0025_phase3_capacity_planning"
down_revision = "0024_phase3_risk_reviews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.create_table(
        "capacity_profiles",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("user_id", uuid, nullable=False),
        sa.Column("weekday", sa.SmallInteger(), nullable=False),
        sa.Column("available_minutes", sa.Integer(), nullable=False),
        sa.Column("focus_minutes", sa.Integer(), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("weekday BETWEEN 0 AND 6", name="ck_capacity_profiles_weekday"),
        sa.CheckConstraint(
            "available_minutes BETWEEN 0 AND 1440", name="ck_capacity_profiles_available"
        ),
        sa.CheckConstraint(
            "focus_minutes BETWEEN 0 AND available_minutes", name="ck_capacity_profiles_focus"
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "user_id"], ["users.workspace_id", "users.id"], ondelete="CASCADE"
        ),
        sa.UniqueConstraint(
            "workspace_id", "user_id", "weekday", name="uq_capacity_profiles_workspace_user_weekday"
        ),
    )

    op.create_table(
        "planning_constraints",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("user_id", uuid, nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("source_type", sa.String(32), nullable=True),
        sa.Column("source_id", uuid, nullable=True),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("hardness", sa.String(16), nullable=False),
        sa.Column("priority", sa.SmallInteger(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.BigInteger(), nullable=False, server_default="1"),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('fixed_time','deadline','preference')",
            name="ck_planning_constraints_kind",
        ),
        sa.CheckConstraint("hardness IN ('hard','soft')", name="ck_planning_constraints_hardness"),
        sa.CheckConstraint(
            "source_type IS NULL OR source_type IN ('task','commitment','calendar_event')",
            name="ck_planning_constraints_source_type",
        ),
        sa.CheckConstraint(
            "(source_type IS NULL) = (source_id IS NULL)",
            name="ck_planning_constraints_source_pair",
        ),
        sa.CheckConstraint(
            "kind <> 'fixed_time' OR (starts_at IS NOT NULL AND ends_at IS NOT NULL)",
            name="ck_planning_constraints_fixed_time_range",
        ),
        sa.CheckConstraint(
            "kind <> 'deadline' OR ends_at IS NOT NULL",
            name="ck_planning_constraints_deadline_range",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "user_id"], ["users.workspace_id", "users.id"], ondelete="CASCADE"
        ),
    )
    op.create_index(
        "ix_planning_constraints_workspace_user_active",
        "planning_constraints",
        ["workspace_id", "user_id"],
        postgresql_where=sa.text("archived_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_planning_constraints_workspace_user_active", table_name="planning_constraints"
    )
    op.drop_table("planning_constraints")
    op.drop_table("capacity_profiles")
