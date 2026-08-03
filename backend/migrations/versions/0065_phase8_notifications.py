"""Phase 8 Task 7: `member_notifications` (`docs/superpowers/plans/2026-08-01-
phase-8-multi-user.md` Task 7, `docs/phases/phase-008/PERMISSION-CONTRACT.md`'s
"affected members see redacted audit/activity without private-content
leakage").

**Exactly the plan's own literal column list** -- `id`, `workspace_id`,
`account_id`, `notification_type`, `resource_ref`, `read_at`, `created_at`.
`resource_ref` is one text column, not a `(resource_type, resource_id)`
pair -- the plan names a single column, so `ecc.platform.notifications`
encodes both as `f"{resource_type}:{resource_id}"` rather than widening the
schema beyond what was specified.

**The `UNIQUE (workspace_id, account_id, notification_type, resource_ref)`
constraint is this migration's own answer to the plan's "notification
idempotency (a duplicate underlying event does not double-notify)" test
requirement** -- every call site inserts via `INSERT ... ON CONFLICT (...)
DO NOTHING`, so a underlying event that fires twice for the same
notification (`ecc.domains.collaboration.delegations`'s own lazy-expiry
race between two concurrent requests both transitioning the same overdue
`proposed` delegation is the concrete case this closes) produces at most
one row, not two. This is deliberately a broader guarantee than "replay the
exact same idempotency-keyed HTTP request" (`idempotency_records`'s own
job) -- it holds even when two genuinely different requests both cause the
same notification to be due.

No `owner_id`/`visibility` columns -- a notification is inherently
recipient-scoped (`account_id` already answers "who may see this row" more
precisely than the generic visibility mechanism), the identical reasoning
migration `0064_phase8_delegations.py` gives for its own three tables.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0065_phase8_notifications"
down_revision = "0064_phase8_delegations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.create_table(
        "member_notifications",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("account_id", uuid, nullable=False),
        sa.Column("notification_type", sa.String(100), nullable=False),
        sa.Column("resource_ref", sa.String(200), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "workspace_id",
            "account_id",
            "notification_type",
            "resource_ref",
            name="uq_member_notifications_dedup",
        ),
    )
    op.create_index(
        "ix_member_notifications_workspace_account_created",
        "member_notifications",
        ["workspace_id", "account_id", sa.text("created_at DESC")],
    )
    op.create_index(
        "ix_member_notifications_unread",
        "member_notifications",
        ["workspace_id", "account_id"],
        postgresql_where=sa.text("read_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_member_notifications_unread", table_name="member_notifications")
    op.drop_index(
        "ix_member_notifications_workspace_account_created", table_name="member_notifications"
    )
    op.drop_table("member_notifications")
