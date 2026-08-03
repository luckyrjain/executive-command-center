"""Phase 8 second whole-phase review fix: index `workspace_memberships`
on `(workspace_id, users_id)`.

**A real, previously-unindexed hot path, found in the second whole-phase
review.** `authz.py:_current_role` -- the very first statement of
`authorize()`, called on every single read/write across every one of the
~69 Phase 8-wired resource tables -- queries `workspace_memberships` by
`WHERE workspace_id = :workspace_id AND users_id = :users_id AND status =
'active'`. The identical query shape recurs in `accounts.py` and
`invitations.py`. `workspace_memberships`' only indexes before this
migration were its primary key (`id`) and the unique constraint on
`(workspace_id, account_id)` (from `0061_phase8_accounts_memberships.py`'s
own `uq_workspace_memberships_workspace_account`) -- neither helps a
`users_id` lookup, and Postgres never auto-indexes an FK-referencing
column on its own (the exact justification `0063_phase8_authz_visibility.
py`'s own docstring already gives for adding `ix_{table}_workspace_owner`
to every `owner_id` column it introduces). Without an index here, every
authorized request in the system pays a sequential scan whose cost grows
with total historical membership rows -- `membership_removal.py` never
deletes a row, only flips `status`, so a workspace's own removed-member
history accumulates in the same table forever -- not merely active ones.

Partial (`WHERE status = 'active'`), matching the hot-path predicate this
index exists to serve exactly: `_current_role` and every other caller of
this shape always filters on `status = 'active'` in the same query, so a
full index covering `removed`/`suspended` rows too would only add bulk an
active-membership lookup never needs.
"""

import sqlalchemy as sa
from alembic import op

revision = "0068_phase8_membership_index"
down_revision = "0067_phase8_grant_delegation_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_workspace_memberships_workspace_users",
        "workspace_memberships",
        ["workspace_id", "users_id"],
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index("ix_workspace_memberships_workspace_users", table_name="workspace_memberships")
