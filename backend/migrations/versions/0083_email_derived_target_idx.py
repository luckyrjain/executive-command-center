"""Security remediation Spec A (deep review F2, plan task FX3): index the
executed `create` targets of `email_action_detected` recommendations.

`ecc.platform.connector_security.PERSONAL_DERIVED_PREDICATES` recognises a
task/commitment/risk derived from a member's email (created by confirming an
email recommendation) by an `EXISTS` on `recommendations` matching
`(workspace_id, execution_result ->> 'target_id')` to the row's workspace and
id. That predicate runs on every share path (grant, grant preview, ownership
transfer, delegation create/accept) by id, and inside member removal's
`owned_resource_summary` as an anti-join over every task/commitment/risk the
member owns -- under the exclusive membership lock and the 5 s statement
timeout. No existing index covers the JSONB expression
(`ix_recommendations_workspace_target` indexes the recommendation's own
`target_id` column, which is NULL for `create`). Keys `(workspace_id,
operation, target_type, target_id)`: every equality the predicate has is an
index condition, so the anti-join reads only the removed member's
workspace's email recommendations (or probes them per row) and the per-id
share probe is one equality lookup.

**Statistics.** `stx_recommendations_execution_result_target` gives the
planner per-expression statistics for `execution_result ->> 'operation'` /
`'target_type'` / `'target_id'` (a partial index's expression statistics are
not used for selectivity). Without them it assumes 0.5% for each JSONB
equality and ~200 distinct target ids: it estimated ~50 matching rows where
there were 2M and built the hash on the 2M-row side (a 64-batch, on-disk
hash anti-join: 3.6-17 s under load), and it priced each per-task index
probe at ~1000 rows, so it never chose the probe.

**ANALYZE.** `CREATE STATISTICS` creates an empty object, and nothing is
known about the new expressions until the table is next analyzed --
autoanalyze may take days on a large, slowly-changing table. Until then the
removal count keeps the disk-spilling plan above (6-17 s cold at 200k-2M
rows: past the removal's 5 s statement timeout). `upgrade()` therefore ends
with `ANALYZE recommendations` (sampled: ~1 s at 2M rows). ANALYZE's own
SHARE UPDATE EXCLUSIVE lock blocks neither reads nor writes, but it runs in
the migration's transaction, after the index build -- see Locking below.

Partial: only email recommendations that have been executed carry a derived
target, so the index stays small. The predicate spells the type as a
literal so the planner can prove the partial-index condition for generic
(prepared) plans too.

**Locking / rollout.** Created with a plain `op.create_index`, matching the
`0082` precedent (`migrations/env.py` runs each migration in a transaction;
`CREATE INDEX CONCURRENTLY` would need an `op.get_context().autocommit_
block()` and a non-atomic migration, which this repository does not use
yet). The build takes a SHARE lock on `recommendations` for a full scan
of the table (the partial condition filters rows, not the scan). The
migration is one transaction, so that lock is held until COMMIT -- through
the `CREATE STATISTICS` and the `ANALYZE` as well -- and writes to
`recommendations` (recommendation generation, publish/confirm/dismiss) are
blocked for the index build plus the ANALYZE: ~6 s at 2.3M rows. The build
grows with the whole table, not with the number of indexed rows: run it in
a maintenance window on large deployments.

No data change and no provenance column: the executed recommendation's
`execution_result` (written once, by `confirm_recommendation`; executed
email recommendations are redacted in place, never deleted) already is the
provenance, for rows confirmed before and after `ECC_PERSONAL_DATA_ISOLATION`
was enabled. Rows confirmed while the flag was off stay `workspace`-visible
until the FX2 backfill rule makes them private, so this change ships with or
after that rule (plan note N40). `downgrade()` only drops the index and the
statistics (the predicate keeps working, more slowly).
"""

import sqlalchemy as sa
from alembic import op

revision = "0083_email_derived_target_idx"
down_revision = "0082_revoke_idx_backfill_log"
branch_labels = None
depends_on = None

_INDEX = "ix_recommendations_email_derived_target"
_STATISTICS = "stx_recommendations_execution_result_target"


def upgrade() -> None:
    op.create_index(
        _INDEX,
        "recommendations",
        [
            "workspace_id",
            sa.text("(execution_result ->> 'operation')"),
            sa.text("(execution_result ->> 'target_type')"),
            sa.text("(execution_result ->> 'target_id')"),
        ],
        postgresql_where=sa.text(
            "recommendation_type = 'email_action_detected' AND execution_result IS NOT NULL"
        ),
    )

    op.execute(
        f"CREATE STATISTICS {_STATISTICS} "
        "ON (execution_result ->> 'operation'), (execution_result ->> 'target_type'), "
        "(execution_result ->> 'target_id') "
        "FROM recommendations"
    )
    # Populate the new statistics object and the index's expression stats
    # now, not whenever autoanalyze next visits the table (see docstring).
    op.execute("ANALYZE recommendations")


def downgrade() -> None:
    op.execute(f"DROP STATISTICS IF EXISTS {_STATISTICS}")
    op.drop_index(_INDEX, table_name="recommendations")
