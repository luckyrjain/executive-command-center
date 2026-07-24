"""Create model_definitions and routing_policies for Phase 4 AI Runtime Task 1.

Phase 4 Task 1 (`docs/superpowers/plans/2026-07-23-phase-4-ai-runtime.md`).
Renumbered from the plan's nominal ``0022_phase4_model_registry.py`` to
``0028`` -- Phase 3 (merged first) already claimed 0022-0027, matching this
repository's existing rule that migration file numbers follow actual
implementation/chain order, not the task numbers a plan happened to draft
them under (see the plan's "Planned file structure" note; same allowance
Phase 2/3 used).

Both tables are deliberately **not** workspace-scoped, unlike almost every
other table in this schema. ``DATA-MODEL.md``'s own framing line -- "Every
table is workspace scoped **where user data is present**" -- is a
conditional, not a blanket rule, and neither table holds user data:
``model_definitions`` is the platform's approved model/provider catalog
(design doc Decision 1/2 -- exactly one row, `deployment=local`, shared by
every workspace this deployment serves) and ``routing_policies`` is the
fixed, versioned routing configuration for a task type (Decision 2's
eligibility/preference pipeline is explicitly "not per-policy configurable
in this first cut", per `phase-004/DATA-MODEL.md`). This mirrors this exact
codebase's own precedent for genuinely global, non-user-data tables:
``event_inbox``/``event_dead_letters`` (migration 0001) carry no
`workspace_id` either, for the identical reason -- they are platform
bookkeeping, not a workspace's own records. Seeding "exactly one row" (the
migration's own Step 2 instruction) would otherwise be ambiguous under a
workspace-scoped design (one row total, or one row per existing workspace,
re-seeded on every new workspace?) -- treating this as global data resolves
that ambiguity the way the data model already intends.

``model_definitions.data_classes``/``capabilities`` use ``ARRAY(TEXT)``,
matching ``recommendations.evidence_ids``/``risk_reviews``' own precedent
for small fixed-vocabulary lists in this codebase (not JSONB, which is
reserved here for genuinely structured/nested data --
``routing_policies.candidates``/``constraints``/``fallback``).

Seeds exactly one ``model_definitions`` row (design doc Decision 1/`DATA-
MODEL.md`): ``provider='ollama'``, ``model_id='qwen2.5:1.5b-instruct-
q4_K_M'``, ``deployment='local'``, all four data classes (Decision 7:
``public``/``internal``/``sensitive``/``restricted``), capabilities
``{extraction, summarization, explanation}``. ``context_window_tokens`` is
set to 32768, Qwen2.5's documented native context length for this model
family at this size -- ``ADR-0012`` does not pin an exact figure, so this is
recorded here as the concrete value the router's context-limit eligibility
step (Decision 2 step 4) actually compares against, revisit only if a real
Ollama ``show`` call against this model reports otherwise.

Seeds exactly one ``routing_policies`` row for the only task type this
activation registers, ``attention.explain_item`` (Decision 9): a single
candidate pointing at the seeded model, and ``constraints`` echoing Decision
5's budget table (max input/output tokens, per-model-call timeout, total run
budget) so a later task can read them from the policy row rather than a
second hardcoded copy. ``fallback`` is an empty object -- there is no second
model to fall back to in this activation (Decision 8).

A partial unique index enforces at most one ``active`` ``routing_policies``
version per ``task_type``, mirroring the ``active``-scoped partial unique
index pattern Task 2's ``prompt_versions``/``tool_definitions`` will use
(Decision 3) and this repository's existing precedent
(``uq_meeting_packs_active_per_meeting``, migration 0027).
"""

from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0028_phase4_model_registry"
down_revision = "0027_phase3_meetings"
branch_labels = None
depends_on = None

_MODEL_ID = "qwen2.5:1.5b-instruct-q4_K_M"
_PROVIDER = "ollama"
_TASK_TYPE = "attention.explain_item"


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.create_table(
        "model_definitions",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("model_id", sa.String(200), nullable=False),
        sa.Column("deployment", sa.String(20), nullable=False),
        sa.Column("data_classes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("capabilities", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("context_window_tokens", sa.Integer(), nullable=False),
        sa.Column(
            "structured_output_supported",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "deployment IN ('local', 'remote')", name="ck_model_definitions_deployment"
        ),
        sa.CheckConstraint("status IN ('active', 'disabled')", name="ck_model_definitions_status"),
        sa.UniqueConstraint("provider", "model_id", name="uq_model_definitions_provider_model"),
    )
    op.create_index(
        "ix_model_definitions_status",
        "model_definitions",
        ["status"],
    )

    op.create_table(
        "routing_policies",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("task_type", sa.String(100), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("candidates", postgresql.JSONB(), nullable=False),
        sa.Column(
            "constraints",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "fallback",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('active', 'retired')", name="ck_routing_policies_status"),
        sa.UniqueConstraint("task_type", "version", name="uq_routing_policies_task_type_version"),
    )
    # At most one active policy version per task type -- see module
    # docstring; mirrors uq_meeting_packs_active_per_meeting's partial
    # unique index pattern (migration 0027).
    op.create_index(
        "uq_routing_policies_active_per_task_type",
        "routing_policies",
        ["task_type"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

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
            model_id=_MODEL_ID,
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
            candidates=[{"provider": _PROVIDER, "model_id": _MODEL_ID}],
            constraints={
                "max_input_tokens": 3072,
                "max_output_tokens": 512,
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
    op.drop_index("uq_routing_policies_active_per_task_type", table_name="routing_policies")
    op.drop_table("routing_policies")
    op.drop_index("ix_model_definitions_status", table_name="model_definitions")
    op.drop_table("model_definitions")
