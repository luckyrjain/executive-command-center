"""Architecture review: index two automation hot-path reads that were
scanning without support.

**`workflow_runs` rate-limit count** (`ecc.domains.automation.worker.
_count_runs_in_rate_limit_window`) runs `SELECT COUNT(*) FROM workflow_runs
WHERE workspace_id = :workspace_id AND workflow_id = :workflow_id AND
queued_at > now() - interval '...'` on every single call to `enqueue_run`
-- the one choke point both `POST /automations/runs` and every scheduler
tick's fire path go through. `0039_phase5_workflow_runs.py` only indexed
`(status, queued_at)` and `(workspace_id, status)`; neither supports a
`(workspace_id, workflow_id, queued_at)` lookup, so this scan grows more
expensive as `workflow_runs` grows -- an append-only audit trail per that
migration's own docstring (queued/failed/cancelled runs are never
deleted). Matches the precedent `0068_phase8_membership_index.py` already
set for this exact class of gap (a real, previously-unindexed hot path
found during review).

**`triggers.trigger_type` scheduler-tick scan**
(`ecc.domains.automation.triggers.list_schedule_triggers`) runs `SELECT
... FROM triggers WHERE trigger_type = 'schedule' ORDER BY created_at ASC`
with no `workspace_id` filter (by design -- the module docstring says this
read is deliberately unscoped, evaluated by every scheduler tick).
`0038_phase5_workflow_schema.py` only indexed `(workspace_id,
workflow_id)`, which does not support this query. Partial on `trigger_type
= 'schedule'`, matching the query's own predicate exactly (an `event`- or
`manual`-type trigger row never needs to be found by this scan).
"""

import sqlalchemy as sa
from alembic import op

revision = "0080_automation_hot_path_indexes"
down_revision = "0079_phase4_repair_retry_budget"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_workflow_runs_workspace_workflow_queued",
        "workflow_runs",
        ["workspace_id", "workflow_id", "queued_at"],
    )
    op.create_index(
        "ix_triggers_schedule_created",
        "triggers",
        ["trigger_type", "created_at"],
        postgresql_where=sa.text("trigger_type = 'schedule'"),
    )


def downgrade() -> None:
    op.drop_index("ix_triggers_schedule_created", table_name="triggers")
    op.drop_index("ix_workflow_runs_workspace_workflow_queued", table_name="workflow_runs")
