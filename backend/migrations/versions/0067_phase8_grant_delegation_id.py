"""Phase 8 final review fix: `resource_grants.delegation_id`.

**A real correctness bug, found and fixed during the whole-phase final
review, not part of the plan's own Task 6 text.** `delegations.py`'s own
`_grant_evidence`/`_revoke_evidence_grants` matched evidence grants purely
on `(workspace_id, grantee_account_id, resource_type, resource_id,
revoked_at IS NULL)` -- there was no column linking a `resource_grants` row
back to the delegation that created it. If the same recipient is delegated
two different obligations that both happen to name the same evidence
resource (a realistic scenario: two colleagues each delegate a task
referencing the same incident to one person), completing or revoking
delegation A would revoke the evidence grant for delegation B too, even
though B is still `accepted` -- silently breaking the recipient's access to
evidence for a delegation that is still active, and violating
`DELEGATION-CONTRACT.md`'s own "acceptance creates ... exactly the evidence
the delegation itself names" guarantee (which implicitly promises that
guarantee holds independently per delegation, not merely in aggregate).

Nullable, not backfilled from any inference -- every `resource_grants` row
created before this migration has no way to know which delegation (if any)
it belongs to, and guessing would risk attributing a row to the wrong
delegation, worse than leaving it `NULL` (meaning: "not a delegation-
evidence grant, or created before this column existed"). `_revoke_evidence_
grants`'s own fix (this migration's paired application-code change) scopes
its `UPDATE` by `delegation_id` going forward, so only grants this exact
delegation itself created are ever revoked by it.

`ON DELETE SET NULL`, not `CASCADE` or `RESTRICT` -- `delegations` rows are
never deleted (`0064_phase8_delegations.py`'s own state-machine-only
lifecycle has no delete path), so this is defensive rather than a path this
codebase's own code ever exercises: if a `delegations` row were ever
removed by some future path, the grant it created should outlive it as an
ordinary (now delegation-unlinked) grant, not be silently deleted itself
(a real access-revoking side effect a schema-only FK action should never
cause) or block the delete (`RESTRICT`, wrong for a column whose whole
purpose is an optional back-reference).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0067_phase8_grant_delegation_id"
down_revision = "0066_phase8_ownership_transfers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.add_column("resource_grants", sa.Column("delegation_id", uuid, nullable=True))
    op.create_foreign_key(
        "fk_resource_grants_delegation",
        "resource_grants",
        "delegations",
        ["delegation_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_resource_grants_delegation",
        "resource_grants",
        ["delegation_id"],
        postgresql_where=sa.text("delegation_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_resource_grants_delegation", table_name="resource_grants")
    op.drop_constraint("fk_resource_grants_delegation", "resource_grants", type_="foreignkey")
    op.drop_column("resource_grants", "delegation_id")
