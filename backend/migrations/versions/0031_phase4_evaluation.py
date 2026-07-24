"""Create evaluation_sets, evaluation_runs and generated_artifacts for
Phase 4 AI Runtime Task 5.

Phase 4 Task 5 (`docs/superpowers/plans/2026-07-23-phase-4-ai-runtime.md`).
Renumbered from the plan's nominal ``0025_phase4_evaluation.py`` to
``0031`` -- Tasks 1/2/4 already claimed ``0028``/``0029``/``0030`` on this
branch (themselves renumbered past Phase 3's 0022-0027), so ``0031`` is the
actual next available number, matching this repository's existing rule
(restated by every prior Phase 4 migration's own docstring) that migration
file numbers follow actual implementation/chain order, not the task numbers
a plan happened to draft them under.

**``evaluation_sets`` is global platform catalog data, not workspace-scoped
user data** -- the same reasoning ``0028``/``0029``'s docstrings already
give for ``model_definitions``/``prompt_versions``: a versioned, labelled
evaluation dataset is shared by every workspace this deployment serves,
exactly like the prompt template it evaluates. ``examples`` is the full
20-example dataset (design doc Decision 9,
`docs/phases/phase-004/EVALUATION-CONTRACT.md`) as a JSONB array, seeded
below with the *identical* content ``tests/fixtures/
phase4_evaluation_attention_explain.py`` defines -- duplicated literally,
not imported, per this codebase's established migration-self-containment
convention (``0029``'s docstring: "migrations in this codebase do not
import ``ecc`` application code"; the same reasoning applies a fortiori to
test fixture code, which is not guaranteed present in every environment
that runs migrations). Keep both copies in sync by convention if this
dataset is ever revised -- exactly the same discipline ``0029``'s
``_canonical_hash`` asks of ``ecc.domains.ai_runtime.prompts.
compute_template_hash``.

**``evaluation_runs`` and ``generated_artifacts`` *are* workspace-scoped.**
Unlike ``evaluation_sets`` (the shared, versioned dataset itself), a
specific evaluation *run* is a genuine invocation triggered under a
specific workspace/session context -- `ecc.domains.ai_runtime.evaluation.
run_evaluation` drives every example through `runtime.py:execute_run`,
which requires a real `AuthContext` and writes real (if ephemeral)
`attention_items`/`ai_runs` rows scoped to that workspace, exactly the same
"genuine invocation" reasoning migration ``0030``'s docstring gives for
``ai_runs``/``ai_run_steps`` themselves. ``generated_artifacts`` similarly
scopes to the workspace whose `ai_runs` row it derives from (composite FK
back to ``ai_runs``, the same ``(workspace_id, id)`` pattern ``ai_run_
steps`` already uses against ``ai_runs`` in migration ``0030``).

**Metrics recorded on ``evaluation_runs`` (`DATA-MODEL.md`, design doc
Decision 9 / `EVALUATION-CONTRACT.md`'s four floors).**
``schema_validity_rate``/``grounding_rate`` are fractions in ``[0, 1]``;
``prohibited_fact_count`` is a raw count (`EVALUATION-CONTRACT.md`: "stated
as a count, not a percentage, because even one instance is a fabricated
fact"); ``latency_p95_seconds`` is the full-run p95 including the tool call.
``passed`` is the precomputed result of `ecc.domains.ai_runtime.evaluation.
check_promotion_floors` against those four numbers at the moment the run
completed -- stored, not just derivable, so `prompts.py`'s promotion gate
(`POST /ai/policies/{id}/activate`) can consult a completed run's verdict
without re-deriving it, while still being able to re-derive it from the raw
metrics if ever needed for audit. ``failures`` is a small JSONB array of
per-example failure summaries (example key, failure reason) -- never raw
model output text, matching this codebase's redaction discipline
(`ai_run_steps.trace`'s identical rule, migration ``0030``).

**``generated_artifacts``** (`DATA-MODEL.md`: "Derived proposed output ...
Never becomes authoritative"). ``source_versions`` pins the
`attention_items` row version the run actually read (`{"attention_item_id":
..., "source_entity_version": ...}`); ``evidence`` is the cited factor
codes (`ai_runs.evidence`'s same shape); ``schema_version`` is the
`output_schema_ref` the validated output was checked against
(`prompt_versions.output_schema_ref`, e.g. ``attention.explain_item.
output.v1``). A composite FK back to ``(ai_runs.workspace_id, ai_runs.id)``
means every artifact traces to the exact run that produced it -- the
grounding check itself is already enforced at write time inside
`execute_run` (`check_explain_item_grounding`, Task 4, unchanged); this
table's ``evidence``/``source_versions`` columns record that already-
enforced result, they do not re-enforce it.

Neither ``evaluation_runs`` nor ``generated_artifacts`` seeds any rows --
like ``ai_runs``/``ai_run_steps`` (migration ``0030``), a run is created
only by an authenticated evaluation invocation, never by migration-time
fixture data.
"""

from uuid import uuid4

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0031_phase4_evaluation"
down_revision = "0030_phase4_ai_runs"
branch_labels = None
depends_on = None

_TASK_TYPE = "attention.explain_item"

# ---------------------------------------------------------------------------
# Seed data -- literal duplicate of
# tests/fixtures/phase4_evaluation_attention_explain.py's EXAMPLES (see
# module docstring for why this is a duplicate, not an import). Keep in
# sync if that fixture is ever revised.
# ---------------------------------------------------------------------------

_EXAMPLES = [
    {
        "key": "task_overdue_critical_pinned_blocked",
        "entity_type": "task",
        "score": 86,
        "confidence": 0.8,
        "factors": [
            {
                "code": "manual_priority",
                "label": "Manual priority critical",
                "points": 30,
                "source_field": "manual_priority",
            },
            {
                "code": "overdue",
                "label": "Due timing overdue",
                "points": 25,
                "source_field": "due_date,due_at",
            },
            {
                "code": "pinned",
                "label": "Explicitly pinned",
                "points": 15,
                "source_field": "pinned",
            },
            {"code": "blocked", "label": "Task is blocked", "points": 10, "source_field": "status"},
            {
                "code": "stale_14d",
                "label": "No movement for 14 days",
                "points": 6,
                "source_field": "updated_at",
            },
        ],
        "must_cite": ["manual_priority", "overdue", "pinned"],
        "must_not_state": ["waiting on another person", "due within 48 hours"],
        "reference_explanation": (
            "This critical, pinned task is overdue and currently blocked, with no "
            "progress in two weeks -- it needs attention now."
        ),
    },
    {
        "key": "task_due_48h_medium_waiting",
        "entity_type": "task",
        "score": 38,
        "confidence": 1.0,
        "factors": [
            {
                "code": "manual_priority",
                "label": "Manual priority medium",
                "points": 15,
                "source_field": "manual_priority",
            },
            {
                "code": "due_48h",
                "label": "Due timing due_48h",
                "points": 20,
                "source_field": "due_date,due_at",
            },
            {
                "code": "waiting_on",
                "label": "Waiting on another person",
                "points": 10,
                "source_field": "blocked_on_person_id",
            },
        ],
        "must_cite": ["due_48h", "waiting_on"],
        "must_not_state": ["overdue", "pinned"],
        "reference_explanation": (
            "This medium-priority task is due within two days and is waiting on "
            "someone else to act."
        ),
    },
    {
        "key": "task_no_due_high_stale14d",
        "entity_type": "task",
        "score": 33,
        "confidence": 1.0,
        "factors": [
            {
                "code": "manual_priority",
                "label": "Manual priority high",
                "points": 25,
                "source_field": "manual_priority",
            },
            {
                "code": "stale_14d",
                "label": "No movement for 14 days",
                "points": 6,
                "source_field": "updated_at",
            },
        ],
        "must_cite": ["manual_priority", "stale_14d"],
        "must_not_state": ["overdue", "a specific due date", "pinned"],
        "reference_explanation": (
            "This high-priority task has no due date but hasn't moved in two weeks."
        ),
    },
    {
        "key": "task_recently_created_pinned",
        "entity_type": "task",
        "score": 28,
        "confidence": 1.0,
        "factors": [
            {
                "code": "manual_priority",
                "label": "Manual priority low",
                "points": 5,
                "source_field": "manual_priority",
            },
            {
                "code": "pinned",
                "label": "Explicitly pinned",
                "points": 15,
                "source_field": "pinned",
            },
            {
                "code": "recently_created",
                "label": "Recently created",
                "points": 8,
                "source_field": "created_at",
            },
        ],
        "must_cite": ["pinned", "recently_created"],
        "must_not_state": ["overdue", "blocked", "waiting on another person"],
        "reference_explanation": (
            "This low-priority task was just created and has been manually pinned for visibility."
        ),
    },
    {
        "key": "commitment_overdue_critical_pinned_made_to_me",
        "entity_type": "commitment",
        "score": 88,
        "confidence": 0.95,
        "factors": [
            {
                "code": "importance",
                "label": "Importance critical",
                "points": 30,
                "source_field": "importance",
            },
            {
                "code": "overdue",
                "label": "Due timing overdue",
                "points": 25,
                "source_field": "due_date,due_at",
            },
            {
                "code": "waiting_on",
                "label": "Waiting on another person",
                "points": 10,
                "source_field": "direction",
            },
            {
                "code": "pinned",
                "label": "Explicitly pinned",
                "points": 15,
                "source_field": "pinned",
            },
        ],
        "must_cite": ["importance", "overdue", "waiting_on"],
        "must_not_state": ["due within 48 hours", "no movement in over a week"],
        "reference_explanation": (
            "This critical commitment made to you is overdue and pinned for visibility."
        ),
    },
    {
        "key": "commitment_due_48h_high_made_by_me",
        "entity_type": "commitment",
        "score": 33,
        "confidence": 0.6,
        "factors": [
            {
                "code": "importance",
                "label": "Importance high",
                "points": 25,
                "source_field": "importance",
            },
            {
                "code": "due_48h",
                "label": "Due timing due_48h",
                "points": 20,
                "source_field": "due_date,due_at",
            },
        ],
        "must_cite": ["importance", "due_48h"],
        "must_not_state": ["overdue", "waiting on another person", "pinned"],
        "reference_explanation": (
            "This high-importance commitment you made is due within two days."
        ),
    },
    {
        "key": "commitment_no_due_low",
        "entity_type": "commitment",
        "score": 4,
        "confidence": 0.5,
        "factors": [
            {
                "code": "importance",
                "label": "Importance low",
                "points": 5,
                "source_field": "importance",
            },
        ],
        "must_cite": ["importance"],
        "must_not_state": ["overdue", "a due date", "pinned", "waiting on another person"],
        "reference_explanation": "This is a low-importance commitment with no due date.",
    },
    {
        "key": "risk_high_impact_review_overdue_pinned",
        "entity_type": "risk",
        "score": 80,
        "confidence": 1.0,
        "factors": [
            {
                "code": "risk_impact",
                "label": "Risk impact 25",
                "points": 30,
                "source_field": "probability,impact",
            },
            {
                "code": "review_overdue",
                "label": "Risk review overdue",
                "points": 20,
                "source_field": "review_at",
            },
            {
                "code": "pinned",
                "label": "Explicitly pinned",
                "points": 15,
                "source_field": "pinned",
            },
        ],
        "must_cite": ["risk_impact", "review_overdue", "pinned"],
        "must_not_state": ["review due within 48 hours"],
        "reference_explanation": (
            "This high-impact risk's review is overdue and it has been pinned for attention."
        ),
    },
    {
        "key": "risk_medium_impact_review_due_soon",
        "entity_type": "risk",
        "score": 30,
        "confidence": 1.0,
        "factors": [
            {
                "code": "risk_impact",
                "label": "Risk impact 12",
                "points": 15,
                "source_field": "probability,impact",
            },
            {
                "code": "review_due_soon",
                "label": "Risk review due within 48 hours",
                "points": 10,
                "source_field": "review_at",
            },
        ],
        "must_cite": ["risk_impact", "review_due_soon"],
        "must_not_state": ["overdue", "pinned"],
        "reference_explanation": ("This medium-impact risk's review is coming up within two days."),
    },
    {
        "key": "risk_low_impact_no_review",
        "entity_type": "risk",
        "score": 8,
        "confidence": 1.0,
        "factors": [
            {
                "code": "risk_impact",
                "label": "Risk impact 6",
                "points": 5,
                "source_field": "probability,impact",
            },
        ],
        "must_cite": ["risk_impact"],
        "must_not_state": ["review overdue", "pinned"],
        "reference_explanation": "This is a low-impact risk with no scheduled review.",
    },
    {
        "key": "risk_zero_impact_pinned",
        "entity_type": "risk",
        "score": 15,
        "confidence": 1.0,
        "factors": [
            {
                "code": "pinned",
                "label": "Explicitly pinned",
                "points": 15,
                "source_field": "pinned",
            },
        ],
        "must_cite": ["pinned"],
        "must_not_state": ["risk impact", "review overdue", "review due soon"],
        "reference_explanation": (
            "This risk currently has no scored impact but has been manually pinned."
        ),
    },
    {
        "key": "waiting_link_waiting_on_me_overdue_stale",
        "entity_type": "waiting_link",
        "score": 75,
        "confidence": 1.0,
        "factors": [
            {
                "code": "waiting_direction",
                "label": "Waiting: waiting on me",
                "points": 30,
                "source_field": "direction",
            },
            {
                "code": "overdue",
                "label": "Expected timing passed",
                "points": 25,
                "source_field": "expected_at",
            },
            {
                "code": "stale_14d",
                "label": "Waiting for 14+ days",
                "points": 6,
                "source_field": "since_at",
            },
        ],
        "must_cite": ["waiting_direction", "overdue", "stale_14d"],
        "must_not_state": ["due within 48 hours", "blocked by"],
        "reference_explanation": (
            "This is waiting on you directly, the expected time has passed, and "
            "it's been outstanding for over two weeks."
        ),
    },
    {
        "key": "waiting_link_blocked_by_due_48h",
        "entity_type": "waiting_link",
        "score": 60,
        "confidence": 1.0,
        "factors": [
            {
                "code": "waiting_direction",
                "label": "Waiting: blocked by",
                "points": 30,
                "source_field": "direction",
            },
            {
                "code": "due_48h",
                "label": "Expected within 48 hours",
                "points": 20,
                "source_field": "expected_at",
            },
        ],
        "must_cite": ["waiting_direction", "due_48h"],
        "must_not_state": ["overdue", "no movement in over a week"],
        "reference_explanation": (
            "Your work is blocked by this, and it's expected within the next two days."
        ),
    },
    {
        "key": "waiting_link_waiting_on_them_stale7d",
        "entity_type": "waiting_link",
        "score": 18,
        "confidence": 1.0,
        "factors": [
            {
                "code": "waiting_direction",
                "label": "Waiting: waiting on them",
                "points": 10,
                "source_field": "direction",
            },
            {
                "code": "stale_7d",
                "label": "Waiting for 7+ days",
                "points": 4,
                "source_field": "since_at",
            },
        ],
        "must_cite": ["waiting_direction", "stale_7d"],
        "must_not_state": ["overdue", "blocked by", "waiting on me"],
        "reference_explanation": (
            "You're waiting on someone else for this, and it's been a week with no update."
        ),
    },
    {
        "key": "risk_review_overdue_high_impact",
        "entity_type": "risk_review",
        "score": 70,
        "confidence": 1.0,
        "factors": [
            {
                "code": "review_overdue",
                "label": "Risk review overdue",
                "points": 25,
                "source_field": "review_at",
            },
            {
                "code": "risk_impact",
                "label": "Risk impact 20",
                "points": 20,
                "source_field": "probability,impact",
            },
        ],
        "must_cite": ["review_overdue", "risk_impact"],
        "must_not_state": ["review due within 48 hours", "pinned"],
        "reference_explanation": (
            "This risk's scheduled review is overdue and the underlying risk has "
            "significant impact."
        ),
    },
    {
        "key": "risk_review_due_soon_pinned",
        "entity_type": "risk_review",
        "score": 40,
        "confidence": 1.0,
        "factors": [
            {
                "code": "review_due_soon",
                "label": "Risk review due within 48 hours",
                "points": 10,
                "source_field": "review_at",
            },
            {
                "code": "pinned",
                "label": "Explicitly pinned",
                "points": 15,
                "source_field": "pinned",
            },
        ],
        "must_cite": ["review_due_soon", "pinned"],
        "must_not_state": ["overdue"],
        "reference_explanation": "This risk review is due within two days and has been pinned.",
    },
    {
        "key": "risk_review_no_next_review_scheduled",
        "entity_type": "risk_review",
        "score": 12,
        "confidence": 1.0,
        "factors": [
            {
                "code": "no_next_review_scheduled",
                "label": "No follow-up review scheduled",
                "points": 8,
                "source_field": "next_review_at",
            },
        ],
        "must_cite": ["no_next_review_scheduled"],
        "must_not_state": ["review overdue", "review due within 48 hours", "pinned"],
        "reference_explanation": (
            "This risk was reviewed, but no follow-up review has been scheduled."
        ),
    },
    {
        "key": "meeting_starts_soon_missing_agenda",
        "entity_type": "meeting",
        "score": 55,
        "confidence": 1.0,
        "factors": [
            {
                "code": "starts_soon",
                "label": "Meeting starts within 2 hours",
                "points": 25,
                "source_field": "starts_at",
            },
            {
                "code": "missing_agenda",
                "label": "No agenda set",
                "points": 15,
                "source_field": "agenda",
            },
        ],
        "must_cite": ["starts_soon", "missing_agenda"],
        "must_not_state": ["no preparation notes", "pinned"],
        "reference_explanation": "This meeting starts soon and has no agenda set yet.",
    },
    {
        "key": "meeting_no_preparation_notes",
        "entity_type": "meeting",
        "score": 20,
        "confidence": 1.0,
        "factors": [
            {
                "code": "no_preparation_notes",
                "label": "No preparation notes recorded",
                "points": 10,
                "source_field": "preparation",
            },
        ],
        "must_cite": ["no_preparation_notes"],
        "must_not_state": ["starts within 2 hours", "missing agenda"],
        "reference_explanation": (
            "No preparation notes have been recorded for this upcoming meeting."
        ),
    },
    {
        "key": "meeting_pinned_recently_created",
        "entity_type": "meeting",
        "score": 33,
        "confidence": 1.0,
        "factors": [
            {
                "code": "pinned",
                "label": "Explicitly pinned",
                "points": 15,
                "source_field": "pinned",
            },
            {
                "code": "recently_created",
                "label": "Recently created",
                "points": 8,
                "source_field": "created_at",
            },
        ],
        "must_cite": ["pinned", "recently_created"],
        "must_not_state": ["starts soon", "missing agenda"],
        "reference_explanation": (
            "This meeting was just added to your calendar and has been pinned for visibility."
        ),
    },
]

assert len(_EXAMPLES) == 20


def upgrade() -> None:
    uuid = postgresql.UUID(as_uuid=True)

    # --- evaluation_sets: global platform catalog (see module docstring) --
    op.create_table(
        "evaluation_sets",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("task_type", sa.String(100), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("classification", sa.String(20), nullable=False, server_default="labelled"),
        sa.Column("example_count", sa.Integer(), nullable=False),
        sa.Column("examples", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "classification IN ('labelled', 'development')",
            name="ck_evaluation_sets_classification",
        ),
        sa.CheckConstraint("status IN ('active', 'retired')", name="ck_evaluation_sets_status"),
        sa.UniqueConstraint("task_type", "version", name="uq_evaluation_sets_task_type_version"),
    )
    # At most one active dataset version per task type -- mirrors
    # uq_routing_policies_active_per_task_type (migration 0028) and
    # uq_prompt_versions_active_per_prompt_id (migration 0029).
    op.create_index(
        "uq_evaluation_sets_active_per_task_type",
        "evaluation_sets",
        ["task_type"],
        unique=True,
        postgresql_where=sa.text("status = 'active'"),
    )

    # --- evaluation_runs: workspace-scoped (see module docstring) ----------
    op.create_table(
        "evaluation_runs",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("actor_id", uuid, nullable=False),
        sa.Column("task_type", sa.String(100), nullable=False),
        sa.Column("evaluation_set_id", uuid, nullable=False),
        sa.Column("dataset_version", sa.Integer(), nullable=False),
        sa.Column("prompt_id", sa.String(200), nullable=False),
        sa.Column("prompt_version", sa.Integer(), nullable=False),
        sa.Column("model_id", sa.String(200), nullable=False),
        sa.Column("provider", sa.String(50), nullable=False),
        sa.Column("total_examples", sa.Integer(), nullable=False),
        sa.Column("schema_validity_rate", sa.Numeric(5, 4), nullable=False),
        sa.Column("grounding_rate", sa.Numeric(5, 4), nullable=False),
        sa.Column("prohibited_fact_count", sa.Integer(), nullable=False),
        sa.Column("latency_p95_seconds", sa.Numeric(10, 3), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column(
            "failures", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="completed"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("status IN ('completed', 'failed')", name="ck_evaluation_runs_status"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "actor_id"],
            ["users.workspace_id", "users.id"],
            name="fk_evaluation_runs_workspace_actor",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(["evaluation_set_id"], ["evaluation_sets.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("workspace_id", "id", name="uq_evaluation_runs_workspace_id_id"),
    )
    op.create_index(
        "ix_evaluation_runs_workspace_prompt",
        "evaluation_runs",
        ["workspace_id", "task_type", "prompt_id", "prompt_version"],
    )
    op.create_index(
        "ix_evaluation_runs_workspace_created",
        "evaluation_runs",
        ["workspace_id", "created_at"],
    )

    # --- generated_artifacts: workspace-scoped (see module docstring) ------
    op.create_table(
        "generated_artifacts",
        sa.Column("id", uuid, primary_key=True),
        sa.Column("workspace_id", uuid, nullable=False),
        sa.Column("ai_run_id", uuid, nullable=False),
        sa.Column("task_type", sa.String(100), nullable=False),
        sa.Column(
            "source_versions",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("schema_version", sa.String(200), nullable=False),
        sa.Column("output", postgresql.JSONB(), nullable=False),
        sa.Column(
            "evidence", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="proposed"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('proposed', 'superseded')", name="ck_generated_artifacts_status"
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["workspace_id", "ai_run_id"],
            ["ai_runs.workspace_id", "ai_runs.id"],
            name="fk_generated_artifacts_workspace_ai_run",
            ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_generated_artifacts_workspace_ai_run",
        "generated_artifacts",
        ["workspace_id", "ai_run_id"],
    )
    op.create_index(
        "ix_generated_artifacts_workspace_created",
        "generated_artifacts",
        ["workspace_id", "created_at"],
    )

    # --- Seed evaluation_sets version 1 -------------------------------------
    evaluation_sets = sa.table(
        "evaluation_sets",
        sa.column("id", uuid),
        sa.column("task_type", sa.String()),
        sa.column("version", sa.Integer()),
        sa.column("classification", sa.String()),
        sa.column("example_count", sa.Integer()),
        sa.column("examples", postgresql.JSONB()),
        sa.column("status", sa.String()),
        sa.column("created_at", sa.DateTime(timezone=True)),
        sa.column("updated_at", sa.DateTime(timezone=True)),
    )
    op.execute(
        evaluation_sets.insert().values(
            id=uuid4(),
            task_type=_TASK_TYPE,
            version=1,
            classification="labelled",
            example_count=len(_EXAMPLES),
            examples=_EXAMPLES,
            status="active",
            created_at=sa.func.now(),
            updated_at=sa.func.now(),
        )
    )


def downgrade() -> None:
    op.drop_index("ix_generated_artifacts_workspace_created", table_name="generated_artifacts")
    op.drop_index("ix_generated_artifacts_workspace_ai_run", table_name="generated_artifacts")
    op.drop_table("generated_artifacts")
    op.drop_index("ix_evaluation_runs_workspace_created", table_name="evaluation_runs")
    op.drop_index("ix_evaluation_runs_workspace_prompt", table_name="evaluation_runs")
    op.drop_table("evaluation_runs")
    op.drop_index("uq_evaluation_sets_active_per_task_type", table_name="evaluation_sets")
    op.drop_table("evaluation_sets")
