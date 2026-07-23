"""Budgets, timeouts, cancellation and circuit breakers (design doc
Decision 5's table: routing overhead, per-model-call/per-tool-call
timeouts, total run budget, max input/output tokens, circuit breaker,
cancellation).

**No new numeric copy of Decision 5's five budget numbers.** `RunBudget`
is never constructed with literal `60`/`20`/`5`/`3072`/`512` in this
module -- `RunBudget.from_policy` reads all five from the exact same
source Task 1 already established: `router.py:RoutingPolicy.constraints`
(the `routing_policies` row migration `0028_phase4_model_registry.py`
seeds). Two of those five (`per_model_call_seconds`/`max_output_tokens`)
also have a second, independently-typed Python-side owner already --
`router.py:TASK_REQUIREMENTS[task_type]`, which the eligibility pipeline
reads for an unrelated purpose (routing preference, not budget
enforcement) -- so `from_policy` prefers `TASK_REQUIREMENTS` for exactly
those two fields when an entry exists, rather than letting the constraints
row and `TASK_REQUIREMENTS` silently diverge into two different "the
timeout" answers.

**Circuit breaker <-> router.py integration (no change to router.py).**
`router.py`'s `_eligible` step 5 already reads `state.health_state ==
"open"` off a plain `CandidateState` field -- that field already accepts
exactly the `HealthState` literal (`"closed"/"open"/"half_open"`) this
module's `CircuitBreaker.state` returns. `candidate_state_for` below is
the glue: it builds a `CandidateState` whose `health_state` is read live
off a real breaker, so a caller (Task 4's orchestration loop, and this
module's own tests) plugs a live breaker straight into `router.route()`'s
existing, already-committed, already-tested eligibility pipeline with zero
edits to `router.py` itself.

**Cancellation <-> ollama_client.py integration.** `ollama_client.py`'s
`generate()` accepts an optional `CancellationToken` and checks
`is_cancelled()` at the same point, on the same per-chunk cadence, as its
existing wall-clock deadline check -- raising `OllamaCallCancelled`, which
unwinds through the existing `finally: close()` cleanup (no second
stream-closing mechanism is introduced; see that module's docstring).
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from .router import (
    TASK_REQUIREMENTS,
    CandidateState,
    ContextEstimate,
    HealthState,
    RoutingPolicy,
    TaskRequirements,
)

RunStatus = Literal["running", "completed", "cancelled", "degraded", "failed"]

# design doc Decision 5's circuit breaker row.
_FAILURE_THRESHOLD = 3
_ROLLING_WINDOW_SECONDS = 60.0
_HALF_OPEN_COOLDOWN_SECONDS = 30.0


class RunBudgetExceeded(Exception):
    """Raised by `RunGuard`/the standalone budget-check helpers below when
    a Decision 5 limit is exceeded. `status` is the `RunStatus` the caller
    should record -- `"degraded"` for a total-wall-clock overrun (Decision
    5: "a run exceeding this is cancelled and marked degraded/failed,
    never left running"), `"failed"` for a pre-call rejection (nothing
    ever started, so there is nothing to degrade).
    """

    def __init__(self, message: str, *, status: RunStatus = "failed") -> None:
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# CircuitBreaker -- three-state, per design doc Decision 5.
# ---------------------------------------------------------------------------


class CircuitBreaker:
    """Standard three-state circuit breaker for one `model_definitions`
    row (design doc Decision 5): `closed` -> `open` after 3 *consecutive*
    failures inside a rolling 60s window; `open` -> `half_open` after a
    30s cool-down; `half_open` -> `closed` on one probe success;
    `half_open` -> `open` (for another 30s) on one probe failure.

    "Consecutive" is enforced by clearing the failure history on every
    success while closed -- an interleaved success resets the streak, it
    does not merely fail to increment it. The open -> half_open transition
    is computed lazily off `clock()` (not a timer callback) every time
    `state`/`record_success`/`record_failure` is read, matching this
    module's -- and `ollama_client.py`'s -- existing convention of an
    injectable `clock` so tests exercise multi-second transitions without
    a real sleep.

    Not a `@dataclass` like this module's/`router.py`'s value types --
    a breaker is mutable coordination state accessed from concurrent
    call paths (a model call's failure, `router.route()`'s eligibility
    read), so it owns a lock, unlike this codebase's frozen value objects.
    """

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._state: HealthState = "closed"
        self._failure_timestamps: list[float] = []
        self._opened_at: float | None = None

    def _maybe_cool_down(self) -> None:
        """Must be called with `self._lock` held. Applies the lazy
        `open` -> `half_open` transition if the 30s cool-down has
        elapsed.
        """
        if self._state == "open" and self._opened_at is not None:
            if self._clock() - self._opened_at >= _HALF_OPEN_COOLDOWN_SECONDS:
                self._state = "half_open"

    @property
    def state(self) -> HealthState:
        """The breaker's current state, exactly the `HealthState` literal
        `router.py:CandidateState.health_state`/`_eligible` step 5 already
        expect -- see `candidate_state_for` below for how a live value here
        reaches `router.route()`.
        """
        with self._lock:
            self._maybe_cool_down()
            return self._state

    def record_success(self) -> None:
        """A successful call (or a successful half-open probe)."""
        with self._lock:
            self._maybe_cool_down()
            if self._state == "half_open":
                self._state = "closed"
                self._opened_at = None
            # "Consecutive" failures reset on any success, whether the
            # breaker was closed or was a half-open probe that just
            # closed it.
            self._failure_timestamps = []

    def record_failure(self) -> None:
        """A failed call (or a failed half-open probe)."""
        with self._lock:
            self._maybe_cool_down()
            now = self._clock()
            if self._state == "half_open":
                # One probe failure reopens the breaker for another 30s
                # cool-down -- design doc Decision 5.
                self._state = "open"
                self._opened_at = now
                self._failure_timestamps = [now]
                return

            # Rolling 60s window: drop failures older than the window
            # before appending this one, so failures more than 60s apart
            # never accumulate toward the 3-consecutive threshold.
            self._failure_timestamps = [
                ts for ts in self._failure_timestamps if now - ts <= _ROLLING_WINDOW_SECONDS
            ]
            self._failure_timestamps.append(now)
            if len(self._failure_timestamps) >= _FAILURE_THRESHOLD:
                self._state = "open"
                self._opened_at = now


def candidate_state_for(
    breaker: CircuitBreaker,
    *,
    observed_p95_latency_seconds: float | None = None,
    remaining_budget: float = float("inf"),
    evaluation_quality_floor_passed: bool = False,
    observed_cost: float = 0.0,
) -> CandidateState:
    """Build a `router.py:CandidateState` whose `health_state` is read
    live off `breaker.state` -- the shape this module's Task 3 tests (and
    Task 4's future orchestration loop) use to feed a real circuit
    breaker's state into `router.route()`'s already-committed, already-
    tested eligibility step 5 (`_eligible`: `state.health_state ==
    "open"`), with zero changes to `router.py` itself. The other
    `CandidateState` fields are passed straight through -- this function
    only ever resolves `health_state`, matching `CandidateState`'s own
    "everything else defaults to healthy/unconstrained" precedent.
    """
    return CandidateState(
        health_state=breaker.state,
        observed_p95_latency_seconds=observed_p95_latency_seconds,
        remaining_budget=remaining_budget,
        evaluation_quality_floor_passed=evaluation_quality_floor_passed,
        observed_cost=observed_cost,
    )


# ---------------------------------------------------------------------------
# RunBudget -- design doc Decision 5's five numbers, read from
# routing_policies.constraints (migration 0028_phase4_model_registry.py),
# never a second hardcoded copy.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunBudget:
    """One run's Decision 5 budget: 60s total wall clock, 20s per-model-
    call, 5s per-tool-call, 3072 max input tokens, 512 max output tokens
    (for `attention.explain_item`, this activation's only task type).
    Always built via `from_policy` -- the dataclass constructor itself
    takes no defaults, precisely so nothing in this module can silently
    fall back to an undocumented literal instead of the seeded policy row.
    """

    total_wall_clock_seconds: float
    per_model_call_seconds: float
    per_tool_call_seconds: float
    max_input_tokens: int
    max_output_tokens: int

    @classmethod
    def from_policy(
        cls, policy: RoutingPolicy, *, task_requirements: TaskRequirements | None = None
    ) -> RunBudget:
        """Build a `RunBudget` from a task type's active `routing_policies`
        row (`policy.constraints`) -- the single source of truth for all
        five Decision 5 numbers.

        `per_model_call_seconds`/`max_output_tokens` are read from
        `router.py:TASK_REQUIREMENTS[policy.task_type]` instead of
        `policy.constraints` whenever an entry exists there (this
        activation always registers one for `attention.explain_item`) --
        those two fields already have a Python-side owner in `router.py`
        for the eligibility pipeline's own purposes, so this avoids a
        second, independently-typed copy of the same two numbers that
        could silently diverge from the routing pipeline's own view of
        them. `task_requirements` is resolved automatically from
        `policy.task_type`; the parameter exists only so a caller (or a
        test) can supply a synthetic one without needing a real
        `TASK_REQUIREMENTS` entry.

        The other three (`total_wall_clock_seconds`, `per_tool_call_seconds`,
        `max_input_tokens`) have no other Python-side owner yet -- they
        come from `policy.constraints` directly, using the exact key names
        migration `0028_phase4_model_registry.py` seeds
        (`total_run_budget_seconds`, `per_tool_call_timeout_seconds`,
        `max_input_tokens`).
        """
        constraints: dict[str, Any] = policy.constraints
        requirements = (
            task_requirements
            if task_requirements is not None
            else TASK_REQUIREMENTS.get(policy.task_type)
        )
        per_model_call_seconds = (
            requirements.timeout_seconds
            if requirements is not None
            else float(constraints["per_model_call_timeout_seconds"])
        )
        max_output_tokens = (
            requirements.max_output_tokens
            if requirements is not None
            else int(constraints["max_output_tokens"])
        )
        return cls(
            total_wall_clock_seconds=float(constraints["total_run_budget_seconds"]),
            per_model_call_seconds=per_model_call_seconds,
            per_tool_call_seconds=float(constraints["per_tool_call_timeout_seconds"]),
            max_input_tokens=int(constraints["max_input_tokens"]),
            max_output_tokens=max_output_tokens,
        )


def check_input_token_budget(context_estimate: ContextEstimate, budget: RunBudget) -> None:
    """Reject a prompt **before** the model call is attempted if its
    pre-call estimate already exceeds `budget.max_input_tokens` (Decision
    5's 3072 cap) -- reuses `router.py:ContextEstimate`, the exact same
    pre-call estimate object `router.route()`'s eligibility step 4 already
    computes and checks against a *candidate's* `context_window_tokens`
    (with a 90% margin) for a routing-eligibility purpose. This function
    checks the same `estimated_prompt_tokens` field against the task-wide
    budget cap instead -- a different question over the same shared
    estimate, not a second, disconnected token-counting mechanism.

    Raises `RunBudgetExceeded(status="failed")` -- nothing has run yet, so
    there is nothing to degrade, matching Decision 5's "rejected before
    the model call is attempted".
    """
    if context_estimate.estimated_prompt_tokens > budget.max_input_tokens:
        raise RunBudgetExceeded(
            f"estimated prompt tokens {context_estimate.estimated_prompt_tokens} "
            f"exceeds the {budget.max_input_tokens} max_input_tokens budget",
            status="failed",
        )


def check_output_token_budget(*, eval_count: int | None, budget: RunBudget) -> None:
    """Application-level guard for Decision 5's 512 max output tokens.

    `ollama_client.py` already asks Ollama to stop generating at
    `budget.max_output_tokens` via `options={"num_predict": max_tokens}`
    on every `generate()` call -- the model itself is the primary
    enforcement mechanism, not reinvented here. This function is the
    fallback safety net for the case that contract does not hold (a model
    that does not honor `num_predict`, or a future non-Ollama provider,
    design doc Decision 8's later slice, that maps the option
    differently): checked against the final streamed chunk's `eval_count`
    (Ollama's own reported output-token count), `None` on every
    non-final chunk and therefore a no-op until the stream actually
    completes.
    """
    if eval_count is None:
        return
    if eval_count > budget.max_output_tokens:
        raise RunBudgetExceeded(
            f"model produced {eval_count} output tokens, exceeding the "
            f"{budget.max_output_tokens} max_output_tokens budget",
            status="degraded",
        )


# ---------------------------------------------------------------------------
# RunGuard -- tracks one run's status/wall-clock budget. Not the
# orchestration loop itself (Task 4's runtime.py, not built here) -- just
# the reusable status/budget primitive that loop will wrap each run in.
# ---------------------------------------------------------------------------


class RunGuard:
    """Tracks one run's `RunStatus` against `budget.total_wall_clock_seconds`
    (Decision 5: "a run exceeding this is cancelled and marked
    degraded/failed, never left running"). Deliberately not a dataclass --
    `status`/the start time are run-local mutable state a single in-flight
    run owns, not an immutable value.
    """

    def __init__(self, budget: RunBudget, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._budget = budget
        self._clock = clock
        self._started_at = clock()
        self.status: RunStatus = "running"

    def elapsed_seconds(self) -> float:
        return self._clock() - self._started_at

    def check_total_budget(self, cancellation_token: CancellationToken | None = None) -> None:
        """Raise `RunBudgetExceeded(status="degraded")` and mark this run
        `degraded` if the total wall-clock budget has been exceeded --
        checked cooperatively, e.g. before each step of an orchestration
        loop (this module's cancellation-token check placement precedent).
        Cancels `cancellation_token` too, when supplied, so an in-flight
        model call closes its stream rather than being left to finish on
        its own (design doc Decision 5's "closes the stream mid-
        generation" cancellation mechanism).
        """
        if self.status != "running":
            return
        if self.elapsed_seconds() > self._budget.total_wall_clock_seconds:
            self.status = "degraded"
            if cancellation_token is not None:
                cancellation_token.cancel(reason="total_run_budget_exceeded")
            raise RunBudgetExceeded(
                f"run exceeded the {self._budget.total_wall_clock_seconds}s total "
                "wall-clock budget",
                status="degraded",
            )

    def check_tool_call_duration(self, elapsed_seconds: float) -> None:
        """Raise `RunBudgetExceeded(status="degraded")` if a single tool
        call ran past `budget.per_tool_call_seconds` (Decision 5's 5s
        cap). No tool executor exists yet (Task 4's `runtime.py`) -- this
        is the reusable check that loop will call once it does.
        """
        if elapsed_seconds > self._budget.per_tool_call_seconds:
            self.status = "degraded"
            raise RunBudgetExceeded(
                f"tool call ran {elapsed_seconds:.2f}s, exceeding the "
                f"{self._budget.per_tool_call_seconds}s per-tool-call budget",
                status="degraded",
            )

    def complete(self) -> None:
        """Mark `completed` -- only takes effect while still `running`, so
        a run already `cancelled`/`degraded`/`failed` can never be
        overwritten back to `completed` by a caller that races the final
        step against a budget/cancellation check.
        """
        if self.status == "running":
            self.status = "completed"

    def cancel(self) -> None:
        """Mark `cancelled` -- the terminal state a run reaches when
        `CancellationToken.cancel()` interrupts an in-flight model call
        (`ollama_client.py:OllamaCallCancelled`), distinct from
        `degraded`/`failed`, which are budget-exceeded outcomes."""
        self.status = "cancelled"


# ---------------------------------------------------------------------------
# CancellationToken -- cooperative, checked before each step; threaded
# into ollama_client.py's streaming generate() call.
# ---------------------------------------------------------------------------


class CancellationToken:
    """Cooperative cancellation flag (design doc Decision 5's
    "Cancellation" row). Checked before each step of an orchestration
    loop (Task 4) and, specifically, inside `ollama_client.py:generate()`'s
    per-chunk loop -- at the same point, on the same cadence, as that
    loop's existing wall-clock deadline check -- so a cancellation closes
    the stream mid-generation instead of waiting for a non-preemptible
    call to finish (design doc Decision 5's stated reason for using
    Ollama's streaming endpoint at all).

    Deliberately not a frozen dataclass like this module's/`router.py`'s
    value types: a `CancellationToken` **is** shared mutable coordination
    state -- the same reason `threading.Event` itself is mutable -- and
    more than one component (an operator-triggered cancel endpoint,
    `RunGuard.check_total_budget`, `ollama_client.py`'s per-chunk loop)
    all have to observe the same flip on the same instance.
    """

    __slots__ = ("_event", "_reason")

    def __init__(self) -> None:
        self._event = threading.Event()
        self._reason: str | None = None

    def cancel(self, reason: str = "cancelled") -> None:
        """Request cancellation. Idempotent: a second call never
        overwrites the first call's `reason` -- whichever component
        cancelled first is the one whose reason is recorded.
        """
        if not self._event.is_set():
            self._reason = reason
        self._event.set()

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def reason(self) -> str | None:
        return self._reason
