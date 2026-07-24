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
Each example needs a real `attention_items` row for `attention.get_item`
(and therefore `attention.explain_item`) to read -- `run_evaluation` inserts
one synthetic row per example into the *caller's own* workspace, runs it
through `execute_run`, and deletes every synthetic row it created once the
full dataset has been scored, so an evaluation run never leaves fabricated
rows behind that could show up in that workspace's real Attention Queue.
The `ai_runs`/`ai_run_steps` rows `execute_run` itself persists are *not*
deleted -- they are genuine historical run records, retained for
reproducibility exactly like any other run (`EVALUATION-CONTRACT.md`:
"Evaluation results, environment and artifact hashes ... are retained for
reproducibility").

**`generated_artifacts`.** For every example that reaches `completed`,
`run_evaluation` writes one `generated_artifacts` row deriving from that
example's `ai_runs` row -- `source_versions` pins the synthetic item's
`source_entity_version`, `evidence` is the run's cited factor codes,
`output` is the validated `{explanation_text, cited_factor_codes}` payload.
This is the first concrete producer of `generated_artifacts` rows in this
activation (Task 4's `POST /ai/runs` does not write one); wiring the
production run path to do the same is a later task's decision, not
attempted here.
"""

import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from json import dumps
from math import ceil
from typing import Annotated, Any, Literal, TypedDict
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import engine, get_session
from ecc.observability import record_idempotency_conflict

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
_SCHEMA_VALIDITY_FLOOR = 1.0
_GROUNDING_FLOOR = 1.0
_PROHIBITED_FACT_FLOOR = 0
_LATENCY_P95_CEILING_SECONDS = 20.0


class EvaluationExample(TypedDict):
    """One row of `evaluation_sets.examples` (design doc Decision 9) --
    matches `tests/fixtures/phase4_evaluation_attention_explain.py`'s
    `EXAMPLES` shape and migration `0031_phase4_evaluation.py`'s seeded
    JSONB content exactly.
    """

    key: str
    entity_type: str
    score: int
    confidence: float
    factors: list[dict[str, Any]]
    must_cite: list[str]
    must_not_state: list[str]
    reference_explanation: str


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
    examples: list[EvaluationExample]
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
    zero prohibited-fact occurrences, p95 latency strictly under 20s. A
    pure function over already-computed metrics -- no database access, no
    knowledge of *how* `evaluation_run` was produced, so `prompts.py`'s
    promotion gate and this module's own persistence path both call the
    exact same logic rather than two independently-maintained copies of
    "did it pass".
    """
    metrics = evaluation_run.metrics
    return (
        metrics.schema_validity_rate >= _SCHEMA_VALIDITY_FLOOR
        and metrics.grounding_rate >= _GROUNDING_FLOOR
        and metrics.prohibited_fact_count <= _PROHIBITED_FACT_FLOOR
        and metrics.latency_p95_seconds < _LATENCY_P95_CEILING_SECONDS
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


def _prohibited_matches(example: EvaluationExample, run: AiRun) -> tuple[str, ...]:
    """`must_not_state`'s hallucination probe (design doc Decision 9): a
    completed run's `explanation_text` containing any of the example's
    forbidden phrases (case-insensitive substring match) is a prohibited-
    fact occurrence. Only meaningful for a `completed` run -- a run that
    never produced a validated `output` has nothing to check.
    """
    if run.output is None:
        return ()
    explanation = str(run.output.get("explanation_text", "")).casefold()
    return tuple(phrase for phrase in example["must_not_state"] if phrase.casefold() in explanation)


def _score_example(
    example: EvaluationExample, run: AiRun, *, latency_seconds: float
) -> _ExampleScore:
    outcome = _classify_outcome(run)
    matches = _prohibited_matches(example, run) if outcome == "completed" else ()
    return _ExampleScore(
        key=example["key"],
        outcome=outcome,
        latency_seconds=latency_seconds,
        prohibited_matches=matches,
        error_code=run.error_code,
        ai_run=run,
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
            failures.append(
                {"key": score.key, "reason": score.outcome, "error_code": score.error_code}
            )
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
    session: Session, auth: AuthContext, example: EvaluationExample, *, now: datetime
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
# generated_artifacts -- module docstring's "first concrete producer".
# ---------------------------------------------------------------------------


def _write_generated_artifact(
    session: Session,
    auth: AuthContext,
    *,
    run: AiRun,
    task_type: str,
    attention_item_id: UUID,
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
            "source_versions": dumps(
                {
                    "attention_item_id": str(attention_item_id),
                    "source_entity_version": _SYNTHETIC_SOURCE_ENTITY_VERSION,
                }
            ),
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
                status, started_at, completed_at, created_at
            ) VALUES (
                :id, :workspace_id, :actor_id, :task_type, :evaluation_set_id,
                :dataset_version, :prompt_id, :prompt_version, :model_id, :provider,
                :total_examples, :schema_validity_rate, :grounding_rate,
                :prohibited_fact_count, :latency_p95_seconds, :passed,
                CAST(:failures AS jsonb), 'completed', :started_at, :completed_at,
                :completed_at
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

    try:
        for example in evaluation_set.examples:
            now = datetime.now(UTC)
            item_id = _insert_synthetic_item(session, auth, example, now=now)
            synthetic_item_ids.append(item_id)

            call_started = time.perf_counter()
            run = execute_run(
                task_type,
                _EVALUATION_DATA_CLASS,
                {"attention_item_id": str(item_id)},
                session=session,
                auth=auth,
                ollama_adapter=ollama_adapter,
            )
            latency_seconds = time.perf_counter() - call_started

            score = _score_example(example, run, latency_seconds=latency_seconds)
            scores.append(score)

            if score.outcome == "completed":
                _write_generated_artifact(
                    session,
                    auth,
                    run=run,
                    task_type=task_type,
                    attention_item_id=item_id,
                    schema_version=active_prompt.output_schema_ref,
                )
                session.commit()
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
    task_type: Literal["attention.explain_item"]
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
    """
    lock_key = f"{auth.workspace_id}:{auth.user_id}:{key}"
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
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
        with session.begin():
            _store_idempotency(session, auth, idempotency_key, request_hash, response, now)
        return response


@router.get("/evaluations/runs/{run_id}", response_model=EvaluationRunResponse)
def get_evaluation_run_endpoint(
    run_id: UUID, auth: AuthDep, session: SessionDep
) -> EvaluationRunResponse:
    run = get_evaluation_run(session, auth, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="EVALUATION_RUN_NOT_FOUND")
    return _to_response(run)
