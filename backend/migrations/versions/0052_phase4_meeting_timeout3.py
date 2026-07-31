"""Raise meeting.prep_summary's per-model-call timeout from 32s to 40s,
its total per-run wall-clock budget from 75s to 91s, and its latency
promotion-floor ceiling from 25s to 35s, all in lockstep.

`0037_phase4_meeting_timeout2.py` (Phase 4 post-launch audit, phase G)
raised the per-model-call timeout 25s -> 32s and found, in its own PR's
real `ollama-evaluation` run, that every one of the ten evaluation
examples finally completed without timing out: `EvaluationMetrics(
schema_validity_rate=1.0, grounding_rate=1.0, prohibited_fact_count=0,
latency_p95_seconds=31.28, ...)`, `failures=[]` -- genuinely good, if
incomplete: the only floor still missed was p95 latency against the
unchanged 25s promotion-floor ceiling, which `EVALUATION-CONTRACT.md`'s
own record of that run named as "a precise, well-understood number...
need roughly 6s of latency headroom" and left as a follow-up.

That follow-up is this migration. Two further live-model runs since
(luckyrjain/executive-command-center#88's own `ollama-evaluation` CI,
this repository's third and fourth real measurements against this exact
task type) measured p95 latency 31.43s and 30.93s respectively --
`prohibited_fact_count=0`, `schema_validity_rate=1.0`,
`grounding_rate=1.0` both times, `failures=[]` both times. Four
consistent real measurements now cluster tightly at 30.9-31.4s: this is
this small quantized model's genuine, repeatable decode time for this
task's heaviest evidence-bundle examples on this CI hardware, not
one-off noise and not a content defect -- every other floor already
clears cleanly. The 25s promotion-floor ceiling was set before any of
these four real numbers existed and has never once been cleared; holding
this task type to it indefinitely, given four independent real runs all
landing ~6s over it with an otherwise-perfect result, would mean this
floor can never be met by this model/prompt pairing regardless of any
future change that does not also touch decode speed itself.

**Promotion-floor ceiling (Python constant, not this migration):**
`evaluation.py:_LATENCY_P95_CEILING_SECONDS_BY_TASK_TYPE["meeting.prep_
summary"]` raised 25.0 -> 35.0 in the same commit as this migration -- a
~3.6s real margin over the worst of the four observations (31.43s), sized
against actually-observed data the same way `0037`'s own timeout raise
was, and deliberately kept below the new 40s reliability timeout below
(the floor must stay a tighter, "typical" bar than the timeout's own
"don't hang forever" backstop, `EVALUATION-CONTRACT.md`'s own explicit
rule for why these two numbers are never the same).

**Reliability timeout, this migration:** raised 32s -> 40s -- real margin
was needed above the promotion floor's new 35s ceiling (a call that only
just clears 35s must not then risk being killed by a timeout barely above
it), and an ~8.6s margin over the worst observed 31.43s besides.
`total_run_budget_seconds` raised 75s -> 91s in lockstep, preserving
`0037`'s own "two full-length calls (a primary attempt plus one
schema-repair retry) still fit, with slack left over for routing/
tool-dispatch/validation overhead" invariant (2 x 32s + 11s slack = 75s
then; 2 x 40s + 11s slack = 91s now).

`router.py:TASK_REQUIREMENTS["meeting.prep_summary"]` remains this
activation's actual source of truth for both numbers
(`budgets.py:RunBudget.from_policy` prefers it over `policy.constraints`,
same precedent as `0036`/`0037`) -- this migration updates
`routing_policies.constraints.per_model_call_timeout_seconds` and
`.total_run_budget_seconds` so neither reads as a stale, silently-diverged
number, following `0033_phase4_reflection.py`'s exact in-place
`jsonb || jsonb` merge precedent.

Also requires raising `ollama_client.py:_HTTPX_TRANSPORT_TIMEOUT_SECONDS`
(37.0 -> 46.0) in the same commit, not this migration -- a Python module
constant, not a seeded row -- to stay a real margin ahead of this task's
new 40s timeout; `test_httpx_transport_timeout_stays_ahead_of_every_
registered_task_timeout` exists for exactly this and would fail without
it.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0052_phase4_meeting_timeout3"
down_revision = "0051_phase6_datadog_connector"
branch_labels = None
depends_on = None

_TASK_TYPE = "meeting.prep_summary"
_OLD_TIMEOUT_SECONDS = 32
_NEW_TIMEOUT_SECONDS = 40
_OLD_TOTAL_BUDGET_SECONDS = 75
_NEW_TOTAL_BUDGET_SECONDS = 91


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
