"""attention.explain_item.v1 prompt version 2: fix the deterministic
prohibited-fact floor miss (design doc Decision 9 / EVALUATION-CONTRACT.md).

`docs/phases/phase-004/IMPLEMENTATION-STATUS.md` (post commit `10eca69`'s
determinism fix) documents the one evaluation floor still missed against
real `qwen2.5:1.5b-instruct-q4_K_M` output: `prohibited_fact_count` 0 -> 1,
always the same case (`task_due_48h_medium_waiting`), always the same
phrase (the model describes a task whose only timing factor is `due_48h`
-- due within two days, not overdue -- as "overdue"). Two earlier attempts
at broader prompt rewrites were tried and reverted because each regressed
schema-validity/grounding on other examples more than it fixed this one.

This version's first draft (before this docstring's current revision)
added an explicit paragraph naming the literal factor codes `due_48h`/
`due_today`/`overdue` -- that fixed the prohibited-fact floor (0
occurrences) but broke the grounding floor instead (0.85, three new
`cited_factor_codes` hallucinations of `due_48h` on examples that do not
have that factor at all), confirmed against real CI output on this exact
branch. `runtime.py:_render_factors_block`'s own comment already
documents this small model's tendency to echo whichever token is most
salient in its context back into `cited_factor_codes` (previously
`source_field` values; here, a snake_case factor-code identifier repeated
in the fixed instructions on every single call, regardless of whether
that example actually has the factor). The revision below removes every
literal factor-code name from the instructions and states the rule
conceptually instead ("only call something overdue ... if a factor below
indicates the deadline has already passed"), keeping every other line of
v1's template (60-word cap, grounding instructions, JSON response shape)
untouched.

New `prompt_versions` row, not an in-place edit -- Decision 3's "no
in-place edit path once a row has ever been active or retired" (enforced
by `trg_prompt_versions_immutability`, migration `0029_phase4_prompt_tool_
versions.py`) forbids editing the active v1 row directly. Activated here
by direct data migration, retiring v1 in the same transaction -- following
`0029`'s own precedent of seeding the *first* version directly active
(bypassing `prompts.py:activate_policy`'s HTTP path entirely). The HTTP
path's evaluation-floor gate for this exact prompt_id (`prompts.py:
_GATED_PROMPT_IDS`) requires a passing `evaluation_runs` row for the
*target* version to already exist before that version can go active --
but `evaluation.py:run_evaluation` will only ever evaluate the version
that is *currently* active (`active_prompt.version != prompt_version`
raises `prompt_version_not_active`), so there is no way to produce that
evaluation_runs row for a still-draft version through the running
application. A migration is the only way to break this chicken-and-egg
loop, exactly as it was for v1's own bootstrap -- the real verification
here is `.github/workflows/ollama-evaluation.yml`'s live-model CI run
against this exact new template, immediately after this migration lands.
"""

import json
from hashlib import sha256
from typing import Any
from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0034_phase4_explain_prompt_v2"
down_revision = "0033_phase4_reflection"
branch_labels = None
depends_on = None

_PROMPT_ID = "attention.explain_item.v1"
_PROMPT_TEMPLATE = (
    "You are helping an executive understand why an attention item is "
    "ranked where it is.\n\n"
    "Explain, in 60 words or fewer, why this item deserves attention, "
    "grounded entirely in the factors listed below. Do not invent facts "
    "that are not present in the factors list. Every code you list in "
    "cited_factor_codes must be one of the factor codes given here.\n\n"
    "Describe timing precisely: only call something overdue, past due, or "
    "missed if a factor below indicates the deadline has already passed. "
    "A deadline that is merely approaching or coming up soon is not the "
    "same as one that has already passed, even if it feels urgent.\n\n"
    "Item type: {{ entity_type }}\n"
    "Score: {{ score }}\n"
    "Confidence: {{ confidence }}\n"
    "Factors:\n{{ factors }}\n\n"
    'Respond with JSON matching exactly: {"explanation_text": string, '
    '"cited_factor_codes": [string, ...]}'
)
_PROMPT_INPUT_SCHEMA_REF = "attention.explain_item.input.v1"
_PROMPT_OUTPUT_SCHEMA_REF = "attention.explain_item.output.v1"


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

    # Retire v1 first -- the partial unique index `uq_prompt_versions_
    # active_per_prompt_id` (one active row per prompt_id) would otherwise
    # reject v2's insert below within the same transaction.
    op.execute(
        prompt_versions.update()
        .where(prompt_versions.c.prompt_id == _PROMPT_ID, prompt_versions.c.status == "active")
        .values(status="retired", updated_at=sa.func.now())
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
            version=2,
            template=_PROMPT_TEMPLATE,
            template_hash=prompt_hash,
            input_schema_ref=_PROMPT_INPUT_SCHEMA_REF,
            output_schema_ref=_PROMPT_OUTPUT_SCHEMA_REF,
            status="active",
            created_at=sa.func.now(),
            updated_at=sa.func.now(),
        )
    )


def downgrade() -> None:
    prompt_versions = sa.table(
        "prompt_versions",
        sa.column("prompt_id", sa.String()),
        sa.column("version", sa.Integer()),
        sa.column("status", sa.String()),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    op.execute(
        prompt_versions.delete().where(
            prompt_versions.c.prompt_id == _PROMPT_ID, prompt_versions.c.version == 2
        )
    )
    op.execute(
        prompt_versions.update()
        .where(prompt_versions.c.prompt_id == _PROMPT_ID, prompt_versions.c.version == 1)
        .values(status="active", updated_at=sa.func.now())
    )
