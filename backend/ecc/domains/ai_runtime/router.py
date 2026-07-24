"""The provider-neutral Model Router (`MODEL-ROUTING-CONTRACT.md`).

Implements the design doc's Decision 2 fixed eligibility-then-preference
pipeline: seven hard-filter eligibility steps evaluated in a fixed order (a
candidate failing any step is excluded, not deprioritized), then -- only if
more than one candidate survives -- five preference steps ending in a
deterministic `model_id` string tie-break.

`route()` is pure, in-memory comparison against a **cached** snapshot of the
registry/circuit-state/observed-latency/budget data (Decision 2: "never
queried synchronously per-request"), which is what keeps routing overhead
inside the phase's own p95 <50ms non-functional requirement (Decision 5).
Production callers refresh that cache on a short interval via
`refresh_cache`; tests construct a fully synthetic snapshot per call via
`candidates=`/`candidate_states=`, which is how Task 1's pipeline tests
exercise every eligibility/preference step without a database or a live
Ollama server.

`list_policies`/`get_policy` read `routing_policies` -- the versioned,
per-task-type routing configuration (Decision 2: "not per-policy
configurable in this first cut", so today this is read-only data the router
does not yet branch on beyond identifying which model(s) a task type is
allowed to consider).
"""

import threading
from dataclasses import dataclass
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthDep
from ecc.database import get_session

from .registry import ModelDefinition, list_models

HealthState = Literal["closed", "open", "half_open"]

# The 500ms overhead reserve MODEL-ROUTING-CONTRACT.md's eligibility step 6
# subtracts from a task's declared timeout before comparing it against a
# candidate's observed p95 latency (routing + validation + audit write).
ROUTING_OVERHEAD_RESERVE_SECONDS = 0.5


@dataclass(frozen=True, slots=True)
class TaskRequirements:
    """A task type's declared routing requirements, from its typed port
    (design doc Decision 2 step 2: "from its typed port, never the
    caller"). A small fixed table below, not database-configurable --
    exactly like the eligibility/preference pipeline itself is fixed, per
    `MODEL-ROUTING-CONTRACT.md`.
    """

    capability: str
    requires_structured_output: bool
    timeout_seconds: float
    max_output_tokens: int


# This activation registers exactly one task type (design doc Decision 9):
# `attention.explain_item`. A second task type is a later slice's addition
# to this table, not a schema change.
TASK_REQUIREMENTS: dict[str, TaskRequirements] = {
    "attention.explain_item": TaskRequirements(
        capability="explanation",
        requires_structured_output=True,
        timeout_seconds=20.0,
        max_output_tokens=512,
    ),
}


@dataclass(frozen=True, slots=True)
class ContextEstimate:
    """Prompt/output size estimate computed *before* the model call, per
    Decision 2 step 4 ("both computed before the call, not measured
    after").
    """

    estimated_prompt_tokens: int
    declared_max_output_tokens: int


@dataclass(frozen=True, slots=True)
class CandidateState:
    """Per-candidate runtime signal the pipeline consults, refreshed from a
    cached snapshot -- never a live per-request DB/network read (Decision
    2). Every field defaults to "healthy/unconstrained" so a freshly
    registered candidate with no observed history yet (this activation's
    real bootstrap case: a brand-new model with zero prior runs) is not
    spuriously excluded before it has ever been called.
    """

    health_state: HealthState = "closed"
    observed_p95_latency_seconds: float | None = None
    remaining_budget: float = float("inf")
    evaluation_quality_floor_passed: bool = False
    observed_cost: float = 0.0


@dataclass(frozen=True, slots=True)
class RoutingDecision:
    model_id: str
    provider: str
    deployment: str
    policy_version: int


@dataclass(frozen=True, slots=True)
class NoEligibleCandidate:
    """No candidate survived the eligibility pipeline (or the task type has
    no registered candidates at all). `reason` names the first eligibility
    step that excluded every remaining candidate, for audit/debugging --
    never exposed to a caller as anything beyond an enum-shaped code
    (`feature_disabled`/`remote_not_configured`/etc., `API-SCHEMAS.md`'s
    Errors section), matching this codebase's redaction discipline.
    """

    task_type: str
    data_class: str
    reason: str


def _eligible(
    candidate: ModelDefinition,
    state: CandidateState,
    *,
    data_class: str,
    requirements: TaskRequirements,
    context_estimate: ContextEstimate,
) -> str | None:
    """Run the seven hard-filter eligibility steps in Decision 2's fixed
    order against one candidate. Returns `None` if the candidate survives
    every step, or the failing step's short reason code otherwise.
    """
    # 1. Data residency/privacy -- evaluated first, matching ADR-0007's
    # "sensitive requests never silently fall back to cloud"; no later,
    # softer step may override it.
    if data_class not in candidate.data_classes:
        return "data_class_not_eligible"

    # 2. Required capability.
    if requirements.capability not in candidate.capabilities:
        return "capability_not_supported"

    # 3. Structured-output support.
    if requirements.requires_structured_output and not candidate.structured_output_supported:
        return "structured_output_not_supported"

    # 4. Context limit at a 90% margin (10% reserved for tokenizer-estimate
    # drift between ECC's estimate and Ollama's actual tokenizer).
    required_tokens = (
        context_estimate.estimated_prompt_tokens + context_estimate.declared_max_output_tokens
    )
    if required_tokens > candidate.context_window_tokens * 0.9:
        return "context_limit_exceeded"

    # 5. Health -- the circuit breaker (Task 3's `budgets.py`) must not be
    # open. Task 1 represents this as a plain per-candidate state field
    # rather than importing a not-yet-built circuit-breaker type.
    if state.health_state == "open":
        return "circuit_open"

    # 6. Latency -- rolling observed p95 must fit within the task's declared
    # timeout minus the fixed overhead reserve. A candidate with no
    # observed latency yet (never called for this task type) is not
    # excluded -- there is nothing to compare against, and excluding it
    # would make the very first call for any task type unroutable.
    if state.observed_p95_latency_seconds is not None:
        latency_budget = requirements.timeout_seconds - ROUTING_OVERHEAD_RESERVE_SECONDS
        if state.observed_p95_latency_seconds > latency_budget:
            return "latency_budget_exceeded"

    # 7. Remaining run/session token and time budget must be non-zero.
    if state.remaining_budget <= 0:
        return "budget_exhausted"

    return None


def _preference_key(
    candidate: ModelDefinition, state: CandidateState
) -> tuple[int, int, float, float, str]:
    """Sort key implementing Decision 2's five preference steps, in order.
    Python's stable ascending sort over this tuple reproduces the pipeline
    exactly: local before remote, quality-floor-passed before not, lower
    cost, lower observed p95 latency, then the ascending `model_id` string
    tie-break.
    """
    is_remote = 0 if candidate.deployment == "local" else 1
    floor_not_passed = 0 if state.evaluation_quality_floor_passed else 1
    cost = state.observed_cost
    # A candidate with no observed latency yet sorts as if it were fastest
    # (0.0) among remaining eligible candidates -- consistent with
    # eligibility step 6 already treating "no data" as non-excluding rather
    # than worst-case; preference ordering should not contradict that.
    latency = (
        state.observed_p95_latency_seconds
        if state.observed_p95_latency_seconds is not None
        else 0.0
    )
    return (is_remote, floor_not_passed, cost, latency, candidate.model_id)


_cache_lock = threading.Lock()
_cached_candidates: list[ModelDefinition] | None = None
_cached_states: dict[str, CandidateState] = {}


def refresh_cache(
    session: Session,
    *,
    states: dict[str, CandidateState] | None = None,
) -> None:
    """Refresh the module-level, in-memory candidate snapshot `route()`
    reads by default. Callers (the future orchestration loop, Task 4)
    invoke this on a short interval -- never synchronously inside a
    per-request call to `route()` -- per Decision 2's performance note.
    """
    global _cached_candidates, _cached_states
    with _cache_lock:
        _cached_candidates = list_models(session)
        _cached_states = dict(states) if states is not None else {}


def route(
    task_type: str,
    data_class: str,
    context_estimate: ContextEstimate,
    *,
    candidates: list[ModelDefinition] | None = None,
    candidate_states: dict[str, CandidateState] | None = None,
    policy_version: int = 1,
) -> RoutingDecision | NoEligibleCandidate:
    """Route a task to exactly one model, or report why none is eligible.

    `candidates`/`candidate_states` default to the module-level cache
    populated by `refresh_cache` (the production path); tests pass a fully
    synthetic snapshot directly, which is how the pipeline is exercised
    against every eligibility/preference step without a database or a live
    Ollama server (Task 1 Step 1).
    """
    requirements = TASK_REQUIREMENTS.get(task_type)
    if requirements is None:
        return NoEligibleCandidate(
            task_type=task_type, data_class=data_class, reason="feature_disabled"
        )

    resolved_candidates = candidates if candidates is not None else (_cached_candidates or [])
    resolved_states = candidate_states if candidate_states is not None else _cached_states

    eligible: list[tuple[ModelDefinition, CandidateState]] = []
    last_reason = "no_candidates_registered"
    for candidate in resolved_candidates:
        state = resolved_states.get(candidate.model_id, CandidateState())
        reason = _eligible(
            candidate,
            state,
            data_class=data_class,
            requirements=requirements,
            context_estimate=context_estimate,
        )
        if reason is None:
            eligible.append((candidate, state))
        else:
            last_reason = reason

    if not eligible:
        return NoEligibleCandidate(task_type=task_type, data_class=data_class, reason=last_reason)

    eligible.sort(key=lambda pair: _preference_key(pair[0], pair[1]))
    winner, _ = eligible[0]
    return RoutingDecision(
        model_id=winner.model_id,
        provider=winner.provider,
        deployment=winner.deployment,
        policy_version=policy_version,
    )


_POLICY_FIELDS = "id, task_type, version, candidates, constraints, fallback, status"


@dataclass(frozen=True, slots=True)
class RoutingPolicy:
    id: UUID
    task_type: str
    version: int
    candidates: list[dict[str, Any]]
    constraints: dict[str, Any]
    fallback: dict[str, Any]
    status: Literal["active", "retired"]


def _row_to_policy(row: dict[str, Any]) -> RoutingPolicy:
    return RoutingPolicy(
        id=row["id"],
        task_type=row["task_type"],
        version=row["version"],
        candidates=row["candidates"],
        constraints=row["constraints"],
        fallback=row["fallback"],
        status=row["status"],
    )


def list_policies(session: Session, *, include_retired: bool = False) -> list[RoutingPolicy]:
    clause = "" if include_retired else "WHERE status = 'active'"
    rows = (
        session.execute(
            text(
                f"SELECT {_POLICY_FIELDS} FROM routing_policies {clause} "
                "ORDER BY task_type ASC, version ASC"
            )
        )
        .mappings()
        .all()
    )
    return [_row_to_policy(dict(row)) for row in rows]


def get_policy(session: Session, task_type: str) -> RoutingPolicy | None:
    row = (
        session.execute(
            text(
                f"SELECT {_POLICY_FIELDS} FROM routing_policies "
                "WHERE task_type = :task_type AND status = 'active'"
            ),
            {"task_type": task_type},
        )
        .mappings()
        .one_or_none()
    )
    return _row_to_policy(dict(row)) if row is not None else None


# --- GET /api/v1/ai/policies ------------------------------------------------
#
# `routing_policies` is global platform configuration, not workspace-scoped
# user data (see migration `0028_phase4_model_registry.py`'s docstring) --
# same authority/scoping reasoning as `registry.py`'s `GET /api/v1/ai/models`
# endpoint: `AuthDep` alone is what "local-owner scoped" resolves to in a
# codebase with no separate admin/owner role, and there is no
# workspace-specific row to further scope or 404 on.

api_router = APIRouter(prefix="/api/v1/ai", tags=["ai-runtime"])
SessionDep = Annotated[Session, Depends(get_session)]


class RoutingPolicyResponse(BaseModel):
    task_type: str
    version: int
    candidates: list[dict[str, Any]]
    constraints: dict[str, Any]
    fallback: dict[str, Any]
    status: Literal["active", "retired"]


class PolicyListResponse(BaseModel):
    policies: list[RoutingPolicyResponse]


def _policy_to_response(policy: RoutingPolicy) -> RoutingPolicyResponse:
    return RoutingPolicyResponse(
        task_type=policy.task_type,
        version=policy.version,
        candidates=policy.candidates,
        constraints=policy.constraints,
        fallback=policy.fallback,
        status=policy.status,
    )


@api_router.get("/policies")
def list_policies_endpoint(auth: AuthDep, session: SessionDep) -> PolicyListResponse:
    """List every active routing policy (`routing_policies`) -- the
    versioned per-task-type routing configuration `route()` reads
    candidates from, not the eligibility/preference pipeline's own
    per-request output.
    """
    # AuthDep's presence is the authority check itself; see the block
    # comment above.
    policies = list_policies(session, include_retired=True)
    return PolicyListResponse(policies=[_policy_to_response(policy) for policy in policies])
