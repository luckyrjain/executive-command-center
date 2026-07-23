"""Create prompt_versions and tool_definitions for Phase 4 AI Runtime Task 2.

Phase 4 Task 2 (`docs/superpowers/plans/2026-07-23-phase-4-ai-runtime.md`).
Renumbered from the plan's nominal ``0023_phase4_prompt_tool_versions.py``
to ``0029`` -- Task 1 already claimed ``0028`` (itself renumbered from the
plan's nominal ``0022`` because Phase 3, merged first, already claimed
0022-0027), so ``0029`` is the actual next available number. This follows
the same "migration file numbers match actual implementation/chain order,
not the task numbers a plan happened to draft them under" rule Task 1's own
migration docstring already cites (matching Phase 2/3's precedent).

Both tables are deliberately **not** workspace-scoped, for the identical
reason ``0028_phase4_model_registry.py``'s docstring already gives for
``model_definitions``/``routing_policies``: neither table holds user data.
``prompt_versions``/``tool_definitions`` are the platform's global,
versioned catalog of prompt templates and tool contracts (design doc
Decision 3, `phase-004/DATA-MODEL.md`) -- every workspace this deployment
serves is served by the same active prompt/tool version, exactly like every
workspace reads the same ``model_definitions`` row. This mirrors this
codebase's existing precedent for genuinely global, non-user-data tables
(``event_inbox``/``event_dead_letters``, migration 0001; ``model_
definitions``/``routing_policies``, migration 0028).

**Identity and hashing (design doc Decision 3).** ``prompt_versions.prompt_
id`` is a stable slug (e.g. ``attention.explain_item.v1``); ``version`` is
the authoritative integer column, so a prompt family can iterate through
multiple immutable rows (``version=1``, ``version=2``, ...) under the same
``prompt_id`` slug. ``template_hash`` is ``sha256`` over the canonical
(UTF-8, sorted-object-keys) JSON bytes of ``{template, input_schema_ref,
output_schema_ref}`` -- computed identically here (seed data) and at
runtime by ``ecc.domains.ai_runtime.prompts.compute_template_hash`` (kept
in sync by convention; migrations in this codebase do not import ``ecc``
application code, matching every prior migration's self-containment).
``tool_definitions`` uses the same scheme over ``{input_schema,
output_schema, scopes, handler_ref}``, stored in ``definition_hash`` --
``DATA-MODEL.md``'s tool_definitions field list does not name a hash column
explicitly, but Decision 3's prose is explicit that tools use "the same
sha256 scheme", so this migration adds the column that scheme requires
(``ecc.domains.ai_runtime.tools.compute_definition_hash`` mirrors it).

**Immutability trigger.** Decision 3: "Once a ... row's status leaves draft
... its template/template_hash (or schema/scope columns for tools) are
immutable -- enforced by a PostgreSQL trigger ... not just an application-
layer check". This migration's triggers guard the *entire* hashed identity
envelope, not only the two columns Decision 3's prose names first
(``template``/``template_hash``): ``prompt_versions`` also guards ``input_
schema_ref``/``output_schema_ref``, and ``tool_definitions`` also guards
``handler_ref``/``definition_hash`` alongside ``input_schema``/``output_
schema``/``scopes``. Leaving any of those columns mutable post-activation
would let ``template_hash``/``definition_hash`` silently go stale relative
to the very content it hashes -- exactly the "bug in the runtime code
[silently mutating] a version already referenced by a completed ai_run"
Decision 3 names as the property this trigger exists to prevent. The
trigger does **not** block changes to ``status``/``updated_at`` at any
status -- see ``ecc.domains.ai_runtime.prompts.activate_prompt_version``/
``tools.activate_tool_version``, which only ever write those two columns,
never the guarded ones.

**Activation.** A partial unique index enforces exactly one ``active``
version per ``prompt_id`` (respectively tool ``name``), matching this
codebase's existing partial-unique-active-row precedent
(``uq_meeting_packs_active_per_meeting``, migration 0027; ``uq_routing_
policies_active_per_task_type``, migration 0028).

**Seed data (design doc Decision 6/9, `DATA-MODEL.md`).** Exactly one
``prompt_versions`` row, ``prompt_id='attention.explain_item.v1'``,
``version=1``, ``status='active'`` -- the first and only evaluated task
type this activation registers. Exactly two ``tool_definitions`` rows,
both ``status='active'``, both read-only: ``attention.get_item``
(``scopes=['read:attention']``) and ``knowledge.get_entity``
(``scopes=['read:knowledge']``) -- registered but not wired to an
evaluated task in this slice (Decision 6). ``handler_ref`` on each points
at the Task 4 module/function that will implement it
(``backend/ecc/domains/attention/tools.py``/``knowledge/tools.py``, not
yet created); this is a string data pointer only, not an import, so it is
safe to seed ahead of that module existing.
"""

import json
from hashlib import sha256
from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0029_phase4_prompt_tool_versions"
down_revision = "0028_phase4_model_registry"
branch_labels = None
depends_on = None


def _canonical_hash(material: dict) -> str:
    """Mirrors ``ecc.domains.ai_runtime.prompts.compute_template_hash``/
    ``tools.compute_definition_hash`` exactly: sha256 over canonical
    (UTF-8, sorted-object-keys, compact-separator) JSON bytes.
    """
    canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(canonical.encode("utf-8")).hexdigest()


_PROMPT_ID = "attention.explain_item.v1"
_PROMPT_TEMPLATE = (
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
_PROMPT_INPUT_SCHEMA_REF = "attention.explain_item.input.v1"
_PROMPT_OUTPUT_SCHEMA_REF = "attention.explain_item.output.v1"

_ATTENTION_GET_ITEM_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"attention_item_id": {"type": "string", "format": "uuid"}},
    "required": ["attention_item_id"],
    "additionalProperties": False,
}
_ATTENTION_GET_ITEM_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "entity_type": {"type": "string"},
        "score": {"type": "number"},
        "confidence": {"type": "number"},
        "factors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "label": {"type": "string"},
                    "points": {"type": "number"},
                    "source_field": {"type": "string"},
                },
                "required": ["code", "label", "points", "source_field"],
                "additionalProperties": False,
            },
        },
        "evidence_refs": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["entity_type", "score", "confidence", "factors", "evidence_refs"],
    "additionalProperties": False,
}

_KNOWLEDGE_GET_ENTITY_INPUT_SCHEMA = {
    "type": "object",
    "properties": {"entity_id": {"type": "string", "format": "uuid"}},
    "required": ["entity_id"],
    "additionalProperties": False,
}
_KNOWLEDGE_GET_ENTITY_OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "claims": {"type": "array", "items": {"type": "object"}},
        "evidence": {"type": "array", "items": {"type": "object"}},
    },
    "required": ["title", "claims", "evidence"],
    "additionalProperties": False,
}


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.create_table(
        "prompt_versions",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("prompt_id", sa.String(200), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("template", sa.Text(), nullable=False),
        sa.Column("template_hash", sa.String(64), nullable=False),
        sa.Column("input_schema_ref", sa.String(200), nullable=False),
        sa.Column("output_schema_ref", sa.String(200), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'retired')", name="ck_prompt_versions_status"
        ),
        sa.UniqueConstraint("prompt_id", "version", name="uq_prompt_versions_prompt_id_version"),
    )
    op.create_index(
        "uq_prompt_versions_active_per_prompt_id",
        "prompt_versions",
        ["prompt_id"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    op.create_table(
        "tool_definitions",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("scopes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("input_schema", postgresql.JSONB(), nullable=False),
        sa.Column("output_schema", postgresql.JSONB(), nullable=False),
        sa.Column("handler_ref", sa.String(300), nullable=False),
        sa.Column("definition_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'retired')", name="ck_tool_definitions_status"
        ),
        sa.UniqueConstraint("name", "version", name="uq_tool_definitions_name_version"),
    )
    op.create_index(
        "uq_tool_definitions_active_per_name",
        "tool_definitions",
        ["name"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    # --- Immutability triggers (design doc Decision 3) ---------------------
    #
    # Rejects an UPDATE that changes any column of the hashed identity
    # envelope once the row's OLD status has already left 'draft'. A draft
    # row (OLD.status = 'draft') is never restricted here -- editing before
    # first activation is normal; there is simply no in-place edit path once
    # a row has ever been active or retired (Decision 3: "there is no
    # in-place edit path at any layer" post-draft). `status`/`updated_at`
    # are deliberately excluded from the guarded set -- activation
    # (`prompts.py:activate_prompt_version`/`tools.py:activate_tool_version`)
    # only ever writes those two columns.
    op.execute(
        """
        CREATE FUNCTION enforce_prompt_version_immutability() RETURNS trigger AS $$
        BEGIN
            IF OLD.status <> 'draft' AND (
                NEW.template IS DISTINCT FROM OLD.template
                OR NEW.template_hash IS DISTINCT FROM OLD.template_hash
                OR NEW.input_schema_ref IS DISTINCT FROM OLD.input_schema_ref
                OR NEW.output_schema_ref IS DISTINCT FROM OLD.output_schema_ref
            ) THEN
                RAISE EXCEPTION
                    'prompt_versions row % (prompt_id=%, version=%) is immutable once '
                    'status <> draft (current status=%)',
                    OLD.id, OLD.prompt_id, OLD.version, OLD.status;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_prompt_versions_immutability
        BEFORE UPDATE ON prompt_versions
        FOR EACH ROW
        EXECUTE FUNCTION enforce_prompt_version_immutability();
        """
    )
    op.execute(
        """
        CREATE FUNCTION enforce_tool_definition_immutability() RETURNS trigger AS $$
        BEGIN
            IF OLD.status <> 'draft' AND (
                NEW.input_schema IS DISTINCT FROM OLD.input_schema
                OR NEW.output_schema IS DISTINCT FROM OLD.output_schema
                OR NEW.scopes IS DISTINCT FROM OLD.scopes
                OR NEW.handler_ref IS DISTINCT FROM OLD.handler_ref
                OR NEW.definition_hash IS DISTINCT FROM OLD.definition_hash
            ) THEN
                RAISE EXCEPTION
                    'tool_definitions row % (name=%, version=%) is immutable once '
                    'status <> draft (current status=%)',
                    OLD.id, OLD.name, OLD.version, OLD.status;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER trg_tool_definitions_immutability
        BEFORE UPDATE ON tool_definitions
        FOR EACH ROW
        EXECUTE FUNCTION enforce_tool_definition_immutability();
        """
    )

    # --- Seed data -----------------------------------------------------------

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
    tool_seeds = [
        {
            "name": "attention.get_item",
            "scopes": ["read:attention"],
            "input_schema": _ATTENTION_GET_ITEM_INPUT_SCHEMA,
            "output_schema": _ATTENTION_GET_ITEM_OUTPUT_SCHEMA,
            "handler_ref": "ecc.domains.attention.tools:get_item_tool",
        },
        {
            "name": "knowledge.get_entity",
            "scopes": ["read:knowledge"],
            "input_schema": _KNOWLEDGE_GET_ENTITY_INPUT_SCHEMA,
            "output_schema": _KNOWLEDGE_GET_ENTITY_OUTPUT_SCHEMA,
            "handler_ref": "ecc.domains.knowledge.tools:get_entity_tool",
        },
    ]
    for seed in tool_seeds:
        definition_hash = _canonical_hash(
            {
                "input_schema": seed["input_schema"],
                "output_schema": seed["output_schema"],
                "scopes": seed["scopes"],
                "handler_ref": seed["handler_ref"],
            }
        )
        op.execute(
            tool_definitions.insert().values(
                id=uuid4(),
                name=seed["name"],
                version=1,
                scopes=seed["scopes"],
                input_schema=seed["input_schema"],
                output_schema=seed["output_schema"],
                handler_ref=seed["handler_ref"],
                definition_hash=definition_hash,
                status="active",
                created_at=sa.func.now(),
                updated_at=sa.func.now(),
            )
        )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_tool_definitions_immutability ON tool_definitions")
    op.execute("DROP FUNCTION IF EXISTS enforce_tool_definition_immutability()")
    op.execute("DROP TRIGGER IF EXISTS trg_prompt_versions_immutability ON prompt_versions")
    op.execute("DROP FUNCTION IF EXISTS enforce_prompt_version_immutability()")
    op.drop_index("uq_tool_definitions_active_per_name", table_name="tool_definitions")
    op.drop_table("tool_definitions")
    op.drop_index("uq_prompt_versions_active_per_prompt_id", table_name="prompt_versions")
    op.drop_table("prompt_versions")
