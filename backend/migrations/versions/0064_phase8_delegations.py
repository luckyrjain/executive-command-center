"""Phase 8 Task 6: `delegations`, `delegation_evidence` and
`delegation_events` (`docs/superpowers/plans/2026-08-01-phase-8-multi-
user.md` Task 6, `docs/phases/phase-008/DELEGATION-CONTRACT.md`).

**`delegation_evidence` is a real addition beyond the plan's own literal
migration bullet, disclosed rather than silently added.** The plan names
only `delegations`/`delegation_events`, but `DELEGATION-CONTRACT.md` is
explicit that "acceptance creates scoped, read-only `resource_grants` rows
... naming exactly the evidence the delegation itself names" -- the
contract requires a delegation to name a set of evidence resources
somewhere, and the plan's own `delegations` column list (`obligation_type`/
`obligation_resource_id`, singular) has no room for that plural set. This
child table is the natural place: one row per `(resource_type,
resource_id)` a delegation's acceptance should grant the recipient
read-only access to, beyond the obligation resource itself (which
`ecc.domains.collaboration.delegations.accept_delegation_endpoint` always
grants in addition, so a recipient can always at least see what they
accepted).

**No `owner_id`/`visibility` columns on any of these three tables,**
unlike the ~69-table migration `0063_phase8_authz_visibility.py` gave
every other workspace-scoped table -- a delegation already has a precise,
inherently bilateral access model (`delegator_account_id`/
`recipient_account_id`, both explicit columns), not the ambiguous
"who on the team should see this" question `owner_id`/`visibility`/
`resource_grants` exists to answer. Folding delegations into that generic
mechanism instead of using the two account columns directly would be
strictly less precise, not more general.

**`delegations.status`'s `CHECK` constraint allows `cancelled` even though
no endpoint in this task can ever produce it** -- the state machine
`DELEGATION-CONTRACT.md` names is `accepted -> completed|revoked|cancelled`,
but `API-SCHEMAS.md`'s own route list for this task is `POST /delegations/
{id}/accept|reject|revoke|complete`, with no `/cancel` route at all.
`cancelled` is reserved for a future *system*-initiated transition (Task
8's member-removal flow force-resolving an active delegation involving a
removed member), not a user-facing action -- schema-level future-proofing,
the identical pattern `0049_phase6_decisions_incidents.py`'s own
`DecisionStatus` gave `'superseded'`.

**`expired` is reachable, not aspirational** -- `ecc.domains.collaboration.
delegations._maybe_expire` lazily transitions a `proposed` delegation past
its own `due_at` to `expired` the moment any endpoint next reads or
mutates it (no background scheduler needed for this task's own scope, and
none is named in the plan), writing a `delegation_events` row with a
`NULL` `actor_account_id` (no human actor caused it).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0064_phase8_delegations"
down_revision = "0063_phase8_authz_visibility"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.create_table(
        "delegations",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("delegator_account_id", uuid, nullable=False),
        sa.Column("recipient_account_id", uuid, nullable=False),
        sa.Column("obligation_type", sa.String(100), nullable=False),
        sa.Column("obligation_resource_id", uuid, nullable=False),
        sa.Column("expected_outcome", sa.Text(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('proposed','accepted','rejected','expired',"
            "'completed','revoked','cancelled')",
            name="ck_delegations_status",
        ),
        sa.CheckConstraint(
            "delegator_account_id != recipient_account_id", name="ck_delegations_not_self"
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["delegator_account_id"], ["accounts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["recipient_account_id"], ["accounts.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_delegations_workspace_delegator",
        "delegations",
        ["workspace_id", "delegator_account_id"],
    )
    op.create_index(
        "ix_delegations_workspace_recipient",
        "delegations",
        ["workspace_id", "recipient_account_id"],
    )

    op.create_table(
        "delegation_evidence",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("delegation_id", uuid, nullable=False),
        sa.Column("resource_type", sa.String(100), nullable=False),
        sa.Column("resource_id", uuid, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["delegation_id"], ["delegations.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "delegation_id", "resource_type", "resource_id", name="uq_delegation_evidence_resource"
        ),
    )
    op.create_index("ix_delegation_evidence_delegation", "delegation_evidence", ["delegation_id"])

    op.create_table(
        "delegation_events",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("delegation_id", uuid, nullable=False),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("actor_account_id", uuid, nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("detail", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["delegation_id"], ["delegations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_account_id"], ["accounts.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_delegation_events_delegation", "delegation_events", ["delegation_id", "occurred_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_delegation_events_delegation", table_name="delegation_events")
    op.drop_table("delegation_events")

    op.drop_index("ix_delegation_evidence_delegation", table_name="delegation_evidence")
    op.drop_table("delegation_evidence")

    op.drop_index("ix_delegations_workspace_recipient", table_name="delegations")
    op.drop_index("ix_delegations_workspace_delegator", table_name="delegations")
    op.drop_table("delegations")
