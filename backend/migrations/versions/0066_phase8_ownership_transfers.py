"""Phase 8 Task 8: `ownership_transfers` (`docs/superpowers/plans/2026-08-01-
phase-8-multi-user.md` Task 8, `docs/phases/phase-008/DATA-MODEL.md`).

**Exactly the plan's own literal column list** -- `id`, `workspace_id`,
`resource_type`, `resource_id`, `from_account_id`, `to_account_id`,
`status`, `initiated_by`, `created_at`, `completed_at`.

**`status` carries no `CHECK` constraint, unlike every other status-like
column this phase has added (`delegations.status`, `invitations`' three
terminal-state columns).** Neither the plan nor any contract document names
a multi-step transfer lifecycle (no "pending, awaiting the new owner's
acceptance" state is ever described, unlike `delegations`' own explicit
`proposed -> accepted|rejected` diagram) -- `ecc.platform.authz`'s own
`create_ownership_transfer_endpoint` only ever writes `'completed'`, with
`completed_at` always equal to `created_at` in the same statement. Left
open (no `CHECK`) rather than hard-coded to a single-value enum, since nothing
in this migration's own scope rules out a future task adding a genuine
bilateral confirmation step -- narrowing a real future need to a today's
single-value CHECK would be a bigger footgun than leaving one enum column
unconstrained, unlike `delegations.status`, whose full contract-defined
state machine (including the not-yet-reachable `cancelled`) is already
completely known today.

`initiated_by` composite-FKs to `users.(workspace_id, id)` -- the same
`granted_by`/`invited_by` shape `resource_grants`/`invitations` already use
-- not `accounts.id`, since the acting party's authority is scoped to one
workspace at a time (`AuthContext.user_id` is always a `users.id`).
`from_account_id`/`to_account_id` FK `accounts.id` directly, matching
`resource_grants.grantee_account_id`'s own shape -- ownership is expressed
in account terms (workspace-independent identity), not per-workspace
`users` rows, the same reasoning `delegations.delegator_account_id`/
`recipient_account_id` already established.

No `owner_id`/`visibility` columns on this table itself -- an ownership
transfer's own two account columns already answer "who may see this row"
precisely (`ecc.platform.authz.list_ownership_transfers_endpoint` scopes by
`from_account_id`/`to_account_id` for non-`owner`/`admin` callers), the
identical reasoning `delegations`'s own migration gave for skipping the
generic mechanism.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0066_phase8_ownership_transfers"
down_revision = "0065_phase8_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.create_table(
        "ownership_transfers",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("resource_type", sa.String(100), nullable=False),
        sa.Column("resource_id", uuid, nullable=False),
        sa.Column("from_account_id", uuid, nullable=False),
        sa.Column("to_account_id", uuid, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("initiated_by", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["from_account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["to_account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "initiated_by"],
            ["users.workspace_id", "users.id"],
            name="fk_ownership_transfers_workspace_initiated_by",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_ownership_transfers_resource",
        "ownership_transfers",
        ["workspace_id", "resource_type", "resource_id"],
    )
    op.create_index(
        "ix_ownership_transfers_workspace_from",
        "ownership_transfers",
        ["workspace_id", "from_account_id"],
    )
    op.create_index(
        "ix_ownership_transfers_workspace_to",
        "ownership_transfers",
        ["workspace_id", "to_account_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_ownership_transfers_workspace_to", table_name="ownership_transfers")
    op.drop_index("ix_ownership_transfers_workspace_from", table_name="ownership_transfers")
    op.drop_index("ix_ownership_transfers_resource", table_name="ownership_transfers")
    op.drop_table("ownership_transfers")
