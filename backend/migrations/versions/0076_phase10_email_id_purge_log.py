"""Phase 10 Task 7 Loop 2 round 8 review: add `email_message_id_purge_log`,
closing a sequential (non-concurrent) cross-owner `pkos_evidence` leak
`gmail_revocation.cascade_email_revocation` had no way to prevent without it.

**The gap this closes.** `cascade_email_revocation`'s own ambiguous-id check
(added Loop 2 round 4 review) only ever compared against *currently live*
`email_messages` rows: "does some *other* owner in this workspace still have
a row with this `external_message_id` right now?" That is correct within a
single cascade run, but the check has no memory across separate cascade
runs. Concrete sequence: owner A and owner B, same workspace, each connected
their own Gmail account and coincidentally share one message's raw
`external_message_id` (only unique per `(workspace_id, thread_id)`, migration
`0069`, not per-workspace). A disables `email` first: A's own check correctly
sees B's still-live row and leaves the shared `pkos_evidence` row alone. A's
own `email_messages` row for that id is still deleted (it is *A's own* data,
correctly purged) -- but that deletion also erases the only signal that made
the id ambiguous in the first place. B later disables `email` independently:
B's own check now finds no *other* owner's live row for that id (A's is
already gone), concludes it is unambiguously safe, and deletes the shared
`pkos_evidence` row after all -- destroying evidence A's own cascade had
specifically preserved.

**The fix.** A tiny, append-only ledger, written to (not read from) by every
cascade run for every `external_message_id` it processes, regardless of
whether that particular id turned out ambiguous or not: "owner O once had a
message with this raw id in this workspace." `cascade_email_revocation`'s
ambiguity check now additionally treats an id as ambiguous if a *different*
owner has a ledger row for it -- catching exactly the sequential case above,
since A's ledger row survives A's own `email_messages` deletion. Deliberately
never deleted or pruned by any code path in this codebase -- `owner_id` here
carries no FK of its own (member removal never hard-deletes a `users` row,
only flips `workspace_memberships.status`, so there is no owner-removal event
to hang a purge off), and the `workspace_id` `ON DELETE CASCADE` FK below is
this table's only removal path; a handful of opaque Gmail message ids plus
owner references carries no content of its own to purge, matching this
table's own `deletion_jobs` sibling's identical "permanent record of an
action, not itself in scope for a domain's own privacy purge" precedent
(`0075_phase10_thread_forget.py`'s own docstring makes the same call for its
`deletion_jobs.resource_id` addition).

Corrects `docs/phases/phase-010/DATA-MODEL.md`'s own prior "Task 7 needed no
new migration at all" claim, now stale.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0076_phase10_email_id_purge_log"
down_revision = "0075_phase10_thread_forget"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.create_table(
        "email_message_id_purge_log",
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("external_message_id", sa.Text(), nullable=False),
        sa.Column("owner_id", uuid, nullable=False),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint(
            "workspace_id", "external_message_id", "owner_id", name="pk_email_message_id_purge_log"
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("email_message_id_purge_log")
