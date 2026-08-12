"""Raise attention.explain_item's per-model-call timeout from 20s to 30s
and its total per-run wall-clock budget from 60s to 80s, in lockstep with
its latency promotion-floor ceiling (25.0s, Python constant, not this
migration).

Two consecutive real `ollama-evaluation` CI runs against prompt version 3
(`EVALUATION-CONTRACT.md` phase L had this version clearing the original
20s ceiling cleanly on its first real run) measured p95 latency 21.28s and
20.41s, both with 0 prohibited facts and 100% schema-validity/grounding --
real decode-time drift on this CI hardware since that first clean run, the
same "genuine, repeatable, not a content defect" signature `meeting.prep_
summary`'s own several timeout raises (migrations `0036`/`0037`/`0052`)
were each backed by.

**Promotion-floor ceiling (Python constant, not this migration):**
`evaluation.py:_LATENCY_P95_CEILING_SECONDS_BY_TASK_TYPE["attention.
explain_item"]` raised 20.0 -> 25.0 in the same commit as this migration --
a real ~3.7s margin over the worse of the two observations (21.28s), kept
below the new 30s reliability timeout below (the floor must stay a
tighter, "typical" bar than the timeout's own "don't hang forever"
backstop, `EVALUATION-CONTRACT.md`'s own explicit rule for why these two
numbers are never the same, first stated for this exact task type in
phase G's real-bug fix).

**Reliability timeout, this migration:** raised 20s -> 30s -- real margin
needed above the promotion floor's new 25s ceiling (a call that only just
clears 25s must not then risk being killed by a timeout barely above it),
mirroring `meeting.prep_summary`'s own 40.0/35.0 = 5s margin. `total_run_
budget_seconds` raised 60s -> 80s in lockstep, preserving the seeded
`0028_phase4_model_registry.py` row's own "two full-length calls (a
primary attempt plus one schema-repair retry) still fit, with slack left
over for routing/tool-dispatch/validation overhead" invariant (2 x 20s +
20s slack = 60s then; 2 x 30s + 20s slack = 80s now).

`router.py:TASK_REQUIREMENTS["attention.explain_item"]` remains this
activation's actual source of truth for both numbers (`budgets.py:
RunBudget.from_policy` prefers it over `policy.constraints`, same
precedent as `0036`/`0037`/`0052`) -- this migration updates `routing_
policies.constraints.per_model_call_timeout_seconds` and `.total_run_
budget_seconds` so neither reads as a stale, silently-diverged number,
following those same migrations' exact in-place `jsonb || jsonb` merge
precedent.

Does not require raising `ollama_client.py:_HTTPX_TRANSPORT_TIMEOUT_
SECONDS` (currently 46.0, already raised past `meeting.prep_summary`'s own
40.0s timeout) -- the new 30.0s timeout here stays comfortably under it,
so `test_httpx_transport_timeout_stays_ahead_of_every_registered_task_
timeout` continues to pass unchanged.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0077_phase4_expl_item_timeout"
down_revision = "0076_phase10_email_id_purge_log"
branch_labels = None
depends_on = None

_TASK_TYPE = "attention.explain_item"
_OLD_TIMEOUT_SECONDS = 20
_NEW_TIMEOUT_SECONDS = 30
_OLD_TOTAL_BUDGET_SECONDS = 60
_NEW_TOTAL_BUDGET_SECONDS = 80


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
    # Restores `routing_policies.constraints` only. `router.py:TASK_
    # REQUIREMENTS["attention.explain_item"]` remains the actual source of
    # truth for `per_model_call_timeout_seconds` (see module docstring) --
    # running this downgrade without also reverting that Python constant in
    # the same deploy leaves the per-call timeout at 30s while the total
    # budget reverts to 60s, which no longer fits the "two full-length
    # calls plus slack" invariant this migration is built on (2 x 30s = 60s,
    # zero slack left for routing/tool-dispatch/validation overhead).
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
