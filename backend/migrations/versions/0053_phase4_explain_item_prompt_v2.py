"""Activate attention.explain_item.v2: a prompt-content change via a new
versioned row, not an in-place edit of the already-shipped version=1 row.

Phase 4 post-launch audit, phase H (repository owner: relanded properly).
The phase H commit that first tried this fix (a third, narrowly-scoped
prompt attempt for `attention.explain_item`'s `prohibited_fact_count`
floor miss -- Task evidence, `EVALUATION-CONTRACT.md`'s own "phase H"/
"Phase J" sections) hand-edited migration `0029_phase4_prompt_tool_
versions.py`'s seeded `_PROMPT_TEMPLATE` literal directly. A round-4
correctness review caught this as a real bug, not just a style issue:
`0029` was already merged and applied to `origin/main` on 2026-07-24
(commit `98e7cc4`, PR #38) -- editing its seed data in a later PR changes
what a *fresh* database gets, but does nothing for any database that
already ran `alembic upgrade` through `0029` before this fix landed (a
persistent local/staging/demo environment, not recreated from scratch).
Worse, `prompts.py`'s own module docstring and the `trg_prompt_versions_
immutability` trigger `0029` itself creates both establish, in plain
terms, that this is never how a prompt is supposed to be changed: "once a
row's `status` leaves `draft` that envelope is immutable... Editing a
prompt always means inserting a new row with `version = previous + 1`;
`activate_prompt_version` never edits an existing row's template/schema-
ref columns." `0029`'s own edit was reverted back to its original
`version=1` text in the same commit as this migration, and the intended
content change is redone here the way the table's own design requires:
a new `version=2` row, activated in place of `version=1` via the exact
`status` transition (`active` -> `retired`, `draft`/new -> `active`)
`prompts.py:activate_prompt_version` itself performs at runtime -- this
migration does the same thing declaratively, for the same reason Task 1
seeds `model_definitions`/`routing_policies` rows declaratively rather
than requiring an API call after every fresh deploy.

**Content of the change** (unchanged from the reverted `0029` edit,
`EVALUATION-CONTRACT.md`'s "phase H" paragraph has the full reasoning):
adds one clause to the prompt instructing the model that a `due_48h`/
`due_today` factor means the deadline has not passed yet -- scoped to
only the one code that has ever actually failed in real live-model
testing (`task_due_48h_medium_waiting`), never uses the word "overdue"
at all (avoiding the negation-priming failure mode two earlier, already-
reverted attempts at this same fix hit), and gives no vocabulary for any
other timing code (avoiding the cross-example hallucination the second
reverted attempt hit). Verified against a real model, twice, on this
same PR's own `ollama-evaluation` CI (`Phase I`/`Phase J` sections):
`prohibited_fact_count` reads 0 on both runs, `schema_validity_rate`/
`grounding_rate` both 1.0.

`template_hash` is computed here exactly as `ecc.domains.ai_runtime.
prompts.compute_template_hash` computes it at runtime (this migration's
own `_canonical_hash` helper, copied from `0029`'s identical one) --
kept in sync by convention, matching every other prompt/tool version
migration's own precedent.
"""

import json
from hashlib import sha256
from typing import Any
from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# NOTE: the revision id below is *not* this file's full basename, matching
# `0042_phase5_compensation_retry_kill_switch`'s own precedent -- `alembic_
# version.version_num` is a plain `VARCHAR(32)` (alembic's own default,
# unconfigured in this repository), and this file's full basename (`0053_
# phase4_explain_item_prompt_v2`, 34 characters) does not fit. Confirmed
# directly, not assumed: `alembic upgrade head` raised `psycopg.errors.
# StringDataRightTruncation` with the full name before this shortening.
# Resolved by abbreviating `explain` to `expl` while keeping the file's own
# basename fully descriptive -- a disclosed, mechanical divergence from the
# naming convention, not a silent one.
revision = "0053_phase4_expl_item_prompt_v2"
down_revision = "0052_phase4_meeting_timeout3"
branch_labels = None
depends_on = None

_PROMPT_ID = "attention.explain_item.v1"
_PROMPT_INPUT_SCHEMA_REF = "attention.explain_item.input.v1"
_PROMPT_OUTPUT_SCHEMA_REF = "attention.explain_item.output.v1"
_OLD_TEMPLATE = (
    "You are helping an executive understand why an attention item is "
    "ranked where it is.\n\n"
    "Explain, in 60 words or fewer, why this item deserves attention, "
    "grounded entirely in the factors listed below. Do not invent facts "
    "that are not present in the factors list. Every code you list in "
    "cited_factor_codes must be one of the factor codes given here.\n\n"
    "Item type: {{ entity_type }}\n"
    "Score: {{ score }}\n"
    "Confidence: {{ confidence }}\n"
    "Factors:\n{{ factors }}\n\n"
    'Respond with JSON matching exactly: {"explanation_text": string, '
    '"cited_factor_codes": [string, ...]}'
)
_NEW_TEMPLATE = (
    "You are helping an executive understand why an attention item is "
    "ranked where it is.\n\n"
    "Explain, in 60 words or fewer, why this item deserves attention, "
    "grounded entirely in the factors listed below. Do not invent facts "
    "that are not present in the factors list. Every code you list in "
    "cited_factor_codes must be one of the factor codes given here. A "
    "due_48h or due_today factor means the deadline has not passed "
    "yet.\n\n"
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
    # version=1, but written as a status-based WHERE (not a hardcoded
    # version=1) so this migration stays correct even if some environment's
    # active version has already moved for an unrelated reason.
    op.execute(
        prompt_versions.update()
        .where(prompt_versions.c.prompt_id == _PROMPT_ID, prompt_versions.c.status == "active")
        .values(status="retired", updated_at=sa.func.now())
    )
    op.execute(
        prompt_versions.insert().values(
            id=uuid4(),
            prompt_id=_PROMPT_ID,
            version=2,
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
