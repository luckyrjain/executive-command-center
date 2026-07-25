"""Raise meeting.prep_summary's per-model-call timeout from 20s to 25s.

Phase 4 post-launch audit, phase C. `meeting.prep_summary`'s three
heaviest evaluation examples (`rich_all_sections`, `participants_and_
commitments_heavy`, `timeline_heavy`) measured 20.07-20.2s against real
Ollama (`EVALUATION-CONTRACT.md`'s "Sandbox constraint" section) -- a
genuine, repeatable near-miss against the 20s per-model-call budget this
task type shared with `attention.explain_item`, not one-off CI noise.
This task's full seven-section evidence-bundle prompt is substantially
larger than `attention.explain_item`'s single-item explanation, and this
small local model's decode throughput on the larger prompt needs real
margin, not a budget that sits right at the observed p95.

`router.py:TASK_REQUIREMENTS["meeting.prep_summary"].timeout_seconds` is
this activation's actual source of truth for the per-call deadline
(`budgets.py:RunBudget.from_policy` prefers it over `policy.constraints`
whenever a `TASK_REQUIREMENTS` entry exists, same precedent `max_output_
tokens` already established) -- this migration updates only the
`routing_policies.constraints.per_model_call_timeout_seconds` row so it
does not read as a stale, silently-diverged number, following `0033_
phase4_reflection.py`'s exact in-place `jsonb || jsonb` merge precedent
for updating an existing active row's constraints without a new
`routing_policies` version.

This migration does **not** by itself change enforced runtime behavior --
before this same phase C change, `RunBudget.per_model_call_seconds` was
computed correctly but never actually reached `OllamaAdapter.generate()`'s
real per-call deadline, which was silently always that module's fixed
constructor-default constant regardless of task type (`ollama_client.py`'s
own updated module docstring). `runtime.py:execute_run`'s `call_model`
closure now passes `budget.per_model_call_seconds` into `generate()`'s new
`timeout_seconds` override, so this row (and `TASK_REQUIREMENTS`'s Python
value) are what genuinely takes effect from this change onward.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0036_phase4_meeting_prep_timeout"
down_revision = "0035_phase4_meeting_eval"
branch_labels = None
depends_on = None

_TASK_TYPE = "meeting.prep_summary"
_OLD_TIMEOUT_SECONDS = 20
_NEW_TIMEOUT_SECONDS = 25


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
            # cast Python string) -- overwrites just this one key,
            # leaving every other seeded constraint untouched.
            constraints=routing_policies.c.constraints.op("||")(
                sa.literal(
                    {"per_model_call_timeout_seconds": _NEW_TIMEOUT_SECONDS},
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
                    {"per_model_call_timeout_seconds": _OLD_TIMEOUT_SECONDS},
                    type_=postgresql.JSONB(),
                )
            ),
            updated_at=sa.func.now(),
        )
    )
