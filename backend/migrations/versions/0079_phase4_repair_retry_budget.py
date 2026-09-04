"""Raise every registered task type's total per-run wall-clock budget in
lockstep with `validator.py:validate_with_bounded_repair`'s widened repair
bound (Phase Q, `EVALUATION-CONTRACT.md`): one repair retry -> two
(`_MAX_REPAIR_ATTEMPTS` 2 -> 3 total attempts).

**Why every task type, not just `meeting.prep_summary`.** The widened
bound is a generic `validator.py` change (Decision 4/5's mechanism, not a
per-task branch), so it applies uniformly to `attention.explain_item`,
`meeting.prep_summary`, `personal.generate_insight`, and
`email.detect_action` alike. Every one of their `routing_policies.
constraints.total_run_budget_seconds` rows was previously sized to the
same "two full-length calls (a primary attempt plus one schema-repair
retry) plus slack" invariant every prior timeout migration here documents
(`0028`/`0037`/`0052`/`0057`/`0072`/`0077`/`0078`) -- leaving that budget
unraised while widening the repair bound would starve the new second
retry: it would hit `RunGuard.check_total_budget` (`budget_exceeded`,
`status=degraded`) well before a third full-length call could complete,
converting an intermittent `schema_invalid` into a near-certain
`budget_exceeded` on the very examples this fix targets -- not a real
improvement.

**The four rows, same "N full-length calls + established slack" formula
each task type's own prior migration already used, just N=3 instead of
N=2** (`per_model_call_timeout_seconds` is unchanged by this migration --
only the call *count* budgeted for changes):

| task_type | timeout | old budget (2 calls + slack) | new budget (3 calls + same slack) |
|---|---|---|---|
| `attention.explain_item` | 30s | 80s (2x30+20) | 110s (3x30+20) |
| `meeting.prep_summary` | 45s | 101s (2x45+11) | 146s (3x45+11) |
| `personal.generate_insight` | 40s | 95s (2x40+15) | 135s (3x40+15) |
| `email.detect_action` | 40s | 95s (2x40+15) | 135s (3x40+15) |

`router.py:TASK_REQUIREMENTS` remains the actual source of truth for
`per_model_call_timeout_seconds` (`budgets.py:RunBudget.from_policy`
prefers it over `policy.constraints`) -- unchanged here, since this
migration only widens the call count the total budget must cover, not any
single call's own timeout. `total_wall_clock_seconds` has no
`TASK_REQUIREMENTS` equivalent (`RunBudget.from_policy`'s own docstring),
so it is read from this row directly; updated here so it does not read as
a stale, silently-diverged number, following `0033_phase4_reflection.py`'s
exact in-place `jsonb || jsonb` merge precedent.

No `ollama_client.py:_HTTPX_TRANSPORT_TIMEOUT_SECONDS` change needed --
that guard-rail is sized against each task's own *per-call* timeout
(unchanged here), not the total run budget.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0079_phase4_repair_retry_budget"
down_revision = "0078_phase4_meeting_timeout4"
branch_labels = None
depends_on = None

# task_type -> (old_total_run_budget_seconds, new_total_run_budget_seconds)
_BUDGET_CHANGES: dict[str, tuple[int, int]] = {
    "attention.explain_item": (80, 110),
    "meeting.prep_summary": (101, 146),
    "personal.generate_insight": (95, 135),
    "email.detect_action": (95, 135),
}


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
    for task_type, (_old, new) in _BUDGET_CHANGES.items():
        op.execute(
            routing_policies.update()
            .where(
                routing_policies.c.task_type == task_type,
                routing_policies.c.status == "active",
            )
            .values(
                constraints=routing_policies.c.constraints.op("||")(
                    sa.literal(
                        {"total_run_budget_seconds": new},
                        type_=postgresql.JSONB(),
                    )
                ),
                updated_at=sa.func.now(),
            )
        )


def downgrade() -> None:
    routing_policies = _routing_policies_table()
    for task_type, (old, _new) in _BUDGET_CHANGES.items():
        op.execute(
            routing_policies.update()
            .where(
                routing_policies.c.task_type == task_type,
                routing_policies.c.status == "active",
            )
            .values(
                constraints=routing_policies.c.constraints.op("||")(
                    sa.literal(
                        {"total_run_budget_seconds": old},
                        type_=postgresql.JSONB(),
                    )
                ),
                updated_at=sa.func.now(),
            )
        )
