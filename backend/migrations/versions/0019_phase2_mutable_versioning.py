"""Add updated_at/version to entity_aliases, knowledge_claims and
entity_operations, closing a gap an audit of the shipped Phase 2 code
found: DATA-MODEL.md's Rules section says "Every table carries
workspace_id, timestamps and optimistic version where mutable", but these
three tables are all mutated in place by existing code (merge's
_rehome_aliases UPDATEs entity_aliases.entity_id; claim supersede UPDATEs
knowledge_claims.superseded_by/valid_to; reverse/split UPDATE
entity_operations.status) while only ever having carried created_at, with
no updated_at or version column at all.

Scope note: this adds the columns and has every existing UPDATE statement
maintain them accurately (version incremented, updated_at refreshed), so
each row's own history is self-describing. It deliberately does NOT wire
up new expected_version request-body fields/409 conflict responses on the
supersede/reverse/split endpoints the way entities.py's PATCH and
entity_operations.py's merge already do for pkos_nodes.version -- those
three mutations are already race-protected by a `SELECT ... FOR UPDATE`
lock plus a business-state check (superseded_by IS NOT NULL,
status != 'active') that reject the specific double-mutation race that
matters, and entity_aliases has no direct end-user endpoint at all (it's
mutated only as an internal side effect of merge, keyed by bulk
source_id, not a single alias row an API caller could plausibly pass an
expected_version for). Wiring optimistic-conflict UX on top would be a
real API-contract change requiring coordinated frontend updates, not
schema hygiene.

Unlike migration 0016's evidence_id column, server_default is kept
permanently (never dropped) on both new columns -- matching pkos_nodes'
own version/status/confidence columns (migration 0010), which also keep
their server_default forever as a safety net for any INSERT that doesn't
explicitly list every column, rather than requiring every future
raw-SQL writer (test fixtures, scripts) to be updated in lockstep.
"""

import sqlalchemy as sa
from alembic import op

revision = "0019_phase2_mutable_versioning"
down_revision = "0018_phase2_split_operation"
branch_labels = None
depends_on = None

_TABLES = ("entity_aliases", "knowledge_claims", "entity_operations")


def upgrade() -> None:
    for table in _TABLES:
        op.add_column(
            table,
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                nullable=False,
                server_default=sa.text("now()"),
            ),
        )
        op.add_column(
            table,
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        )
        op.execute(f"UPDATE {table} SET updated_at = created_at")  # noqa: S608


def downgrade() -> None:
    for table in _TABLES:
        op.drop_column(table, "version")
        op.drop_column(table, "updated_at")
