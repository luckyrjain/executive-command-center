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
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from json import dumps
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session
from ecc.observability import (
    queue_lifecycle_event,
    record_audit_outbox_failure,
    record_idempotency_conflict,
)

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
class WorkflowSummary:
    workflow_id: str
    latest_version: int
    latest_status: WorkflowStatus
    active_version: int | None


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


def list_workflows(session: Session, workspace_id: UUID) -> list[WorkflowSummary]:
    """One summary per `workflow_id` family in the caller's workspace,
    ordered by slug. Derived entirely from `workflow_versions` (every
    `workflow_definitions` row always has at least one version -- the two
    are inserted together in `create_workflow_draft`), matching `ai_runtime.
    registry.list_models`'s "simple query, reduce in Python" style rather
    than a window-function query, since this activation's per-workspace
    workflow count is small.
    """
    rows = (
        session.execute(
            text(
                f"SELECT {_WORKFLOW_VERSION_FIELDS} FROM workflow_versions "
                "WHERE workspace_id = :workspace_id ORDER BY workflow_id ASC, version ASC"
            ),
            {"workspace_id": workspace_id},
        )
        .mappings()
        .all()
    )
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

    Locking (`FOR UPDATE` on the family row, or on the existing version
    rows when the family already exists) serializes concurrent callers
    deriving the next version number for the same `workflow_id`, mirroring
    `capacity.py`'s `_current_profile(for_update=True)`/`ai_runtime.prompts.
    activate_prompt_version`'s identical race-closing rationale. The
    `(workspace_id, workflow_id, version)` unique constraint is the DB-level
    backstop if two callers still race past the lock under a lower
    isolation level -- the caller (the HTTP endpoint below) converts that
    into a clean 409 rather than a raw `IntegrityError`.
    """
    now = datetime.now(UTC)
    family = (
        session.execute(
            text(
                "SELECT id FROM workflow_definitions "
                "WHERE workspace_id = :workspace_id AND workflow_id = :workflow_id FOR UPDATE"
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
                    id, workspace_id, workflow_id, created_by, created_at, updated_at
                ) VALUES (:id, :workspace_id, :workflow_id, :created_by, :now, :now)
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
                definition_hash, status, created_by, updated_by, created_at, updated_at
            ) VALUES (
                :id, :workspace_id, :workflow_id, :version, CAST(:graph AS jsonb),
                CAST(:trigger_refs AS jsonb), :policy_ref, :definition_hash, 'draft',
                :created_by, :updated_by, :now, :now
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
    session: Session, workspace_id: UUID, version_id: UUID
) -> WorkflowVersion | WorkflowVersionNotFound | WorkflowVersionNotDraft:
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


def _request_hash(payload: BaseModel, action: str) -> str:
    material = {"action": action, "payload": payload.model_dump(mode="json")}
    return sha256(dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _request_ids(request: Request) -> tuple[UUID, UUID]:
    try:
        return UUID(request.state.request_id), UUID(request.state.correlation_id)
    except (AttributeError, TypeError, ValueError):
        return uuid4(), uuid4()


def _lock_idempotency(session: Session, auth: AuthContext, key: str) -> None:
    lock_key = f"{auth.workspace_id}:{auth.user_id}:{key}"
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": lock_key},
    )


def _load_cached(
    session: Session, auth: AuthContext, key: str, request_hash: str
) -> WorkflowVersionResponse | None:
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
        record_idempotency_conflict("automation_workflows")
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
    return WorkflowVersionResponse.model_validate(row["response_body"])


def _store_idempotency(
    session: Session,
    auth: AuthContext,
    key: str,
    request_hash: str,
    response: WorkflowVersionResponse,
    now: datetime,
    response_status: int = 200,
) -> None:
    session.execute(
        text(
            """
            INSERT INTO idempotency_records (
                workspace_id, actor_id, key, request_hash, response_status,
                response_body, created_at, expires_at
            ) VALUES (
                :workspace_id, :actor_id, :key, :request_hash, :response_status,
                CAST(:response_body AS jsonb), :created_at, :expires_at
            )
            """
        ),
        {
            "workspace_id": auth.workspace_id,
            "actor_id": auth.user_id,
            "key": key,
            "request_hash": request_hash,
            "response_status": response_status,
            "response_body": dumps(response.model_dump(mode="json")),
            "created_at": now,
            "expires_at": now + timedelta(days=365),
        },
    )


def _write_side_effects(
    session: Session,
    auth: AuthContext,
    request: Request,
    *,
    event_type: str,
    aggregate_id: UUID,
    version: int,
    now: datetime,
) -> None:
    """Audit + outbox for a workflow-version mutation, matching every other
    mutating endpoint's `_write_side_effects` convention exactly (`ai_
    runtime.prompts._write_activation_audit`, `attention.capacity.
    _write_side_effects`).
    """
    request_id, correlation_id = _request_ids(request)
    try:
        session.execute(
            text(
                """
                INSERT INTO audit_events (
                    id, workspace_id, event_type, aggregate_type, aggregate_id,
                    aggregate_version, actor_id, request_id, correlation_id,
                    changed_fields, authorization_result, source, metadata, occurred_at
                ) VALUES (
                    :id, :workspace_id, :event_type, 'workflow_version', :aggregate_id,
                    :aggregate_version, :actor_id, :request_id, :correlation_id,
                    ARRAY['status'], 'allowed', 'user', '{}'::jsonb, :occurred_at
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": auth.workspace_id,
                "event_type": event_type,
                "aggregate_id": aggregate_id,
                "aggregate_version": version,
                "actor_id": auth.user_id,
                "request_id": request_id,
                "correlation_id": correlation_id,
                "occurred_at": now,
            },
        )
        session.execute(
            text(
                """
                INSERT INTO event_outbox (
                    event_id, workspace_id, event_type, event_version,
                    correlation_id, payload, occurred_at, attempt_count
                ) VALUES (
                    :event_id, :workspace_id, :event_type_v1, 1,
                    :correlation_id, CAST(:payload AS jsonb), :occurred_at, 0
                )
                """
            ),
            {
                "event_id": uuid4(),
                "workspace_id": auth.workspace_id,
                "event_type_v1": f"{event_type}.v1",
                "correlation_id": correlation_id,
                "payload": dumps({"aggregate_id": str(aggregate_id), "version": version}),
                "occurred_at": now,
            },
        )
    except SQLAlchemyError:
        record_audit_outbox_failure("automation_workflows")
        raise
    queue_lifecycle_event(session, "automation_workflows", event_type, "allowed")


def _policy_ref_exists(session: Session, workspace_id: UUID, policy_ref: UUID) -> bool:
    return (
        session.execute(
            text(
                "SELECT 1 FROM automation_policies WHERE workspace_id = :workspace_id "
                "AND id = :id LIMIT 1"
            ),
            {"workspace_id": workspace_id, "id": policy_ref},
        ).first()
        is not None
    )


@router.get("/workflows", response_model=WorkflowListResponse)
def list_workflows_endpoint(auth: AuthDep, session: SessionDep) -> WorkflowListResponse:
    summaries = list_workflows(session, auth.workspace_id)
    return WorkflowListResponse(
        workflows=[
            WorkflowSummaryResponse(
                workflow_id=summary.workflow_id,
                latest_version=summary.latest_version,
                latest_status=summary.latest_status,
                active_version=summary.active_version,
            )
            for summary in summaries
        ]
    )


@router.get("/workflows/{version_id}", response_model=WorkflowVersionResponse)
def get_workflow_endpoint(
    version_id: UUID, auth: AuthDep, session: SessionDep
) -> WorkflowVersionResponse:
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
    """
    graph_dict = payload.graph.model_dump(mode="json")
    violations = validate_graph_shape(graph_dict)
    if violations:
        raise HTTPException(
            status_code=422, detail={"code": "SCHEMA_INVALID", "violations": violations}
        )

    request_hash = _request_hash(payload, "create_workflow")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached

        if payload.policy_ref is not None and not _policy_ref_exists(
            session, auth.workspace_id, payload.policy_ref
        ):
            raise HTTPException(status_code=404, detail="POLICY_NOT_FOUND")

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
        _write_side_effects(
            session,
            auth,
            request,
            event_type="workflow_version.drafted",
            aggregate_id=created.id,
            version=created.version,
            now=now,
        )
        _store_idempotency(
            session,
            auth,
            idempotency_key,
            request_hash,
            response,
            now,
            response_status=status.HTTP_201_CREATED,
        )
        return response


class _EmptyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


@router.post("/workflows/{version_id}/publish", response_model=WorkflowVersionResponse)
def publish_workflow_endpoint(
    version_id: UUID,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> WorkflowVersionResponse:
    request_hash = _request_hash(_EmptyBody(), f"publish:{version_id}")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached

        try:
            result = activate_workflow_version(session, auth.workspace_id, version_id)
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

        response = _to_response(result)
        _write_side_effects(
            session,
            auth,
            request,
            event_type="workflow_version.activated",
            aggregate_id=result.id,
            version=result.version,
            now=now,
        )
        _store_idempotency(session, auth, idempotency_key, request_hash, response, now)
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
    request_hash = _request_hash(_EmptyBody(), f"disable:{version_id}")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
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
        _write_side_effects(
            session,
            auth,
            request,
            event_type="workflow_version.disabled",
            aggregate_id=result.id,
            version=result.version,
            now=now,
        )
        _store_idempotency(session, auth, idempotency_key, request_hash, response, now)
        return response
