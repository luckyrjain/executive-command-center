"""Security remediation Spec A, S1.12: add the global revoke-safety index on
`connector_accounts (provider, external_account_id)` and the ops-only
`personal_visibility_backfill_log` table.

**The index.** `ecc.platform.connector_security.revoke_is_safe` (scope
`global`) answers "does any live `connector_accounts` row, in *any*
workspace, still use this provider account?" before an OAuth grant is
revoked at the provider, via `_LIVE_ROW_EXISTS_SQL`'s `EXISTS` on
`(provider, external_account_id)`. The only existing index covering those
columns is the `(workspace_id, provider, external_account_id)` unique key
from `0044_phase6_connector_platform.py`, which leads with `workspace_id`
and so cannot serve a cross-workspace lookup. This index lets that lookup
avoid a full table scan. It is a named tenancy exception
(`docs/phases/phase-009/TENANCY-CONTRACT.md`,
`docs/security/PHASE-0-SECURITY-BASELINE.md`): the check returns a boolean
only, never row data. Created with a plain `op.create_index`, not
`CONCURRENTLY`: `migrations/env.py` runs every migration inside one
transaction, where `CREATE INDEX CONCURRENTLY` is not allowed.

**The table.** `scripts/backfill_personal_visibility.py` (S1.8(b), a
re-runnable ops command rather than a migration) records one row per
personal-data row it flips to `visibility='private'`: the prior
`visibility` and `owner_id`, and how many `resource_grants` it revoked.
`--restore <run_id>` reads this log to put visibility and owner back
(revoked grants are not restored). The table is created here so that no
later migration has to wait on the backfill's privacy sign-off.
`UNIQUE (run_id, table_name, row_id)` keeps one entry per row per run, so a
retried run cannot log the same row twice; its index leads with `run_id`,
so it also serves `--restore <run_id>`'s lookup (no separate `run_id`
index). `row_id` and `previous_owner_id`
deliberately carry no foreign keys: the rows they point at (across several
tables) may be deleted later, and the log must outlive them. The table has
no `workspace_id` and is not an authz resource; only the ops command reads
or writes it.

**Downgrade warning.** `downgrade()` drops `personal_visibility_backfill_log`
with its rows. Once the backfill has run, that log is the only record
`--restore <run_id>` can use to put previous visibility and owners back. Do
not downgrade past this revision after a backfill run without first
backing the table up (e.g. `pg_dump -t personal_visibility_backfill_log`).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0082_revoke_idx_backfill_log"
down_revision = "0081_backfill_resume_cursor"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.create_index(
        "ix_connector_accounts_provider_external_id",
        "connector_accounts",
        ["provider", "external_account_id"],
    )

    op.create_table(
        "personal_visibility_backfill_log",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("run_id", uuid, nullable=False),
        sa.Column("table_name", sa.Text(), nullable=False),
        sa.Column("row_id", uuid, nullable=False),
        sa.Column("previous_visibility", sa.Text(), nullable=False),
        sa.Column("previous_owner_id", uuid, nullable=True),
        sa.Column("grants_revoked", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "run_id",
            "table_name",
            "row_id",
            name="uq_personal_visibility_backfill_log_run_row",
        ),
    )


def downgrade() -> None:
    op.drop_table("personal_visibility_backfill_log")
    op.drop_index("ix_connector_accounts_provider_external_id", table_name="connector_accounts")
