"""First-slice Reflection Engine for Phase 4 AI Runtime, attention.explain_item only.

The user's own re-raised deferred-scope item -- "2. Agent Runtime,
multi-agent coordination, Reflection Engine" (design doc `docs/superpowers/
specs/2026-07-23-phase-4-ai-runtime-design.md` line 120's "every task in
this activation is a single bounded request/response with at most one
repair retry and up to two tool calls -- no multi-step planning loop, no
agent-to-agent handoff") -- narrowed to its smallest concrete first piece:
one additional, optional, bounded, fail-open model call on `attention.
explain_item`, per `docs/architecture/chapter-03-ai-runtime.md`'s
aspirational Reflection Engine section ("Was anything missed? Were
assumptions made? Does evidence support conclusions? Can confidence be
improved?"). This migration does **not** build that document's Agent
Runtime, multi-agent coordination, or Coordinator concepts -- only
reflection, and only for this one task type.

**Two independent pieces, following two different established precedents:**

1. `routing_policies.constraints.reflection_enabled` (boolean, default
   `false`) -- an in-place JSONB merge onto the existing *active*
   `attention.explain_item` row, following `0032_phase4_second_model.py`'s
   precedent that config/metadata changes update an existing active row
   rather than requiring a new `routing_policies` version (no evaluation-
   floor-gated activation flow exists for `routing_policies` in this
   activation, per that migration's own docstring). Uses the `jsonb ||
   jsonb` merge operator, not a full replace, so the five Decision-5 budget
   keys `0028_phase4_model_registry.py` already seeded in this column are
   preserved untouched. Read by `ecc.domains.ai_runtime.budgets.
   reflection_enabled`.

   Defaulting `false` (not `true`): the base single-call pipeline still
   has not cleared every evaluation floor (`docs/phases/phase-004/
   IMPLEMENTATION-STATUS.md`'s live-Ollama results, after the separate
   `10eca69` determinism fix: schema validity and grounding both now 100%,
   but the prohibited-fact-count floor -- 0 occurrences required -- still
   shows one stable, deterministic known case), and Chapter 3 itself frames
   reflection as "optional for lightweight tasks" -- `attention.
   explain_item` is exactly that, not a "strategic" task. Turning on an
   unproven second model call by default, before the base pipeline clears
   every floor, would confound whether a future evaluation failure is a
   base-pipeline defect or a reflection-layer one. This key is a
   deliberate, reversible switch to flip on later via this same migration
   pattern once real live-Ollama evidence exists for what reflection does
   to quality/latency on this small local model family.

2. A new `prompt_versions` row, `prompt_id='attention.explain_item.reflect.
   v1'`, following `0029_phase4_prompt_tool_versions.py`'s exact seeding
   pattern -- new prompt *content* is new immutable data, not an in-place
   update, matching Decision 3's "no in-place edit path once a row has ever
   been active or retired" (already enforced generically by that
   migration's trigger; no new trigger needed here). `ecc.domains.
   ai_runtime.runtime.TASK_PORTS["attention.explain_item"]`'s
   `reflection_prompt_id` points at this `prompt_id`; `_canonical_hash` is
   computed identically to `prompts.py:compute_template_hash` (mirrored
   scheme, matching 0029's own precedent).

This second reflection prompt_id is deliberately **not** added to
`prompts.py`'s `_GATED_PROMPT_IDS`/`_GATED_PROMPT_ID_TASK_TYPES`
evaluation-floor-promotion gate -- reflection's own safety is enforced at
call time in `runtime.py:_reflect_on_answer`, which forces any proposed
revision back through the exact same `validate_output`/
`check_explain_item_grounding` checks the primary answer had to pass
before a revision can ever replace it, not by a separate evaluation-gated
activation flow.
"""

import json
from hashlib import sha256
from typing import Any
from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0033_phase4_reflection"
down_revision = "0032_phase4_second_model"
branch_labels = None
depends_on = None

_TASK_TYPE = "attention.explain_item"

_REFLECT_PROMPT_ID = "attention.explain_item.reflect.v1"
_REFLECT_PROMPT_TEMPLATE = (
    "You are reviewing your own prior explanation of why an attention item "
    "is ranked where it is, before it is shown to an executive.\n\n"
    "Item type: {{ entity_type }}\n"
    "Score: {{ score }}\n"
    "Confidence: {{ confidence }}\n"
    "Factors:\n{{ factors }}\n\n"
    "Your prior answer:\n{{ prior_answer }}\n\n"
    "Critique your prior answer against these questions: Was anything "
    "missed? Were assumptions made that are not supported by the factors "
    "above? Does every cited factor code actually support the explanation? "
    "Could the wording be made clearer or more accurate without inventing "
    "new facts?\n\n"
    "If your prior answer is already good, respond approved with both "
    "revised fields null. If it can be improved, propose a revised "
    "explanation of 60 words or fewer, grounded entirely in the factors "
    "above -- do not cite any factor_code that is not listed above, and do "
    "not introduce facts not present in the factors list.\n\n"
    'Respond with JSON matching exactly: {"approved": boolean, '
    '"revised_explanation_text": string or null, '
    '"revised_cited_factor_codes": [string, ...] or null}. Do not include '
    "any other text."
)
_REFLECT_INPUT_SCHEMA_REF = "attention.explain_item.reflect.input.v1"
_REFLECT_OUTPUT_SCHEMA_REF = "attention.explain_item.reflect.output.v1"


def _canonical_hash(material: dict[str, Any]) -> str:
    """Mirrors `0029_phase4_prompt_tool_versions.py`'s `_canonical_hash` /
    `ecc.domains.ai_runtime.prompts.compute_template_hash` exactly: sha256
    over canonical (UTF-8, sorted-object-keys, compact-separator) JSON
    bytes.
    """
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(canonical.encode("utf-8")).hexdigest()


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    routing_policies = sa.table(
        "routing_policies",
        sa.column("task_type", sa.String()),
        sa.column("status", sa.String()),
        sa.column("constraints", postgresql.JSONB()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    op.execute(
        routing_policies.update()
        .where(routing_policies.c.task_type == _TASK_TYPE, routing_policies.c.status == "active")
        .values(
            # `sa.literal({...}, type_=JSONB())` -- not `sa.cast(<python
            # str>, JSONB())` -- is deliberate: casting a Python *string*
            # bind parameter to JSONB runs it through the JSONB bind
            # processor a second time, which `json.dumps`-encodes the
            # already-JSON-text string into a quoted JSON *string scalar*
            # (`"{\"reflection_enabled\": false}"`) instead of an object.
            # Postgres's `jsonb || jsonb` then treats one side as scalar
            # and wraps both into a two-element array rather than merging
            # keys -- confirmed against a real local Postgres instance
            # while authoring this migration, not a hypothetical. Passing
            # a Python **dict** here lets the bind processor's single
            # `json.dumps` produce a real JSONB object the `||` merge
            # operator treats as an object.
            constraints=routing_policies.c.constraints.op("||")(
                sa.literal({"reflection_enabled": False}, type_=postgresql.JSONB())
            ),
            updated_at=sa.func.now(),
        )
    )

    prompt_versions = sa.table(
        "prompt_versions",
        sa.column("id", uuid),
        sa.column("prompt_id", sa.String()),
        sa.column("version", sa.Integer()),
        sa.column("template", sa.Text()),
        sa.column("template_hash", sa.String()),
        sa.column("input_schema_ref", sa.String()),
        sa.column("output_schema_ref", sa.String()),
        sa.column("status", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    reflect_hash = _canonical_hash(
        {
            "template": _REFLECT_PROMPT_TEMPLATE,
            "input_schema_ref": _REFLECT_INPUT_SCHEMA_REF,
            "output_schema_ref": _REFLECT_OUTPUT_SCHEMA_REF,
        }
    )
    op.execute(
        prompt_versions.insert().values(
            id=uuid4(),
            prompt_id=_REFLECT_PROMPT_ID,
            version=1,
            template=_REFLECT_PROMPT_TEMPLATE,
            template_hash=reflect_hash,
            input_schema_ref=_REFLECT_INPUT_SCHEMA_REF,
            output_schema_ref=_REFLECT_OUTPUT_SCHEMA_REF,
            status="active",
            created_at=sa.func.now(),
            updated_at=sa.func.now(),
        )
    )


def downgrade() -> None:
    prompt_versions = sa.table(
        "prompt_versions",
        sa.column("prompt_id", sa.String()),
    )
    op.execute(prompt_versions.delete().where(prompt_versions.c.prompt_id == _REFLECT_PROMPT_ID))

    routing_policies = sa.table(
        "routing_policies",
        sa.column("task_type", sa.String()),
        sa.column("status", sa.String()),
        sa.column("constraints", postgresql.JSONB()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    op.execute(
        routing_policies.update()
        .where(routing_policies.c.task_type == _TASK_TYPE, routing_policies.c.status == "active")
        .values(
            # `sa.literal(..., type_=sa.Text())` -- not a bare Python str
            # -- for the identical reason the `upgrade()` merge above needs
            # an explicit type: without it, SQLAlchemy infers the operand's
            # type from `constraints` (JSONB) and double-encodes it,
            # producing a JSONB *string* operand instead of the plain text
            # `jsonb - text` (remove key) expects.
            constraints=routing_policies.c.constraints.op("-")(
                sa.literal("reflection_enabled", type_=sa.Text())
            ),
            updated_at=sa.func.now(),
        )
    )
