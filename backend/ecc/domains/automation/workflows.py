"""Workflow definition/version reads, drafting and activation
(`workflow_definitions`/`workflow_versions`), plus
`GET|POST /api/v1/automations/workflows`, `GET /api/v1/automations/
workflows/{id}` and `POST /api/v1/automations/workflows/{id}/publish|disable`
(`docs/phases/phase-005/API-SCHEMAS.md`).

Reuses Phase 4's immutable-versioning idiom exactly (`ecc.domains.ai_runtime.
prompts` -- see migration `0038_phase5_workflow_schema.py`'s module
docstring for the two deliberate divergences: workspace-scoped, and a
separate `workflow_definitions` identity table so `automation_policies`/
`triggers` have a stable FK target for "which workflow family").
`definition_hash` is `sha256` over the canonical UTF-8 sorted-object-keys
JSON bytes of `{graph, trigger_refs, policy_ref}` (design doc Decision 2),
computed here and kept immutable post-draft by
`trg_workflow_versions_immutability` (not only by this module declining to
expose an edit path). Editing a workflow always inserts a new
`workflow_versions` row at `version = previous + 1` -- there is no update
path for `graph`/`trigger_refs`/`policy_ref`/`definition_hash` anywhere in
this module; `activate_workflow_version`/`disable_workflow_version` only
ever write `status`/`updated_at`, the two columns the trigger never guards,
so neither function can ever collide with the trigger it depends on.

Every function here takes `workspace_id` as an explicit parameter and every
query filters by it -- no function in this module (and no endpoint below)
accepts or derives a workspace scope from anything other than the caller's
own `AuthContext`, closing the confused-deputy path the design doc's Threat
model section names (`API-SCHEMAS.md`: "no endpoint accepts a caller-
supplied `workspace_id` override").
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from hashlib import sha256
from json import dumps
from typing import Annotated, Any, Literal, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session
from ecc.observability import queue_lifecycle_event
from ecc.platform import audit_outbox, authz
from ecc.platform.idempotency import load_cached, lock_idempotency, request_hash, store_idempotency
from ecc.platform.request_models import EmptyBody as _EmptyBody

from .adapter_contract import ACTION_TYPES, DATA_CLASSES, dispatch_value, has_dispatch_value
from .adapters import ActionAdapter, AdapterRegistry
from .adapters import registry as _production_adapter_registry
from .approvals import evaluate_approval_requirement, evaluate_policy_scope
from .policy import AutomationPolicy, get_policy, is_policy_usable, policy_status

# Task 7a: safe to import `.adapters`/`.policy`/`.approvals` here -- confirmed
# directly, none of those three modules imports `workflows.py` or `worker.py`
# back (only `worker.py` imports `workflows.py`; importing `worker.py` itself
# from here would be the one real circular-import risk, which is exactly why
# `_redact_payload`/`SimulateWorkspaceScopeMismatch` below are small,
# deliberate, disclosed duplicates of `worker.py`'s own equivalents rather
# than imports of them -- see their own docstrings).

WorkflowStatus = Literal["draft", "active", "retired"]
_TERMINAL_OUTCOMES = frozenset({"succeeded", "failed", "compensating"})
_STEP_TYPES = frozenset({"action", "approval_gate", "condition", "compensation"})

_WORKFLOW_VERSION_FIELDS = """
    id, workspace_id, workflow_id, version, graph, trigger_refs, policy_ref,
    definition_hash, status, created_by, updated_by, created_at, updated_at
"""


@dataclass(frozen=True, slots=True)
class WorkflowVersion:
    id: UUID
    workspace_id: UUID
    workflow_id: str
    version: int
    graph: dict[str, Any]
    trigger_refs: tuple[str, ...]
    policy_ref: UUID | None
    definition_hash: str
    status: WorkflowStatus
    created_by: UUID
    updated_by: UUID
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class WorkflowVersionNotFound:
    """No `workflow_versions` row matches the given lookup key -- distinct
    from "not currently active" (`get_active_workflow_version` returning
    `None`), mirroring `ai_runtime.prompts.PromptVersionNotFound`.
    """


@dataclass(frozen=True, slots=True)
class WorkflowVersionNotDraft:
    """`activate_workflow_version` was asked to publish a row whose status
    is `retired` -- publishing only ever promotes a `draft` row (an already-
    `active` target is a no-op, handled separately; there is no "republish a
    retired version" path -- Decision 2's own append-only editing rule means
    a workflow author must create a new draft version instead).
    """

    workflow_id: str
    version: int
    status: WorkflowStatus


@dataclass(frozen=True, slots=True)
class WorkflowVersionNotActive:
    """`disable_workflow_version` was asked to retire a row that is not
    currently `active` -- the mechanical source of `API-SCHEMAS.md`'s
    required `workflow_not_active` error code within this task's scope.
    """

    workflow_id: str
    version: int
    status: WorkflowStatus


@dataclass(frozen=True, slots=True)
class WorkflowVersionUnregisteredAdapter:
    """`activate_workflow_version` was asked to publish a version whose
    graph names an `action_ref` (on an `action`/`compensation` step) that
    does not resolve in the adapter registry it was given -- Task 7a's own
    gap closure. `API-SCHEMAS.md` already states, as an already-decided
    guarantee, that "a workflow with an adapter that cannot be simulated (no
    `simulate()` implementation) is rejected at `publish` time, not
    discovered mid-simulation" -- but `validate_graph_shape` never actually
    checked that `action_ref` resolves to anything at all, and every object
    `AdapterRegistry.register` accepts already satisfies the `ActionAdapter`
    Protocol, which requires *both* `simulate()` and `execute()` (`adapters.
    py`'s own docstring). So there is no registered-but-`simulate()`-less
    adapter this activation's own contract can produce -- the only way a
    step's adapter "cannot be simulated" is if `action_ref` does not resolve
    in the registry at all. This dataclass is the mechanical source of the
    narrower, real check this task closes.
    """

    workflow_id: str
    version: int
    violations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkflowVersionHighImpactCompensationAdapter:
    """`activate_workflow_version` was asked to publish a version whose
    graph contains a `step_type='compensation'` step whose own `action_ref`
    resolves, in the adapter registry it was given, to an adapter declaring
    a non-empty `high_impact_categories` -- rejected fail-closed at publish
    time by `high_impact_compensation_action_refs` below.

    **The gate-bypass bug this exists to make unreachable by construction.**
    Every ordinary `action` step passes through `worker._evaluate_dispatch_
    gate` before its adapter's `execute()` is ever called, and that gate
    turns any adapter with a non-empty `high_impact_categories` into a
    mandatory per-run `approval_requests` row (`APPROVAL-POLICY.md`'s
    high-impact taxonomy: "always requires per-run approval regardless of
    policy mode"). `worker._dispatch_compensation_step` -- the *only* code
    path that dispatches a `step_type='compensation'` step -- deliberately
    does not run that gate at all: compensation is an automatic
    continuation of a failing run's own rollback, not a fresh, separately
    approvable human-facing action, and there is no approval-inbox surface
    for a compensation step to wait in. That is a defensible design for a
    *low-impact* undo action, but before this check nothing anywhere
    constrained which adapter a compensation step's `action_ref` could
    name: `validate_graph_shape` only constrains a compensation step's
    *shape* (its target is claimed by exactly one action step, is itself
    `step_type='compensation'`, is not itself a compensation target), and
    `unregistered_action_refs` only requires the `action_ref` resolve to
    *something* registered. So a workflow author could publish a graph
    whose low-impact `s0` declared `compensate_ref='c0'` where `c0` named a
    high-impact adapter (e.g. `local.send_test_notification`,
    `high_impact_categories={'person-directed'}`); once any later step in
    that run failed, `c0` would call that adapter's real `execute()` with
    **no** `approval_requests` row ever created -- a direct violation of
    the "always requires per-run approval" invariant and of `EXECUTION-
    CONTRACT.md`'s "compensation executes only declared, authorized steps".

    Rejecting the *graph* is the fix, not adding a dispatch-time
    high-impact check: a structural, publish-time rejection makes the
    bypass unreachable for every adapter that will ever be registered
    (including Phase 6's real connectors), whereas a dispatch-time check
    could only ever fail a run that already reached rollback, leaving an
    already-published graph as a standing liability. `worker._dispatch_
    compensation_step` additionally re-checks policy usability at dispatch
    time (`PolicyUnusableDuringCompensation`), which covers the different
    failure mode this check cannot: a policy revoked *after* an
    already-legal graph was published.
    """

    workflow_id: str
    version: int
    violations: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkflowVersionActionOutsidePolicyScope:
    """`activate_workflow_version` (with a registry) refused to publish a
    version an action or compensation step of which names an adapter its
    own enforced `policy_ref` does not authorize (`approvals.evaluate_
    policy_scope`; scope-enforcement design, Decision 5 item 1). Each
    violation is `{step_id, action_ref, reason}`. Catches authoring errors
    before any run exists; the dispatch gate stays authoritative."""

    workflow_id: str
    version: int
    violations: tuple[dict[str, str], ...]


def action_refs_outside_policy_scope(
    graph: dict[str, Any], adapter_registry: AdapterRegistry, policy: AutomationPolicy
) -> list[dict[str, str]]:
    """Every `action`/`compensation` step whose registered adapter `policy`
    does not authorize. A compensation step is checked unconditionally --
    deliberately stricter than dispatch, where its own adapter only runs
    when the original adapter has no `compensate()`: publish cannot cheaply
    tell which branch will run, and an out-of-scope rollback action is an
    authoring error either way. Unregistered refs are skipped (a separate
    check owns them)."""
    violations: list[dict[str, str]] = []
    for step in graph.get("steps", []):
        if step.get("step_type") not in ("action", "compensation"):
            continue
        adapter = adapter_registry.get(step.get("action_ref") or "")
        if adapter is None:
            continue
        reason = evaluate_policy_scope(adapter, policy)
        if reason is not None:
            violations.append(
                {
                    "step_id": str(step.get("step_id")),
                    "action_ref": adapter.adapter_id,
                    "reason": reason,
                }
            )
    return violations


def high_impact_compensation_action_refs(
    graph: dict[str, Any], adapter_registry: AdapterRegistry
) -> list[str]:
    """No `step_type='compensation'` step's own `action_ref` may resolve to
    an adapter declaring a non-empty `high_impact_categories` -- returns a
    list of violation strings, empty means every compensation step names a
    non-high-impact adapter. See `WorkflowVersionHighImpactCompensation
    Adapter`'s own docstring for the full reasoning (the approval-gate
    bypass this makes structurally unreachable).

    A plain function alongside `unregistered_action_refs`, for the identical
    reason that one is not folded into `validate_graph_shape`: this check
    needs a live `AdapterRegistry` to answer "what does this `action_ref`
    actually declare," while `validate_graph_shape` is deliberately pure and
    runs at *draft* creation time, when an author's intended adapter may not
    even be registered yet. Like `unregistered_action_refs`, this therefore
    runs at *publish* time only (`activate_workflow_version` below).

    An `action_ref` that does not resolve at all is deliberately **not** a
    violation here -- `unregistered_action_refs` already rejects that case,
    with its own distinct error code, and `activate_workflow_version` runs
    it first; reporting the same graph defect twice under two codes would
    make the 422 a caller sees depend on check ordering rather than on what
    is actually wrong.
    """
    violations: list[str] = []
    for step in graph.get("steps", []):
        if step.get("step_type") != "compensation":
            continue
        action_ref = step.get("action_ref")
        if not action_ref:
            continue
        adapter: ActionAdapter | None = adapter_registry.get(action_ref)
        if adapter is None:
            continue
        categories = adapter.high_impact_categories
        if categories:
            violations.append(
                f"step '{step.get('step_id')}': action_ref '{action_ref}' declares "
                f"high_impact_categories {sorted(categories)}; a step_type='compensation' "
                "step must name a non-high-impact adapter, because compensation dispatch "
                "never creates a per-run approval request"
            )
    return violations


def unregistered_action_refs(graph: dict[str, Any], adapter_registry: AdapterRegistry) -> list[str]:
    """Every `action`/`compensation` step's own `action_ref` must resolve in
    `adapter_registry` -- returns a list of violation strings, empty means
    every `action_ref` resolves. A plain function (mirrors `validate_graph_
    shape`'s own "kept as a plain function so both the HTTP layer and any
    future non-HTTP caller can reuse it" rationale) rather than folded into
    `validate_graph_shape` itself, since this check needs a live
    `AdapterRegistry` (a runtime, mutable-in-theory concern) while `validate_
    graph_shape` is deliberately pure/context-free and runs at *draft*
    creation time, before an author's intended adapter may even be
    registered yet -- this check instead runs at *publish* time
    (`activate_workflow_version` below), matching `API-SCHEMAS.md`'s own
    "rejected at publish time" wording exactly, not at draft time.
    """
    violations: list[str] = []
    for step in graph.get("steps", []):
        step_type = step.get("step_type")
        if step_type not in {"action", "compensation"}:
            continue
        action_ref = step.get("action_ref")
        if action_ref and action_ref not in adapter_registry:
            violations.append(
                f"step '{step.get('step_id')}': action_ref '{action_ref}' is not a "
                "registered adapter"
            )
    return violations


@dataclass(frozen=True, slots=True)
class WorkflowSummary:
    workflow_id: str
    latest_version: int
    latest_status: WorkflowStatus
    active_version: int | None
    # Task 7: a real, disclosed gap found and closed while building the
    # frontend against this endpoint -- `GET /workflows/{version_id}`
    # (below) is addressed by a version row's own UUID, never by
    # `(workflow_id, version)`, but this summary previously exposed only
    # the bare version *number*, giving a caller (a workflow-list UI) no
    # way to link from this list into that detail endpoint at all. Adding
    # the two version rows' own row ids this summary already has on hand
    # (it already loaded every version row to compute `latest`/`active`)
    # is a small, additive field addition, not a new query or endpoint --
    # mirrors Task 7a's own "small, disclosed, additive gap closure found
    # while building the thing that needs it" precedent (`kill_switches.
    # py`'s `GET .../kill_switch`, `/simulate`).
    latest_version_id: UUID
    active_version_id: UUID | None


def compute_definition_hash(
    *, graph: dict[str, Any], trigger_refs: list[str] | tuple[str, ...], policy_ref: UUID | None
) -> str:
    """`sha256` over the canonical (UTF-8, sorted-object-keys) JSON bytes of
    `{graph, trigger_refs, policy_ref}` -- design doc Decision 2's hashing
    scheme, identical in form to `ai_runtime.prompts.compute_template_hash`.
    """
    material = {
        "graph": graph,
        "trigger_refs": list(trigger_refs),
        "policy_ref": str(policy_ref) if policy_ref is not None else None,
    }
    canonical = dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(canonical.encode("utf-8")).hexdigest()


def validate_graph_shape(graph: dict[str, Any]) -> list[str]:
    """Structural validation beyond what Pydantic's field types already
    check on `WorkflowGraphModel` below (unique `step_id`s, every `on_
    success`/`on_failure` resolves to either a terminal outcome or a
    strictly-later step -- design doc Decision 2: "finite, acyclic and ...
    strictly sequential", "no loops"). Returns a list of violation strings;
    empty means valid. Kept as a plain function (not folded into the
    Pydantic model) so both the HTTP layer and any future non-HTTP caller
    (the Task 2 worker validating a graph before dispatch) can reuse it.

    **Task 6 addition: `compensate_ref` structural rules (Decision 9).**
    Four rules, checked per step: (1) `compensate_ref` is only meaningful on
    a `step_type == "action"` step -- an `approval_gate`/`condition`/
    `compensation` step declaring one is rejected (a compensation step
    itself is not something further compensation-chains, and `approval_
    gate`/`condition` steps have no side effect of their own to undo in this
    activation's scope); (2) it must resolve to a `step_id` that actually
    exists in the same graph; (3) that target step's own `step_type` must
    be `"compensation"` (a `compensate_ref` cannot silently point at an
    ordinary action step, which would let a graph's own main sequence
    "accidentally" execute what was meant to be a rollback-only action);
    (4) a `step_type == "compensation"` step must never itself be the
    target of any `on_success`/`on_failure` edge (compensation is reachable
    *only* through the worker's own explicit compensation-dispatch path,
    Decision 9: "invoked only when ... explicitly marks ... never automatic
    or inferred" -- routing the main sequence into one would let an author
    accidentally treat it as an ordinary next step) and must never itself
    declare a `compensate_ref` (no nested compensation, matching this
    task's own scope boundary -- Decision 9 describes one level of
    compensation, not a chain); (5) a given `step_type == "compensation"`
    step must be the `compensate_ref` target of at most one action step.
    `worker._dispatch_compensation_step` idempotency-gates a compensation
    dispatch on `workflow_run_steps`' own `(run_id, step_index)` key --
    the compensation step's *graph position*, not the pair of (compensation
    step, original step) -- exactly like every other step in this table.
    If two action steps declared the same `compensate_ref`, the second
    step's dispatch would find the first's row already `succeeded` at that
    `step_index` and return early without ever calling `compensate()` or
    writing a `compensation_steps` ledger row for the second original step
    -- a silent, unrecorded skip a human reviewing `compensation_steps`
    would have no way to notice, discovered during this task's own review.
    Rejecting the shared target at graph-validation time (an author who
    genuinely needs to undo two steps' effects the same way authors two
    separate `step_type='compensation'` steps, even if both name the same
    `action_ref`) is the correct fix here, not changing the dispatch
    idempotency key -- that key's exact shape is the same
    `(run_id, step_index)` invariant every other step in this table
    depends on, and loosening it would be a far larger, riskier change than
    this activation's simple, one-compensation-target-per-step graph model
    needs.

    **Not checked here, deliberately: which adapter a compensation step's
    own `action_ref` names.** The five rules above are all about graph
    *shape* -- answerable from the graph alone, which is exactly why this
    function can stay pure and run at draft time. The sixth rule a
    compensation step must satisfy (it may not name an adapter declaring a
    non-empty `high_impact_categories`, since `worker._dispatch_
    compensation_step` never creates a per-run approval request the way
    `worker._evaluate_dispatch_gate` does for an ordinary action step)
    cannot be answered without a live `AdapterRegistry`, so it lives in
    `high_impact_compensation_action_refs` above and runs at publish time
    alongside `unregistered_action_refs`. A reader adding a new
    compensation-step constraint should decide between the two on exactly
    that basis: pure/graph-only goes here, registry-dependent goes there.
    """
    violations: list[str] = []
    steps = graph.get("steps", [])
    step_ids = [step.get("step_id") for step in steps]
    seen: set[str] = set()
    duplicates: set[str] = set()
    for step_id in step_ids:
        if step_id in seen:
            duplicates.add(step_id)
        seen.add(step_id)
    if duplicates:
        violations.append(f"duplicate step_id(s): {sorted(duplicates)}")

    index_by_id = {step_id: index for index, step_id in enumerate(step_ids)}
    step_type_by_id = {
        step_id: step.get("step_type") for step_id, step in zip(step_ids, steps, strict=True)
    }

    compensation_target_claimed_by: dict[str, str] = {}
    for index, step in enumerate(steps):
        step_type = step.get("step_type")
        action_ref = step.get("action_ref")
        step_id = step.get("step_id")
        if step_type in {"action", "compensation"} and not action_ref:
            violations.append(f"step '{step_id}': action_ref required for {step_type}")
        for edge_name in ("on_success", "on_failure"):
            target = step.get(edge_name)
            if target is None:
                continue
            if target in _TERMINAL_OUTCOMES:
                continue
            target_index = index_by_id.get(target)
            if target_index is None:
                violations.append(
                    f"step '{step_id}': {edge_name} references unknown step '{target}'"
                )
            elif target_index <= index:
                violations.append(
                    f"step '{step_id}': {edge_name} references '{target}', "
                    "which is not strictly later -- graphs must be acyclic and sequential"
                )
            elif step_type_by_id.get(target) == "compensation":
                violations.append(
                    f"step '{step_id}': {edge_name} references '{target}', a "
                    "step_type='compensation' step -- compensation is reachable only "
                    "through the worker's own explicit compensation-dispatch path, "
                    "never as a target of on_success/on_failure"
                )

        compensate_ref = step.get("compensate_ref")
        if compensate_ref is not None:
            if step_type == "compensation":
                violations.append(
                    f"step '{step_id}': step_type='compensation' must not itself declare "
                    "a compensate_ref -- no nested compensation"
                )
            elif step_type != "action":
                violations.append(
                    f"step '{step_id}': compensate_ref is only valid on step_type='action' "
                    f"steps, not '{step_type}'"
                )
            else:
                target_index = index_by_id.get(compensate_ref)
                if target_index is None:
                    violations.append(
                        f"step '{step_id}': compensate_ref references unknown step "
                        f"'{compensate_ref}'"
                    )
                elif step_type_by_id.get(compensate_ref) != "compensation":
                    violations.append(
                        f"step '{step_id}': compensate_ref '{compensate_ref}' must name a "
                        "step_type='compensation' step, not "
                        f"'{step_type_by_id.get(compensate_ref)}'"
                    )
                else:
                    prior_claimant = compensation_target_claimed_by.get(compensate_ref)
                    if prior_claimant is not None:
                        violations.append(
                            f"step '{step_id}': compensate_ref '{compensate_ref}' is already "
                            f"claimed by step '{prior_claimant}' -- a compensation step may be "
                            "the compensate_ref target of at most one action step"
                        )
                    else:
                        compensation_target_claimed_by[compensate_ref] = step_id
    return violations


def _row_to_workflow_version(row: dict[str, Any]) -> WorkflowVersion:
    return WorkflowVersion(
        id=row["id"],
        workspace_id=row["workspace_id"],
        workflow_id=row["workflow_id"],
        version=row["version"],
        graph=row["graph"],
        trigger_refs=tuple(row["trigger_refs"]),
        policy_ref=row["policy_ref"],
        definition_hash=row["definition_hash"],
        status=row["status"],
        created_by=row["created_by"],
        updated_by=row["updated_by"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def get_workflow_version_by_id(
    session: Session, workspace_id: UUID, version_id: UUID
) -> WorkflowVersion | None:
    """Look up a single version row by its own row id, scoped to the
    caller's workspace -- a row belonging to another workspace is
    indistinguishable from a missing one (never raises, never leaks
    existence), matching this codebase's cross-workspace-isolation
    convention (`waiting.py`'s `WAITING_LINK_NOT_FOUND` precedent).
    """
    row = (
        session.execute(
            text(
                f"SELECT {_WORKFLOW_VERSION_FIELDS} FROM workflow_versions "
                "WHERE workspace_id = :workspace_id AND id = :id"
            ),
            {"workspace_id": workspace_id, "id": version_id},
        )
        .mappings()
        .one_or_none()
    )
    return _row_to_workflow_version(dict(row)) if row is not None else None


def get_workflow_version(
    session: Session, workspace_id: UUID, workflow_id: str, version: int
) -> WorkflowVersion | None:
    row = (
        session.execute(
            text(
                f"SELECT {_WORKFLOW_VERSION_FIELDS} FROM workflow_versions "
                "WHERE workspace_id = :workspace_id AND workflow_id = :workflow_id "
                "AND version = :version"
            ),
            {"workspace_id": workspace_id, "workflow_id": workflow_id, "version": version},
        )
        .mappings()
        .one_or_none()
    )
    return _row_to_workflow_version(dict(row)) if row is not None else None


def get_active_workflow_version(
    session: Session, workspace_id: UUID, workflow_id: str
) -> WorkflowVersion | None:
    row = (
        session.execute(
            text(
                f"SELECT {_WORKFLOW_VERSION_FIELDS} FROM workflow_versions "
                "WHERE workspace_id = :workspace_id AND workflow_id = :workflow_id "
                "AND status = 'active'"
            ),
            {"workspace_id": workspace_id, "workflow_id": workflow_id},
        )
        .mappings()
        .one_or_none()
    )
    return _row_to_workflow_version(dict(row)) if row is not None else None


def list_workflows(session: Session, auth: AuthContext) -> list[WorkflowSummary]:
    """One summary per `workflow_id` family in the caller's workspace,
    ordered by slug. Derived entirely from `workflow_versions` (every
    `workflow_definitions` row always has at least one version -- the two
    are inserted together in `create_workflow_draft`), matching `ai_runtime.
    registry.list_models`'s "simple query, reduce in Python" style rather
    than a window-function query, since this activation's per-workspace
    workflow count is small.
    """
    rows = authz.list_visible_resources(
        session,
        auth,
        resource_type="workflow_versions",
        columns=_WORKFLOW_VERSION_FIELDS,
        order_by="workflow_id ASC, version ASC",
    )
    session.rollback()
    by_workflow: dict[str, list[WorkflowVersion]] = {}
    for row in rows:
        version = _row_to_workflow_version(dict(row))
        by_workflow.setdefault(version.workflow_id, []).append(version)

    summaries: list[WorkflowSummary] = []
    for workflow_id, versions in sorted(by_workflow.items()):
        latest = versions[-1]
        active = next((v for v in versions if v.status == "active"), None)
        summaries.append(
            WorkflowSummary(
                workflow_id=workflow_id,
                latest_version=latest.version,
                latest_status=latest.status,
                active_version=active.version if active is not None else None,
                latest_version_id=latest.id,
                active_version_id=active.id if active is not None else None,
            )
        )
    return summaries


def create_workflow_draft(
    session: Session,
    workspace_id: UUID,
    actor_id: UUID,
    *,
    workflow_id: str,
    graph: dict[str, Any],
    trigger_refs: list[str],
    policy_ref: UUID | None,
) -> WorkflowVersion:
    """Creates the `workflow_definitions` family row on first use, then
    inserts a new `draft` `workflow_versions` row at `version = previous +
    1` (or `1` for a brand-new family) -- there is no in-place edit path,
    matching Decision 2 verbatim ("Editing a workflow always inserts a new
    row with `version = previous + 1`").

    Locking (`FOR NO KEY UPDATE` on the family row, `FOR UPDATE` on the
    latest version row when the family already exists) serializes concurrent callers
    deriving the next version number for the same `workflow_id`, mirroring
    `capacity.py`'s `_current_profile(for_update=True)`/`ai_runtime.prompts.
    activate_prompt_version`'s identical race-closing rationale. The
    `(workspace_id, workflow_id, version)` unique constraint is the DB-level
    backstop if two callers still race past the lock under a lower
    isolation level -- the caller (the HTTP endpoint below) converts that
    into a clean 409 rather than a raw `IntegrityError`.

    Does no authorization of its own: it will append to any family in
    `workspace_id`. The HTTP endpoint authorizes the caller against an
    existing family first (`_lock_and_authorize_family`).
    """
    now = datetime.now(UTC)
    family = (
        session.execute(
            text(
                "SELECT id FROM workflow_definitions "
                "WHERE workspace_id = :workspace_id AND workflow_id = :workflow_id "
                "FOR NO KEY UPDATE"
            ),
            {"workspace_id": workspace_id, "workflow_id": workflow_id},
        )
        .mappings()
        .one_or_none()
    )
    if family is None:
        session.execute(
            text(
                """
                INSERT INTO workflow_definitions (
                    id, workspace_id, workflow_id, created_by, created_at, updated_at,
                    owner_id, visibility
                ) VALUES (
                    :id, :workspace_id, :workflow_id, :created_by, :now, :now,
                    :created_by, 'workspace'
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "workflow_id": workflow_id,
                "created_by": actor_id,
                "now": now,
            },
        )
        next_version = 1
    else:
        # Postgres rejects `FOR UPDATE` combined with an aggregate (`MAX`)
        # directly -- lock the single highest-version row instead (`ORDER
        # BY version DESC LIMIT 1 FOR UPDATE`). Every concurrent caller
        # deriving "the next version" for this `workflow_id` reads and
        # locks this same row, so this is an equally effective mutex point
        # without needing to lock every existing version row.
        latest_row = (
            session.execute(
                text(
                    "SELECT version FROM workflow_versions "
                    "WHERE workspace_id = :workspace_id AND workflow_id = :workflow_id "
                    "ORDER BY version DESC LIMIT 1 FOR UPDATE"
                ),
                {"workspace_id": workspace_id, "workflow_id": workflow_id},
            )
            .mappings()
            .one()
        )
        next_version = latest_row["version"] + 1

    definition_hash = compute_definition_hash(
        graph=graph, trigger_refs=trigger_refs, policy_ref=policy_ref
    )
    version_id = uuid4()
    session.execute(
        text(
            """
            INSERT INTO workflow_versions (
                id, workspace_id, workflow_id, version, graph, trigger_refs, policy_ref,
                definition_hash, status, created_by, updated_by, created_at, updated_at,
                owner_id, visibility
            ) VALUES (
                :id, :workspace_id, :workflow_id, :version, CAST(:graph AS jsonb),
                CAST(:trigger_refs AS jsonb), :policy_ref, :definition_hash, 'draft',
                :created_by, :updated_by, :now, :now, :created_by, 'workspace'
            )
            """
        ),
        {
            "id": version_id,
            "workspace_id": workspace_id,
            "workflow_id": workflow_id,
            "version": next_version,
            "graph": dumps(graph),
            "trigger_refs": dumps(list(trigger_refs)),
            "policy_ref": policy_ref,
            "definition_hash": definition_hash,
            "created_by": actor_id,
            "updated_by": actor_id,
            "now": now,
        },
    )
    result = get_workflow_version_by_id(session, workspace_id, version_id)
    assert result is not None  # just inserted, in the same transaction
    return result


def activate_workflow_version(
    session: Session,
    workspace_id: UUID,
    version_id: UUID,
    *,
    adapter_registry: AdapterRegistry | None = None,
) -> (
    WorkflowVersion
    | WorkflowVersionNotFound
    | WorkflowVersionNotDraft
    | WorkflowVersionUnregisteredAdapter
    | WorkflowVersionHighImpactCompensationAdapter
    | WorkflowVersionActionOutsidePolicyScope
):
    """Publish a draft version (design doc Decision 2's activation
    mechanism): retires whichever version is currently `active` for this
    `workflow_id` (if any and if it is not already the target row) and
    marks the target row `active`, each via an `UPDATE` touching only
    `status`/`updated_at` -- never `graph`/`trigger_refs`/`policy_ref`/
    `definition_hash`, the columns `trg_workflow_versions_immutability`
    guards. This function therefore structurally can never collide with
    that trigger, regardless of caller behavior.

    `FOR UPDATE` locks both the target row and the current active row (if
    distinct) for the rest of the caller's transaction, mirroring
    `ai_runtime.prompts.activate_prompt_version`'s identical rationale. The
    partial unique index (`uq_workflow_versions_active_per_workflow`)
    remains the authoritative backstop -- the caller (the HTTP endpoint
    below) catches the rare `IntegrityError` a still-successful race past
    this lock would raise and converts it into a clean 409 rather than
    propagating a raw Postgres exception.

    **Task 7a: `adapter_registry` is optional, deliberately not required.**
    When given, two registry-dependent publish-time checks run, in this
    order: (1) every `action`/`compensation` step's `action_ref` must
    resolve in it or this call returns `WorkflowVersionUnregisteredAdapter`
    instead of activating anything (`unregistered_action_refs`'s own
    docstring has the full reasoning for why this check belongs here, at
    publish time, not at draft time); (2) no `step_type='compensation'`
    step's own `action_ref` may resolve to an adapter declaring a non-empty
    `high_impact_categories`, or this call returns `WorkflowVersion
    HighImpactCompensationAdapter` instead -- the structural, fail-closed
    half of the compensation approval-gate-bypass fix (that class's own
    docstring has the full bug description; `worker._dispatch_compensation_
    step`'s dispatch-time policy re-check is the other half). When omitted
    (`None`, the default), neither check runs -- **not** because they are
    optional in principle, but because this function is called directly (bypassing
    `publish_workflow_endpoint`) by every other Phase 5 task's own test
    suite to publish a workflow naming a test-only, deliberately-
    unregistered `action_ref` (a private `AdapterRegistry()` instance those
    tests build separately, never the shared production `adapters.
    registry`) purely to exercise lifecycle behavior (locking, immutability,
    already-active idempotency) unrelated to this task's own gap. Requiring
    a registry unconditionally would break every one of those pre-existing
    call sites for a property they were never testing. `publish_workflow_
    endpoint` below is the one real caller that matters for this task's own
    guarantee -- it always passes the shared production `adapters.registry`,
    so every *real* publish (the only path a genuine future `/simulate` or
    `/runs` call could ever reach) is checked, while direct, test-only calls
    to this function remain exactly as permissive as they were before this
    task, unless a caller opts in.
    """
    target_row = (
        session.execute(
            text(
                f"SELECT {_WORKFLOW_VERSION_FIELDS} FROM workflow_versions "
                "WHERE workspace_id = :workspace_id AND id = :id FOR UPDATE"
            ),
            {"workspace_id": workspace_id, "id": version_id},
        )
        .mappings()
        .one_or_none()
    )
    if target_row is None:
        return WorkflowVersionNotFound()

    if target_row["status"] == "active":
        return _row_to_workflow_version(dict(target_row))  # already active: a no-op
    if target_row["status"] == "retired":
        return WorkflowVersionNotDraft(
            workflow_id=target_row["workflow_id"],
            version=target_row["version"],
            status=target_row["status"],
        )

    if adapter_registry is not None:
        violations = unregistered_action_refs(target_row["graph"], adapter_registry)
        if violations:
            return WorkflowVersionUnregisteredAdapter(
                workflow_id=target_row["workflow_id"],
                version=target_row["version"],
                violations=tuple(violations),
            )
        # Checked strictly after the unregistered-adapter check above, and
        # only when that one found nothing: an `action_ref` that resolves to
        # nothing at all is that check's concern, not this one, so ordering
        # these two guarantees a caller's 422 names the defect that actually
        # applies (see `high_impact_compensation_action_refs`' own docstring).
        high_impact_violations = high_impact_compensation_action_refs(
            target_row["graph"], adapter_registry
        )
        if high_impact_violations:
            return WorkflowVersionHighImpactCompensationAdapter(
                workflow_id=target_row["workflow_id"],
                version=target_row["version"],
                violations=tuple(high_impact_violations),
            )
        # Scope (third, only with a registry). Skipped -- the dispatch gate
        # stays authoritative -- when there is no policy_ref, or the policy
        # is legacy, revoked or expired: those already block at dispatch
        # with their own reasons, and failing publish for them would be new
        # behaviour outside the scope-enforcement design.
        scope_policy = (
            get_policy(session, workspace_id, target_row["policy_ref"])
            if target_row["policy_ref"] is not None
            else None
        )
        if (
            scope_policy is not None
            and scope_policy.scope_enforced
            and is_policy_usable(scope_policy)
        ):
            scope_violations = action_refs_outside_policy_scope(
                target_row["graph"], adapter_registry, scope_policy
            )
            if scope_violations:
                return WorkflowVersionActionOutsidePolicyScope(
                    workflow_id=target_row["workflow_id"],
                    version=target_row["version"],
                    violations=tuple(scope_violations),
                )

    now = datetime.now(UTC)
    current_active = (
        session.execute(
            text(
                "SELECT id FROM workflow_versions "
                "WHERE workspace_id = :workspace_id AND workflow_id = :workflow_id "
                "AND status = 'active' FOR UPDATE"
            ),
            {"workspace_id": workspace_id, "workflow_id": target_row["workflow_id"]},
        )
        .mappings()
        .one_or_none()
    )
    if current_active is not None:
        session.execute(
            text(
                "UPDATE workflow_versions SET status = 'retired', updated_at = :now WHERE id = :id"
            ),
            {"id": current_active["id"], "now": now},
        )
    session.execute(
        text("UPDATE workflow_versions SET status = 'active', updated_at = :now WHERE id = :id"),
        {"id": target_row["id"], "now": now},
    )
    result = get_workflow_version_by_id(session, workspace_id, version_id)
    assert result is not None
    return result


def disable_workflow_version(
    session: Session, workspace_id: UUID, version_id: UUID
) -> WorkflowVersion | WorkflowVersionNotFound | WorkflowVersionNotActive:
    """Retires an `active` version, leaving `workflow_id` with no active
    version until a later `publish` call. Only ever writes `status`/
    `updated_at`, for the identical reason `activate_workflow_version`
    does -- structurally incapable of tripping the immutability trigger.
    """
    target_row = (
        session.execute(
            text(
                f"SELECT {_WORKFLOW_VERSION_FIELDS} FROM workflow_versions "
                "WHERE workspace_id = :workspace_id AND id = :id FOR UPDATE"
            ),
            {"workspace_id": workspace_id, "id": version_id},
        )
        .mappings()
        .one_or_none()
    )
    if target_row is None:
        return WorkflowVersionNotFound()
    if target_row["status"] != "active":
        return WorkflowVersionNotActive(
            workflow_id=target_row["workflow_id"],
            version=target_row["version"],
            status=target_row["status"],
        )

    now = datetime.now(UTC)
    session.execute(
        text("UPDATE workflow_versions SET status = 'retired', updated_at = :now WHERE id = :id"),
        {"id": target_row["id"], "now": now},
    )
    result = get_workflow_version_by_id(session, workspace_id, version_id)
    assert result is not None
    return result


# --- GET|POST /api/v1/automations/workflows, GET .../{id}, --------------
# --- POST .../{id}/publish|disable ---------------------------------------

router = APIRouter(prefix="/api/v1/automations", tags=["automation"])
SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]

_WORKFLOW_ID_PATTERN = r"^[a-z0-9][a-z0-9._-]{0,198}[a-z0-9]$"


class GraphStepModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_id: str = Field(min_length=1, max_length=100)
    step_type: Literal["action", "approval_gate", "condition", "compensation"]
    action_ref: str | None = Field(default=None, max_length=300)
    input_mapping: dict[str, Any] = Field(default_factory=dict)
    on_success: str | None = Field(default=None, max_length=100)
    on_failure: str | None = Field(default=None, max_length=100)
    # Task 6 addition (Decision 9's compensation model): a real, disclosed
    # gap between two already-merged tasks' own assumptions -- migration
    # 0039's own docstring already refers to `compensate_ref` as this
    # codebase's "existing 'opaque UUID reference into a registry/config
    # table' precedent for `action_ref`/`compensate_ref` inside
    # `workflow_versions.graph` itself", and Decision 9 requires it ("A
    # workflow step may declare a paired `compensate_ref`") -- but no prior
    # task actually added the field to this model. See `validate_graph_
    # shape` below for the structural rules this field must satisfy.
    compensate_ref: str | None = Field(default=None, max_length=100)


class WorkflowGraphModel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    steps: list[GraphStepModel] = Field(min_length=1, max_length=50)


class WorkflowCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    workflow_id: str = Field(pattern=_WORKFLOW_ID_PATTERN)
    graph: WorkflowGraphModel
    trigger_refs: list[str] = Field(default_factory=list, max_length=20)
    policy_ref: UUID | None = None


class WorkflowVersionResponse(BaseModel):
    id: UUID
    workflow_id: str
    version: int
    graph: dict[str, Any]
    trigger_refs: list[str]
    policy_ref: UUID | None
    definition_hash: str
    status: WorkflowStatus
    created_at: datetime
    updated_at: datetime


class WorkflowSummaryResponse(BaseModel):
    workflow_id: str
    latest_version: int
    latest_status: WorkflowStatus
    active_version: int | None
    # Task 7 addition -- see `WorkflowSummary`'s own docstring comment above
    # for the full "why this was missing" reasoning.
    latest_version_id: UUID
    active_version_id: UUID | None


class WorkflowListResponse(BaseModel):
    workflows: list[WorkflowSummaryResponse]


def _to_response(version: WorkflowVersion) -> WorkflowVersionResponse:
    return WorkflowVersionResponse(
        id=version.id,
        workflow_id=version.workflow_id,
        version=version.version,
        graph=version.graph,
        trigger_refs=list(version.trigger_refs),
        policy_ref=version.policy_ref,
        definition_hash=version.definition_hash,
        status=version.status,
        created_at=version.created_at,
        updated_at=version.updated_at,
    )


def lock_active_version(
    session: Session, select_sql: str, lock_clause: str, params: dict[str, Any]
) -> UUID | None:
    """Locks and returns the family's active version id, or `None` when it
    has none. A publish that commits while the locking select waits on the
    row it retires makes that select skip the row (re-checked, no longer
    active) without seeing the newly active one (still a draft in the
    statement's snapshot), so `None` alone does not mean "no active
    version". A fresh, unlocked statement settles it; if it finds one, lock
    that instead. That publish has committed, so locking its row cannot
    deadlock with it. A row the locking select does return is active and
    stays so while the lock is held. Each extra pass needs yet another
    publish to have committed in between, so the loop ends.

    `select_sql` must select one id filtered on `status = 'active'`;
    `lock_clause` is the row lock to take (`FOR UPDATE`, `FOR SHARE`, ...).
    """
    while True:
        locked = session.execute(text(f"{select_sql} {lock_clause}"), params).scalar_one_or_none()
        if locked is not None:
            return cast(UUID, locked)
        if session.execute(text(select_sql), params).scalar_one_or_none() is None:
            return None


def _lock_and_authorize_family(session: Session, auth: AuthContext, workflow_id: str) -> None:
    """Gate appending a draft to an existing `workflow_id` family: the
    caller must be able to read (else `404 WORKFLOW_NOT_FOUND`) and write
    (else `403 INSUFFICIENT_ROLE`) both the family's latest version and its
    active version. The latest is the one the new draft supersedes (and
    whose number the response's `version` discloses); the active one is
    what publishing the new draft would retire. Checking only the latest
    would let a visible draft stacked on someone else's private active
    version open the family. A family that does not exist yet passes: the
    caller is creating it.

    All read checks run before any write check, so a family with any
    version the caller cannot see answers the same 404, never a 403 that
    shows part of it is visible. That 404 is the one an unknown
    `version_id` answers, and carries no version number. A taken slug
    still cannot be created as a new family -- `workflow_id` is unique per
    workspace -- so that one bit is inherent; nothing else is disclosed.

    Must run inside the write transaction, after the membership and
    idempotency locks and before `load_cached` (ADR-0014). The family row
    and both version rows are locked first, so an ownership or visibility
    change committed while this waits is what the checks see.
    `create_workflow_draft` re-locks the same rows (a no-op).
    """
    # NO KEY UPDATE: still serializes concurrent appends to this family,
    # without also waiting on every transaction that holds a foreign-key
    # share lock on it (any write to one of its version rows).
    family = session.execute(
        text(
            "SELECT id FROM workflow_definitions "
            "WHERE workspace_id = :workspace_id AND workflow_id = :workflow_id "
            "FOR NO KEY UPDATE"
        ),
        {"workspace_id": auth.workspace_id, "workflow_id": workflow_id},
    ).one_or_none()
    if family is None:
        return
    params = {"workspace_id": auth.workspace_id, "workflow_id": workflow_id}
    # Latest first, then active: the same order `activate_workflow_version`
    # takes (its target, then the active row), so the two cannot deadlock.
    latest_id = session.execute(
        text(
            "SELECT id FROM workflow_versions "
            "WHERE workspace_id = :workspace_id AND workflow_id = :workflow_id "
            "ORDER BY version DESC LIMIT 1 FOR UPDATE"
        ),
        params,
    ).scalar_one_or_none()
    active_id = lock_active_version(
        session,
        "SELECT id FROM workflow_versions "
        "WHERE workspace_id = :workspace_id AND workflow_id = :workflow_id "
        "AND status = 'active'",
        "FOR UPDATE",
        params,
    )
    version_ids = list(dict.fromkeys(v for v in (latest_id, active_id) if v is not None))
    for version_id in version_ids:
        if not authz.authorize(
            session, auth, resource_type="workflow_versions", resource_id=version_id, action="read"
        ):
            raise HTTPException(status_code=404, detail="WORKFLOW_NOT_FOUND")
    for version_id in version_ids:
        if not authz.authorize(
            session, auth, resource_type="workflow_versions", resource_id=version_id, action="write"
        ):
            raise HTTPException(status_code=403, detail="INSUFFICIENT_ROLE")


def _lock_and_authorize_policy_ref(session: Session, auth: AuthContext, policy_ref: UUID) -> None:
    """A `policy_ref` must name a policy the caller can read: one that does
    not exist and one they cannot see both answer `404 POLICY_NOT_FOUND`,
    so the reference cannot be used to probe for policy ids. Read, not
    write: the draft only points at the policy. `FOR SHARE` holds off a
    visibility or ownership change until this transaction ends.
    """
    locked = session.execute(
        text(
            "SELECT id FROM automation_policies "
            "WHERE workspace_id = :workspace_id AND id = :id FOR SHARE"
        ),
        {"workspace_id": auth.workspace_id, "id": policy_ref},
    ).one_or_none()
    if locked is None or not authz.authorize(
        session, auth, resource_type="automation_policies", resource_id=policy_ref, action="read"
    ):
        raise HTTPException(status_code=404, detail="POLICY_NOT_FOUND")


def _authorize_retiring_active_version(
    session: Session, auth: AuthContext, version_id: UUID
) -> None:
    """Publishing a draft retires the family's current active version, so
    the caller must be able to write that version too, not only the draft
    -- otherwise anyone who owns a draft in a family could switch off the
    version someone else made active. Locked here, before the check, and
    re-locked as a no-op by `activate_workflow_version`.

    A version the caller cannot read answers `403 INSUFFICIENT_ROLE` as
    well, not 404: the caller can read the draft they are publishing, so
    they already know the family exists, and a 404 would misreport that
    visible draft as missing. A 403 here discloses only that the active
    version is not theirs to change.
    """
    active_id = lock_active_version(
        session,
        "SELECT a.id FROM workflow_versions AS a "
        "JOIN workflow_versions AS t "
        "  ON t.workspace_id = a.workspace_id AND t.workflow_id = a.workflow_id "
        "WHERE t.workspace_id = :workspace_id AND t.id = :id "
        "AND a.status = 'active' AND a.id <> t.id",
        "FOR UPDATE OF a",
        {"workspace_id": auth.workspace_id, "id": version_id},
    )
    if active_id is None:
        return
    if not authz.authorize(
        session, auth, resource_type="workflow_versions", resource_id=active_id, action="read"
    ) or not authz.authorize(
        session, auth, resource_type="workflow_versions", resource_id=active_id, action="write"
    ):
        raise HTTPException(status_code=403, detail="INSUFFICIENT_ROLE")


class AdapterResponse(BaseModel):
    adapter_id: str
    action_type: str
    data_class: str
    reversible: bool
    high_impact_categories: list[str]
    has_dispatch_value: bool


class AdapterListResponse(BaseModel):
    adapters: list[AdapterResponse]
    # Closed vocabularies a policy's scope is chosen from; `data_classes`
    # is ordered by ascending sensitivity (a policy names a ceiling).
    action_types: list[str]
    data_classes: list[str]


@router.get("/adapters", response_model=AdapterListResponse)
def list_adapters_endpoint(auth: AuthDep, session: SessionDep) -> AdapterListResponse:
    """Every registered action adapter's static declarations plus the scope
    vocabularies, so the policy UI offers closed choices instead of free
    text (scope-enforcement design, Decision 5 surfacing table). The
    registry is process-global, so this returns no workspace data; an
    active membership is still required, like every automation read."""
    authz.require_active_role(session, auth)  # rolls back its own read
    adapters = [
        _production_adapter_registry.get(adapter_id)
        for adapter_id in _production_adapter_registry.adapter_ids()
    ]
    return AdapterListResponse(
        adapters=[
            AdapterResponse(
                adapter_id=adapter.adapter_id,
                action_type=adapter.action_type,
                data_class=adapter.data_class,
                reversible=adapter.reversible,
                high_impact_categories=sorted(adapter.high_impact_categories),
                has_dispatch_value=has_dispatch_value(adapter),
            )
            for adapter in adapters
            if adapter is not None
        ],
        action_types=sorted(ACTION_TYPES),
        data_classes=list(DATA_CLASSES),
    )


@router.get("/workflows", response_model=WorkflowListResponse)
def list_workflows_endpoint(auth: AuthDep, session: SessionDep) -> WorkflowListResponse:
    summaries = list_workflows(session, auth)
    return WorkflowListResponse(
        workflows=[
            WorkflowSummaryResponse(
                workflow_id=summary.workflow_id,
                latest_version=summary.latest_version,
                latest_status=summary.latest_status,
                active_version=summary.active_version,
                latest_version_id=summary.latest_version_id,
                active_version_id=summary.active_version_id,
            )
            for summary in summaries
        ]
    )


@router.get("/workflows/{version_id}", response_model=WorkflowVersionResponse)
def get_workflow_endpoint(
    version_id: UUID, auth: AuthDep, session: SessionDep
) -> WorkflowVersionResponse:
    visible = authz.authorize(
        session, auth, resource_type="workflow_versions", resource_id=version_id, action="read"
    )
    session.rollback()
    if not visible:
        raise HTTPException(status_code=404, detail="WORKFLOW_NOT_FOUND")
    version = get_workflow_version_by_id(session, auth.workspace_id, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="WORKFLOW_NOT_FOUND")
    return _to_response(version)


@router.post(
    "/workflows", response_model=WorkflowVersionResponse, status_code=status.HTTP_201_CREATED
)
def create_workflow_endpoint(
    payload: WorkflowCreateRequest,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> WorkflowVersionResponse:
    """Creates the `workflow_id` family on first use, otherwise appends a
    new `draft` version (`version = previous + 1`) -- one endpoint serves
    both "create a new workflow" and "edit an existing one" (Decision 2:
    editing always inserts a new row), matching this task's constrained API
    surface (`API-SCHEMAS.md` lists no separate "add version" route).

    Appending to an existing family requires read and write on its latest
    and active versions, and a `policy_ref` must be readable
    (`_lock_and_authorize_family`/`_lock_and_authorize_policy_ref`). Both
    run before the idempotency cache, so a same-key replay is
    re-authorized (ADR-0014).
    """
    authz.require_role_action(session, auth, "write")
    graph_dict = payload.graph.model_dump(mode="json")
    violations = validate_graph_shape(graph_dict)
    if violations:
        raise HTTPException(
            status_code=422, detail={"code": "SCHEMA_INVALID", "violations": violations}
        )

    req_hash = request_hash(payload, "create_workflow")
    now = datetime.now(UTC)
    with session.begin():
        authz.lock_membership_for_write(session, auth, role_action="write")
        lock_idempotency(session, auth, idempotency_key)
        _lock_and_authorize_family(session, auth, payload.workflow_id)
        if payload.policy_ref is not None:
            _lock_and_authorize_policy_ref(session, auth, payload.policy_ref)
        cached = load_cached(
            session,
            auth,
            idempotency_key,
            req_hash,
            domain="automation_workflows",
            response_model=WorkflowVersionResponse,
        )
        if cached is not None:
            return cached

        try:
            created = create_workflow_draft(
                session,
                auth.workspace_id,
                auth.user_id,
                workflow_id=payload.workflow_id,
                graph=graph_dict,
                trigger_refs=payload.trigger_refs,
                policy_ref=payload.policy_ref,
            )
        except IntegrityError as exc:
            raise HTTPException(status_code=409, detail="WORKFLOW_VERSION_CONFLICT") from exc

        response = _to_response(created)
        audit_outbox.write_audit_and_outbox(
            session,
            auth,
            request,
            event_type="workflow_version.drafted",
            aggregate_type="workflow_version",
            aggregate_id=created.id,
            aggregate_version=created.version,
            changed_fields=["status"],
            payload={"aggregate_id": str(created.id), "version": created.version},
            now=now,
            domain="automation_workflows",
        )
        queue_lifecycle_event(
            session, "automation_workflows", "workflow_version.drafted", "allowed"
        )
        store_idempotency(
            session,
            auth,
            idempotency_key,
            req_hash,
            response.model_dump(mode="json"),
            now,
            status.HTTP_201_CREATED,
        )
        return response


@router.post("/workflows/{version_id}/publish", response_model=WorkflowVersionResponse)
def publish_workflow_endpoint(
    version_id: UUID,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> WorkflowVersionResponse:
    req_hash = request_hash(_EmptyBody(), f"publish:{version_id}")
    now = datetime.now(UTC)
    with session.begin():
        authz.lock_membership_for_write(session, auth)
        lock_idempotency(session, auth, idempotency_key)

        # Lock before authorizing: an ownership transfer that commits while
        # this request waits on the row lock must be seen by the checks below
        # (READ COMMITTED: each later statement reads the committed row), not
        # by checks that ran against the pre-transfer row. Ahead of the
        # idempotency cache too: it is read only after these checks pass, so
        # a caller who has since lost access (removed, suspended, demoted, or
        # no longer able to see the row) never has a cached success replayed.
        # (`activate_workflow_version` re-selects this row FOR UPDATE below:
        # a no-op re-lock within this transaction.)
        locked = session.execute(
            text(
                "SELECT id FROM workflow_versions "
                "WHERE workspace_id = :workspace_id AND id = :id FOR UPDATE"
            ),
            {"workspace_id": auth.workspace_id, "id": version_id},
        ).one_or_none()
        if locked is None:
            raise HTTPException(status_code=404, detail="WORKFLOW_NOT_FOUND")
        if not authz.authorize(
            session, auth, resource_type="workflow_versions", resource_id=version_id, action="read"
        ):
            raise HTTPException(status_code=404, detail="WORKFLOW_NOT_FOUND")
        if not authz.authorize(
            session, auth, resource_type="workflow_versions", resource_id=version_id, action="write"
        ):
            raise HTTPException(status_code=403, detail="INSUFFICIENT_ROLE")
        _authorize_retiring_active_version(session, auth, version_id)

        # After authz, before the state checks in the helper below: a
        # same-key replay of a successful call finds the row already
        # transitioned and must get the cached 200, not a 409.
        cached = load_cached(
            session,
            auth,
            idempotency_key,
            req_hash,
            domain="automation_workflows",
            response_model=WorkflowVersionResponse,
        )
        if cached is not None:
            return cached

        try:
            # Task 7a: always checked against the shared production
            # registry -- the one real call site this task's own guarantee
            # ("a workflow with an adapter that cannot be simulated ... is
            # rejected at publish time") needs to hold for. See `activate_
            # workflow_version`'s own docstring for why the plain function
            # itself leaves this optional.
            result = activate_workflow_version(
                session,
                auth.workspace_id,
                version_id,
                adapter_registry=_production_adapter_registry,
            )
        except IntegrityError as exc:
            # Backstop only -- FOR UPDATE locking inside
            # activate_workflow_version already closes this race under this
            # codebase's normal (READ COMMITTED) isolation level. Converts
            # a would-be raw Postgres exception into a clean 409, per this
            # task's own instruction.
            raise HTTPException(status_code=409, detail="WORKFLOW_VERSION_ACTIVE_CONFLICT") from exc

        if isinstance(result, WorkflowVersionNotFound):
            raise HTTPException(status_code=404, detail="WORKFLOW_NOT_FOUND")
        if isinstance(result, WorkflowVersionNotDraft):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "WORKFLOW_VERSION_NOT_DRAFT",
                    "workflow_id": result.workflow_id,
                    "version": result.version,
                    "status": result.status,
                },
            )
        if isinstance(result, WorkflowVersionUnregisteredAdapter):
            # Task 7a. 422, not 409 -- this is a graph-content validation
            # failure (the same class of problem SCHEMA_INVALID's own 422
            # covers at draft time: the payload itself is unpublishable),
            # not a conflict about the row's own lifecycle state the way
            # WORKFLOW_VERSION_NOT_DRAFT/WORKFLOW_VERSION_ACTIVE_CONFLICT
            # are. Documented choice -- see this task's own PR evidence.
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "ACTION_REF_NOT_REGISTERED",
                    "workflow_id": result.workflow_id,
                    "version": result.version,
                    "violations": list(result.violations),
                },
            )
        if isinstance(result, WorkflowVersionHighImpactCompensationAdapter):
            # The compensation approval-gate-bypass fix's publish-time half
            # (`WorkflowVersionHighImpactCompensationAdapter`'s own docstring
            # has the bug description). 422, on the identical
            # graph-content-validation reasoning as ACTION_REF_NOT_REGISTERED
            # immediately above -- the submitted graph itself is
            # unpublishable, which is not a lifecycle conflict -- and with
            # the same `violations` list shape, so an existing client
            # rendering that list needs no new field to show a useful
            # message. A *distinct* code, not a reuse of ACTION_REF_NOT_
            # REGISTERED: the remedy differs (that one means "register the
            # adapter or fix the typo"; this one means "this action may not
            # be used as a rollback at all"), and a caller that cannot tell
            # them apart cannot tell an author what to do. An explicit
            # `message` is supplied (`main._error_payload` prefers it over
            # its title-cased-code fallback) so any client without a
            # code-specific branch for this new code still renders a
            # readable sentence rather than "Compensation Action Ref High
            # Impact".
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "COMPENSATION_ACTION_REF_HIGH_IMPACT",
                    "message": (
                        "A compensation step cannot use a high-impact action: compensation "
                        "runs automatically during rollback and never asks for per-run "
                        "approval."
                    ),
                    "workflow_id": result.workflow_id,
                    "version": result.version,
                    "violations": list(result.violations),
                },
            )
        if isinstance(result, WorkflowVersionActionOutsidePolicyScope):
            raise HTTPException(
                status_code=422,
                detail={
                    "code": "ACTION_REF_OUTSIDE_POLICY_SCOPE",
                    "message": (
                        "A step uses an action this workflow's policy does not authorize: "
                        "its action type or data class is outside the policy's scope."
                    ),
                    "workflow_id": result.workflow_id,
                    "version": result.version,
                    "violations": [dict(v) for v in result.violations],
                },
            )

        response = _to_response(result)
        audit_outbox.write_audit_and_outbox(
            session,
            auth,
            request,
            event_type="workflow_version.activated",
            aggregate_type="workflow_version",
            aggregate_id=result.id,
            aggregate_version=result.version,
            changed_fields=["status"],
            payload={"aggregate_id": str(result.id), "version": result.version},
            now=now,
            domain="automation_workflows",
        )
        queue_lifecycle_event(
            session, "automation_workflows", "workflow_version.activated", "allowed"
        )
        store_idempotency(
            session, auth, idempotency_key, req_hash, response.model_dump(mode="json"), now
        )
        return response


@router.post("/workflows/{version_id}/disable", response_model=WorkflowVersionResponse)
def disable_workflow_endpoint(
    version_id: UUID,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> WorkflowVersionResponse:
    req_hash = request_hash(_EmptyBody(), f"disable:{version_id}")
    now = datetime.now(UTC)
    with session.begin():
        authz.lock_membership_for_write(session, auth)
        lock_idempotency(session, auth, idempotency_key)

        # Lock before authorizing: an ownership transfer that commits while
        # this request waits on the row lock must be seen by the checks below
        # (READ COMMITTED: each later statement reads the committed row), not
        # by checks that ran against the pre-transfer row. Ahead of the
        # idempotency cache too: it is read only after these checks pass, so
        # a caller who has since lost access (removed, suspended, demoted, or
        # no longer able to see the row) never has a cached success replayed.
        # (`disable_workflow_version` re-selects this row FOR UPDATE below:
        # a no-op re-lock within this transaction.)
        locked = session.execute(
            text(
                "SELECT id FROM workflow_versions "
                "WHERE workspace_id = :workspace_id AND id = :id FOR UPDATE"
            ),
            {"workspace_id": auth.workspace_id, "id": version_id},
        ).one_or_none()
        if locked is None:
            raise HTTPException(status_code=404, detail="WORKFLOW_NOT_FOUND")
        if not authz.authorize(
            session, auth, resource_type="workflow_versions", resource_id=version_id, action="read"
        ):
            raise HTTPException(status_code=404, detail="WORKFLOW_NOT_FOUND")
        if not authz.authorize(
            session, auth, resource_type="workflow_versions", resource_id=version_id, action="write"
        ):
            raise HTTPException(status_code=403, detail="INSUFFICIENT_ROLE")

        # After authz, before the state checks in the helper below: a
        # same-key replay of a successful call finds the row already
        # transitioned and must get the cached 200, not a 409.
        cached = load_cached(
            session,
            auth,
            idempotency_key,
            req_hash,
            domain="automation_workflows",
            response_model=WorkflowVersionResponse,
        )
        if cached is not None:
            return cached

        result = disable_workflow_version(session, auth.workspace_id, version_id)
        if isinstance(result, WorkflowVersionNotFound):
            raise HTTPException(status_code=404, detail="WORKFLOW_NOT_FOUND")
        if isinstance(result, WorkflowVersionNotActive):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "WORKFLOW_NOT_ACTIVE",
                    "workflow_id": result.workflow_id,
                    "version": result.version,
                    "status": result.status,
                },
            )

        response = _to_response(result)
        audit_outbox.write_audit_and_outbox(
            session,
            auth,
            request,
            event_type="workflow_version.disabled",
            aggregate_type="workflow_version",
            aggregate_id=result.id,
            aggregate_version=result.version,
            changed_fields=["status"],
            payload={"aggregate_id": str(result.id), "version": result.version},
            now=now,
            domain="automation_workflows",
        )
        queue_lifecycle_event(
            session, "automation_workflows", "workflow_version.disabled", "allowed"
        )
        store_idempotency(
            session, auth, idempotency_key, req_hash, response.model_dump(mode="json"), now
        )
        return response


# ---------------------------------------------------------------------------
# Task 7a: POST /api/v1/automations/workflows/{version_id}/simulate
# (design doc Decision 4, `API-SCHEMAS.md`'s own `/simulate` paragraph).
#
# Runs the identical graph-walk/step-resolution shape `worker._resolve_step`
# uses for real dispatch (`step.get("input_mapping", {})`, verbatim -- this
# codebase does not yet implement live `$trigger.*`/prior-step-output
# templating, `worker.py`'s own module docstring has the full disclosure,
# equally true here), but this module deliberately does **not** import
# anything from `worker.py` itself: `worker.py` already imports `workflows.
# get_active_workflow_version`/`get_workflow_version`, so importing back from
# `worker.py` here would be a real circular import, not a hypothetical one.
# `_redact_payload`/`SimulateWorkspaceScopeMismatch` below are small,
# deliberate, disclosed duplicates of `worker._redact_payload`/`worker.
# WorkspaceScopeMismatch` for exactly this reason -- the same "a module-level
# helper duplicated per-module to avoid a real architectural problem"
# precedent this file's own `_request_hash`/`_lock_idempotency`/`_load_
# cached`/`_store_idempotency` already set relative to `runs.py`/`policy.py`/
# `kill_switches.py`'s identically-named, independently-defined equivalents.
#
# No `workflow_runs`/`workflow_run_steps`/`compensation_steps`/`approval_
# requests`/`audit_events`/`event_outbox` row is ever written by this
# endpoint -- there is no code path here that opens a transaction, and
# `adapter.execute()`/`adapter.compensate()` are never called, only
# `adapter.simulate()` -- proven directly by `tests/test_automation_
# simulate_postgres.py`'s own zero-rows-changed test across all six tables,
# extending `TEST-PLAN.md`'s per-adapter fault-injection requirement (Decision
# 4) to the endpoint level.
# ---------------------------------------------------------------------------

_SIMULATE_REDACTION_MARKERS = (
    "secret",
    "password",
    "token",
    "credential",
    "api_key",
    "authorization",
)
_SIMULATE_REDACTED_VALUE = "[REDACTED]"

DispatchGate = Literal[
    "clear",
    "requires_approval",
    "policy_blocked",
    "adapter_not_registered",
    "input_invalid",
    "not_applicable",
]
PolicyBlockReason = Literal[
    "no_policy",
    "policy_revoked",
    "policy_expired",
    "action_type_not_authorized",
    "data_class_not_authorized",
]


class SimulateWorkspaceScopeMismatch(ValueError):
    """This module's own copy of `worker.WorkspaceScopeMismatch`'s exact
    confused-deputy check (`worker.py`'s own docstring has the full
    reasoning), applied here against the *workflow version's* own pinned
    `workspace_id` rather than a `run`'s (a simulation has no `workflow_
    runs` row to compare against). A separate class, not an import of
    `worker.WorkspaceScopeMismatch` itself, for the identical circular-
    import reason this section's own module-docstring-adjacent comment
    states -- `worker.py` cannot be imported from here.
    """


def _simulate_redact_payload(value: Any) -> Any:
    """A deliberate, disclosed duplicate of `worker._redact_payload` (same
    marker list, same recursive shape) -- see this section's own header
    comment for why this is an import-cycle-avoiding duplication, not an
    oversight. Applied to a step's `simulate()` output before it is ever
    returned over HTTP: this response is never persisted, but it is still an
    adapter's own output a caller did not author, and `DATA-MODEL.md`'s
    "step payloads are redacted" discipline extends naturally to it.
    """
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, inner in value.items():
            if any(marker in key.lower() for marker in _SIMULATE_REDACTION_MARKERS):
                redacted[key] = _SIMULATE_REDACTED_VALUE
            else:
                redacted[key] = _simulate_redact_payload(inner)
        return redacted
    if isinstance(value, list):
        return [_simulate_redact_payload(item) for item in value]
    return value


class SimulateStepResult(BaseModel):
    step_index: int
    step_id: str
    step_type: str
    action_ref: str | None = None
    # Decision 9's compensation model, surfaced here per this task's own
    # instruction: an action step's own declared compensate_ref target is
    # shown as part of that step's own entry -- compensation steps
    # themselves are never part of the main preview walk below (matches
    # Task 6's own "reachable only through compensate_ref" rule).
    compensate_ref: str | None = None
    preview: dict[str, Any] | None = None
    # API-SCHEMAS.md: "declared side effects (per adapter, reversible and
    # high_impact_categories)" -- realized verbatim as these two fields,
    # the adapter's own static declarations (Decision 8), not derived from
    # the simulate() call itself. `high_impact_categories` is also this
    # response's own realization of API-SCHEMAS.md's "requested permissions/
    # scopes" wording -- this activation has no separate scope concept
    # beyond the seven-category taxonomy (Decision 5) an adapter already
    # declares statically.
    reversible: bool | None = None
    high_impact_categories: list[str] = Field(default_factory=list)
    # Policy-scope declarations (static, like the two above) and the value
    # this step's validated static input would move -- what a real dispatch
    # would record as `workflow_run_steps.dispatch_value`.
    action_type: str | None = None
    data_class: str | None = None
    dispatch_value: Decimal | None = None
    dispatch_gate: DispatchGate
    policy_block_reason: PolicyBlockReason | None = None
    error: str | None = None


class SimulateResponse(BaseModel):
    workflow_id: str
    version: int
    steps: list[SimulateStepResult]


def _simulate_steps(
    session: Session, version: WorkflowVersion, adapter_registry: AdapterRegistry
) -> list[SimulateStepResult]:
    """The graph-walk itself. `adapter.execute()`/`adapter.compensate()` are
    never referenced anywhere in this function's body -- the orchestration
    loop here has no code path that can reach either, matching Decision 4's
    own "not merely 'not called'" requirement literally, not just in intent.

    Reuses `approvals.evaluate_approval_requirement` -- the exact same
    policy-checking logic `worker._evaluate_dispatch_gate` already uses for
    real dispatch -- against the version's own pinned `policy_ref`, so this
    preview's "every approval point the run would hit" (`API-SCHEMAS.md`,
    verbatim) is a genuine re-evaluation of the same policy logic a real run
    would apply, not a separate, potentially-drifting guess. `action_step_
    count_so_far` is tracked the same way `worker._count_dispatched_action_
    steps` would report it for a real run that had reached this point with
    every prior action step having succeeded -- i.e. the count of *prior*
    action steps in this walk, never off-by-one against the step under
    evaluation, mirroring that function's own docstring precisely.
    """
    steps: list[dict[str, Any]] = version.graph.get("steps", [])
    policy_row: AutomationPolicy | None = None
    if version.policy_ref is not None:
        policy_row = get_policy(session, version.workspace_id, version.policy_ref)

    results: list[SimulateStepResult] = []
    action_step_count_so_far = 0
    run_value_so_far = Decimal("0")
    for index, step in enumerate(steps):
        step_type_raw = step.get("step_type")
        step_type = str(step_type_raw)
        step_id = str(step.get("step_id", ""))

        if step_type_raw == "compensation":
            # Reachable only through compensate_ref (Task 6's own rule) --
            # never part of the main preview walk, matches the required
            # behavior for this endpoint exactly.
            continue

        if step_type_raw in {"approval_gate", "condition"}:
            # No adapter to call -- represented structurally only, matching
            # process_claimed_run's own main-loop skip logic for these two
            # step types (Task 6).
            results.append(
                SimulateStepResult(
                    step_index=index,
                    step_id=step_id,
                    step_type=step_type,
                    dispatch_gate="not_applicable",
                )
            )
            continue

        # step_type_raw == "action" from here on (validate_graph_shape
        # restricts step_type to the four known values at draft time).
        action_ref = step.get("action_ref")
        compensate_ref = step.get("compensate_ref")
        adapter: ActionAdapter | None = adapter_registry.get(action_ref) if action_ref else None

        # Policy usability is checked BEFORE adapter registration, exactly
        # mirroring worker._evaluate_dispatch_gate's own real-dispatch
        # ordering (policy_id/get_policy/is_policy_usable first; the
        # adapter is resolved only once the policy already cleared) --
        # found during review: this branch originally checked adapter
        # registration first, which meant a step whose policy was
        # unusable *and* whose action_ref was unregistered wrongly
        # reported "adapter_not_registered" here while a real run on the
        # identical graph would report "policy_blocked" first and never
        # even reach the adapter-registration question. Reordered to
        # match Decision 4's own "identical graph-walk ... not a
        # separate, potentially-drifting guess" requirement literally.
        # Order mirrors worker._evaluate_dispatch_gate exactly (scope-
        # enforcement design, Decision 5): usability -> adapter -> scope ->
        # input validation -> value and approval.
        resolved_input: dict[str, Any] = step.get("input_mapping", {})
        action_input: BaseModel | None = None
        step_value: Decimal | None = None
        validation_error: str | None = None
        if adapter is not None:
            try:
                action_input = adapter.input_schema.model_validate(resolved_input)
                step_value = dispatch_value(adapter, action_input)
            except Exception as exc:  # noqa: BLE001 -- mirrors the gate's own broad catch
                action_input = None
                validation_error = type(exc).__name__

        dispatch_gate: DispatchGate
        policy_block_reason: PolicyBlockReason | None = None
        if policy_row is None:
            dispatch_gate = "policy_blocked"
            policy_block_reason = "no_policy"
        elif not is_policy_usable(policy_row):
            dispatch_gate = "policy_blocked"
            lifecycle = policy_status(policy_row)
            policy_block_reason = "policy_revoked" if lifecycle == "revoked" else "policy_expired"
        elif adapter is None:
            dispatch_gate = "adapter_not_registered"
        elif (scope_reason := evaluate_policy_scope(adapter, policy_row)) is not None:
            dispatch_gate = "policy_blocked"
            policy_block_reason = scope_reason
        elif validation_error is not None or step_value is None:
            dispatch_gate = "input_invalid"
        else:
            categories = evaluate_approval_requirement(
                adapter,
                policy_row,
                action_step_count_so_far=action_step_count_so_far,
                run_value_so_far=run_value_so_far,
                step_value=step_value,
            )
            # `is None`, never truthiness: an empty set still requires approval.
            dispatch_gate = "clear" if categories is None else "requires_approval"

        if adapter is None:
            # A version drafted before this task's own publish-time check
            # existed (or hand-constructed to bypass it) could still name an
            # unresolvable action_ref -- degrade gracefully, never a 500,
            # and never call simulate() for this step (this task's own
            # required behavior).
            results.append(
                SimulateStepResult(
                    step_index=index,
                    step_id=step_id,
                    step_type=step_type,
                    action_ref=action_ref,
                    compensate_ref=compensate_ref,
                    dispatch_gate=dispatch_gate,
                    policy_block_reason=policy_block_reason,
                    error=(
                        "AdapterNotRegistered"
                        if dispatch_gate == "adapter_not_registered"
                        else None
                    ),
                )
            )
            action_step_count_so_far += 1
            continue

        preview_dict: dict[str, Any] | None = None
        error: str | None = validation_error
        if action_input is not None:
            try:
                input_workspace_id = getattr(action_input, "workspace_id", None)
                if input_workspace_id is not None and input_workspace_id != version.workspace_id:
                    raise SimulateWorkspaceScopeMismatch(
                        f"step '{step_id}' resolved input names workspace_id="
                        f"{input_workspace_id}, which does not match workflow version "
                        f"{version.id}'s own workspace_id={version.workspace_id}"
                    )
                preview_model = adapter.simulate(action_input)
                preview_dict = _simulate_redact_payload(preview_model.model_dump(mode="json"))
            except Exception as exc:  # noqa: BLE001 -- mirrors run_step's own broad adapter-call catch
                error = type(exc).__name__

        results.append(
            SimulateStepResult(
                step_index=index,
                step_id=step_id,
                step_type=step_type,
                action_ref=action_ref,
                compensate_ref=compensate_ref,
                preview=preview_dict,
                reversible=adapter.reversible,
                high_impact_categories=sorted(adapter.high_impact_categories),
                action_type=adapter.action_type,
                data_class=adapter.data_class,
                dispatch_value=step_value,
                dispatch_gate=dispatch_gate,
                policy_block_reason=policy_block_reason,
                error=error,
            )
        )
        action_step_count_so_far += 1
        # Only a step real dispatch would actually record a value for: a
        # blocked or invalid step writes no `dispatched` row.
        if step_value is not None and dispatch_gate in ("clear", "requires_approval"):
            run_value_so_far += step_value

    return results


@router.post("/workflows/{version_id}/simulate", response_model=SimulateResponse)
def simulate_workflow_endpoint(
    version_id: UUID, auth: AuthDep, session: SessionDep, _csrf: CsrfDep
) -> SimulateResponse:
    """Decision 4's simulation entrypoint. Workspace-scoped and 404-isolated
    exactly like every other endpoint in this module (`get_workflow_version_
    by_id`); addressed by `version_id`, exactly like `publish`/`disable`.

    **`CsrfDep` but no `Idempotency-Key` -- the minimal correct pattern for a
    mutating-method route with no state change** (security audit batch C
    corrected the earlier reasoning here, which had bundled the two together
    and dropped both). The two protect different properties and are not a
    package deal: an `Idempotency-Key` makes a *state change* replay-safe, and
    this endpoint genuinely has none to replay-protect (no code path here
    opens a transaction, and `_simulate_steps` reaches only `adapter.
    simulate()`, never `execute()`/`compensate()` -- proven by this module's
    own zero-rows-changed test across all seven tables), so it is correctly
    still absent. CSRF, by contrast, protects against a *cross-site caller*
    driving this route with the victim's ambient session cookie at all --
    which is worth refusing regardless of whether a row is written, because
    this route is authenticated, workspace-scoped, and returns the caller's
    own pinned graph plus every adapter's declared side effects and each
    step's approval/policy gating: a read a third-party origin has no
    business eliciting. It was also the only mutating-method `/api/v1` route
    in the codebase without it, so leaving it out made the codebase's
    otherwise-uniform "every non-`GET` route carries `CsrfDep`" invariant
    unstateable and unauditable, which is its own cost. `POST /attention/
    regenerate` and `POST /ai/runs/{run_id}/cancel` are the established
    precedent for CSRF-without-idempotency, so this matches an existing
    pattern rather than inventing one.

    Kept as `POST`, not `GET`, only because `API-SCHEMAS.md`'s own
    already-approved route is a `POST` -- this activation's `input_mapping`
    has no live templating today (`worker._resolve_step`'s own docstring), so
    this endpoint's simulation is fully determined by the version's own
    already-pinned graph and needs no request body; a later task giving
    `/simulate` real per-call input overrides would be the natural place to
    add one, and would be the point at which an `Idempotency-Key` might
    finally have something to protect.
    """
    visible = authz.authorize(
        session, auth, resource_type="workflow_versions", resource_id=version_id, action="read"
    )
    session.rollback()
    if not visible:
        raise HTTPException(status_code=404, detail="WORKFLOW_NOT_FOUND")
    version = get_workflow_version_by_id(session, auth.workspace_id, version_id)
    if version is None:
        raise HTTPException(status_code=404, detail="WORKFLOW_NOT_FOUND")
    steps = _simulate_steps(session, version, _production_adapter_registry)
    return SimulateResponse(workflow_id=version.workflow_id, version=version.version, steps=steps)
