"""The evaluation harness (design doc Decision 9,
`docs/phases/phase-004/EVALUATION-CONTRACT.md`).

`run_evaluation` drives every example in the active `evaluation_sets`
version for a task type through the *real* orchestration loop
(`runtime.py:execute_run`, Task 4, unmodified) and scores the result
against `EVALUATION-CONTRACT.md`'s four floors (schema validity, grounding,
prohibited-fact count, p95 latency). `check_promotion_floors` is the pure
function that turns a completed `EvaluationRun`'s four metrics into a
single pass/fail verdict -- the same function `prompts.py`'s `POST
/ai/policies/{id}/activate` consults (via a call-time-deferred import, see
that module's `_prompt_evaluation_floor_met`) before allowing a new version
of the `attention.explain_item` prompt to become `active` (Decision 9:
"Promotion ... always re-runs the full 20-example set and requires the
table above to pass in full before the new version can become active").

**Why `evaluation.py` imports `runtime.py` but never the reverse.**
`runtime.py` (Task 4) has no knowledge of this module and is not modified
by it -- `run_evaluation` only ever calls `execute_run` through its already-
committed public signature. `prompts.py` (Task 2) *does* need this module's
`check_promotion_floors`/`get_latest_evaluation_run`, but importing this
module at `prompts.py`'s top level would create a cycle (`prompts` ->
`evaluation` -> `runtime` -> `prompts`, since `runtime.py` already does
`from .prompts import get_active_prompt`) -- `prompts.py` breaks that cycle
with a call-time-local import instead of a module-level one, which is safe
because by the time any HTTP request reaches `activate_policy`, every
module in this package has already finished importing (see that module's
comment for the full explanation).

**What this activation's `run_evaluation` deliberately does not attempt.**
`execute_run` (Task 4) always renders whichever prompt version is currently
`active` and always routes through `router.route()`'s live eligibility
pipeline -- it has no parameter to force a specific *draft* prompt version
or to pin a specific model bypassing routing. `run_evaluation`'s
`prompt_version`/`model_id` parameters are therefore *assertions*, not
overrides: it verifies the requested prompt version is the one currently
active and the requested model is currently registered and eligible
*before* running anything, and raises `EvaluationConfigError` immediately
if either does not hold, rather than silently evaluating a different
configuration than the caller asked for. In this single-model,
single-prompt-version first activation that means `run_evaluation`
establishes the evaluation baseline for whatever is already active --
evaluating a genuinely new candidate version ahead of activating it is a
later slice's problem (this activation has no draft-prompt execution path
at all, a real and openly documented limitation, not an oversight).

**Ephemeral, workspace-scoped synthetic data, cleaned up after the run.**
Each example needs a real domain row for `execute_run`'s Step 1 tool
dispatch to read -- one `attention_items` row for `attention.get_item`
(and therefore `attention.explain_item`), one full synthetic meeting
evidence bundle for `meeting.get_prep_pack` (and therefore `meeting.prep_
summary`), or a set of synthetic `personal_domains`/`cross_domain_grants`/
`domain_records` rows for `personal.get_insight_sources` (and therefore
`personal.generate_insight`). `run_evaluation` inserts that source into
the *caller's own* workspace, runs it through `execute_run`, and deletes
it again, so an evaluation run never leaves fabricated rows behind that
could show up in that workspace's real Attention Queue, meeting list, or
personal domain vault. These task types' synthetic sources are cleaned up
on different schedules for real reasons, not stylistic inconsistency --
see `_insert_synthetic_meeting`'s docstring for why `meeting.prep_
summary`'s cannot be batched like `attention.explain_item`'s, and
`_insert_synthetic_personal_insight_sources`'s docstring for the distinct,
`UNIQUE` constraint-driven reason `personal.generate_insight`'s follows
the same per-example schedule. The `ai_runs`/`ai_run_steps` rows `execute_
run` itself persists are
*not* deleted -- they are genuine historical run records, retained for
reproducibility exactly like any other run (`EVALUATION-CONTRACT.md`:
"Evaluation results, environment and artifact hashes ... are retained for
reproducibility").

**`generated_artifacts`.** For every example that reaches `completed`,
`run_evaluation` writes one `generated_artifacts` row deriving from that
example's `ai_runs` row -- `source_versions` identifies the synthetic
source that produced it (the item id + `source_entity_version` for
`attention.explain_item`, the meeting id for `meeting.prep_summary`),
`evidence` is the run's cited ids, `output` is the validated response
payload (`{explanation_text, cited_factor_codes}` or `{summary_text,
cited_evidence_ids}`, task-type dependent). This is the first concrete
producer of `generated_artifacts` rows in this activation (Task 4's `POST
/ai/runs` does not write one); wiring the production run path to do the
same is a later task's decision, not attempted here.
"""

import time
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from json import dumps
from math import ceil
from typing import Annotated, Any, Literal, TypedDict
from uuid import NAMESPACE_OID, UUID, uuid4, uuid5

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session, lock_engine
from ecc.domains.personal.crypto import encrypt_field
from ecc.domains.personal.domains import classification_for, encrypt_record_payload
from ecc.observability import record_database_failure, record_idempotency_conflict
from ecc.platform import authz

from .ollama_client import OllamaAdapter
from .prompts import get_active_prompt
from .registry import get_model
from .runtime import TASK_PORTS, AiRun, OllamaAdapterDep, execute_run

__all__ = [
    "EvaluationConfigError",
    "EvaluationMetrics",
    "EvaluationRun",
    "EvaluationSet",
    "check_promotion_floors",
    "get_active_evaluation_set",
    "get_evaluation_run",
    "get_latest_evaluation_run",
    "list_evaluation_sets",
    "router",
    "run_evaluation",
]

_EVALUATION_DATA_CLASS = "sensitive"

# EVALUATION-CONTRACT.md's four promotion floors (design doc Decision 9).
# Three are shared across every task type; the fourth (latency) is not --
# `EVALUATION-CONTRACT.md`'s own floor table has always listed a distinct
# ceiling per task type ("<20s (attention.explain_item), <25s (meeting.
# prep_summary)"), matching each task's own declared per-model-call timeout
# minus real margin (Decision 5's per-task budgets, `router.py:TASK_
# REQUIREMENTS`) -- but `check_promotion_floors` below held a single flat
# 20.0s ceiling for every task type until this fix (Phase 4 post-launch
# audit, phase G), silently applying `attention.explain_item`'s own tighter
# number to `meeting.prep_summary` too and making its documented 25s floor
# entirely unreachable regardless of how the per-model-call timeout was
# tuned. Deliberately kept as its own dict here, not derived from
# `TASK_REQUIREMENTS[task_type].timeout_seconds` directly: the promotion
# floor is a production-quality bar (typical latency must stay comfortably
# fast), the per-model-call timeout is a reliability backstop (don't hang
# forever) -- raising the timeout for safety margin should never silently
# widen what counts as promotable-quality latency. An unregistered task
# type falls back to the strictest defined ceiling (fail-safe, matching
# this module's "assertions, not overrides" contract) rather than silently
# admitting an unbounded latency.
_SCHEMA_VALIDITY_FLOOR = 1.0
_GROUNDING_FLOOR = 1.0
_PROHIBITED_FACT_FLOOR = 0
# `meeting.prep_summary`'s own ceiling raised 25.0 -> 35.0 (phase H,
# migration `0052_phase4_meeting_timeout3.py`, `router.py`'s own updated
# comment on that task's `timeout_seconds` has the full real-CI-measurement
# history): four consecutive live-model runs against this task type's
# heaviest examples all clustered at p95 30.9-31.4s with every other floor
# already clearing cleanly (0 prohibited facts, 100% schema-validity and
# grounding each time) -- a genuine, repeatable decode-time characteristic
# of this small model on this task's largest prompts, not a defect this
# floor should keep failing on indefinitely. 35.0 is a real ~3.6s margin
# over the worst of the four observations (31.43s), still a tighter,
# "typical" bar than the reliability timeout's own 40s backstop (raised in
# lockstep, same migration) -- raising the timeout never widens this
# ceiling on its own; this value is sized against the actual measured
# generation time, independently.
_LATENCY_P95_CEILING_SECONDS_BY_TASK_TYPE: dict[str, float] = {
    "attention.explain_item": 20.0,
    "meeting.prep_summary": 35.0,
    # Phase 7 Task 5 part 2's `personal.generate_insight` -- an initial
    # value, not yet tuned against a real live-model measurement history
    # (this task type has none yet, unlike the other two -- see `router.py:
    # TASK_REQUIREMENTS["personal.generate_insight"]`'s own identical
    # disclosure). Set below `router.py`'s own `timeout_seconds=40.0` for
    # this task type, matching every other entry's own "promotion floor is
    # a production-quality bar, tighter than the reliability backstop"
    # relationship (this dict's own module-docstring-adjacent comment
    # above) -- expected to move once a real `ollama-evaluation` run
    # against this task type's own dataset exists to size it against.
    "personal.generate_insight": 35.0,
    # Phase 10 Task 5's `email.detect_action` -- same "initial value, no
    # live-model history yet" position as `personal.generate_insight`'s
    # own entry above, sized by the identical analogy (`router.py:
    # TASK_REQUIREMENTS["email.detect_action"]`'s own comment): a single
    # email's full body is not obviously larger than that task's
    # cross-domain evidence bundle, so this starts at the same 35.0.
    "email.detect_action": 35.0,
}


class EvaluationExample(TypedDict):
    """One row of `evaluation_sets.examples` for `task_type='attention.
    explain_item'` (design doc Decision 9) -- matches `tests/fixtures/
    phase4_evaluation_attention_explain.py`'s `EXAMPLES` shape and migration
    `0031_phase4_evaluation.py`'s seeded JSONB content exactly.
    """

    key: str
    entity_type: str
    score: int
    confidence: float
    factors: list[dict[str, Any]]
    must_cite: list[str]
    must_not_state: list[str]
    reference_explanation: str


class MeetingPrepEvaluationExample(TypedDict):
    """One row of `evaluation_sets.examples` for `task_type='meeting.prep_
    summary'` -- matches `tests/fixtures/phase4_evaluation_meeting_prep.py`'s
    `EXAMPLES` shape and migration `0035_phase4_meeting_prep_evaluation.py`'s
    seeded JSONB content exactly. Structurally different from
    `EvaluationExample` above (no single scalar item/factor list -- a
    multi-section evidence bundle), the same reason `runtime.py`'s
    `_PreparedRequest`/`_prepare_meeting_prep_request` are separate from
    `attention.explain_item`'s equivalents rather than a shared shape forced
    to fit both.
    """

    key: str
    objective: str
    participants: list[dict[str, Any]]
    timeline: list[dict[str, Any]]
    commitments: list[dict[str, Any]]
    decisions: list[dict[str, Any]]
    notes: list[dict[str, Any]]
    risks: list[dict[str, Any]]
    dependencies: list[dict[str, Any]]
    must_cite: list[str]
    must_not_state: list[str]
    reference_summary: str


class PersonalInsightSourceExample(TypedDict):
    """One synthetic cross-domain source for a `PersonalInsightEvaluation
    Example` -- one `personal_domains`/`cross_domain_grants` pair plus the
    `domain_records` rows granted under it. `records[*]["record_type"]`
    values become the synthetic grant's own `granted_categories` (the
    union of every `record_type` used across this source's records, see
    `_insert_synthetic_personal_insight_sources`), matching how a real
    grant's categories are chosen by the user, not fixed per domain.
    """

    domain_key: str
    records: list[dict[str, Any]]


class PersonalInsightEvaluationExample(TypedDict):
    """One row of `evaluation_sets.examples` for `task_type='personal.
    generate_insight'` -- matches `tests/fixtures/phase7_evaluation_
    personal_insight.py`'s `EXAMPLES` shape and migration `0058_phase7_
    personal_insight_eval.py`'s seeded JSONB content exactly. Structurally
    closer to `MeetingPrepEvaluationExample` than to `EvaluationExample`
    (a variable-shaped evidence bundle rather than one scalar item), but
    the bundle here is *cross-domain* (`sources`, a list of independent
    domains each with their own records) rather than one meeting's fixed
    seven sections.
    """

    key: str
    sources: list[PersonalInsightSourceExample]
    must_cite: list[str]
    must_not_state: list[str]
    reference_explanation: str


class EmailDetectActionMessageExample(TypedDict):
    """One synthetic `email_messages` row for an `EmailDetectAction
    EvaluationExample`'s thread -- `sent_at_days_ago` mirrors `Personal
    InsightSourceExample`'s own `effective_at_days_ago` convention for a
    reproducible relative timestamp (`_insert_synthetic_email_thread`
    below converts it the identical way).
    """

    sender: str
    direction: Literal["inbound", "outbound"]
    sent_at_days_ago: int
    body: str


class EmailDetectActionEvaluationExample(TypedDict):
    """One row of `evaluation_sets.examples` for `task_type='email.
    detect_action'` -- matches `tests/fixtures/phase10_evaluation_email_
    detect_action.py`'s `EXAMPLES` shape and migration `0073_phase10_
    email_detect_action_eval.py`'s seeded JSONB content exactly. A
    negative example (a newsletter/FYI-only/already-resolved thread) sets
    `reference_has_action=False`; documentation only, like every other
    `reference_*` field here -- `_score_example` never reads it, since a
    live-model run's real output is scored structurally (schema validity,
    grounding, prohibited facts, latency), not compared against a fixed
    reference answer.
    """

    key: str
    subject: str | None
    messages: list[EmailDetectActionMessageExample]
    must_cite: list[str]
    must_not_state: list[str]
    reference_has_action: bool


# `evaluation_sets.examples` is untyped JSONB and this activation now stores
# three structurally different example shapes in it (one per registered
# task type) -- every function below that is genuinely task-type-agnostic
# (`_score_example`/`_prohibited_matches`/`_aggregate`/`EvaluationSet.
# examples` itself) accordingly types its `example` parameter as
# `dict[str, Any]` rather than any specific TypedDict, and only ever
# touches the two fields every shape shares (`key`, `must_not_state`). The
# three TypedDicts above exist purely as construction/documentation aids for
# each dataset's own fixture module and insertion helper.


class EvaluationConfigError(Exception):
    """`run_evaluation` refuses to run against a configuration it cannot
    verify (design doc module docstring above: "assertions, not
    overrides"). `code` is a short machine-readable reason the HTTP layer
    (`create_evaluation_run` below) maps to a 404/422 response; never
    exposes anything beyond that short code and a redacted message.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# evaluation_sets reads (global platform catalog -- see migration
# 0031_phase4_evaluation.py's module docstring for why this table, unlike
# evaluation_runs/generated_artifacts, is not workspace-scoped).
# ---------------------------------------------------------------------------

_EVALUATION_SET_FIELDS = """
    id, task_type, version, classification, example_count, examples, status
"""


@dataclass(frozen=True, slots=True)
class EvaluationSet:
    id: UUID
    task_type: str
    version: int
    classification: Literal["labelled", "development"]
    example_count: int
    examples: list[dict[str, Any]]
    status: Literal["active", "retired"]


def _row_to_evaluation_set(row: dict[str, Any]) -> EvaluationSet:
    return EvaluationSet(
        id=row["id"],
        task_type=row["task_type"],
        version=row["version"],
        classification=row["classification"],
        example_count=row["example_count"],
        examples=list(row["examples"]),
        status=row["status"],
    )


def get_active_evaluation_set(session: Session, task_type: str) -> EvaluationSet | None:
    """The currently active labelled dataset for `task_type`, or `None` if
    none is registered. Never raises on a missing row, matching every
    other `get_active_*` read in this package (`prompts.get_active_prompt`,
    `tools.get_active_tool`).
    """
    row = (
        session.execute(
            text(
                f"SELECT {_EVALUATION_SET_FIELDS} FROM evaluation_sets "
                "WHERE task_type = :task_type AND status = 'active'"
            ),
            {"task_type": task_type},
        )
        .mappings()
        .one_or_none()
    )
    return _row_to_evaluation_set(dict(row)) if row is not None else None


def list_evaluation_sets(session: Session, *, include_retired: bool = True) -> list[EvaluationSet]:
    clause = "" if include_retired else "WHERE status = 'active'"
    rows = (
        session.execute(
            text(
                f"SELECT {_EVALUATION_SET_FIELDS} FROM evaluation_sets {clause} "
                "ORDER BY task_type ASC, version ASC"
            )
        )
        .mappings()
        .all()
    )
    return [_row_to_evaluation_set(dict(row)) for row in rows]


# ---------------------------------------------------------------------------
# Scoring -- EVALUATION-CONTRACT.md's four metrics, computed from the real
# execute_run outcome each example produced.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EvaluationMetrics:
    schema_validity_rate: float
    grounding_rate: float
    prohibited_fact_count: int
    latency_p95_seconds: float
    total_examples: int


@dataclass(frozen=True, slots=True)
class EvaluationRun:
    id: UUID
    task_type: str
    prompt_id: str
    prompt_version: int
    model_id: str
    provider: str
    dataset_version: int
    metrics: EvaluationMetrics
    failures: list[dict[str, Any]]
    status: Literal["completed"]
    started_at: datetime
    completed_at: datetime


def check_promotion_floors(evaluation_run: EvaluationRun) -> bool:
    """`EVALUATION-CONTRACT.md`'s four floors, all required simultaneously
    (design doc Decision 9's table): 100% schema validity, 100% grounding,
    zero prohibited-fact occurrences, p95 latency strictly under the
    evaluated task type's own declared ceiling (20s for `attention.
    explain_item`, 35s for `meeting.prep_summary` -- distinct numbers, not
    one shared value; see `_LATENCY_P95_CEILING_SECONDS_BY_TASK_TYPE`'s own
    comment). A pure function over already-computed metrics -- no database
    access, no knowledge of *how* `evaluation_run` was produced, so
    `prompts.py`'s promotion gate and this module's own persistence path
    both call the exact same logic rather than two independently-maintained
    copies of "did it pass".
    """
    metrics = evaluation_run.metrics
    latency_ceiling = _LATENCY_P95_CEILING_SECONDS_BY_TASK_TYPE.get(
        evaluation_run.task_type, min(_LATENCY_P95_CEILING_SECONDS_BY_TASK_TYPE.values())
    )
    return (
        metrics.schema_validity_rate >= _SCHEMA_VALIDITY_FLOOR
        and metrics.grounding_rate >= _GROUNDING_FLOOR
        and metrics.prohibited_fact_count <= _PROHIBITED_FACT_FLOOR
        and metrics.latency_p95_seconds < latency_ceiling
    )


def _percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile over `values` (no numpy dependency, matching
    `RFC-005`'s existing dependency baseline). Returns `0.0` for an empty
    series -- `run_evaluation` never calls this with zero examples (a
    dataset with `example_count == 0` is not a state this activation's
    seeded dataset can reach), but a total-avoidance-of-`IndexError` return
    is cheap insurance for any future caller.
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, ceil(fraction * len(ordered)) - 1))
    return ordered[index]


@dataclass(frozen=True, slots=True)
class _ExampleScore:
    key: str
    outcome: Literal["completed", "schema_invalid", "grounding_failed", "other_failure"]
    latency_seconds: float
    prohibited_matches: tuple[str, ...]
    error_code: str | None
    ai_run: AiRun
    schema_invalid_detail: str | None = None


def _classify_outcome(
    run: AiRun,
) -> Literal["completed", "schema_invalid", "grounding_failed", "other_failure"]:
    if run.status == "completed":
        return "completed"
    if run.error_code == "schema_invalid":
        return "schema_invalid"
    if run.error_code == "grounding_failed":
        return "grounding_failed"
    return "other_failure"


def _fetch_schema_invalid_detail(session: Session, run: AiRun) -> str | None:
    """The redacted validation-error summary (`validator.py`'s
    `SchemaInvalid.detail` -- field path + Pydantic error type only, never
    raw response text or a validated/rejected field value) lives on the
    final `model_call` step's trace (`runtime.py`'s `_model_step`), not on
    `AiRun` itself. Fetched only for `schema_invalid` outcomes, where it is
    the only way to tell *why* an example failed without the raw response
    text this codebase never logs.
    """
    row = session.execute(
        text(
            "SELECT trace FROM ai_run_steps WHERE run_id = :run_id AND kind = 'model_call' "
            "ORDER BY sequence DESC LIMIT 1"
        ),
        {"run_id": run.id},
    ).first()
    if row is None:
        return None
    trace = row[0]
    detail = trace.get("detail") if isinstance(trace, dict) else None
    return detail if isinstance(detail, str) else None


# Which field of a completed run's validated `output` holds the free-text
# summary/explanation to scan for `must_not_state` phrases -- the one place
# the two task types' output schemas genuinely differ in shape
# (`ExplainItemOutput.explanation_text` vs `MeetingPrepSummary.summary_text`,
# `validator.py`).
_OUTPUT_TEXT_FIELD: dict[str, str] = {
    "attention.explain_item": "explanation_text",
    "meeting.prep_summary": "summary_text",
    "personal.generate_insight": "explanation_text",
    "email.detect_action": "rationale",
}


def _prohibited_matches(task_type: str, example: dict[str, Any], run: AiRun) -> tuple[str, ...]:
    """`must_not_state`'s hallucination probe (design doc Decision 9): a
    completed run's free-text output containing any of the example's
    forbidden phrases (case-insensitive substring match) is a prohibited-
    fact occurrence. Only meaningful for a `completed` run -- a run that
    never produced a validated `output` has nothing to check.
    """
    if run.output is None:
        return ()
    field = _OUTPUT_TEXT_FIELD[task_type]
    text_value = str(run.output.get(field, "")).casefold()
    return tuple(phrase for phrase in example["must_not_state"] if phrase.casefold() in text_value)


def _score_example(
    session: Session,
    task_type: str,
    example: dict[str, Any],
    run: AiRun,
    *,
    latency_seconds: float,
) -> _ExampleScore:
    outcome = _classify_outcome(run)
    matches = _prohibited_matches(task_type, example, run) if outcome == "completed" else ()
    detail = _fetch_schema_invalid_detail(session, run) if outcome == "schema_invalid" else None
    return _ExampleScore(
        key=example["key"],
        outcome=outcome,
        latency_seconds=latency_seconds,
        prohibited_matches=matches,
        error_code=run.error_code,
        ai_run=run,
        schema_invalid_detail=detail,
    )


def _aggregate(scores: list[_ExampleScore]) -> tuple[EvaluationMetrics, list[dict[str, Any]]]:
    total = len(scores)
    schema_valid = sum(1 for s in scores if s.outcome in ("completed", "grounding_failed"))
    grounded = sum(1 for s in scores if s.outcome == "completed")
    prohibited_count = sum(len(s.prohibited_matches) for s in scores)
    latencies = [s.latency_seconds for s in scores]

    failures: list[dict[str, Any]] = []
    for score in scores:
        if score.outcome != "completed":
            failure: dict[str, Any] = {
                "key": score.key,
                "reason": score.outcome,
                "error_code": score.error_code,
            }
            if score.outcome == "grounding_failed" and score.ai_run.evidence:
                # `evidence` holds the specific cited-but-ungrounded factor
                # codes on this outcome (runtime.py's grounding-failed
                # path) -- redacted by construction (factor codes only,
                # never raw response text), safe to surface here.
                failure["ungrounded_codes"] = score.ai_run.evidence
            if score.outcome == "schema_invalid" and score.schema_invalid_detail:
                failure["detail"] = score.schema_invalid_detail
            failures.append(failure)
        for phrase in score.prohibited_matches:
            failures.append({"key": score.key, "reason": "prohibited_fact", "phrase": phrase})

    metrics = EvaluationMetrics(
        schema_validity_rate=schema_valid / total if total else 0.0,
        grounding_rate=grounded / total if total else 0.0,
        prohibited_fact_count=prohibited_count,
        latency_p95_seconds=_percentile(latencies, 0.95),
        total_examples=total,
    )
    return metrics, failures


# ---------------------------------------------------------------------------
# Synthetic attention_items -- ephemeral, workspace-scoped, cleaned up
# after the run (module docstring).
# ---------------------------------------------------------------------------

_SYNTHETIC_SOURCE_ENTITY_VERSION = 1


def _insert_synthetic_item(
    session: Session, auth: AuthContext, example: dict[str, Any], *, now: datetime
) -> UUID:
    """Deliberately *not* wrapped in `with session.begin():` -- like
    `runtime.py:_persist_terminal`'s identical choice (see that function's
    docstring), this is called after `run_evaluation`'s own preceding
    read-only lookups (`get_active_prompt`/`get_model`/`get_active_
    evaluation_set`) have already autobegun an implicit transaction on this
    session; a context-managed `session.begin()` here would raise
    `InvalidRequestError: A transaction is already begun`. `session.
    commit()` below commits whichever transaction is actually active,
    matching `_persist_terminal`'s exact reasoning.
    """
    item_id = uuid4()
    session.execute(
        text(
            """
            INSERT INTO attention_items (
                id, workspace_id, entity_type, entity_id, source_entity_version,
                score, confidence, factors, explanation, generated_at, expires_at,
                pinned, policy_version
            ) VALUES (
                :id, :workspace_id, :entity_type, :entity_id, :source_entity_version,
                :score, :confidence, CAST(:factors AS jsonb), :explanation, :now, :expires_at,
                false, 1
            )
            """
        ),
        {
            "id": item_id,
            "workspace_id": auth.workspace_id,
            "entity_type": example["entity_type"],
            "entity_id": uuid4(),
            "source_entity_version": _SYNTHETIC_SOURCE_ENTITY_VERSION,
            "score": example["score"],
            "confidence": example["confidence"],
            "factors": dumps(example["factors"]),
            "explanation": f"evaluation fixture: {example['key']}",
            "now": now,
            "expires_at": now,
        },
    )
    session.commit()
    return item_id


def _delete_synthetic_items(session: Session, auth: AuthContext, item_ids: list[UUID]) -> None:
    """Not context-managed -- see `_insert_synthetic_item`'s docstring;
    called from `run_evaluation`'s `finally` block, where the session's
    transaction state depends on exactly where the loop stopped.
    """
    if not item_ids:
        return
    session.execute(
        text(
            "DELETE FROM attention_items WHERE workspace_id = :workspace_id AND id = ANY(:item_ids)"
        ),
        {"workspace_id": auth.workspace_id, "item_ids": item_ids},
    )
    session.commit()


# ---------------------------------------------------------------------------
# Synthetic meetings (`task_type='meeting.prep_summary'`) -- ephemeral,
# workspace-scoped, but cleaned up per-example rather than batched at the
# end of the run (see `_insert_synthetic_meeting`'s docstring for why).
# ---------------------------------------------------------------------------

_SYNTHETIC_MEETING_STARTS_IN = timedelta(days=1)
_SYNTHETIC_MEETING_DURATION = timedelta(hours=1)

_MeetingPrepIds = tuple[UUID, list[UUID], list[UUID]]


def _salted_synthetic_id(workspace_id: UUID, base_id: str) -> UUID:
    """Deterministic per-(workspace, base_id) transform of a dataset-
    declared row id.

    The fixture/migration's own ids are a pure function of `(example key,
    section, index)` with no workspace component -- necessarily, since the
    migration seeds one global `evaluation_sets` row shared by every
    workspace. Used bare as a real primary key (`meeting_participants.id`,
    `timeline_entries.id`, `commitments.id`, `notes.id`, `risks.id`,
    `waiting_links.id` -- all bare `id UUID PRIMARY KEY`, not composite
    with `workspace_id`), two workspaces evaluating `meeting.prep_summary`
    concurrently would collide on the same example's rows. Salting with
    `workspace_id` keeps the id fully deterministic and reproducible for
    a given `(workspace_id, base_id)` pair -- callers that need to know a
    row's real id in advance (tests building mocked "fully grounded"
    citations) compute the same salt -- while making cross-workspace
    collisions as unlikely as any other independent UUID5 draw.
    """
    return uuid5(NAMESPACE_OID, f"{workspace_id}:{base_id}")


def _insert_synthetic_meeting(
    session: Session, auth: AuthContext, example: dict[str, Any], *, now: datetime
) -> _MeetingPrepIds:
    """Inserts one full synthetic meeting-prep evidence bundle -- a
    `meetings` row plus every populated section (`pkos_nodes`/`meeting_
    participants` for `participants`, `timeline_entries`, `commitments`,
    `notes` for both `decisions` and `notes`, `risks`, `waiting_links` for
    `dependencies`) -- for one `MeetingPrepEvaluationExample`, using exactly
    the ids the fixture already assigned each row, so `must_cite`'s
    symbolic references and `meeting.get_prep_pack`'s real output agree.

    Returns `(meeting_id, participant_node_ids, risk_ids)`. Not context-
    managed -- see `_insert_synthetic_item`'s identical rationale (this
    session's transaction may already be autobegun by preceding reads).
    """
    meeting_id = uuid4()
    session.execute(
        text(
            """
            INSERT INTO meetings (
                id, workspace_id, title, standalone_starts_at, standalone_ends_at,
                standalone_timezone, status, agenda, created_by, updated_by,
                created_at, updated_at, version
            ) VALUES (
                :id, :workspace_id, :title, :starts_at, :ends_at, 'UTC',
                'planned', :agenda, :actor_id, :actor_id, :now, :now, 1
            )
            """
        ),
        {
            "id": meeting_id,
            "workspace_id": auth.workspace_id,
            "title": f"evaluation fixture: {example['key']}",
            "agenda": example["objective"],
            "starts_at": now + _SYNTHETIC_MEETING_STARTS_IN,
            "ends_at": now + _SYNTHETIC_MEETING_STARTS_IN + _SYNTHETIC_MEETING_DURATION,
            "actor_id": auth.user_id,
            "now": now,
        },
    )

    node_ids: list[UUID] = []
    for participant in example["participants"]:
        # `meeting_prep.py:_fetch_participants` surfaces `mp.id` (the
        # `meeting_participants` junction row's own id) as `ParticipantRow.
        # id` -- the id `meeting.get_prep_pack`'s participants section
        # actually returns, not `entity_id` (the `pkos_nodes` row this
        # junction row links to, which is never surfaced directly). The
        # fixture's `participant["id"]` is the citable id, so it must
        # become `meeting_participants.id` here, not `pkos_nodes.id` -- a
        # real bug this exact distinction already caused once before, in
        # `tests/test_ai_runtime_runtime_postgres.py`'s own synthetic-
        # participant helper. `node_id` (the `pkos_nodes` row, and the
        # `entity_id` foreign key `commitments`/`timeline_entries`/`waiting_
        # links` below join through) is a fresh, uncited id -- nothing in
        # `meeting.get_prep_pack`'s output surfaces it. Salted with
        # `workspace_id` (see `_salted_synthetic_id`) since this becomes a
        # real, bare, non-workspace-composite primary key.
        participant_id = _salted_synthetic_id(auth.workspace_id, participant["id"])
        node_id = uuid4()
        node_ids.append(node_id)
        session.execute(
            text(
                """
                INSERT INTO pkos_nodes (
                    id, workspace_id, node_type, canonical_name, attributes,
                    status, confidence, version, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, 'person', :name, '{}'::jsonb,
                    'active', 1.00, 1, :now, :now
                )
                """
            ),
            {
                "id": node_id,
                "workspace_id": auth.workspace_id,
                "name": participant["entity_name"],
                "now": now,
            },
        )
        session.execute(
            text(
                """
                INSERT INTO meeting_participants (
                    id, workspace_id, meeting_id, entity_id, role,
                    created_by, updated_by, created_at, updated_at, version
                ) VALUES (
                    :id, :workspace_id, :meeting_id, :entity_id, :role,
                    :actor_id, :actor_id, :now, :now, 1
                )
                """
            ),
            {
                "id": participant_id,
                "workspace_id": auth.workspace_id,
                "meeting_id": meeting_id,
                "entity_id": node_id,
                "role": participant["role"],
                "actor_id": auth.user_id,
                "now": now,
            },
        )

    for entry in example["timeline"]:
        session.execute(
            text(
                """
                INSERT INTO timeline_entries (
                    id, workspace_id, entity_id, effective_at, recorded_at, event_type, summary
                ) VALUES (
                    :id, :workspace_id, :entity_id, :effective_at, :now, :event_type, :summary
                )
                """
            ),
            {
                "id": _salted_synthetic_id(auth.workspace_id, entry["id"]),
                "workspace_id": auth.workspace_id,
                "entity_id": node_ids[0],
                "effective_at": now - timedelta(hours=entry["effective_at_hours_ago"]),
                "event_type": entry["event_type"],
                "summary": entry["summary"],
                "now": now,
            },
        )

    for commitment in example["commitments"]:
        due_at = (
            now + timedelta(days=commitment["due_at_days"])
            if commitment["due_at_days"] is not None
            else None
        )
        session.execute(
            text(
                """
                INSERT INTO commitments (
                    id, workspace_id, owner_id, summary, direction, status,
                    counterparty_person_id, due_at, importance, pinned,
                    created_by, updated_by, created_at, updated_at, version
                ) VALUES (
                    :id, :workspace_id, :owner_id, :summary, :direction, :status,
                    :counterparty_id, :due_at, 'medium', false,
                    :owner_id, :owner_id, :now, :now, 1
                )
                """
            ),
            {
                "id": _salted_synthetic_id(auth.workspace_id, commitment["id"]),
                "workspace_id": auth.workspace_id,
                "owner_id": auth.user_id,
                "summary": commitment["summary"],
                "direction": commitment["direction"],
                "status": commitment["status"],
                "counterparty_id": node_ids[commitment["counterparty_index"]],
                "due_at": due_at,
                "now": now,
            },
        )

    for note_type, section in (("decision", "decisions"), ("general", "notes")):
        for note in example[section]:
            session.execute(
                text(
                    """
                    INSERT INTO notes (
                        id, workspace_id, owner_id, title, body, note_type, meeting_id,
                        source_type, restricted, created_by, updated_by, created_at,
                        updated_at, version
                    ) VALUES (
                        :id, :workspace_id, :owner_id, :title, :body, :note_type, :meeting_id,
                        'local', false, :owner_id, :owner_id, :now, :now, 1
                    )
                    """
                ),
                {
                    "id": _salted_synthetic_id(auth.workspace_id, note["id"]),
                    "workspace_id": auth.workspace_id,
                    "owner_id": auth.user_id,
                    "title": note["title"],
                    "body": note["body"],
                    "note_type": note_type,
                    "meeting_id": meeting_id,
                    "now": now,
                },
            )

    risk_ids: list[UUID] = []
    for risk in example["risks"]:
        risk_id = _salted_synthetic_id(auth.workspace_id, risk["id"])
        risk_ids.append(risk_id)
        review_at = (
            now + timedelta(days=risk["review_at_days"])
            if risk["review_at_days"] is not None
            else None
        )
        session.execute(
            text(
                """
                INSERT INTO risks (
                    id, workspace_id, description, probability, impact, status, review_at,
                    owner_id, created_by, updated_by, created_at, updated_at, version
                ) VALUES (
                    :id, :workspace_id, :description, :probability, :impact, :status, :review_at,
                    :owner_id, :owner_id, :owner_id, :now, :now, 1
                )
                """
            ),
            {
                "id": risk_id,
                "workspace_id": auth.workspace_id,
                "description": risk["description"],
                "probability": risk["probability"],
                "impact": risk["impact"],
                "status": risk["status"],
                "review_at": review_at,
                "owner_id": auth.user_id,
                "now": now,
            },
        )

    for dependency in example["dependencies"]:
        counterparty_id = node_ids[dependency["counterparty_index"]]
        expected_at = (
            now + timedelta(days=dependency["expected_at_days"])
            if dependency["expected_at_days"] is not None
            else None
        )
        session.execute(
            text(
                """
                INSERT INTO waiting_links (
                    id, workspace_id, subject_type, subject_id, counterparty_entity_id,
                    direction, status, since_at, note, expected_at, created_by, updated_by,
                    created_at, updated_at, version
                ) VALUES (
                    :id, :workspace_id, 'knowledge_entity', :counterparty_id, :counterparty_id,
                    :direction, 'open', :now, :note, :expected_at,
                    :owner_id, :owner_id, :now, :now, 1
                )
                """
            ),
            {
                "id": _salted_synthetic_id(auth.workspace_id, dependency["id"]),
                "workspace_id": auth.workspace_id,
                "counterparty_id": counterparty_id,
                "direction": dependency["direction"],
                "note": dependency["note"],
                "expected_at": expected_at,
                "owner_id": auth.user_id,
                "now": now,
            },
        )

    session.commit()
    return meeting_id, node_ids, risk_ids


def _delete_synthetic_meeting(
    session: Session,
    auth: AuthContext,
    meeting_id: UUID,
    node_ids: list[UUID],
    risk_ids: list[UUID],
) -> None:
    """Deletes everything `_insert_synthetic_meeting` created for one
    example, immediately after that example is scored -- see that
    function's docstring for why meeting.prep_summary's cleanup cannot
    wait until the whole dataset has run like `_delete_synthetic_items`
    does. Deletes every table that references `node_ids`/`meeting_id`
    before the rows those ids identify, regardless of what FK `ondelete`
    behavior may or may not already handle -- explicit, not relying on
    cascade. Not context-managed -- see `_insert_synthetic_item`'s
    identical rationale.
    """
    params = {"workspace_id": auth.workspace_id, "meeting_id": meeting_id, "node_ids": node_ids}
    session.execute(
        text(
            "DELETE FROM meeting_participants "
            "WHERE workspace_id = :workspace_id AND meeting_id = :meeting_id"
        ),
        params,
    )
    session.execute(
        text("DELETE FROM notes WHERE workspace_id = :workspace_id AND meeting_id = :meeting_id"),
        params,
    )
    if node_ids:
        session.execute(
            text(
                "DELETE FROM timeline_entries "
                "WHERE workspace_id = :workspace_id AND entity_id = ANY(:node_ids)"
            ),
            params,
        )
        session.execute(
            text(
                "DELETE FROM commitments "
                "WHERE workspace_id = :workspace_id AND counterparty_person_id = ANY(:node_ids)"
            ),
            params,
        )
        session.execute(
            text(
                "DELETE FROM waiting_links "
                "WHERE workspace_id = :workspace_id AND counterparty_entity_id = ANY(:node_ids)"
            ),
            params,
        )
    if risk_ids:
        session.execute(
            text("DELETE FROM risks WHERE workspace_id = :workspace_id AND id = ANY(:risk_ids)"),
            {"workspace_id": auth.workspace_id, "risk_ids": risk_ids},
        )
    if node_ids:
        session.execute(
            text(
                "DELETE FROM pkos_nodes WHERE workspace_id = :workspace_id AND id = ANY(:node_ids)"
            ),
            params,
        )
    session.execute(
        text("DELETE FROM meetings WHERE workspace_id = :workspace_id AND id = :meeting_id"),
        params,
    )
    session.commit()


# ---------------------------------------------------------------------------
# Synthetic personal-insight sources (`task_type='personal.generate_
# insight'`) -- ephemeral, workspace-scoped, cleaned up per-example rather
# than batched at the end of the run, matching `meeting.prep_summary`'s own
# per-example schedule (`_insert_synthetic_meeting`'s docstring) for a
# related but distinct reason: `personal_domains` has a real `UNIQUE
# (workspace_id, owner_id, domain_key)` constraint, and this dataset's
# examples deliberately reuse the same `domain_key` across more than one
# example -- a later example's own fresh `personal_domains` row for that
# same key would violate that constraint (and its `cross_domain_grants`
# row's `granted_categories` may legitimately differ between examples) if
# an earlier example's rows for it were still in place.
# ---------------------------------------------------------------------------

_PersonalInsightSourceIds = tuple[list[UUID], list[UUID], list[UUID]]


def _insert_synthetic_personal_insight_sources(
    session: Session, auth: AuthContext, example: dict[str, Any], *, now: datetime
) -> _PersonalInsightSourceIds:
    """Inserts, for each of this example's `sources`, one `personal_domains`
    row (enabled, so `personal.get_insight_sources`'s own enablement check
    passes), one `cross_domain_grants` row (active, `purpose=
    'insight_generation'`, `granted_categories` the union of every
    `record_type` this source's own records use -- a real grant's own
    categories are chosen the same way, by what the user actually wants
    read, not fixed per domain), and every one of that source's `domain_
    records` rows -- encrypted via `ecc.domains.personal.domains.encrypt_
    record_payload` exactly like a real write would be, so this harness
    genuinely exercises `personal.get_insight_sources`'s own decrypt-
    before-prompting path rather than handing the model already-plaintext
    synthetic data no real record would ever have.

    Returns `(domain_ids, grant_ids, record_ids)` for `_delete_synthetic_
    personal_insight_sources` below. Not context-managed -- see `_insert_
    synthetic_item`'s identical rationale (this session's transaction may
    already be autobegun by `run_evaluation`'s preceding reads).
    """
    domain_ids: list[UUID] = []
    grant_ids: list[UUID] = []
    record_ids: list[UUID] = []
    for source in example["sources"]:
        domain_key = source["domain_key"]
        domain_id = uuid4()
        domain_ids.append(domain_id)
        session.execute(
            text(
                """
                INSERT INTO personal_domains (
                    id, workspace_id, owner_id, domain_key, classification,
                    enabled, enabled_at, created_by, updated_by, created_at, updated_at, version
                ) VALUES (
                    :id, :workspace_id, :owner_id, :domain_key, :classification,
                    true, :now, :actor_id, :actor_id, :now, :now, 1
                )
                """
            ),
            {
                "id": domain_id,
                "workspace_id": auth.workspace_id,
                "owner_id": auth.user_id,
                "domain_key": domain_key,
                "classification": classification_for(domain_key),
                "actor_id": auth.user_id,
                "now": now,
            },
        )

        record_types = sorted({record["record_type"] for record in source["records"]})
        grant_id = uuid4()
        grant_ids.append(grant_id)
        session.execute(
            text(
                """
                INSERT INTO cross_domain_grants (
                    id, workspace_id, owner_id, source_domain_key, purpose,
                    granted_categories, granted_at, expires_at, created_at
                ) VALUES (
                    :id, :workspace_id, :owner_id, :domain_key, 'insight_generation',
                    CAST(:granted_categories AS jsonb), :now, NULL, :now
                )
                """
            ),
            {
                "id": grant_id,
                "workspace_id": auth.workspace_id,
                "owner_id": auth.user_id,
                "domain_key": domain_key,
                "granted_categories": dumps(record_types),
                "now": now,
            },
        )

        for record in source["records"]:
            record_id = uuid4()
            record_ids.append(record_id)
            encrypted_payload = encrypt_record_payload(record["record_type"], record["payload"])
            session.execute(
                text(
                    """
                    INSERT INTO domain_records (
                        id, workspace_id, owner_id, domain_key, record_type, payload,
                        effective_at, created_by, updated_by, created_at, updated_at, version
                    ) VALUES (
                        :id, :workspace_id, :owner_id, :domain_key, :record_type,
                        CAST(:payload AS jsonb), :effective_at, :actor_id, :actor_id, :now, :now, 1
                    )
                    """
                ),
                {
                    "id": record_id,
                    "workspace_id": auth.workspace_id,
                    "owner_id": auth.user_id,
                    "domain_key": domain_key,
                    "record_type": record["record_type"],
                    "payload": dumps(encrypted_payload),
                    "effective_at": now - timedelta(days=record["effective_at_days_ago"]),
                    "actor_id": auth.user_id,
                    "now": now,
                },
            )
    session.commit()
    return domain_ids, grant_ids, record_ids


def _delete_synthetic_personal_insight_sources(
    session: Session,
    auth: AuthContext,
    domain_ids: list[UUID],
    grant_ids: list[UUID],
    record_ids: list[UUID],
) -> None:
    """Deletes everything `_insert_synthetic_personal_insight_sources`
    created for one example, immediately after that example is scored --
    see that function's docstring for why. Deletes child rows (`domain_
    records`/`cross_domain_grants`) before the `personal_domains` row they
    FK-reference, regardless of what `ondelete=CASCADE` may or may not
    already handle -- explicit, not relying on cascade, matching `_delete_
    synthetic_meeting`'s identical discipline. Not context-managed -- see
    `_insert_synthetic_item`'s identical rationale.
    """
    if record_ids:
        session.execute(
            text(
                "DELETE FROM domain_records WHERE workspace_id = :workspace_id AND id = ANY(:ids)"
            ),
            {"workspace_id": auth.workspace_id, "ids": record_ids},
        )
    if grant_ids:
        session.execute(
            text(
                "DELETE FROM cross_domain_grants WHERE workspace_id = :workspace_id "
                "AND id = ANY(:ids)"
            ),
            {"workspace_id": auth.workspace_id, "ids": grant_ids},
        )
    if domain_ids:
        session.execute(
            text(
                "DELETE FROM personal_domains WHERE workspace_id = :workspace_id AND id = ANY(:ids)"
            ),
            {"workspace_id": auth.workspace_id, "ids": domain_ids},
        )
    session.commit()


def _insert_synthetic_email_thread(
    session: Session, auth: AuthContext, example: dict[str, Any], *, now: datetime
) -> tuple[UUID, UUID, list[UUID], UUID | None]:
    """Inserts one synthetic `email_threads` row and its `email_messages`
    -- bodies encrypted via `ecc.domains.personal.crypto.encrypt_field`
    exactly like Task 5's own `gmail_adapter.py:detect_actions_since`
    would store a real fetched body, so this harness genuinely exercises
    `email_action_tools.get_thread_content_tool`'s own decrypt-before-
    prompting path rather than handing the model already-plaintext
    synthetic data no real message would ever have. A synthetic
    `connector_accounts` row is also created -- `email_threads.connector_
    account_id` is `NOT NULL` (migration `0069`) and this evaluation
    harness has no real connected Gmail account to reference; its
    `encrypted_credentials` is never read by anything this harness
    exercises (`get_thread_content_tool` only ever reads `email_threads`/
    `email_messages`), so a placeholder value is sufficient.

    Returns `(connector_account_id, thread_id, message_ids, inserted_domain_
    id)` for `_delete_synthetic_email_thread` below -- `inserted_domain_id`
    is `None` when the `personal_domains` insert below hit its own `ON
    CONFLICT DO NOTHING` (a real, pre-existing `email` personal-domain row
    for this caller), never the id of that pre-existing row. Round 5
    review found this distinction was previously dropped entirely -- the
    original return signature never surfaced whether this call actually
    created the row it inserted, and `_delete_synthetic_email_thread`
    deleted `personal_domains` by `(workspace_id, owner_id, domain_key)`
    alone, which matches and deletes a real pre-existing row exactly as
    readily as a synthetic one this function created, cascading (`email_
    threads`' own FK, `ondelete=\"CASCADE\"`) to permanently delete that
    caller's entire real Gmail sync history plus their `domain_consents`
    row (also `CASCADE`-linked) the first time this evaluation harness runs
    for a caller who has already connected Gmail and enabled the `email`
    personal domain. Not context-managed -- see
    `_insert_synthetic_item`'s identical rationale (this session's
    transaction may already be autobegun by `run_evaluation`'s preceding
    reads). Per-example (not batched, like `attention.explain_item`'s
    synthetic items), the same `personal.generate_insight`-precedent
    reasoning: this evaluation dataset's own examples need fresh rows to
    avoid id/uniqueness collisions across the dataset, not because
    `email_threads`/`email_messages` has any workspace-wide section a
    batched shape would leak across examples the way `meeting.prep_
    summary`'s own `risks` section does.
    """
    # `email_threads` has a 3-column FK to `personal_domains(workspace_id,
    # owner_id, domain_key)` (migration `0069`) -- inserted with `ON
    # CONFLICT DO NOTHING` (keyed off that same FK's own unique
    # constraint) since a real, pre-existing `email` personal-domain row
    # for this caller (they have already connected Gmail and enabled the
    # domain themselves) must be left untouched, not clobbered.
    #
    # `RETURNING id`: round 5 review found that the original code discarded
    # this insert's own success/conflict outcome entirely, so `_delete_
    # synthetic_email_thread` below had no way to tell "this call created
    # the row" apart from "a real row already existed" -- it deleted `
    # personal_domains` by `(workspace_id, owner_id, domain_key)` alone,
    # which matches and deletes a real pre-existing row exactly as readily
    # as a synthetic one, cascading (that same FK, `ondelete="CASCADE"`) to
    # permanently delete that caller's entire real Gmail sync history plus
    # their `domain_consents` row (also `CASCADE`-linked to `personal_
    # domains`, migration `0054`) the first time this harness runs for a
    # caller who already uses the real feature. `.scalar_one_or_none()` is
    # `None` exactly when the `ON CONFLICT` branch fired -- the row already
    # existed and this call inserted nothing -- never the id of that
    # pre-existing row.
    domain_id = uuid4()
    inserted_domain_id = session.execute(
        text(
            """
            INSERT INTO personal_domains (
                id, workspace_id, owner_id, domain_key, classification,
                enabled, enabled_at, created_by, updated_by, created_at, updated_at, version
            ) VALUES (
                :id, :workspace_id, :owner_id, 'email', 'high_stakes',
                true, :now, :actor_id, :actor_id, :now, :now, 1
            )
            ON CONFLICT (workspace_id, owner_id, domain_key) DO NOTHING
            RETURNING id
            """
        ),
        {
            "id": domain_id,
            "workspace_id": auth.workspace_id,
            "owner_id": auth.user_id,
            "actor_id": auth.user_id,
            "now": now,
        },
    ).scalar_one_or_none()

    connector_account_id = uuid4()
    session.execute(
        text(
            """
            INSERT INTO connector_accounts (
                id, workspace_id, provider, external_account_id, display_name,
                granted_scopes, encrypted_credentials, status, version,
                created_by, updated_by, created_at, updated_at, owner_id, visibility
            ) VALUES (
                :id, :workspace_id, 'gmail', :external_account_id, 'evaluation harness',
                '{}', :encrypted_credentials, 'active', 1,
                :actor_id, :actor_id, :now, :now, :actor_id, 'workspace'
            )
            """
        ),
        {
            "id": connector_account_id,
            "workspace_id": auth.workspace_id,
            "external_account_id": f"evaluation-{connector_account_id}",
            "encrypted_credentials": b"",
            "actor_id": auth.user_id,
            "now": now,
        },
    )
    thread_id = uuid4()
    session.execute(
        text(
            """
            INSERT INTO email_threads (
                id, workspace_id, owner_id, domain_key, connector_account_id,
                external_thread_id, subject, last_message_at, created_at, updated_at
            ) VALUES (
                :id, :workspace_id, :owner_id, 'email', :connector_account_id,
                :external_thread_id, :subject, :now, :now, :now
            )
            """
        ),
        {
            "id": thread_id,
            "workspace_id": auth.workspace_id,
            "owner_id": auth.user_id,
            "connector_account_id": connector_account_id,
            "external_thread_id": f"evaluation-{thread_id}",
            "subject": example["subject"],
            "now": now,
        },
    )
    message_ids: list[UUID] = []
    for message in example["messages"]:
        message_id = uuid4()
        message_ids.append(message_id)
        session.execute(
            text(
                """
                INSERT INTO email_messages (
                    id, workspace_id, owner_id, thread_id, external_message_id,
                    sender, recipients, sent_at, direction, snippet, body,
                    body_fetched_at, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, :owner_id, :thread_id, :external_message_id,
                    :sender, :recipients, :sent_at, :direction, NULL, :body,
                    :now, :now, :now
                )
                """
            ),
            {
                "id": message_id,
                "workspace_id": auth.workspace_id,
                "owner_id": auth.user_id,
                "thread_id": thread_id,
                "external_message_id": f"evaluation-{message_id}",
                "sender": message["sender"],
                "recipients": ["owner@evaluation.test"],
                "sent_at": now - timedelta(days=message["sent_at_days_ago"]),
                "direction": message["direction"],
                "body": encrypt_field(message["body"]),
                "now": now,
            },
        )
    session.commit()
    return connector_account_id, thread_id, message_ids, inserted_domain_id


def _delete_synthetic_email_thread(
    session: Session,
    auth: AuthContext,
    connector_account_id: UUID,
    thread_id: UUID,
    message_ids: list[UUID],
    domain_id: UUID | None,
) -> None:
    """Deletes everything `_insert_synthetic_email_thread` created for one
    example, immediately after that example is scored -- see that
    function's docstring for why. Deletes child rows (`email_messages`)
    before the `email_threads`/`connector_accounts` rows they FK-
    reference, regardless of what `ondelete=CASCADE` may or may not
    already handle -- explicit, not relying on cascade, matching `_delete_
    synthetic_personal_insight_sources`'s identical discipline. Not
    context-managed -- see `_insert_synthetic_item`'s identical rationale.

    `domain_id`: `_insert_synthetic_email_thread`'s own `inserted_domain_
    id` -- `None` means that call's `personal_domains` insert hit its own
    `ON CONFLICT DO NOTHING` (a real, pre-existing row for this caller
    already exists), in which case `personal_domains` is left untouched
    entirely, matching `ON CONFLICT DO NOTHING`'s own "don't touch what
    was already there" intent. When not `None`, the delete below is scoped
    by that specific `id`, never by `(workspace_id, owner_id, domain_key)`
    alone -- round 5 review found the original, id-less delete matched and
    destroyed a real pre-existing row exactly as readily as a synthetic
    one this function created, cascading to that caller's entire real
    Gmail sync history and `domain_consents` row (see `_insert_synthetic_
    email_thread`'s own docstring for the full mechanism). Mirrors `_delete_
    synthetic_personal_insight_sources`'s own `id = ANY(:ids)`-scoped
    `personal_domains` delete, which never had this gap.
    """
    if message_ids:
        session.execute(
            text(
                "DELETE FROM email_messages WHERE workspace_id = :workspace_id AND id = ANY(:ids)"
            ),
            {"workspace_id": auth.workspace_id, "ids": message_ids},
        )
    session.execute(
        text("DELETE FROM email_threads WHERE workspace_id = :workspace_id AND id = :id"),
        {"workspace_id": auth.workspace_id, "id": thread_id},
    )
    if domain_id is not None:
        session.execute(
            text("DELETE FROM personal_domains WHERE workspace_id = :workspace_id AND id = :id"),
            {"workspace_id": auth.workspace_id, "id": domain_id},
        )
    session.execute(
        text("DELETE FROM connector_accounts WHERE workspace_id = :workspace_id AND id = :id"),
        {"workspace_id": auth.workspace_id, "id": connector_account_id},
    )
    session.commit()


# ---------------------------------------------------------------------------
# generated_artifacts -- module docstring's "first concrete producer".
# ---------------------------------------------------------------------------


def _write_generated_artifact(
    session: Session,
    auth: AuthContext,
    *,
    run: AiRun,
    task_type: str,
    source_versions: dict[str, Any],
    schema_version: str,
) -> None:
    session.execute(
        text(
            """
            INSERT INTO generated_artifacts (
                id, workspace_id, ai_run_id, task_type, source_versions,
                schema_version, output, evidence, status, created_at
            ) VALUES (
                :id, :workspace_id, :ai_run_id, :task_type, CAST(:source_versions AS jsonb),
                :schema_version, CAST(:output AS jsonb), CAST(:evidence AS jsonb),
                'proposed', :created_at
            )
            """
        ),
        {
            "id": uuid4(),
            "workspace_id": auth.workspace_id,
            "ai_run_id": run.id,
            "task_type": task_type,
            "source_versions": dumps(source_versions),
            "schema_version": schema_version,
            "output": dumps(run.output),
            "evidence": dumps(run.evidence),
            "created_at": run.completed_at or datetime.now(UTC),
        },
    )


# ---------------------------------------------------------------------------
# evaluation_runs persistence.
# ---------------------------------------------------------------------------

_EVALUATION_RUN_FIELDS = """
    id, task_type, evaluation_set_id, dataset_version, prompt_id, prompt_version,
    model_id, provider, total_examples, schema_validity_rate, grounding_rate,
    prohibited_fact_count, latency_p95_seconds, passed, failures, status,
    started_at, completed_at
"""


def _row_to_evaluation_run(row: dict[str, Any]) -> EvaluationRun:
    return EvaluationRun(
        id=row["id"],
        task_type=row["task_type"],
        prompt_id=row["prompt_id"],
        prompt_version=row["prompt_version"],
        model_id=row["model_id"],
        provider=row["provider"],
        dataset_version=row["dataset_version"],
        metrics=EvaluationMetrics(
            schema_validity_rate=float(row["schema_validity_rate"]),
            grounding_rate=float(row["grounding_rate"]),
            prohibited_fact_count=row["prohibited_fact_count"],
            latency_p95_seconds=float(row["latency_p95_seconds"]),
            total_examples=row["total_examples"],
        ),
        failures=list(row["failures"] or []),
        status=row["status"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
    )


def _persist_evaluation_run(
    session: Session,
    auth: AuthContext,
    *,
    task_type: str,
    evaluation_set: EvaluationSet,
    prompt_id: str,
    prompt_version: int,
    model_id: str,
    provider: str,
    metrics: EvaluationMetrics,
    failures: list[dict[str, Any]],
    started_at: datetime,
    completed_at: datetime,
) -> EvaluationRun:
    run_id = uuid4()
    passed = check_promotion_floors(
        EvaluationRun(
            id=run_id,
            task_type=task_type,
            prompt_id=prompt_id,
            prompt_version=prompt_version,
            model_id=model_id,
            provider=provider,
            dataset_version=evaluation_set.version,
            metrics=metrics,
            failures=failures,
            status="completed",
            started_at=started_at,
            completed_at=completed_at,
        )
    )
    # Not context-managed -- see `_insert_synthetic_item`'s docstring; the
    # session may already have an autobegun transaction from
    # `run_evaluation`'s preceding reads/example loop by the time this is
    # called.
    session.execute(
        text(
            """
            INSERT INTO evaluation_runs (
                id, workspace_id, actor_id, task_type, evaluation_set_id,
                dataset_version, prompt_id, prompt_version, model_id, provider,
                total_examples, schema_validity_rate, grounding_rate,
                prohibited_fact_count, latency_p95_seconds, passed, failures,
                status, started_at, completed_at, created_at, owner_id, visibility
            ) VALUES (
                :id, :workspace_id, :actor_id, :task_type, :evaluation_set_id,
                :dataset_version, :prompt_id, :prompt_version, :model_id, :provider,
                :total_examples, :schema_validity_rate, :grounding_rate,
                :prohibited_fact_count, :latency_p95_seconds, :passed,
                CAST(:failures AS jsonb), 'completed', :started_at, :completed_at,
                :completed_at, :actor_id, 'workspace'
            )
            """
        ),
        {
            "id": run_id,
            "workspace_id": auth.workspace_id,
            "actor_id": auth.user_id,
            "task_type": task_type,
            "evaluation_set_id": evaluation_set.id,
            "dataset_version": evaluation_set.version,
            "prompt_id": prompt_id,
            "prompt_version": prompt_version,
            "model_id": model_id,
            "provider": provider,
            "total_examples": metrics.total_examples,
            "schema_validity_rate": metrics.schema_validity_rate,
            "grounding_rate": metrics.grounding_rate,
            "prohibited_fact_count": metrics.prohibited_fact_count,
            "latency_p95_seconds": metrics.latency_p95_seconds,
            "passed": passed,
            "failures": dumps(failures),
            "started_at": started_at,
            "completed_at": completed_at,
        },
    )
    session.commit()
    return EvaluationRun(
        id=run_id,
        task_type=task_type,
        prompt_id=prompt_id,
        prompt_version=prompt_version,
        model_id=model_id,
        provider=provider,
        dataset_version=evaluation_set.version,
        metrics=metrics,
        failures=failures,
        status="completed",
        started_at=started_at,
        completed_at=completed_at,
    )


def get_evaluation_run(session: Session, auth: AuthContext, run_id: UUID) -> EvaluationRun | None:
    row = (
        session.execute(
            text(
                f"SELECT {_EVALUATION_RUN_FIELDS} FROM evaluation_runs "
                "WHERE workspace_id = :workspace_id AND id = :run_id"
            ),
            {"workspace_id": auth.workspace_id, "run_id": run_id},
        )
        .mappings()
        .one_or_none()
    )
    return _row_to_evaluation_run(dict(row)) if row is not None else None


def get_latest_evaluation_run(
    session: Session, auth: AuthContext, *, task_type: str, prompt_id: str, prompt_version: int
) -> EvaluationRun | None:
    """The most recent completed `evaluation_runs` row for this exact
    `(task_type, prompt_id, prompt_version)` triple, scoped to `auth`'s own
    workspace (`prompts.py`'s `_prompt_evaluation_floor_met` docstring:
    "the acting administrator's own workspace context", the same reasoning
    `_write_activation_audit` already applies to `audit_events.
    workspace_id` for this same global-catalog-activation action). `None`
    if this exact combination has never been evaluated in this workspace --
    the promotion gate treats that identically to "did not pass".
    """
    row = (
        session.execute(
            text(
                f"SELECT {_EVALUATION_RUN_FIELDS} FROM evaluation_runs "
                "WHERE workspace_id = :workspace_id AND task_type = :task_type "
                "AND prompt_id = :prompt_id AND prompt_version = :prompt_version "
                "AND status = 'completed' "
                "ORDER BY completed_at DESC LIMIT 1"
            ),
            {
                "workspace_id": auth.workspace_id,
                "task_type": task_type,
                "prompt_id": prompt_id,
                "prompt_version": prompt_version,
            },
        )
        .mappings()
        .one_or_none()
    )
    return _row_to_evaluation_run(dict(row)) if row is not None else None


# ---------------------------------------------------------------------------
# run_evaluation -- the harness entry point.
# ---------------------------------------------------------------------------


@contextmanager
def _synthetic_meeting_serialization_lock(workspace_id: UUID) -> Iterator[None]:
    """Serializes `meeting.prep_summary` evaluation runs within one
    workspace -- `_salted_synthetic_id`'s ids are deterministic per
    `(workspace_id, base_id)` (see its own docstring: needed so tests can
    predict them ahead of building mocked "fully grounded" citations), so
    two overlapping `meeting.prep_summary` evaluation runs in the *same*
    workspace (different `Idempotency-Key`s -- `_held_idempotency_lock`
    only serializes requests sharing the same key) would otherwise race
    to `INSERT` the same real primary key and surface as an unhandled
    `IntegrityError`. `attention.explain_item`'s synthetic items use
    random `uuid4()` ids and have no such collision risk, so this lock is
    `meeting.prep_summary`-only -- see `_insert_synthetic_meeting`'s call
    site in `run_evaluation` below. Same `lock_engine`/`pg_advisory_lock`
    pattern as `_held_idempotency_lock`, held on its own dedicated
    connection for this context manager's entire duration.
    """
    lock_key = f"{workspace_id}:meeting.prep_summary:synthetic-eval-data"
    with lock_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(
            text("SELECT pg_advisory_lock(hashtextextended(:lock_key, 0))"), {"lock_key": lock_key}
        )
        try:
            yield
        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock(hashtextextended(:lock_key, 0))"),
                {"lock_key": lock_key},
            )


@contextmanager
def _synthetic_email_thread_serialization_lock(
    workspace_id: UUID, owner_id: UUID
) -> Iterator[None]:
    """Serializes `email.detect_action` evaluation runs for one `(workspace_
    id, owner_id)` -- a different collision mechanism than `_synthetic_
    meeting_serialization_lock`'s deterministic-id `IntegrityError` risk,
    but the same "two overlapping runs for the same caller corrupt each
    other's data" shape, found by round 6 review as a narrower residual of
    round 5's own CRITICAL fix. `_insert_synthetic_email_thread`'s own
    `personal_domains` insert (`ON CONFLICT DO NOTHING`) means at most one
    of two concurrent runs for the same caller ever "owns" that row (round
    5's fix correctly ensures only the owning run's cleanup ever deletes
    it) -- but every `email_threads` row either run creates for this
    caller, this run's own or the other run's, shares that one `personal_
    domains` row via its `ondelete="CASCADE"` FK (migration `0069`).
    `_delete_synthetic_email_thread` runs per-example, not batched at the
    end of `run_evaluation`, so the owning run's very first per-example
    cleanup would cascade-delete every `email_threads` row currently
    matching that shared key -- including the other, still-in-flight run's
    own synthetic thread -- mid-run, not merely at cleanup. Scoped by
    `owner_id` in addition to `workspace_id` (unlike the meeting lock,
    scoped workspace-only) because the collision key here is `(workspace_
    id, owner_id, 'email')`, not workspace-wide. Same `lock_engine`/
    `pg_advisory_lock` pattern as `_synthetic_meeting_serialization_lock`,
    held on its own dedicated connection for this context manager's entire
    duration.
    """
    lock_key = f"{workspace_id}:{owner_id}:email.detect_action:synthetic-eval-data"
    with lock_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(
            text("SELECT pg_advisory_lock(hashtextextended(:lock_key, 0))"), {"lock_key": lock_key}
        )
        try:
            yield
        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock(hashtextextended(:lock_key, 0))"),
                {"lock_key": lock_key},
            )


def run_evaluation(
    task_type: str,
    prompt_version: int,
    model_id: str,
    *,
    session: Session,
    auth: AuthContext,
    ollama_adapter: OllamaAdapter | None = None,
) -> EvaluationRun:
    """Run every example in `task_type`'s active `evaluation_sets` version
    through `runtime.py:execute_run` and score the result against
    `EVALUATION-CONTRACT.md`'s four floors. Raises `EvaluationConfigError`
    (never a partial/degraded `EvaluationRun`) if the requested
    configuration cannot be verified up front -- see the module docstring's
    "assertions, not overrides" section.
    """
    port = TASK_PORTS.get(task_type)
    if port is None:
        raise EvaluationConfigError("unknown_task_type", f"unregistered task_type {task_type!r}")

    active_prompt = get_active_prompt(session, port.prompt_id)
    if active_prompt is None or active_prompt.version != prompt_version:
        raise EvaluationConfigError(
            "prompt_version_not_active",
            f"prompt_version {prompt_version} is not the currently active version of "
            f"{port.prompt_id!r} -- this activation only evaluates the active configuration",
        )

    model = get_model(session, model_id)
    if model is None or model.status != "active":
        raise EvaluationConfigError(
            "model_not_registered", f"model_id {model_id!r} is not a registered, active model"
        )

    evaluation_set = get_active_evaluation_set(session, task_type)
    if evaluation_set is None:
        raise EvaluationConfigError(
            "evaluation_set_not_found", f"no active evaluation_sets row for task_type {task_type!r}"
        )

    started_at = datetime.now(UTC)
    scores: list[_ExampleScore] = []
    synthetic_item_ids: list[UUID] = []

    # `meeting.prep_summary` and `email.detect_action` each need their own
    # serialization lock -- see `_synthetic_meeting_serialization_lock`'s
    # and `_synthetic_email_thread_serialization_lock`'s own docstrings for
    # the two distinct collision mechanisms (a deterministic-id `INSERT`
    # collision vs. a shared-row `CASCADE`-delete race). `attention.
    # explain_item`'s random-uuid4 synthetic items and `personal.
    # generate_insight`'s per-example `domain_ids` have no comparable risk
    # within a single run's own examples, so neither pays for a lock here
    # -- `personal.generate_insight`'s own cross-*run* exposure to the
    # identical `personal_domains`-row hazard (round 6 review: found, but
    # pre-existing Phase 7 code untouched by this PR, so out of scope for
    # this fix) is a separate, already-flagged gap, not something this
    # lock's scope silently claims to cover.
    serialization_lock: AbstractContextManager[None]
    if task_type == "meeting.prep_summary":
        serialization_lock = _synthetic_meeting_serialization_lock(auth.workspace_id)
    elif task_type == "email.detect_action":
        serialization_lock = _synthetic_email_thread_serialization_lock(
            auth.workspace_id, auth.user_id
        )
    else:
        serialization_lock = nullcontext()
    with serialization_lock:
        try:
            for example in evaluation_set.examples:
                now = datetime.now(UTC)
                # `meeting.prep_summary`'s synthetic source cannot use
                # `attention.explain_item`'s batched-insert/batched-
                # cleanup-at-the-end shape (`_insert_synthetic_item`/
                # `synthetic_item_ids`/`_delete_synthetic_items` below,
                # kept verbatim and untouched by this branch): its `risks`
                # section is workspace-wide, not meeting-scoped (`meeting_
                # prep.py:_fetch_risks`'s own comment), so leaving one
                # example's synthetic risks in place while later examples
                # run would leak them into packs that never listed them.
                # `_insert_synthetic_meeting`/`_delete_synthetic_meeting`
                # insert and delete each example's entire bundle within
                # this same iteration instead -- see their docstrings.
                if task_type == "meeting.prep_summary":
                    meeting_id, node_ids, risk_ids = _insert_synthetic_meeting(
                        session, auth, example, now=now
                    )
                    run_input: dict[str, Any] = {"meeting_id": str(meeting_id)}
                    source_versions: dict[str, Any] = {"meeting_id": str(meeting_id)}
                elif task_type == "personal.generate_insight":
                    domain_ids, grant_ids, record_ids = _insert_synthetic_personal_insight_sources(
                        session, auth, example, now=now
                    )
                    source_domain_keys = [source["domain_key"] for source in example["sources"]]
                    run_input = {"source_domain_keys": source_domain_keys}
                    source_versions = {"source_domain_keys": source_domain_keys}
                elif task_type == "email.detect_action":
                    connector_account_id, thread_id, message_ids, email_domain_id = (
                        _insert_synthetic_email_thread(session, auth, example, now=now)
                    )
                    run_input = {"thread_id": str(thread_id)}
                    source_versions = {"thread_id": str(thread_id)}
                else:
                    item_id = _insert_synthetic_item(session, auth, example, now=now)
                    synthetic_item_ids.append(item_id)
                    run_input = {"attention_item_id": str(item_id)}
                    source_versions = {
                        "attention_item_id": str(item_id),
                        "source_entity_version": _SYNTHETIC_SOURCE_ENTITY_VERSION,
                    }

                try:
                    call_started = time.perf_counter()
                    run = execute_run(
                        task_type,
                        _EVALUATION_DATA_CLASS,
                        run_input,
                        session=session,
                        auth=auth,
                        ollama_adapter=ollama_adapter,
                    )
                    latency_seconds = time.perf_counter() - call_started

                    # `execute_run` always routes through the live `model_
                    # definitions` registry (runtime.py's own `route()` call) --
                    # it has no parameter to pin a specific candidate. With a
                    # single registered model this was moot (only one possible
                    # outcome); with two or more, `route()` could legitimately
                    # pick a different candidate than the one this function's
                    # `model_id` parameter asserted was eligible, silently mis-
                    # attributing this evaluation's results to the wrong model
                    # (`EvaluationRun.model_id` below is stamped from the
                    # requested parameter, not from what actually ran). Matches
                    # this module's own "assertions, not overrides" contract
                    # (module docstring): fail loud, not a partial/degraded run
                    # scored against the wrong candidate. `run.model_id` is
                    # `None` on a run that never reached routing (e.g. `feature_
                    # disabled`) -- not a mismatch, already a legitimate `other_
                    # failure` outcome for `_score_example` below.
                    if run.model_id is not None and run.model_id != model_id:
                        raise EvaluationConfigError(
                            "unexpected_model_routed",
                            f"requested model_id {model_id!r} but the router selected "
                            f"{run.model_id!r} instead -- refusing to score this evaluation "
                            "run against the wrong model",
                        )

                    score = _score_example(
                        session, task_type, example, run, latency_seconds=latency_seconds
                    )
                    scores.append(score)

                    if score.outcome == "completed":
                        _write_generated_artifact(
                            session,
                            auth,
                            run=run,
                            task_type=task_type,
                            source_versions=source_versions,
                            schema_version=active_prompt.output_schema_ref,
                        )
                        session.commit()
                finally:
                    # `meeting.prep_summary`'s per-example cleanup, scoped to
                    # this one iteration -- see the comment above this branch's
                    # insert for why it cannot wait for the batched cleanup
                    # below. Runs even if the block above raised (e.g. the
                    # `unexpected_model_routed` guard), matching the outer
                    # `finally`'s own rollback-before-delete guard for the same
                    # reason.
                    if task_type == "meeting.prep_summary":
                        if session.in_transaction():
                            session.rollback()
                        _delete_synthetic_meeting(session, auth, meeting_id, node_ids, risk_ids)
                    elif task_type == "personal.generate_insight":
                        if session.in_transaction():
                            session.rollback()
                        _delete_synthetic_personal_insight_sources(
                            session, auth, domain_ids, grant_ids, record_ids
                        )
                    elif task_type == "email.detect_action":
                        if session.in_transaction():
                            session.rollback()
                        _delete_synthetic_email_thread(
                            session,
                            auth,
                            connector_account_id,
                            thread_id,
                            message_ids,
                            email_domain_id,
                        )
        finally:
            # Guard against a leftover open transaction from a mid-loop
            # exception (should not happen -- execute_run always returns an
            # AiRun rather than raising -- but this keeps the cleanup delete
            # below safe regardless): SQLAlchemy's Session only tolerates one
            # active transaction at a time, and `_delete_synthetic_items` opens
            # its own via `session.begin()`.
            if session.in_transaction():
                session.rollback()
            _delete_synthetic_items(session, auth, synthetic_item_ids)

    completed_at = datetime.now(UTC)
    metrics, failures = _aggregate(scores)
    return _persist_evaluation_run(
        session,
        auth,
        task_type=task_type,
        evaluation_set=evaluation_set,
        prompt_id=active_prompt.prompt_id,
        prompt_version=active_prompt.version,
        model_id=model.model_id,
        provider=model.provider,
        metrics=metrics,
        failures=failures,
        started_at=started_at,
        completed_at=completed_at,
    )


# ---------------------------------------------------------------------------
# GET /api/v1/ai/evaluations, POST /api/v1/ai/evaluations/runs,
# GET /api/v1/ai/evaluations/runs/{id} (`phase-004/API-SCHEMAS.md`).
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1/ai", tags=["ai-runtime"])
SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]


class EvaluationSetResponse(BaseModel):
    task_type: str
    version: int
    classification: Literal["labelled", "development"]
    example_count: int
    status: Literal["active", "retired"]


class EvaluationSetListResponse(BaseModel):
    evaluation_sets: list[EvaluationSetResponse]


class EvaluationRunCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_type: Literal[
        "attention.explain_item",
        "meeting.prep_summary",
        "personal.generate_insight",
        "email.detect_action",
    ]
    prompt_version: int = Field(ge=1)
    model_id: str = Field(min_length=1, max_length=200)


class EvaluationMetricsResponse(BaseModel):
    schema_validity_rate: float
    grounding_rate: float
    prohibited_fact_count: int
    latency_p95_seconds: float
    total_examples: int


class EvaluationRunResponse(BaseModel):
    id: UUID
    task_type: str
    prompt_id: str
    prompt_version: int
    model_id: str
    provider: str
    dataset_version: int
    metrics: EvaluationMetricsResponse
    passed: bool
    failures: list[dict[str, Any]]
    status: Literal["completed"]
    started_at: datetime
    completed_at: datetime


def _to_response(run: EvaluationRun) -> EvaluationRunResponse:
    return EvaluationRunResponse(
        id=run.id,
        task_type=run.task_type,
        prompt_id=run.prompt_id,
        prompt_version=run.prompt_version,
        model_id=run.model_id,
        provider=run.provider,
        dataset_version=run.dataset_version,
        metrics=EvaluationMetricsResponse(
            schema_validity_rate=run.metrics.schema_validity_rate,
            grounding_rate=run.metrics.grounding_rate,
            prohibited_fact_count=run.metrics.prohibited_fact_count,
            latency_p95_seconds=run.metrics.latency_p95_seconds,
            total_examples=run.metrics.total_examples,
        ),
        passed=check_promotion_floors(run),
        failures=run.failures,
        status=run.status,
        started_at=run.started_at,
        completed_at=run.completed_at,
    )


@router.get("/evaluations")
def list_evaluations(auth: AuthDep, session: SessionDep) -> EvaluationSetListResponse:
    """List every registered evaluation dataset -- `evaluation_sets` is
    global platform catalog data (migration `0031_phase4_evaluation.py`'s
    docstring), matching `GET /ai/models`/`GET /ai/policies`'s identical
    "`AuthDep` alone is what local-owner-scoped resolves to" reasoning:
    every authenticated caller sees the same rows.
    """
    sets = list_evaluation_sets(session)
    return EvaluationSetListResponse(
        evaluation_sets=[
            EvaluationSetResponse(
                task_type=item.task_type,
                version=item.version,
                classification=item.classification,
                example_count=item.example_count,
                status=item.status,
            )
            for item in sets
        ]
    )


def _request_hash(payload: BaseModel, action: str) -> str:
    material = {"action": action, "payload": payload.model_dump(mode="json")}
    return sha256(dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@contextmanager
def _held_idempotency_lock(auth: AuthContext, key: str) -> Iterator[None]:
    """A session-scoped `pg_advisory_lock`, held on its own dedicated
    connection for this context manager's entire duration -- see
    `runtime.py:_held_idempotency_lock`'s identical rationale. This
    endpoint's critical section is even longer than `POST /ai/runs`'s: up
    to 20 sequential `execute_run` calls (`run_evaluation`'s per-example
    loop), each of which can itself commit internally partway through --
    `pg_advisory_xact_lock` would release long before the evaluation
    finishes, letting a concurrent duplicate request start its own
    20-example run before the first one's response is even stored.

    Uses `ecc.database.lock_engine` (`NullPool`, no `statement_timeout`),
    not the main `engine` -- see `runtime.py:_held_idempotency_lock`'s
    identical rationale and `lock_engine`'s own docstring in
    `database.py`. This endpoint's lock can be held for minutes (up to 20
    sequential model calls), the longest-lived lock in this codebase, so
    it is the case this matters most for.
    """
    lock_key = f"{auth.workspace_id}:{auth.user_id}:{key}"
    with lock_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(
            text("SELECT pg_advisory_lock(hashtextextended(:lock_key, 0))"), {"lock_key": lock_key}
        )
        try:
            yield
        finally:
            connection.execute(
                text("SELECT pg_advisory_unlock(hashtextextended(:lock_key, 0))"),
                {"lock_key": lock_key},
            )


def _load_cached(
    session: Session, auth: AuthContext, key: str, request_hash: str
) -> EvaluationRunResponse | None:
    row = (
        session.execute(
            text(
                """
                SELECT request_hash, response_body FROM idempotency_records
                WHERE workspace_id = :workspace_id AND actor_id = :actor_id
                  AND key = :key AND expires_at > :now
                """
            ),
            {
                "workspace_id": auth.workspace_id,
                "actor_id": auth.user_id,
                "key": key,
                "now": datetime.now(UTC),
            },
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    if row["request_hash"] != request_hash:
        record_idempotency_conflict("ai_runtime")
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
    return EvaluationRunResponse.model_validate(row["response_body"])


def _store_idempotency(
    session: Session,
    auth: AuthContext,
    key: str,
    request_hash: str,
    response: EvaluationRunResponse,
    now: datetime,
) -> None:
    session.execute(
        text(
            """
            INSERT INTO idempotency_records (
                workspace_id, actor_id, key, request_hash, response_status,
                response_body, created_at, expires_at
            ) VALUES (
                :workspace_id, :actor_id, :key, :request_hash, 200,
                CAST(:response_body AS jsonb), :created_at, :expires_at
            )
            """
        ),
        {
            "workspace_id": auth.workspace_id,
            "actor_id": auth.user_id,
            "key": key,
            "request_hash": request_hash,
            "response_body": dumps(response.model_dump(mode="json")),
            "created_at": now,
            "expires_at": now + timedelta(days=365),
        },
    )


@router.post("/evaluations/runs", response_model=EvaluationRunResponse)
def create_evaluation_run(
    payload: EvaluationRunCreateRequest,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
    adapter: OllamaAdapterDep,
) -> EvaluationRunResponse:
    """Runs the full labelled dataset for `payload.task_type` through
    `run_evaluation` synchronously within this request, mirroring `POST
    /ai/runs`'s own synchronous-execution precedent (`runtime.py:create_
    run`'s docstring: "no async execution exists in this activation").

    The entire body below runs inside `_held_idempotency_lock` (see its
    own docstring) -- a concurrent duplicate request with the same
    Idempotency-Key blocks until this one finishes and stores its
    response, rather than independently starting its own full 20-example
    evaluation run.
    """
    authz.require_role_action(session, auth, "write")
    request_hash = _request_hash(payload, "create_evaluation_run")
    now = datetime.now(UTC)
    with _held_idempotency_lock(auth, idempotency_key):
        with session.begin():
            cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached

        try:
            run = run_evaluation(
                payload.task_type,
                payload.prompt_version,
                payload.model_id,
                session=session,
                auth=auth,
                ollama_adapter=adapter,
            )
        except EvaluationConfigError as exc:
            raise HTTPException(
                status_code=422, detail={"code": exc.code, "message": str(exc)}
            ) from exc

        response = _to_response(run)
        try:
            with session.begin():
                _store_idempotency(session, auth, idempotency_key, request_hash, response, now)
        except SQLAlchemyError:
            # `run` above is already persisted (`_persist_evaluation_run`,
            # inside `run_evaluation`) -- losing only the idempotency
            # bookkeeping record must not turn an already-completed
            # evaluation run into an apparent failure for the caller.
            # Residual risk, not fully closed: a same-key retry after this
            # failure won't find a cached response and will re-invoke
            # `run_evaluation`, a second full labelled-set run -- but that
            # requires this exact statement to fail specifically (a DB
            # blip, not a concurrent request; `_held_idempotency_lock`
            # above already fully serializes those), the same narrower
            # window `runtime.py:create_run`'s identical fix accepts.
            record_database_failure("/api/v1/ai/evaluations/runs")
        return response


@router.get("/evaluations/runs/{run_id}", response_model=EvaluationRunResponse)
def get_evaluation_run_endpoint(
    run_id: UUID, auth: AuthDep, session: SessionDep
) -> EvaluationRunResponse:
    visible = authz.authorize(
        session, auth, resource_type="evaluation_runs", resource_id=run_id, action="read"
    )
    session.rollback()
    if not visible:
        raise HTTPException(status_code=404, detail="EVALUATION_RUN_NOT_FOUND")
    run = get_evaluation_run(session, auth, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="EVALUATION_RUN_NOT_FOUND")
    return _to_response(run)
