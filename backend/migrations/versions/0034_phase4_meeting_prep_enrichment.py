"""Wire Phase 3's meeting_prep.py "Optional enrichment" flag on
(MEETING-PREP-CONTRACT.md), the second Phase-4-consuming change now that
Phase 4 (AI Runtime) exists to serve it -- the same follow-up meeting_
prep.py's own module docstring named: "AI enrichment always reports
feature_disabled in Phase 3 regardless of the config flag's value: Phase 4
(AI Runtime) does not exist yet to serve it."

`meeting.prep_summary` is the second task type this activation registers
(the first, `attention.explain_item`, migration 0028's routing_policies
row / 0029's prompt_versions row). Structurally different from it: no
single scalar "item", a multi-section evidence bundle (participants,
timeline, commitments, decisions, notes, risks, dependencies) already
fetched deterministically by `meeting_prep.py:_generate_pack` before the
AI ever sees it -- `runtime.py`'s `execute_run` was generalized (this
same change) to support that shape as a second, independently-plugged
task type, with zero behavior change to `attention.explain_item`'s own
path (verified against its existing test suite, unmodified, still
passing).

Three new rows, following established precedent for each:

1. `tool_definitions`: `meeting.get_prep_pack`, mirroring `attention.get_
   item`'s exact shape and seeding pattern (migration 0029) -- read-only
   (`scopes=["read:meetings"]`), handler `ecc.domains.attention.
   meeting_prep_tools:get_prep_pack_tool` (new module, reusing `meeting_
   prep.py`'s existing `_generate_pack` so the AI runtime's view of "what
   evidence may be summarized" can never diverge from what the
   deterministic pack itself contains).
2. `prompt_versions`: `meeting.prep_summary.v1`, version 1, active --
   0029's exact seeding pattern for a brand-new prompt family (not a new
   version of an existing one, so no retire-then-activate step is needed
   here, unlike a same-family version bump).
3. `routing_policies`: `meeting.prep_summary`, version 1, active --
   0028's exact seeding pattern. `constraints.max_input_tokens` is higher
   than `attention.explain_item`'s (4096 vs 3072): a meeting pack's
   evidence bundle is richer than one attention item's factor list.
   `capability="summarization"` (`router.py:TASK_REQUIREMENTS`, this same
   change) -- both registered models already declare it (migration
   0028/0032: `capabilities={extraction, summarization, explanation}`),
   so no `model_definitions` change is needed here.

`config.py:meeting_prep_ai_enrichment_enabled` (still default `False`,
unchanged by this migration) is the actual per-deployment on/off switch --
these three rows only make the capability *exist*; `meeting_prep.py`'s own
change (same PR) is what makes it *reachable*, gated on that flag.
"""

import json
from hashlib import sha256
from typing import Any
from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0034_phase4_meeting_prep"
down_revision = "0033_phase4_reflection"
branch_labels = None
depends_on = None

_TASK_TYPE = "meeting.prep_summary"
_PROVIDER = "ollama"
_FIRST_MODEL_ID = "qwen2.5:1.5b-instruct-q4_K_M"
_SECOND_MODEL_ID = "qwen2.5:3b-instruct-q4_K_M"

_TOOL_NAME = "meeting.get_prep_pack"
_TOOL_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"meeting_id": {"type": "string", "format": "uuid"}},
    "required": ["meeting_id"],
    "additionalProperties": False,
}
_NULLABLE_STRING = {"type": ["string", "null"]}
_TOOL_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "objective": {"type": "string"},
        "participants": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "entity_name": {"type": "string"},
                    "role": {"type": "string"},
                },
                "required": ["id", "entity_name", "role"],
                "additionalProperties": False,
            },
        },
        "timeline": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "effective_at": {"type": "string"},
                    "event_type": {"type": "string"},
                    "summary": {"type": "string"},
                },
                "required": ["id", "effective_at", "event_type", "summary"],
                "additionalProperties": False,
            },
        },
        "commitments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "direction": {"type": "string"},
                    "summary": {"type": "string"},
                    "status": {"type": "string"},
                    "due_at": _NULLABLE_STRING,
                    "counterparty_name": _NULLABLE_STRING,
                },
                "required": [
                    "id",
                    "direction",
                    "summary",
                    "status",
                    "due_at",
                    "counterparty_name",
                ],
                "additionalProperties": False,
            },
        },
        "decisions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": _NULLABLE_STRING,
                    "body": {"type": "string"},
                    "note_type": {"type": "string"},
                },
                "required": ["id", "title", "body", "note_type"],
                "additionalProperties": False,
            },
        },
        "notes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "title": _NULLABLE_STRING,
                    "body": {"type": "string"},
                    "note_type": {"type": "string"},
                },
                "required": ["id", "title", "body", "note_type"],
                "additionalProperties": False,
            },
        },
        "risks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "description": {"type": "string"},
                    "status": {"type": "string"},
                    "probability": {"type": "integer"},
                    "impact": {"type": "integer"},
                },
                "required": ["id", "description", "status", "probability", "impact"],
                "additionalProperties": False,
            },
        },
        "dependencies": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "direction": {"type": "string"},
                    "note": _NULLABLE_STRING,
                    "expected_at": _NULLABLE_STRING,
                },
                "required": ["id", "direction", "note", "expected_at"],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "objective",
        "participants",
        "timeline",
        "commitments",
        "decisions",
        "notes",
        "risks",
        "dependencies",
    ],
    "additionalProperties": False,
}

_PROMPT_ID = "meeting.prep_summary.v1"
_PROMPT_TEMPLATE = (
    "You are helping an executive prepare for an upcoming meeting.\n\n"
    "Summarize, in 150 words or fewer, the most important context to "
    "review before this meeting, grounded entirely in the information "
    "listed below. Do not invent facts that are not present in that "
    "information. Every ID you list in cited_evidence_ids must be one of "
    "the IDs given here.\n\n"
    "Meeting: {{ objective }}\n\n"
    "Participants:\n{{ participants }}\n\n"
    "Recent timeline:\n{{ timeline }}\n\n"
    "Open commitments:\n{{ commitments }}\n\n"
    "Prior decisions:\n{{ decisions }}\n\n"
    "Other notes:\n{{ notes }}\n\n"
    "Active risks:\n{{ risks }}\n\n"
    "Open dependencies:\n{{ dependencies }}\n\n"
    'Respond with JSON matching exactly: {"summary_text": string, '
    '"cited_evidence_ids": [string, ...]}'
)
_PROMPT_INPUT_SCHEMA_REF = "meeting.prep_summary.input.v1"
_PROMPT_OUTPUT_SCHEMA_REF = "meeting.prep_summary.output.v1"


def _canonical_hash(material: dict[str, Any]) -> str:
    """Mirrors `0029_phase4_prompt_tool_versions.py`'s `_canonical_hash` /
    `prompts.py:compute_template_hash` / `tools.py:compute_definition_
    hash` exactly: sha256 over canonical (UTF-8, sorted-object-keys,
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
    tool_handler_ref = "ecc.domains.attention.meeting_prep_tools:get_prep_pack_tool"
    tool_hash = _canonical_hash(
        {
            "input_schema": _TOOL_INPUT_SCHEMA,
            "output_schema": _TOOL_OUTPUT_SCHEMA,
            "scopes": ["read:meetings"],
            "handler_ref": tool_handler_ref,
        }
    )
    op.execute(
        tool_definitions.insert().values(
            id=uuid4(),
            name=_TOOL_NAME,
            version=1,
            scopes=["read:meetings"],
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
                "max_input_tokens": 4096,
                "max_output_tokens": 768,
                "per_model_call_timeout_seconds": 20,
                "per_tool_call_timeout_seconds": 5,
                "total_run_budget_seconds": 60,
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
