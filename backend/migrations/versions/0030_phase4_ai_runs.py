"""Create ai_runs and ai_run_steps for Phase 4 AI Runtime Task 4.

Phase 4 Task 4 (`docs/superpowers/plans/2026-07-23-phase-4-ai-runtime.md`).
Renumbered from the plan's nominal ``0024_phase4_ai_runs.py`` to ``0030`` --
Tasks 1/2 already claimed ``0028``/``0029`` on this branch (themselves
renumbered past Phase 3's 0022-0027), so ``0030`` is the actual next
available number, matching this repository's existing rule that migration
file numbers follow actual implementation/chain order, not the task numbers
a plan happened to draft them under (see ``0028``/``0029``'s own docstrings
for the same allowance).

Unlike ``model_definitions``/``routing_policies``/``prompt_versions``/
``tool_definitions`` (migrations 0028/0029 -- global platform catalogs with
no ``workspace_id``), ``ai_runs``/``ai_run_steps`` **are** workspace-scoped,
matching ``phase-004/DATA-MODEL.md``'s framing ("Every table is workspace
scoped where user data is present") and this repository's Global
constraints ("Every new table is workspace-scoped with composite foreign
keys"): a run is a genuine invocation triggered by a specific workspace,
referencing that workspace's own ``attention_items`` row -- it is exactly
the kind of user data ``model_definitions`` et al. are not.

``uq_ai_runs_workspace_id_id`` exists purely so ``ai_run_steps`` can hold a
composite foreign key back to ``ai_runs`` (``workspace_id``, ``run_id``) --
the same pattern ``meetings.uq_meetings_workspace_id_id`` (migration 0005)
and ``pkos_nodes.uq_pkos_nodes_workspace_id_id`` (migration 0001) already
establish for exactly this reason (PostgreSQL requires the referenced
column set to be covered by its own unique constraint, even though ``id``
alone is already globally unique via the primary key).

**Redacted trace columns only (design doc Decision 5's threat model /
`DATA-MODEL.md`: "Trace is redacted by default -- raw prompt/output text is
not stored unless a workspace has an explicit, time-bound verbose-trace
opt-in").** ``ai_run_steps.trace`` is a small structured JSONB envelope --
model_id/tool name, timing, token counts, validation outcome enum -- never
the rendered prompt text or the model's raw response text; no column on
either table is sized or named to hold that raw content. This is distinct
from ``ai_runs.output``: that column holds the *validated*, schema-checked
task result (`ecc.domains.ai_runtime.validator.ExplainItemOutput`'s two
typed fields) that ``API-SCHEMAS.md`` requires ``GET /ai/runs/{id}`` to
return to its caller -- a safe-to-return structured value, not "raw model
output" in the sense the redaction rule (RFC-005's Observability baseline)
means to forbid. A response that never validated never reaches this column
at all (`validator.py`'s own "A validation failure never reaches the domain
layer").

Seeds no rows -- unlike 0028/0029's global catalogs, a run is created only
by an authenticated request (`POST /ai/runs`, `ecc.domains.ai_runtime.
runtime`), never by migration-time fixture data.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0030_phase4_ai_runs"
down_revision = "0029_phase4_prompt_tool_versions"
branch_labels = None
depends_on = None


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    op.create_table(
        "ai_runs",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("actor_id", uuid, nullable=False),
        sa.Column("task_type", sa.String(100), nullable=False),
        sa.Column("data_class", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="running"),
        sa.Column("policy_version", sa.Integer()),
        sa.Column("model_id", sa.String(200)),
        sa.Column("provider", sa.String(50)),
        sa.Column("prompt_id", sa.String(200)),
        sa.Column("prompt_version", sa.Integer()),
        sa.Column(
            "input_ref",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("output", postgresql.JSONB()),
        sa.Column(
            "evidence",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("error_code", sa.String(50)),
        sa.Column("prompt_tokens", sa.Integer()),
        sa.Column("output_tokens", sa.Integer()),
        sa.Column("cost", sa.Numeric(10, 4), nullable=False, server_default="0.0"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('running','completed','degraded','failed','cancelled')",
            name="ck_ai_runs_status",
        ),
        sa.CheckConstraint(
            "data_class IN ('public','internal','sensitive','restricted')",
            name="ck_ai_runs_data_class",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "actor_id"],
            ["users.workspace_id", "users.id"],
            name="fk_ai_runs_workspace_actor",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint("workspace_id", "id", name="uq_ai_runs_workspace_id_id"),
    )
    op.create_index("ix_ai_runs_workspace_status", "ai_runs", ["workspace_id", "status"])
    op.create_index("ix_ai_runs_workspace_created", "ai_runs", ["workspace_id", "created_at"])

    op.create_table(
        "ai_run_steps",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("run_id", uuid, nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column(
            "trace",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('model_call','tool_call')", name="ck_ai_run_steps_kind"),
        sa.CheckConstraint(
            "status IN ('succeeded','failed','rejected','cancelled')",
            name="ck_ai_run_steps_status",
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "run_id"],
            ["ai_runs.workspace_id", "ai_runs.id"],
            name="fk_ai_run_steps_workspace_run",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint("run_id", "sequence", name="uq_ai_run_steps_run_sequence"),
    )
    op.create_index("ix_ai_run_steps_workspace_run", "ai_run_steps", ["workspace_id", "run_id"])


def downgrade() -> None:
    op.drop_index("ix_ai_run_steps_workspace_run", table_name="ai_run_steps")
    op.drop_table("ai_run_steps")
    op.drop_index("ix_ai_runs_workspace_created", table_name="ai_runs")
    op.drop_index("ix_ai_runs_workspace_status", table_name="ai_runs")
    op.drop_table("ai_runs")
