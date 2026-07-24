"""Register a second local Ollama model for Phase 4 AI Runtime.

Phase 4's design doc (`docs/superpowers/specs/2026-07-23-phase-4-ai-
runtime-design.md`, Decision 1, "Alternatives considered and deferred, not
rejected outright") explicitly deferred a second registered model to a
later slice: "a second registered model is explicitly in scope for a later
Phase 4 slice, not this one ... Registering it now would mean the routing
algorithm (Decision 2) has to prove tie-breaking between two candidates
before a single candidate's path is even validated end to end." Migration
`0028_phase4_model_registry.py` seeded exactly one `model_definitions` row
for that reason. PR #38 (the first activation slice) has since landed and
been validated end to end -- this migration is that deferred follow-up.

**Model choice.** The design doc named `llama3.2:3b-instruct-q4_K_M` as its
one concrete candidate for this slot, but explicitly left "choosing a
second model or provider" out of that document's own scope (Non-goals) --
it was a deferred name, not a ratified decision. `llama3.2:3b-instruct-
q4_K_M` also carries the Llama Community License, a different license family
from the first model's Apache 2.0 (Qwen2.5) -- a real consideration this
migration's authorization explicitly revisited. `qwen2.5:3b-instruct-
q4_K_M` is registered instead: same model family and Apache 2.0 license as
the first model (no new license to track), while still giving a real
memory/latency/quality delta between the two candidates (roughly double
`qwen2.5:1.5b`'s parameter count) -- exactly the kind of difference the
design doc's own reasoning says is needed to meaningfully exercise
`router.py`'s cost/latency preference steps, not just the final `model_id`
string tie-break.

**`data_classes`/`capabilities` deliberately mirror the first model's
row exactly** (all four data classes, `{extraction, summarization,
explanation}`), not a narrower set -- if the two candidates were eligible
for different data classes or capabilities, `router.py`'s eligibility
filtering alone would decide `attention.explain_item` routing before the
preference/tie-break stage (Decision 2 steps 1-4) is ever reached with two
live candidates, defeating this migration's actual purpose. This is a
concrete case, not a hypothetical -- it was flagged during this task's own
research pass before writing this migration. `context_window_tokens` is set
to 32768, matching migration 0028's same figure for the 1.5B variant and
Qwen2.5's documented native context length for this model family at the 3B
size (yarn-extended 128k variants are a separate configuration, not this
tag).

**`routing_policies.candidates` is documentation/audit metadata here, not
an enforced input.** `router.py`'s `route()` draws its candidate pool from
every *active* `model_definitions` row via `refresh_cache`/`list_models`
(confirmed by reading `router.py` directly before writing this migration)
-- it does not filter by a task type's `routing_policies.candidates` list
at all in this activation. Registering the `model_definitions` row above is
therefore *sufficient by itself* to make `qwen2.5:3b-instruct-q4_K_M` a
real, live candidate for `attention.explain_item` the next time the
router's cache refreshes. This migration still updates the existing active
`routing_policies` row's `candidates` JSONB to include the new model, purely
so that column continues to accurately describe what `route()` actually
considers -- explicitly a documentation-accuracy update, not a behavior
change, and not a reason on its own to bump `version` (no new routing
configuration is being introduced, no evaluation-floor-gated activation
flow exists for `routing_policies` in this activation the way one does for
`prompt_versions`, per `router.py`'s read-only `list_policies`/`get_policy`
functions). `constraints` and `fallback` are untouched: `constraints`
already applies uniformly across every candidate for a task type (not
per-model), and `fallback` remains `{}` -- no fallback-dispatch behavior is
implemented anywhere in this codebase (`route()`/`runtime.py` never read
this field for retry-to-a-different-model logic), so populating it now
would describe a mechanism that does not exist.
"""

from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0032_phase4_second_model"
down_revision = "0031_phase4_evaluation"
branch_labels = None
depends_on = None

_FIRST_MODEL_ID = "qwen2.5:1.5b-instruct-q4_K_M"
_SECOND_MODEL_ID = "qwen2.5:3b-instruct-q4_K_M"
_PROVIDER = "ollama"
_TASK_TYPE = "attention.explain_item"


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    model_definitions = sa.table(
        "model_definitions",
        sa.column("id", uuid),
        sa.column("provider", sa.String()),
        sa.column("model_id", sa.String()),
        sa.column("deployment", sa.String()),
        sa.column("data_classes", postgresql.ARRAY(sa.Text())),
        sa.column("capabilities", postgresql.ARRAY(sa.Text())),
        sa.column("context_window_tokens", sa.Integer()),
        sa.column("structured_output_supported", sa.Boolean()),
        sa.column("status", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    op.execute(
        model_definitions.insert().values(
            id=uuid4(),
            provider=_PROVIDER,
            model_id=_SECOND_MODEL_ID,
            deployment="local",
            data_classes=["public", "internal", "sensitive", "restricted"],
            capabilities=["extraction", "summarization", "explanation"],
            context_window_tokens=32768,
            structured_output_supported=True,
            status="active",
            created_at=sa.func.now(),
            updated_at=sa.func.now(),
        )
    )

    routing_policies = sa.table(
        "routing_policies",
        sa.column("task_type", sa.String()),
        sa.column("status", sa.String()),
        sa.column("candidates", postgresql.JSONB()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    op.execute(
        routing_policies.update()
        .where(routing_policies.c.task_type == _TASK_TYPE, routing_policies.c.status == "active")
        .values(
            candidates=[
                {"provider": _PROVIDER, "model_id": _FIRST_MODEL_ID},
                {"provider": _PROVIDER, "model_id": _SECOND_MODEL_ID},
            ],
            updated_at=sa.func.now(),
        )
    )


def downgrade() -> None:
    routing_policies = sa.table(
        "routing_policies",
        sa.column("task_type", sa.String()),
        sa.column("status", sa.String()),
        sa.column("candidates", postgresql.JSONB()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    op.execute(
        routing_policies.update()
        .where(routing_policies.c.task_type == _TASK_TYPE, routing_policies.c.status == "active")
        .values(
            candidates=[{"provider": _PROVIDER, "model_id": _FIRST_MODEL_ID}],
            updated_at=sa.func.now(),
        )
    )

    model_definitions = sa.table(
        "model_definitions",
        sa.column("provider", sa.String()),
        sa.column("model_id", sa.String()),
    )
    op.execute(
        model_definitions.delete().where(
            model_definitions.c.provider == _PROVIDER,
            model_definitions.c.model_id == _SECOND_MODEL_ID,
        )
    )
