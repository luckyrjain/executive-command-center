"""Raise meeting.prep_summary's per-model-call timeout from 40s to 45s,
its total per-run wall-clock budget from 91s to 101s, and its latency
promotion-floor ceiling from 35s to 40s, all in lockstep.

`0052_phase4_meeting_timeout3.py` (Phase 4 post-launch audit, phase H)
raised the per-model-call timeout 32s -> 40s and the promotion-floor
ceiling 25.0 -> 35.0, backed by four consistent real measurements
clustering tightly at p95 30.9-31.4s.

This is the follow-up, prompted by Phase O's own real-CI-verification of
an unrelated fix (`EVALUATION-CONTRACT.md`'s Phase O: the meeting-prep
objective-citation grounding fix, `runtime.py`). Four fresh real
`ollama-evaluation` CI runs on `luckyrjain/executive-command-center#215`
measured p95 latency 32.10s, 31.27s, 36.53s, 36.03s -- two comfortably
under the old 35.0s ceiling, two genuinely over it. Every other floor was
unaffected across all four runs (0 prohibited facts every time; the only
per-example misses were the already-tracked, unrelated `sparse_pack`
`schema_invalid` intermittency, phases C/G/J/K) -- this is real, repeated
latency overshoot, not noise correlated with anything else in that PR's
diff (the diff touched only `objective`'s untrusted-data description
text, ~20 tokens, not remotely enough to plausibly explain a multi-second
generation-time shift).

**Promotion-floor ceiling (Python constant, not this migration):**
`evaluation.py:_LATENCY_P95_CEILING_SECONDS_BY_TASK_TYPE["meeting.prep_
summary"]` raised 35.0 -> 40.0 in the same commit as this migration -- a
~3.5s real margin over the worst of the four observations (36.53s), the
same margin-over-worst-observed methodology every prior raise here used.

**Reliability timeout, this migration:** raised 40s -> 45s -- the same 5s
margin above the promotion floor's new 40.0s ceiling that
`0052_phase4_meeting_timeout3.py` itself established (35.0 floor / 40s
timeout), so a call that only just clears the new floor is not then at
risk of being killed by a timeout barely above it.
`total_run_budget_seconds` raised 91s -> 101s in lockstep, preserving the
"two full-length calls (a primary attempt plus one schema-repair retry)
still fit, with slack left over for routing/tool-dispatch/validation
overhead" invariant (2 x 45s + 11s slack = 101s).

`router.py:TASK_REQUIREMENTS["meeting.prep_summary"]` remains this
activation's actual source of truth for both numbers
(`budgets.py:RunBudget.from_policy` prefers it over `policy.constraints`,
same precedent as every prior timeout migration) -- this migration
updates `routing_policies.constraints.per_model_call_timeout_seconds` and
`.total_run_budget_seconds` so neither reads as a stale, silently-diverged
number, following `0052`'s exact in-place `jsonb || jsonb` merge
precedent.

Also requires raising `ollama_client.py:_HTTPX_TRANSPORT_TIMEOUT_SECONDS`
(46.0 -> 51.0) in the same commit, not this migration -- a Python module
constant, not a seeded row -- to stay a real margin ahead of this task's
new 45s timeout; `test_httpx_transport_timeout_stays_ahead_of_every_
registered_task_timeout` exists for exactly this and would fail without
it.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0078_phase4_meeting_timeout4"
down_revision = "0077_phase4_expl_item_timeout"
branch_labels = None
depends_on = None

_TASK_TYPE = "meeting.prep_summary"
_OLD_TIMEOUT_SECONDS = 40
_NEW_TIMEOUT_SECONDS = 45
_OLD_TOTAL_BUDGET_SECONDS = 91
_NEW_TOTAL_BUDGET_SECONDS = 101


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
