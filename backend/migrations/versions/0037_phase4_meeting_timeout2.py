"""Raise meeting.prep_summary's per-model-call timeout from 25s to 32s,
and its total per-run wall-clock budget from 60s to 75s in lockstep.

Phase 4 post-launch audit, phase G. `0036_phase4_meeting_prep_timeout.py`
raised this task's per-model-call timeout 20s -> 25s (phase C) for real
margin over an observed ~20.1-20.2s near-miss on its three heaviest
evaluation examples. That margin did not hold in real CI: two subsequent
live-model runs (PR #51, PR #53) both still missed the floor on
`rich_all_sections` specifically, at p95 25.09s and 25.30s -- essentially
consuming the entire 25s budget rather than comfortably clearing it.

Raised again to 32s -- a real ~6.7s margin over the worst *post-fix*
observation (25.30s), not the original pre-fix near-miss, so this margin
is sized against the actual failure this fix is trying to clear.

`total_run_budget_seconds` raised 60s -> 75s in lockstep, preserving the
same "two full-length calls (a primary attempt plus one schema-repair
retry) still fit, with slack left over for routing/tool-dispatch/
validation overhead" invariant `0036`'s own per-model-call raise
established (2 x 25s + 10s slack = 60s then; 2 x 32s + 11s slack = 75s
now) -- raising the per-call timeout alone, without this, would have left
a schema-repair retry that happens to run close to full length on both
attempts hitting `RunBudgetExceeded` (`budget_exceeded`, a degraded
outcome) instead of the timeout this change is trying to clear, which
would not be a real improvement.

`router.py:TASK_REQUIREMENTS["meeting.prep_summary"]` remains this
activation's actual source of truth for both numbers
(`budgets.py:RunBudget.from_policy` prefers it over `policy.constraints`
for `per_model_call_seconds`, same precedent `max_output_tokens` already
established; `total_wall_clock_seconds` is read from this row directly,
with no `TASK_REQUIREMENTS` equivalent) -- this migration updates
`routing_policies.constraints.per_model_call_timeout_seconds` and
`.total_run_budget_seconds` so neither reads as a stale, silently-diverged
number, following `0033_phase4_reflection.py`'s exact in-place
`jsonb || jsonb` merge precedent for updating an existing active row's
constraints without a new `routing_policies` version.

Also required raising `ollama_client.py:_HTTPX_TRANSPORT_TIMEOUT_SECONDS`
(30.0 -> 37.0) in the same commit, not this migration -- a Python module
constant, not a seeded row -- to stay a real margin ahead of this task's
new 32s timeout; the guard-rail test `0036`'s own review pass added
(`test_httpx_transport_timeout_stays_ahead_of_every_registered_task_
timeout`) exists for exactly this and would fail without it.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0037_phase4_meeting_timeout2"
down_revision = "0036_phase4_meeting_prep_timeout"
branch_labels = None
depends_on = None

_TASK_TYPE = "meeting.prep_summary"
_OLD_TIMEOUT_SECONDS = 25
_NEW_TIMEOUT_SECONDS = 32
_OLD_TOTAL_BUDGET_SECONDS = 60
_NEW_TOTAL_BUDGET_SECONDS = 75


def _routing_policies_table() -> sa.TableClause:
    return sa.table(
        "routing_policies",
        sa.column("task_type", sa.String()),
        sa.column("status", sa.String()),
        sa.column("constraints", postgresql.JSONB()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )


def upgrade() -> None:
    routing_policies = _routing_policies_table()
    op.execute(
        routing_policies.update()
        .where(routing_policies.c.task_type == _TASK_TYPE, routing_policies.c.status == "active")
        .values(
            # `jsonb || jsonb` merge (0033's precedent, see that
            # migration's own comment on why a Python dict via
            # `sa.literal(..., type_=JSONB())` is required here, not a
            # cast Python string) -- overwrites just these two keys,
            # leaving every other seeded constraint untouched.
            constraints=routing_policies.c.constraints.op("||")(
                sa.literal(
                    {
                        "per_model_call_timeout_seconds": _NEW_TIMEOUT_SECONDS,
                        "total_run_budget_seconds": _NEW_TOTAL_BUDGET_SECONDS,
                    },
                    type_=postgresql.JSONB(),
                )
            ),
            updated_at=sa.func.now(),
        )
    )


def downgrade() -> None:
    routing_policies = _routing_policies_table()
    op.execute(
        routing_policies.update()
        .where(routing_policies.c.task_type == _TASK_TYPE, routing_policies.c.status == "active")
        .values(
            constraints=routing_policies.c.constraints.op("||")(
                sa.literal(
                    {
                        "per_model_call_timeout_seconds": _OLD_TIMEOUT_SECONDS,
                        "total_run_budget_seconds": _OLD_TOTAL_BUDGET_SECONDS,
                    },
                    type_=postgresql.JSONB(),
                )
            ),
            updated_at=sa.func.now(),
        )
    )
