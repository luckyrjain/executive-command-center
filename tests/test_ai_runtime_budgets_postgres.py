"""Phase 4 Task 3: budgets, timeouts, cancellation and circuit breakers
(design doc Decision 5).

Covers, per `docs/superpowers/plans/2026-07-23-phase-4-ai-runtime.md`'s
Task 3:

1. `budgets.py:CircuitBreaker` -- the three-state state machine: closed ->
   open after 3 consecutive failures within a 60s rolling window; open ->
   half_open after a 30s cool-down; half_open -> closed on one probe
   success; half_open -> open on one probe failure. `candidate_state_for`
   feeds a real breaker's live state into `router.py:route()`'s already-
   committed eligibility step 5 (Task 1's own `test_eligibility_step5_*`
   tests extended here with a real breaker instead of a hand-set
   `CandidateState`).
2. `budgets.py:RunBudget`/`RunGuard` -- built from the real seeded
   `routing_policies.constraints` row (never a second hardcoded copy of
   Decision 5's five numbers); a run exceeding the 60s total wall-clock
   budget is marked `degraded` and cancels its token, never left
   `running`; a prompt exceeding the 3072-token estimate is rejected
   before any model call, reusing `router.py:ContextEstimate`; an output
   exceeding 512 tokens is caught by the application-level guard (after
   confirming Ollama was actually asked to stop at that length via
   `num_predict`, `ollama_client.py`'s existing contract).
3. `budgets.py:CancellationToken` threaded into
   `ollama_client.py:OllamaAdapter.generate()` -- cancelling mid-stream
   raises `OllamaCallCancelled` within a bounded number of chunks (no real
   sleep), closes the stream (the existing `finally: close()` path), and a
   run built around it transitions to `cancelled`, never `completed`.

Kept in the Postgres-only test suite (this codebase's `_postgres`
convention, matching Task 1/2's own `test_ai_runtime_routing_postgres.py`/
`test_ai_runtime_validation_postgres.py`) even where an individual test
touches no database, for discoverability alongside those files.
"""

import json
from uuid import uuid4

import httpx
import pytest

from ecc.config import get_settings
from ecc.database import SessionFactory
from ecc.domains.ai_runtime import router as air
from ecc.domains.ai_runtime.budgets import (
    CancellationToken,
    CircuitBreaker,
    RunBudget,
    RunBudgetExceeded,
    RunGuard,
    candidate_state_for,
    check_input_token_budget,
    check_output_token_budget,
    reflection_enabled,
)
from ecc.domains.ai_runtime.ollama_client import (
    DEFAULT_PER_MODEL_CALL_TIMEOUT_SECONDS,
    Chunk,
    OllamaAdapter,
    OllamaCallCancelled,
)
from ecc.domains.ai_runtime.registry import ModelDefinition

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

SEEDED_MODEL_ID = "qwen2.5:1.5b-instruct-q4_K_M"
SEEDED_TASK_TYPE = "attention.explain_item"


# ---------------------------------------------------------------------------
# CircuitBreaker -- state machine
# ---------------------------------------------------------------------------


class _FakeClock:
    """A settable fake clock, matching `ollama_client.py`'s own injectable
    `clock` convention -- multi-second transitions exercised without a
    real sleep.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def test_circuit_breaker_starts_closed() -> None:
    breaker = CircuitBreaker()
    assert breaker.state == "closed"


def test_circuit_breaker_closed_to_open_after_three_consecutive_failures() -> None:
    clock = _FakeClock()
    breaker = CircuitBreaker(clock=clock)
    breaker.record_failure()
    assert breaker.state == "closed"
    breaker.record_failure()
    assert breaker.state == "closed"
    breaker.record_failure()
    assert breaker.state == "open"


def test_circuit_breaker_success_resets_the_consecutive_failure_streak() -> None:
    """An interleaved success resets the streak -- two failures, a
    success, then two more failures never reaches 3 *consecutive*
    failures, so the breaker stays closed.
    """
    clock = _FakeClock()
    breaker = CircuitBreaker(clock=clock)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == "closed"


def test_circuit_breaker_failures_outside_rolling_window_do_not_accumulate() -> None:
    """Failures more than 60s apart never accumulate toward the
    3-consecutive threshold -- the rolling window ages old failures out.
    """
    clock = _FakeClock()
    breaker = CircuitBreaker(clock=clock)
    breaker.record_failure()
    clock.advance(61.0)
    breaker.record_failure()
    clock.advance(61.0)
    breaker.record_failure()
    assert breaker.state == "closed"


def test_circuit_breaker_open_to_half_open_after_30s_cooldown() -> None:
    clock = _FakeClock()
    breaker = CircuitBreaker(clock=clock)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == "open"

    clock.advance(29.9)
    assert breaker.state == "open"

    clock.advance(0.2)  # total 30.1s -- past the 30s cool-down
    assert breaker.state == "half_open"


def test_circuit_breaker_half_open_to_closed_on_one_probe_success() -> None:
    clock = _FakeClock()
    breaker = CircuitBreaker(clock=clock)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_failure()
    clock.advance(30.0)
    assert breaker.state == "half_open"

    breaker.record_success()
    assert breaker.state == "closed"


def test_circuit_breaker_half_open_to_open_on_one_probe_failure() -> None:
    clock = _FakeClock()
    breaker = CircuitBreaker(clock=clock)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_failure()
    clock.advance(30.0)
    assert breaker.state == "half_open"

    breaker.record_failure()
    assert breaker.state == "open"

    # Reopened for another 30s cool-down -- not yet elapsed.
    clock.advance(29.0)
    assert breaker.state == "open"
    clock.advance(1.5)
    assert breaker.state == "half_open"


# ---------------------------------------------------------------------------
# CircuitBreaker <-> router.py integration (Task 1 Step 1 extended, no
# changes to router.py itself).
# ---------------------------------------------------------------------------


def _model(model_id: str = "local-model") -> ModelDefinition:
    return ModelDefinition(
        id=uuid4(),
        provider="ollama",
        model_id=model_id,
        deployment="local",
        data_classes=("public", "internal", "sensitive", "restricted"),
        capabilities=("explanation",),
        context_window_tokens=32768,
        structured_output_supported=True,
        status="active",
    )


def _ctx() -> air.ContextEstimate:
    return air.ContextEstimate(estimated_prompt_tokens=1000, declared_max_output_tokens=512)


def test_open_circuit_breaker_excludes_candidate_from_router_eligibility() -> None:
    """A real `CircuitBreaker`, driven open by 3 consecutive failures,
    excludes its candidate via `router.route()`'s existing (unmodified)
    eligibility step 5 -- `candidate_state_for` is the only glue needed.
    """
    clock = _FakeClock()
    breaker = CircuitBreaker(clock=clock)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state == "open"

    candidate = _model()
    decision = air.route(
        SEEDED_TASK_TYPE,
        "sensitive",
        _ctx(),
        candidates=[candidate],
        candidate_states={candidate.model_id: candidate_state_for(breaker)},
    )
    assert isinstance(decision, air.NoEligibleCandidate)
    assert decision.reason == "circuit_open"


def test_closed_circuit_breaker_does_not_exclude_candidate() -> None:
    breaker = CircuitBreaker()
    candidate = _model()
    decision = air.route(
        SEEDED_TASK_TYPE,
        "sensitive",
        _ctx(),
        candidates=[candidate],
        candidate_states={candidate.model_id: candidate_state_for(breaker)},
    )
    assert isinstance(decision, air.RoutingDecision)


def test_half_open_circuit_breaker_does_not_exclude_candidate() -> None:
    """`half_open` is a probe state, not an exclusion state -- matches
    Task 1's `test_eligibility_step5_half_open_is_not_excluded`, here
    reached by a real breaker's cool-down transition instead of a hand-set
    `CandidateState`.
    """
    clock = _FakeClock()
    breaker = CircuitBreaker(clock=clock)
    breaker.record_failure()
    breaker.record_failure()
    breaker.record_failure()
    clock.advance(30.0)
    assert breaker.state == "half_open"

    candidate = _model()
    decision = air.route(
        SEEDED_TASK_TYPE,
        "sensitive",
        _ctx(),
        candidates=[candidate],
        candidate_states={candidate.model_id: candidate_state_for(breaker)},
    )
    assert isinstance(decision, air.RoutingDecision)


def test_candidate_state_for_preserves_other_fields() -> None:
    """`candidate_state_for` only ever resolves `health_state` off the
    breaker -- every other `CandidateState` field passes straight through.
    """
    breaker = CircuitBreaker()
    state = candidate_state_for(
        breaker,
        observed_p95_latency_seconds=1.5,
        remaining_budget=42.0,
        evaluation_quality_floor_passed=True,
        observed_cost=0.0,
    )
    assert state.health_state == "closed"
    assert state.observed_p95_latency_seconds == 1.5
    assert state.remaining_budget == 42.0
    assert state.evaluation_quality_floor_passed is True


# ---------------------------------------------------------------------------
# RunBudget -- built from the real seeded routing_policies row, no second
# hardcoded copy of Decision 5's five numbers.
# ---------------------------------------------------------------------------


def _seeded_policy() -> air.RoutingPolicy:
    with SessionFactory() as session:
        policy = air.get_policy(session, SEEDED_TASK_TYPE)
    assert policy is not None
    return policy


def test_run_budget_from_policy_reads_the_seeded_routing_policy_row() -> None:
    """Every one of Decision 5's five numbers, read from the real
    `routing_policies` row migration `0028_phase4_model_registry.py`
    seeds -- not a second hardcoded copy.
    """
    budget = RunBudget.from_policy(_seeded_policy())
    assert budget.total_wall_clock_seconds == 60.0
    assert budget.per_model_call_seconds == 20.0
    assert budget.per_tool_call_seconds == 5.0
    assert budget.max_input_tokens == 3072
    assert budget.max_output_tokens == 512


def test_run_budget_per_model_call_and_max_output_tokens_match_task_requirements() -> None:
    """`per_model_call_seconds`/`max_output_tokens` are read from
    `router.py:TASK_REQUIREMENTS` (that field's other Python-side owner),
    not independently retyped from `policy.constraints` -- proving the two
    cannot silently diverge.
    """
    requirements = air.TASK_REQUIREMENTS[SEEDED_TASK_TYPE]
    budget = RunBudget.from_policy(_seeded_policy())
    assert budget.per_model_call_seconds == requirements.timeout_seconds
    assert budget.max_output_tokens == requirements.max_output_tokens


def test_run_budget_per_model_call_seconds_matches_ollama_adapter_default_timeout() -> None:
    """Cross-checks the budget's per-model-call number against
    `ollama_client.py`'s own default timeout constant -- another place
    this same number must not silently drift.
    """
    budget = RunBudget.from_policy(_seeded_policy())
    assert budget.per_model_call_seconds == DEFAULT_PER_MODEL_CALL_TIMEOUT_SECONDS


def test_run_budget_from_policy_falls_back_to_constraints_for_unregistered_task_type() -> None:
    """A task type with no `TASK_REQUIREMENTS` entry falls back to
    `policy.constraints` for every field -- proves `from_policy` does not
    hard-require a `TASK_REQUIREMENTS` entry to exist.
    """
    synthetic_policy = air.RoutingPolicy(
        id=uuid4(),
        task_type="unregistered.task",
        version=1,
        candidates=[],
        constraints={
            "total_run_budget_seconds": 60,
            "per_model_call_timeout_seconds": 20,
            "per_tool_call_timeout_seconds": 5,
            "max_input_tokens": 3072,
            "max_output_tokens": 512,
        },
        fallback={},
        status="active",
    )
    budget = RunBudget.from_policy(synthetic_policy)
    assert budget.per_model_call_seconds == 20.0
    assert budget.max_output_tokens == 512


# ---------------------------------------------------------------------------
# reflection_enabled -- the Reflection Engine (first slice) gating switch,
# read from the exact same routing_policies.constraints column RunBudget
# already reads its five budget numbers from (migration
# 0033_phase4_reflection.py, default false).
# ---------------------------------------------------------------------------


def _policy_with_constraints(constraints: dict) -> air.RoutingPolicy:
    return air.RoutingPolicy(
        id=uuid4(),
        task_type="synthetic.task",
        version=1,
        candidates=[],
        constraints=constraints,
        fallback={},
        status="active",
    )


def test_reflection_enabled_reads_the_seeded_routing_policy_row() -> None:
    """Migration `0033_phase4_reflection.py` seeds `reflection_enabled:
    false` on the real `attention.explain_item` policy row -- proving
    this reads the live seeded value, not a hardcoded default only.
    """
    assert reflection_enabled(_seeded_policy()) is False


def test_reflection_enabled_true_when_constraint_set_true() -> None:
    policy = _policy_with_constraints({"reflection_enabled": True})
    assert reflection_enabled(policy) is True


def test_reflection_enabled_false_when_constraint_set_false() -> None:
    policy = _policy_with_constraints({"reflection_enabled": False})
    assert reflection_enabled(policy) is False


def test_reflection_enabled_defaults_false_when_key_absent() -> None:
    """A `routing_policies` row that predates migration
    `0033_phase4_reflection.py` (or otherwise omits the key) must not
    silently enable an unproven, un-configured reflection call.
    """
    policy = _policy_with_constraints({})
    assert reflection_enabled(policy) is False


# ---------------------------------------------------------------------------
# Pre-call input-token rejection -- reuses router.py:ContextEstimate.
# ---------------------------------------------------------------------------


def test_prompt_exceeding_max_input_tokens_rejected_before_model_call() -> None:
    budget = RunBudget.from_policy(_seeded_policy())
    over_budget = air.ContextEstimate(
        estimated_prompt_tokens=budget.max_input_tokens + 1, declared_max_output_tokens=512
    )
    with pytest.raises(RunBudgetExceeded) as exc_info:
        check_input_token_budget(over_budget, budget)
    assert exc_info.value.status == "failed"


def test_prompt_within_max_input_tokens_is_not_rejected() -> None:
    budget = RunBudget.from_policy(_seeded_policy())
    within_budget = air.ContextEstimate(
        estimated_prompt_tokens=budget.max_input_tokens, declared_max_output_tokens=512
    )
    check_input_token_budget(within_budget, budget)  # does not raise


def test_input_token_rejection_happens_before_any_model_call_is_attempted() -> None:
    """The rejection is a pure pre-call check against `ContextEstimate` --
    no `OllamaAdapter`/transport is even constructed in this test, proving
    nothing resembling a model call could have been attempted first.
    """
    budget = RunBudget.from_policy(_seeded_policy())
    over_budget = air.ContextEstimate(
        estimated_prompt_tokens=budget.max_input_tokens + 500, declared_max_output_tokens=512
    )
    with pytest.raises(RunBudgetExceeded):
        check_input_token_budget(over_budget, budget)


# ---------------------------------------------------------------------------
# Output-token budget -- verifies the existing num_predict contract, then
# the application-level safety-net guard.
# ---------------------------------------------------------------------------


def _ndjson_response(parts: list[dict]) -> httpx.Response:
    body = "\n".join(json.dumps(part) for part in parts) + "\n"
    return httpx.Response(
        200, content=body.encode(), headers={"content-type": "application/x-ndjson"}
    )


def test_num_predict_is_set_to_the_declared_max_output_tokens() -> None:
    """Confirms the existing contract `check_output_token_budget` is a
    safety net *for*: `ollama_client.py` already asks Ollama to stop
    generating at `budget.max_output_tokens` via `options.num_predict`.
    """
    budget = RunBudget.from_policy(_seeded_policy())
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return _ndjson_response(
            [{"model": "m", "created_at": "now", "response": "ok", "done": True, "eval_count": 1}]
        )

    adapter = OllamaAdapter(transport=httpx.MockTransport(handler))
    list(adapter.generate("explain this item", SEEDED_MODEL_ID, budget.max_output_tokens))

    payload = json.loads(captured[0].content)
    assert payload["options"]["num_predict"] == budget.max_output_tokens


def test_output_within_budget_is_not_flagged() -> None:
    budget = RunBudget.from_policy(_seeded_policy())
    check_output_token_budget(eval_count=budget.max_output_tokens, budget=budget)  # no raise
    check_output_token_budget(eval_count=None, budget=budget)  # non-final chunk -- no-op


def test_output_exceeding_max_output_tokens_flagged_by_application_guard() -> None:
    """The safety net: even if a model somehow ignored `num_predict` and
    produced more than the declared budget, `check_output_token_budget`
    catches it against the real streamed response's `eval_count`.
    """
    budget = RunBudget.from_policy(_seeded_policy())

    def handler(request: httpx.Request) -> httpx.Response:
        return _ndjson_response(
            [
                {
                    "model": "m",
                    "created_at": "now",
                    "response": "way too much output",
                    "done": True,
                    "eval_count": budget.max_output_tokens + 88,
                }
            ]
        )

    adapter = OllamaAdapter(transport=httpx.MockTransport(handler))
    chunks = list(adapter.generate("hi", SEEDED_MODEL_ID, budget.max_output_tokens))
    final = chunks[-1]
    assert final.done

    with pytest.raises(RunBudgetExceeded) as exc_info:
        check_output_token_budget(eval_count=final.eval_count, budget=budget)
    assert exc_info.value.status == "degraded"


# ---------------------------------------------------------------------------
# RunGuard -- total wall-clock budget: cancelled/degraded, never left
# running past it.
# ---------------------------------------------------------------------------


def test_run_guard_starts_running() -> None:
    guard = RunGuard(RunBudget.from_policy(_seeded_policy()))
    assert guard.status == "running"


def test_run_guard_within_budget_stays_running() -> None:
    clock = _FakeClock()
    guard = RunGuard(RunBudget.from_policy(_seeded_policy()), clock=clock)
    clock.advance(59.0)
    guard.check_total_budget()  # does not raise
    assert guard.status == "running"


def test_run_guard_exceeding_total_budget_marks_degraded_never_left_running() -> None:
    clock = _FakeClock()
    guard = RunGuard(RunBudget.from_policy(_seeded_policy()), clock=clock)
    clock.advance(61.0)
    with pytest.raises(RunBudgetExceeded) as exc_info:
        guard.check_total_budget()
    assert guard.status == "degraded"
    assert guard.status != "running"
    assert exc_info.value.status == "degraded"


def test_run_guard_exceeding_total_budget_cancels_the_token() -> None:
    clock = _FakeClock()
    guard = RunGuard(RunBudget.from_policy(_seeded_policy()), clock=clock)
    token = CancellationToken()
    clock.advance(61.0)
    with pytest.raises(RunBudgetExceeded):
        guard.check_total_budget(token)
    assert token.is_cancelled()
    assert token.reason == "total_run_budget_exceeded"


def test_run_guard_degraded_run_cannot_be_completed_afterwards() -> None:
    """Once `degraded`, a later `complete()` call (e.g. a race against a
    final orchestration step) must never flip the run back to
    `completed`.
    """
    clock = _FakeClock()
    guard = RunGuard(RunBudget.from_policy(_seeded_policy()), clock=clock)
    clock.advance(61.0)
    with pytest.raises(RunBudgetExceeded):
        guard.check_total_budget()
    guard.complete()
    assert guard.status == "degraded"


def test_run_guard_complete_marks_completed_when_still_running() -> None:
    guard = RunGuard(RunBudget.from_policy(_seeded_policy()))
    guard.complete()
    assert guard.status == "completed"


def test_run_guard_tool_call_exceeding_per_tool_budget_marks_degraded() -> None:
    guard = RunGuard(RunBudget.from_policy(_seeded_policy()))
    with pytest.raises(RunBudgetExceeded) as exc_info:
        guard.check_tool_call_duration(5.1)
    assert guard.status == "degraded"
    assert exc_info.value.status == "degraded"


def test_run_guard_tool_call_within_per_tool_budget_does_not_raise() -> None:
    guard = RunGuard(RunBudget.from_policy(_seeded_policy()))
    guard.check_tool_call_duration(4.9)  # does not raise
    assert guard.status == "running"


# ---------------------------------------------------------------------------
# CancellationToken -- cooperative, checked before each step.
# ---------------------------------------------------------------------------


def test_cancellation_token_starts_not_cancelled() -> None:
    token = CancellationToken()
    assert token.is_cancelled() is False
    assert token.reason is None


def test_cancellation_token_cancel_is_idempotent_keeps_first_reason() -> None:
    token = CancellationToken()
    token.cancel(reason="first")
    token.cancel(reason="second")
    assert token.is_cancelled() is True
    assert token.reason == "first"


# ---------------------------------------------------------------------------
# CancellationToken threaded into ollama_client.py's streaming generate().
# ---------------------------------------------------------------------------


def _many_chunk_handler(request: httpx.Request) -> httpx.Response:
    parts = [
        {"model": "m", "created_at": "now", "response": f"tok{i}", "done": False} for i in range(50)
    ]
    parts.append({"model": "m", "created_at": "now", "response": "", "done": True})
    return _ndjson_response(parts)


def test_cancellation_mid_stream_raises_within_a_bounded_number_of_chunks() -> None:
    """Cancelling after a few chunks stops the stream well before all 51
    chunks are drained -- checked at the same per-chunk cadence as the
    existing deadline guard (Task 1's mocked-transport fixture, no live
    Ollama, no new mocking approach)."""
    token = CancellationToken()
    adapter = OllamaAdapter(
        host="http://localhost:11434", transport=httpx.MockTransport(_many_chunk_handler)
    )

    received: list[Chunk] = []
    with pytest.raises(OllamaCallCancelled):
        for index, chunk in enumerate(adapter.generate("hi", "m", 100, cancellation_token=token)):
            received.append(chunk)
            if index == 2:
                token.cancel(reason="operator_cancelled")

    # Cancellation is observed on the very next chunk boundary -- proves
    # the stream did not keep draining after the token flipped.
    assert len(received) == 3
    assert 0 < len(received) < 51


def test_cancellation_token_none_leaves_generate_unaffected() -> None:
    """The default `cancellation_token=None` behaves exactly like Task 1's
    already-committed `generate()` -- no cancellation path is ever taken.
    """
    adapter = OllamaAdapter(transport=httpx.MockTransport(_many_chunk_handler))
    chunks = list(adapter.generate("hi", "m", 100))
    assert len(chunks) == 51
    assert chunks[-1].done is True


def test_run_never_transitions_to_completed_after_cancellation() -> None:
    """A `CancellationToken.cancel()` mid-stream results in the run
    transitioning to `cancelled`, never `completed` -- exercised end to
    end against the mocked transport, a real `CancellationToken`, and a
    real `RunGuard`.
    """
    token = CancellationToken()
    guard = RunGuard(RunBudget.from_policy(_seeded_policy()))
    adapter = OllamaAdapter(transport=httpx.MockTransport(_many_chunk_handler))

    try:
        for index, _chunk in enumerate(adapter.generate("hi", "m", 100, cancellation_token=token)):
            if index == 1:
                token.cancel(reason="operator_cancelled")
    except OllamaCallCancelled:
        guard.cancel()

    assert guard.status == "cancelled"
    assert guard.status != "completed"

    # A subsequent stray complete() call (e.g. an orchestration loop's own
    # cleanup path racing the cancellation) must never overwrite it.
    guard.complete()
    assert guard.status == "cancelled"
