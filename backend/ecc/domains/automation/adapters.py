"""Composition root for the shared production adapter registry
(`docs/superpowers/specs/2026-07-25-phase-5-automation-design.md` Decision
8, resolving `docs/phases/PHASE-REVIEW.md`'s F-03).

**The contract itself (`ActionAdapter`, `AdapterRegistry`,
`TransientAdapterError`, `HIGH_IMPACT_CATEGORIES`, `compensable`/
`call_compensate`) lives in `adapter_contract.py`, not here** -- this
module re-exports all of it below so every existing `from ecc.domains.
automation.adapters import ...` call site keeps working unchanged. This
module's own job is narrower: import every concrete `ActionAdapter`
implementation from wherever it actually lives (`local_adapters.py` in
this package; `engineering.write_actions` for the GitHub/GitLab/Jira write
adapters) and register each one into the one shared `registry` instance
`ecc.domains.automation.worker` resolves a workflow step's `action_ref`
against.

**Why the split (architecture review, 2026-09-07, CAR-1).** Before this
split, this single module both defined `TransientAdapterError` and
imported `engineering.write_actions`'s three concrete adapters to register
them -- while `write_actions.py` needed `TransientAdapterError` back,
producing a genuine two-way import cycle closed only by importing that
exception function-locally inside `write_actions.py`, documented there at
length. Moving the contract to a separate, dependency-free `adapter_contract.
py` removes the cycle unconditionally: `write_actions.py` now imports
`TransientAdapterError` from `adapter_contract` at its own top level, this
module still imports `write_actions`'s concrete adapters (that direction
was always the intended one -- a composition root importing implementations
to wire them up), and nothing here is imported back by `write_actions.py`
or any other adapter-implementing module.

Task 2's own tests (`tests/test_automation_worker_postgres.py`) still
define their own minimal fake adapters directly in that test module rather
than importing the three real ones here, matching how Task 1's own tests
constructed workspace/user fixtures directly rather than through a product
registration flow -- unchanged by this task.
"""

from .adapter_contract import (
    HIGH_IMPACT_CATEGORIES,
    ActionAdapter,
    AdapterAlreadyRegistered,
    AdapterCategoryInvalid,
    AdapterRegistry,
    TransientAdapterError,
    call_compensate,
    compensable,
)

__all__ = [
    "HIGH_IMPACT_CATEGORIES",
    "ActionAdapter",
    "AdapterAlreadyRegistered",
    "AdapterCategoryInvalid",
    "AdapterRegistry",
    "TransientAdapterError",
    "call_compensate",
    "compensable",
    "registry",
]

# Phase 6 Engineering Workspace Task 7 ("Approved write actions") --
# GitHub/GitLab/Jira write adapters. Importing them here, at this
# composition root's own top, is now a plain, ordinary import: this module
# depends on `write_actions.py`, but (as of the CAR-1 split above)
# `write_actions.py` no longer depends on this module at all, only on the
# contract-only `adapter_contract.py` -- so there is no cycle to order
# around. See `write_actions.py`'s own docstring for the full scope,
# containment, and retry-safety reasoning behind these three adapters.
from ecc.domains.engineering.write_actions import (  # noqa: E402
    GitHubAddIssueCommentAdapter,
    GitLabAddNoteAdapter,
    JiraAddCommentAdapter,
)

from .local_adapters import (  # noqa: E402
    FakeExternalActionAdapter,
    LocalCreateNoteAdapter,
    LocalSendTestNotificationAdapter,
)

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

# Phase 6 Engineering Workspace Task 7 ("Approved write actions") --
# GitHub/GitLab/Jira write adapters. See that module's own docstring for
# the full scope, containment, and retry-safety reasoning behind these
# three adapters.
registry.register(GitHubAddIssueCommentAdapter())
registry.register(GitLabAddNoteAdapter())
registry.register(JiraAddCommentAdapter())
