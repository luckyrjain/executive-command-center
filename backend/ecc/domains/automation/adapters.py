"""Connector-independent action-adapter contract and in-process registry
(`docs/superpowers/specs/2026-07-25-phase-5-automation-design.md` Decision
8, resolving `docs/phases/PHASE-REVIEW.md`'s F-03).

**This module (Task 2) defines the contract shape**; `local_adapters.py`
(Task 5, "Connector action adapters and sandbox tests") implements the
three local/fake adapters this activation registers -- `local.create_note`,
`local.send_test_notification`, `fake.external_action` -- and this module's
own bottom section performs the actual `registry.register(...)` calls, so
the shared production `registry` instance below is no longer empty as of
Task 5. Every future adapter (this activation's own three; Phase 6's
production GitHub/GitLab/Jira connectors) must satisfy the `ActionAdapter`
Protocol below, and the small in-process registry `ecc.domains.automation.
worker` resolves a workflow step's `action_ref` against is exactly this
module's `registry`. Task 2's own tests (`tests/test_automation_worker_
postgres.py`) still define their own minimal fake adapters directly in that
test module rather than importing the three real ones here, matching how
Task 1's own tests constructed workspace/user fixtures directly rather than
through a product registration flow -- unchanged by this task.

**Contract shape (design doc Decision 8, verbatim).** An adapter declares:
`adapter_id` (str), `input_schema`/`output_schema` (both Pydantic models --
the same structured-output validation idiom Phase 4's `ai_runtime.runtime`
already uses for tool input/output, no new validation mechanism), `reversible`
(bool), `high_impact_categories` (`frozenset[str]`, a subset of
`HIGH_IMPACT_CATEGORIES` below, Decision 5's closed enumeration -- static
per adapter, evaluated at registration time, never computed at runtime
from a step's input), `simulate(action_input) -> BaseModel` and
`execute(action_input) -> BaseModel`. `compensate(action_input) ->
BaseModel` is deliberately **not** part of the `ActionAdapter` protocol
itself -- Decision 9 makes it optional, present only for an adapter with a
genuine compensating action, so a caller checks `hasattr(adapter,
"compensate")` rather than every adapter being forced to implement a
no-op. `ActionAdapter` is a `typing.Protocol`, not an ABC -- matching this
codebase's existing preference for structural typing over an inheritance
hierarchy where the shape, not the lineage, is what a registered object
must satisfy (a fake test adapter needs to be *shaped* like an adapter, it
does not need to inherit from one).

**`simulate()` is never reachable from a real-execution code path** --
that guarantee is `ecc.domains.automation.worker`'s responsibility (it is
the only caller of `execute()`, and nothing in this task's own scope calls
`simulate()` at all, since the simulation entrypoint itself is Decision
4's `/simulate` endpoint, out of this task's scope). This module only
shapes the contract both methods must satisfy; it does not enforce that an
adapter author's own `simulate()` body is actually side-effect-free
(design doc Decision 4's own stated, honest limitation -- "an adapter-
author contract obligation the runtime cannot mechanically prove").
"""

from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from .local_adapters import (
    FakeExternalActionAdapter,
    LocalCreateNoteAdapter,
    LocalSendTestNotificationAdapter,
)

# design doc Decision 5 / `docs/phases/phase-005/APPROVAL-POLICY.md`'s
# closed, seven-category high-impact action taxonomy. `policy-limit-
# exceeding` is the one cross-cutting category evaluated per-run against
# the authorizing policy (design doc Decision 5), not statically declared
# by an adapter the way the other six are -- included here anyway so
# `AdapterRegistry.register`'s validation below (a real, not merely
# documented, guard) accepts it without special-casing.
HIGH_IMPACT_CATEGORIES: frozenset[str] = frozenset(
    {
        "destructive",
        "financial",
        "legal",
        "credential",
        "person-directed",
        "public",
        "policy-limit-exceeding",
    }
)


@runtime_checkable
class ActionAdapter(Protocol):
    """Structural contract every registered action adapter satisfies
    (design doc Decision 8). `@runtime_checkable` so `AdapterRegistry.
    register` can `isinstance()`-check a candidate object before accepting
    it, catching a malformed test fake at registration time rather than at
    first dispatch.
    """

    adapter_id: str
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    reversible: bool
    high_impact_categories: frozenset[str]

    def simulate(self, action_input: BaseModel) -> BaseModel:
        """Must not perform the real side effect, by contract (Decision 4)
        -- returns a preview of what `execute()` would do.
        """
        ...

    def execute(self, action_input: BaseModel) -> BaseModel:
        """The real side effect. `ecc.domains.automation.worker.run_step`
        is this activation's only caller.
        """
        ...


class AdapterAlreadyRegistered(ValueError):
    """Raised by `AdapterRegistry.register` when `adapter_id` is already
    taken -- a registration-time programming error (two adapters cannot
    share an `action_ref`), not a runtime condition a workflow author's
    graph can trigger.
    """


class AdapterCategoryInvalid(ValueError):
    """Raised by `AdapterRegistry.register` when `high_impact_categories`
    contains a value outside `HIGH_IMPACT_CATEGORIES` -- fail-closed at
    registration time (design doc Decision 5: "An adapter that cannot
    classify itself into any category still defaults to requiring per-run
    approval") rather than silently accepting an adapter author's typo
    that a later policy-evaluation task would otherwise trust blindly.
    """


class AdapterRegistry:
    """A small in-process `dict[str, ActionAdapter]`-backed registry
    (design doc Decision 8) `ecc.domains.automation.worker` resolves a
    workflow step's `action_ref` against. Deliberately an instance, not a
    single module-level global a test would have to mutate-and-restore --
    each test builds its own registry with whatever fake adapter(s) it
    needs (this task's own instruction), and the shared production
    registry below (`registry`) is simply one particular empty instance no
    product adapter is registered into by this task.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, ActionAdapter] = {}

    def register(self, adapter: ActionAdapter) -> None:
        if not isinstance(adapter, ActionAdapter):
            raise TypeError(
                f"object registered for adapter_id={adapter.adapter_id!r} does not satisfy "
                "the ActionAdapter protocol (missing a required attribute or method)"
            )
        if adapter.adapter_id in self._by_id:
            raise AdapterAlreadyRegistered(
                f"adapter_id '{adapter.adapter_id}' is already registered"
            )
        invalid = adapter.high_impact_categories - HIGH_IMPACT_CATEGORIES
        if invalid:
            raise AdapterCategoryInvalid(
                f"adapter '{adapter.adapter_id}' declares unknown high_impact_categories "
                f"{sorted(invalid)}; must be a subset of {sorted(HIGH_IMPACT_CATEGORIES)}"
            )
        self._by_id[adapter.adapter_id] = adapter

    def get(self, adapter_id: str) -> ActionAdapter | None:
        return self._by_id.get(adapter_id)

    def adapter_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._by_id))

    def __contains__(self, adapter_id: object) -> bool:
        return adapter_id in self._by_id

    def __len__(self) -> int:
        return len(self._by_id)


def compensable(adapter: ActionAdapter) -> bool:
    """Whether `adapter` declares a genuine compensating action (Decision
    9) -- `hasattr` is the actual mechanism (`compensate` is deliberately
    absent from `ActionAdapter` itself, see module docstring), this
    function exists only so a caller does not need to know that detail.
    """
    return hasattr(adapter, "compensate") and callable(adapter.compensate)


def call_compensate(adapter: ActionAdapter, action_input: BaseModel) -> BaseModel:
    """Invoke `adapter.compensate(action_input)` -- raises `AttributeError`
    if `adapter` does not declare one; callers must check `compensable`
    first (matches this codebase's existing "check before calling" style
    for other optional capabilities rather than a silent no-op).
    """
    compensate: Any = adapter.compensate  # type: ignore[attr-defined]
    result: BaseModel = compensate(action_input)
    return result


# Shared production registry. Task 2 left this empty by design (module
# docstring, historical); Task 5 ("Connector action adapters and sandbox
# tests") registers this activation's three local/fake adapters into this
# exact instance, at import time, below -- the one place the design doc's
# own text ("a later task registers ... into this exact instance") points
# to. Every other test module in this package (Task 2-4's own worker/
# policy/approvals/scheduler/runs tests) still builds its own private
# `AdapterRegistry()` instances for their own test-only fakes instead of
# mutating this one, so those test runs never depend on import order or
# leak fakes into a shared global -- unchanged by this task. `tests/
# test_automation_adapters_postgres.py` (Task 5, new) is the one test
# module that deliberately dispatches through this exact shared `registry`
# instance, proving the three real, now-registered adapters resolve and
# execute through the actual worker dispatch path, not a private test
# double standing in for them.
registry = AdapterRegistry()
registry.register(LocalCreateNoteAdapter())
registry.register(LocalSendTestNotificationAdapter())
registry.register(FakeExternalActionAdapter())
