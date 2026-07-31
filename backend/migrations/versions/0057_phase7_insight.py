"""Phase 7 Task 5 part 2: register `personal.generate_insight`, the third
AI task type this activation registers and the first Phase 7 one --
`trend`/`correlation` insights drawn from one or more domains a user has
explicitly granted for `purpose='insight_generation'` (Task 5 part 1's
`cross_domain_grants`, migration `0056_phase7_cross_domain_grants.py`).

Three new rows, following `0034_phase4_meeting_prep.py`'s exact established
precedent for wiring on a second (now third) task type:

1. `tool_definitions`: `personal.get_insight_sources` (`scopes=
   ["read:personal"]`), handler `ecc.domains.personal.insight_tools:
   get_insight_sources_tool` (new module) -- reads every granted source
   domain's `domain_records`, decrypted, after confirming both the domain
   is currently enabled and an active grant covers it (that module's own
   docstring has the full detail).
2. `prompt_versions`: `personal.generate_insight.v1`, version 1, active.
   The template's own instructions restate `INSIGHT-CONTRACT.md`'s "does
   not diagnose, prescribe, promise financial outcomes or infer sensitive
   traits" in plain language -- belt-and-suspenders alongside the schema-
   level/grounding-check enforcement `validator.py:PersonalInsightOutput`/
   `check_personal_insight_grounding` actually rely on (a prompt
   instruction alone is never this activation's real enforcement
   mechanism, matching every prior task type's own "the schema is the
   contract, the prompt is a hint" discipline).
3. `routing_policies`: `personal.generate_insight`, version 1, active.
   `capability="summarization"` (`router.py:TASK_REQUIREMENTS`, same PR)
   -- both registered models already declare it (migrations 0028/0032),
   so no `model_definitions` change is needed here. `constraints.max_
   input_tokens` (6144) is higher than either existing task type's: this
   prompt's evidence can span more than one source domain, each up to
   `personal.get_insight_sources`'s own `_MAX_RECORDS_PER_DOMAIN=50` cap.

`config.py`'s own feature flag for the actual production call site (the
same "these three rows only make the capability *exist*; the call site is
what makes it *reachable*" split `0034`'s own docstring established) ships
alongside this migration in the same PR, default off.
"""

import json
from hashlib import sha256
from typing import Any
from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0057_phase7_insight"
down_revision = "0056_phase7_cross_domain_grants"
branch_labels = None
depends_on = None

_TASK_TYPE = "personal.generate_insight"
_PROVIDER = "ollama"
_FIRST_MODEL_ID = "qwen2.5:1.5b-instruct-q4_K_M"
_SECOND_MODEL_ID = "qwen2.5:3b-instruct-q4_K_M"

_TOOL_NAME = "personal.get_insight_sources"
_TOOL_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "source_domain_keys": {"type": "array", "items": {"type": "string"}, "minItems": 1},
    },
    "required": ["source_domain_keys"],
    "additionalProperties": False,
}
_TOOL_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "sources": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "domain_key": {"type": "string"},
                    "classification": {"type": "string"},
                    "records": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "record_type": {"type": "string"},
                                "payload": {"type": "object"},
                                "effective_at": {"type": "string"},
                            },
                            "required": ["id", "record_type", "payload", "effective_at"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["domain_key", "classification", "records"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["sources"],
    "additionalProperties": False,
}

_PROMPT_ID = "personal.generate_insight.v1"
_PROMPT_TEMPLATE = (
    "You are helping someone understand their own personal data across "
    "domains they have explicitly granted for this purpose.\n\n"
    "Identify exactly one meaningful trend or correlation, grounded "
    "entirely in the information listed below. Do not invent facts that "
    "are not present in that information. Every ID you list in "
    "cited_record_ids must be one of the IDs given here.\n\n"
    "You must not: diagnose or suggest any medical or mental health "
    "condition; prescribe, recommend or suggest any treatment; promise, "
    "guarantee or imply any financial outcome or return; state or imply "
    "eligibility or ineligibility for credit, employment or insurance; or "
    "infer any sensitive personal trait not directly stated in the "
    "information below. Never compute or state a numeric score, ranking "
    "or leaderboard of any kind. If any source below is health or "
    "finance information, set professional_referral_note to a short note "
    "directing the person to a qualified professional; otherwise set it "
    "to null.\n\n"
    "{{ sources }}"
    'Respond with JSON matching exactly: {"kind": "trend" or '
    '"correlation", "title": string, "explanation_text": string, '
    '"cited_record_ids": [string, ...], "source_period": string, '
    '"missing_data": string, "confidence": "low" or "medium" or "high", '
    '"limitations": string, "professional_referral_note": string or null}'
)
_PROMPT_INPUT_SCHEMA_REF = "personal.generate_insight.input.v1"
_PROMPT_OUTPUT_SCHEMA_REF = "personal.generate_insight.output.v1"


def _canonical_hash(material: dict[str, Any]) -> str:
    """Mirrors `0034_phase4_meeting_prep.py`'s identical helper (in turn
    mirroring `prompts.py:compute_template_hash`/`tools.py:compute_
    definition_hash`) -- sha256 over canonical (UTF-8, sorted-object-keys,
    compact-separator) JSON bytes.
    """
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(canonical.encode("utf-8")).hexdigest()


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    tool_definitions = sa.table(
        "tool_definitions",
        sa.column("id", uuid),
        sa.column("name", sa.String()),
        sa.column("version", sa.Integer()),
        sa.column("scopes", postgresql.ARRAY(sa.Text())),
        sa.column("input_schema", postgresql.JSONB()),
        sa.column("output_schema", postgresql.JSONB()),
        sa.column("handler_ref", sa.String()),
        sa.column("definition_hash", sa.String()),
        sa.column("status", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    tool_handler_ref = "ecc.domains.personal.insight_tools:get_insight_sources_tool"
    tool_hash = _canonical_hash(
        {
            "input_schema": _TOOL_INPUT_SCHEMA,
            "output_schema": _TOOL_OUTPUT_SCHEMA,
            "scopes": ["read:personal"],
            "handler_ref": tool_handler_ref,
        }
    )
    op.execute(
        tool_definitions.insert().values(
            id=uuid4(),
            name=_TOOL_NAME,
            version=1,
            scopes=["read:personal"],
            input_schema=_TOOL_INPUT_SCHEMA,
            output_schema=_TOOL_OUTPUT_SCHEMA,
            handler_ref=tool_handler_ref,
            definition_hash=tool_hash,
            status="active",
            created_at=sa.func.now(),
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
    prompt_hash = _canonical_hash(
        {
            "template": _PROMPT_TEMPLATE,
            "input_schema_ref": _PROMPT_INPUT_SCHEMA_REF,
            "output_schema_ref": _PROMPT_OUTPUT_SCHEMA_REF,
        }
    )
    op.execute(
        prompt_versions.insert().values(
            id=uuid4(),
            prompt_id=_PROMPT_ID,
            version=1,
            template=_PROMPT_TEMPLATE,
            template_hash=prompt_hash,
            input_schema_ref=_PROMPT_INPUT_SCHEMA_REF,
            output_schema_ref=_PROMPT_OUTPUT_SCHEMA_REF,
            status="active",
            created_at=sa.func.now(),
            updated_at=sa.func.now(),
        )
    )

    routing_policies = sa.table(
        "routing_policies",
        sa.column("id", uuid),
        sa.column("task_type", sa.String()),
        sa.column("version", sa.Integer()),
        sa.column("candidates", postgresql.JSONB()),
        sa.column("constraints", postgresql.JSONB()),
        sa.column("fallback", postgresql.JSONB()),
        sa.column("status", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    op.execute(
        routing_policies.insert().values(
            id=uuid4(),
            task_type=_TASK_TYPE,
            version=1,
            candidates=[
                {"provider": _PROVIDER, "model_id": _FIRST_MODEL_ID},
                {"provider": _PROVIDER, "model_id": _SECOND_MODEL_ID},
            ],
            constraints={
                "max_input_tokens": 6144,
                # Kept in sync with router.py's TASK_REQUIREMENTS["personal.
                # generate_insight"].max_output_tokens by convention -- that
                # value wins at runtime (budgets.py:RunBudget.from_policy),
                # but this row should still reflect it rather than a stale
                # number.
                "max_output_tokens": 600,
                "per_model_call_timeout_seconds": 40,
                "per_tool_call_timeout_seconds": 5,
                "total_run_budget_seconds": 95,
            },
            fallback={},
            status="active",
            created_at=sa.func.now(),
            updated_at=sa.func.now(),
        )
    )


def downgrade() -> None:
    routing_policies = sa.table("routing_policies", sa.column("task_type", sa.String()))
    op.execute(routing_policies.delete().where(routing_policies.c.task_type == _TASK_TYPE))

    prompt_versions = sa.table("prompt_versions", sa.column("prompt_id", sa.String()))
    op.execute(prompt_versions.delete().where(prompt_versions.c.prompt_id == _PROMPT_ID))

    tool_definitions = sa.table("tool_definitions", sa.column("name", sa.String()))
    op.execute(tool_definitions.delete().where(tool_definitions.c.name == _TOOL_NAME))
