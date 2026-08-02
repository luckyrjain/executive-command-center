"""Create the Phase 8 `invitations` table for Task 2 (recipient-bound,
single-use, expiring workspace invitations).

Phase 8 Task 2 (`docs/superpowers/specs/2026-08-01-phase-8-multi-user-
design.md` Decision 3, `docs/superpowers/plans/2026-08-01-phase-8-multi-
user.md` Task 2).

**Every verification property is its own column, not one flag standing in
for four.** `email` is the sole recipient binding (case-folded at write
time, matching `accounts.email`'s own convention); `token_hash` (`SHA-256`
hex, the exact `sessions.token_hash` pattern -- the raw token is returned
once at creation and never stored) makes the invitation single-use in
combination with `accepted_at`; `expires_at` (7 days from creation,
matching `sessions`' own 7-day lifetime, this codebase's established
default freshness window) makes it time-bound; `accepted_at`/`rejected_at`/
`revoked_at` are three separate nullable columns, not one `status` enum,
so "was accepted" and "was later revoked by mistake" can never be
conflated into a single overwritten field -- a `CHECK` constraint below
guarantees at most one of the three is ever set (mutually exclusive
terminal states, per the design doc's own line).

**`invited_by` is `users.id`, not `accounts.id`.** Every invitation is
scoped to exactly one workspace, and `invited_by` records which of that
workspace's own `users` rows (not which global account) sent it -- the
same composite-FK-anchor role `owner_id`/`created_by` already play
elsewhere in this codebase, kept consistent rather than introducing a
workspace-independent actor reference for this one table.

**No index enforcing "at most one pending invitation per (workspace,
email)" at the database level.** The design doc's own definition of
"pending" for this purpose includes `expires_at > now()`, which is not an
immutable predicate a partial index can express -- Postgres partial-index
predicates must be IMMUTABLE, and `now()` is only STABLE. The uniqueness
check is instead enforced procedurally in `ecc.domains.identity.
invitations.create_invitation_endpoint`, via `SELECT ... FOR UPDATE`
locking every existing row for the same `(workspace_id, email)` pair
before evaluating the pending predicate and inserting -- the same
row-locking discipline `lock_idempotency` already uses elsewhere in this
codebase for double-submit protection, applied here to serialize
concurrent invitation-creation attempts for the same recipient instead.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0062_phase8_invitations"
down_revision = "0061_phase8_accounts_memberships"
branch_labels = None
depends_on = None

_ROLES = "'owner', 'admin', 'member', 'viewer'"


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.create_table(
        "invitations",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("invited_by", uuid, nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(f"role IN ({_ROLES})", name="ck_invitations_role"),
        sa.CheckConstraint(
            "(accepted_at IS NOT NULL)::int + (rejected_at IS NOT NULL)::int "
            "+ (revoked_at IS NOT NULL)::int <= 1",
            name="ck_invitations_terminal_states_mutually_exclusive",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "invited_by"],
            ["users.workspace_id", "users.id"],
            name="fk_invitations_workspace_invited_by",
            ondelete="RESTRICT",
        ),
    )
    op.create_index("ix_invitations_workspace_email", "invitations", ["workspace_id", "email"])


def downgrade() -> None:
    op.drop_index("ix_invitations_workspace_email", table_name="invitations")
    op.drop_table("invitations")
