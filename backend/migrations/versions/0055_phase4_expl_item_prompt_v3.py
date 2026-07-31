"""Activate attention.explain_item.v3: a fourth prompt attempt for a newly
confirmed grounding-hallucination mode, narrowly scoped like the third
attempt (migration 0053) -- not a rewrite of it.

Post-launch audit, phase L. This session's own repeated live-model
`ollama-evaluation` runs (`luckyrjain/executive-command-center#89`'s and
`#90`'s CI, three separate runs) all failed `test_attention_explain_item_
passes_every_evaluation_floor_against_real_model` with the *identical*
three-example signature every time:

- `commitment_overdue_critical_pinned_made_to_me`: `grounding_failed`,
  `ungrounded_codes=['due_48h']` -- but this example's own overdue factor
  code is `overdue`, not `due_48h` (`tests/fixtures/phase4_evaluation_
  attention_explain.py`). `due_48h` is a real code, just not one this
  item actually has.
- `commitment_no_due_low`: `grounding_failed`, `ungrounded_codes=
  ['confidence']` -- this example's *only* factor is `importance`; it has
  no factor named or coded `confidence` at all.
- `waiting_link_waiting_on_me_overdue_stale`: `schema_invalid`, `detail=
  'explanation_text:value_error'` -- the 60-word cap (`validator.py`'s
  `_max_word_count`), a genuinely different failure mode (word-count
  overrun on a three-factor item), not touched by this migration; not
  confidently fixable without guessing at explanation-length behavior,
  left as a disclosed, still-open limitation.

**Root cause, found from the prompt template itself, not guessed.**
`_OLD_TEMPLATE`/`_NEW_TEMPLATE` (migration 0053) both render `"Score: {{
score }}\nConfidence: {{ confidence }}\n"` directly above `"Factors:\n{{
factors }}"` -- two top-level metadata fields sitting immediately next to
the actual factor list, both scored/quantitative like a factor would be.
`commitment_no_due_low`'s own `confidence` value citing itself as
`cited_factor_codes: ["confidence"]` is the model literally citing the
metadata field's own name back -- not a `source_field`/`code` mix-up
(already fixed, `PHASE-004-ai-runtime.md`'s "What remains" section), a
distinct confusion between the two small labeled fields above the list
and the list itself. `due_48h` is the same class of error with a
different source: a plausible, real-elsewhere code the model reaches for
on an item that doesn't have it, rather than exclusively deriving from
the item's own `factors`.

**The fix, narrowly scoped -- avoids both prior reverted attempts' own
failure modes (`EVALUATION-CONTRACT.md`'s "Task evidence" section: the
first repeated "overdue" itself, negation-priming the model into saying
it *more*; the second gave a copy-paste phrase naming several codes at
once, contaminating unrelated examples).** This attempt introduces no new
factor-code vocabulary at all and touches no timing wording -- one added
sentence stating positively where `cited_factor_codes` values come from:
"cited_factor_codes must use only the code values listed under Factors
below -- never the Score or Confidence values shown above them." Every
word in it (`Score`, `Confidence`, `Factors`) is already present in the
template's own existing text (the two field labels and the list header),
not new vocabulary the model could latch onto for a different example the
way the second reverted attempt's copy-pasted codes did.

Verified: ruff/mypy clean, the migration round-trips (`upgrade` ->
`downgrade -1` -> `upgrade`), and the full non-live `ai_runtime` suite
passes unchanged. Not independently verified against a real model for the
same reason as every prior prompt attempt -- the next real `ollama-
evaluation` run is the actual test; if it regresses any other floor or
introduces a new failure mode, the established response is to revert
immediately rather than iterate further blind.
"""

import json
from hashlib import sha256
from typing import Any
from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0055_phase4_expl_item_prompt_v3"
down_revision = "0054_phase7_personal_domains"
branch_labels = None
depends_on = None

_PROMPT_ID = "attention.explain_item.v1"
_PROMPT_INPUT_SCHEMA_REF = "attention.explain_item.input.v1"
_PROMPT_OUTPUT_SCHEMA_REF = "attention.explain_item.output.v1"
_NEW_TEMPLATE = (
    "You are helping an executive understand why an attention item is "
    "ranked where it is.\n\n"
    "Explain, in 60 words or fewer, why this item deserves attention, "
    "grounded entirely in the factors listed below. Do not invent facts "
    "that are not present in the factors list. Every code you list in "
    "cited_factor_codes must be one of the factor codes given here. A "
    "due_48h or due_today factor means the deadline has not passed "
    "yet. cited_factor_codes must use only the code values listed under "
    "Factors below -- never the Score or Confidence values shown above "
    "them.\n\n"
    "Item type: {{ entity_type }}\n"
    "Score: {{ score }}\n"
    "Confidence: {{ confidence }}\n"
    "Factors:\n{{ factors }}\n\n"
    'Respond with JSON matching exactly: {"explanation_text": string, '
    '"cited_factor_codes": [string, ...]}'
)


def _canonical_hash(material: dict[str, Any]) -> str:
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(canonical.encode("utf-8")).hexdigest()


def _prompt_versions_table() -> sa.TableClause:
    uuid = postgresql.UUID(as_uuid=True)
    return sa.table(
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


def upgrade() -> None:
    prompt_versions = _prompt_versions_table()
    new_hash = _canonical_hash(
        {
            "template": _NEW_TEMPLATE,
            "input_schema_ref": _PROMPT_INPUT_SCHEMA_REF,
            "output_schema_ref": _PROMPT_OUTPUT_SCHEMA_REF,
        }
    )
    # Retire whichever row is currently active for this prompt_id -- normally
    # version=2, but written as a status-based WHERE (not a hardcoded
    # version number) so this migration stays correct even if some
    # environment's active version has already moved for an unrelated
    # reason, matching migration 0053's own identical precedent.
    op.execute(
        prompt_versions.update()
        .where(prompt_versions.c.prompt_id == _PROMPT_ID, prompt_versions.c.status == "active")
        .values(status="retired", updated_at=sa.func.now())
    )
    op.execute(
        prompt_versions.insert().values(
            id=uuid4(),
            prompt_id=_PROMPT_ID,
            version=3,
            template=_NEW_TEMPLATE,
            template_hash=new_hash,
            input_schema_ref=_PROMPT_INPUT_SCHEMA_REF,
            output_schema_ref=_PROMPT_OUTPUT_SCHEMA_REF,
            status="active",
            created_at=sa.func.now(),
            updated_at=sa.func.now(),
        )
    )


def downgrade() -> None:
    prompt_versions = _prompt_versions_table()
    bind = op.get_bind()

    # Mirrors upgrade()'s own defensiveness (a status-based WHERE, not a
    # hardcoded version number) in the opposite direction: only reactivate
    # version=2 if version=3 -- the row *this* migration activated -- is
    # still the active one. If some later migration or runtime `activate_
    # prompt_version` call has since moved the active pointer past
    # version=3, downgrading must not silently steal the active pointer
    # back to version=2 out from under it -- it should just remove its own
    # version=3 row and leave whatever is genuinely active alone.
    v3_is_active = bind.execute(
        sa.select(sa.literal(1)).where(
            prompt_versions.c.prompt_id == _PROMPT_ID,
            prompt_versions.c.version == 3,
            prompt_versions.c.status == "active",
        )
    ).first()

    op.execute(
        prompt_versions.delete().where(
            prompt_versions.c.prompt_id == _PROMPT_ID, prompt_versions.c.version == 3
        )
    )
    if v3_is_active is not None:
        op.execute(
            prompt_versions.update()
            .where(prompt_versions.c.prompt_id == _PROMPT_ID, prompt_versions.c.version == 2)
            .values(status="active", updated_at=sa.func.now())
        )
