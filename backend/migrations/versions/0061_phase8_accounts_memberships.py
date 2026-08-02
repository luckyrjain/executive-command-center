"""Create the Phase 8 account/membership framework for Task 1
(account/membership/session framework -- platform identity layer, not the
full nine-task `PHASE-008-multi-user.md` scope).

Phase 8 Task 1 (`docs/superpowers/specs/2026-08-01-phase-8-multi-user-
design.md` Decision 1, `docs/phases/phase-008/DATA-MODEL.md` v0.2.0).

**`accounts` is new and workspace-independent; `users` keeps its exact
current FK role.** Design doc Decision 1's central choice: rather than
making `users` itself span workspaces (which would require rewriting
every one of the ~14 existing `owner_id` composite FKs to `users.
(workspace_id, id)` across Phase 1/7 tables -- a wide, high-risk migration
for no correctness benefit), a new `accounts` table becomes the thing a
person authenticates as, and `users` gains only an `account_id` column.
Every existing `owner_id`/`created_by`/`updated_by` FK across this
codebase is completely unaffected by this migration -- `users.
(workspace_id, id)` still means exactly what it always has.

**`email`/`password_hash` move from `users` to `accounts`, not
duplicated.** Backfill: one `accounts` row per existing `users` row (not
deduplicated by email -- see the loop below for why collapsing two
different `users` rows that happen to share an email string would be a
real identity-merging security bug, not a convenience). `accounts.email`
is `UNIQUE` globally (case-folded at write time, matching every future
write path in `ecc.domains.identity.accounts`); if the backfill would
violate that (two existing `users` rows already share the exact same
email, which no seed/fixture data in this repository has ever done, per
`scripts/seed_phase1_acceptance.py`'s own distinct-per-workspace emails
and `scripts/bootstrap_dev.py`'s own single fixed email), this migration
fails loudly with a clear `IntegrityError` rather than silently merging
two identities or silently dropping data -- the same "fail loud on a
real collision, don't guess" discipline this repository's idempotency-key
conflict handling already uses.

**`workspace_memberships` carries the mutable role/status a removed
member's historical ownership must survive losing.** Split deliberately
from `users` (which stays a stable, never-deleted FK anchor): removing a
member sets `workspace_memberships.status = 'removed'` and never touches
the `users` row every `owner_id`/`created_by` column across the whole
system points to, so a removed member's historically owned/authored
records stay attributable and un-orphaned. Every pre-existing `users` row
is backfilled an `active`/`owner`-role membership (self-invited, since no
real invitation flow existed before this row was created) -- the
workspace's original sole user becomes that workspace's first owner,
matching how `scripts/bootstrap_dev.py`'s dev-only flow already treated
the sole user as the workspace's de facto owner.

**No `invitations`/`resource_grants`/authorization-matrix tables here.**
Those are Task 2 and Task 3's own scope respectively, per the
implementation plan's own "framework first, one reference slice, then
breadth" sequencing (Phase 7 Task 1's identical precedent).
"""

from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0061_phase8_accounts_memberships"
down_revision = "0060_phase7_insight_eval_v2"
branch_labels = None
depends_on = None

_ROLES = "'owner', 'admin', 'member', 'viewer'"
_MEMBERSHIP_STATUSES = "'active', 'suspended', 'removed'"


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)
    bind = op.get_bind()

    # --- accounts -----------------------------------------------------------

    op.create_table(
        "accounts",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("email", sa.String(320), nullable=False, unique=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("disabled_at", sa.DateTime(timezone=True), nullable=True),
    )

    # --- users gains account_id, loses email/password_hash ------------------

    op.add_column("users", sa.Column("account_id", uuid, nullable=True))

    existing_users = (
        bind.execute(sa.text("SELECT id, email, password_hash, created_at FROM users"))
        .mappings()
        .all()
    )
    for row in existing_users:
        account_id = uuid4()
        # `.strip().casefold()`, matching `ecc.domains.identity.accounts.
        # _normalize_email` exactly -- a bare `.casefold()` here would let a
        # legacy `users.email` with incidental leading/trailing whitespace
        # backfill into `accounts.email` un-stripped, silently escaping the
        # `UNIQUE` constraint's collision check against a future stripped
        # write of the same logical address.
        normalized_email = row["email"].strip().casefold()
        bind.execute(
            sa.text(
                """
                INSERT INTO accounts (id, email, password_hash, display_name, created_at)
                VALUES (:id, :email, :password_hash, :display_name, :created_at)
                """
            ),
            {
                "id": account_id,
                "email": normalized_email,
                "password_hash": row["password_hash"],
                "display_name": normalized_email.split("@", 1)[0],
                "created_at": row["created_at"],
            },
        )
        bind.execute(
            sa.text("UPDATE users SET account_id = :account_id WHERE id = :user_id"),
            {"account_id": account_id, "user_id": row["id"]},
        )

    op.alter_column("users", "account_id", nullable=False)
    op.create_unique_constraint(
        "uq_users_workspace_account", "users", ["workspace_id", "account_id"]
    )
    op.create_foreign_key(
        "fk_users_account", "users", "accounts", ["account_id"], ["id"], ondelete="RESTRICT"
    )
    op.drop_constraint("uq_users_workspace_email", "users", type_="unique")
    op.drop_column("users", "email")
    op.drop_column("users", "password_hash")

    # --- workspace_memberships -----------------------------------------------

    op.create_table(
        "workspace_memberships",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("account_id", uuid, nullable=False),
        sa.Column("users_id", uuid, nullable=False),
        sa.Column("role", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("invited_by", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("removed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(f"role IN ({_ROLES})", name="ck_workspace_memberships_role"),
        sa.CheckConstraint(
            f"status IN ({_MEMBERSHIP_STATUSES})", name="ck_workspace_memberships_status"
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["account_id"], ["accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "users_id"],
            ["users.workspace_id", "users.id"],
            name="fk_workspace_memberships_workspace_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id", "invited_by"],
            ["users.workspace_id", "users.id"],
            name="fk_workspace_memberships_workspace_invited_by",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "workspace_id", "account_id", name="uq_workspace_memberships_workspace_account"
        ),
    )

    now_rows = (
        bind.execute(sa.text("SELECT id, workspace_id, account_id, created_at FROM users"))
        .mappings()
        .all()
    )
    for row in now_rows:
        bind.execute(
            sa.text(
                """
                INSERT INTO workspace_memberships (
                    id, workspace_id, account_id, users_id, role, status,
                    invited_by, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, :account_id, :users_id, 'owner', 'active',
                    :users_id, :created_at, :created_at
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": row["workspace_id"],
                "account_id": row["account_id"],
                "users_id": row["id"],
                "created_at": row["created_at"],
            },
        )


def downgrade() -> None:
    bind = op.get_bind()

    op.drop_table("workspace_memberships")

    op.add_column("users", sa.Column("email", sa.String(320), nullable=True))
    op.add_column("users", sa.Column("password_hash", sa.Text(), nullable=True))
    bind.execute(
        sa.text(
            """
            UPDATE users
            SET email = accounts.email, password_hash = accounts.password_hash
            FROM accounts
            WHERE users.account_id = accounts.id
            """
        )
    )
    op.alter_column("users", "email", nullable=False)
    op.alter_column("users", "password_hash", nullable=False)
    op.create_unique_constraint("uq_users_workspace_email", "users", ["workspace_id", "email"])

    op.drop_constraint("fk_users_account", "users", type_="foreignkey")
    op.drop_constraint("uq_users_workspace_account", "users", type_="unique")
    op.drop_column("users", "account_id")

    op.drop_table("accounts")
