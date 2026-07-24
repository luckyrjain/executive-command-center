"""Prompt contract reads/activation (`prompt_versions`), plus the shared
`POST /api/v1/ai/policies/{prompt_id_or_tool_name}/activate` administrative
endpoint (`phase-004/API-SCHEMAS.md`) covering both `prompt_versions` and
`tool_definitions` -- one path, dispatched to whichever table the name
resolves against (`_resolve_policy_kind` below), since the endpoint's own
path parameter is literally named `prompt_id_or_tool_name` in the accepted
API surface, not two separate routes.

`docs/phases/phase-004/DATA-MODEL.md`: `prompt_versions` is the platform's
global, versioned prompt-template catalog -- **not** workspace-scoped user
data (see migration `0029_phase4_prompt_tool_versions.py`'s module
docstring for why), the same kind of global platform catalog as Task 1's
`model_definitions`/`routing_policies`. Exactly one row is seeded by that
migration: `prompt_id='attention.explain_item.v1'`, `version=1`,
`status='active'` (design doc Decision 9).

Implements the design doc's Decision 3 mechanism: `template_hash` is
`sha256` over the canonical UTF-8 sorted-object-keys bytes of `{template,
input_schema_ref, output_schema_ref}`, and once a row's `status` leaves
`draft` that envelope is immutable -- enforced by the
`trg_prompt_versions_immutability` PostgreSQL trigger the migration
creates, not only by this module declining to expose an edit path. Editing
a prompt always means inserting a new row with `version = previous + 1`;
`activate_prompt_version` never edits an existing row's template/schema-ref
columns, only `status`/`updated_at`.

`POST .../activate` is "an explicit administrative action ... [that] writes
an audit event and does not retroactively change which version any
already-completed `ai_run` recorded" (Decision 3) -- it flips which row is
`active`, it never mutates a row's content. Requires `AuthDep`/`CsrfDep`
like every other mutating endpoint in this codebase (`API-SCHEMAS.md`:
"requires local-owner authority" -- in this single-operator, no-separate-
admin-role codebase, `AuthDep` alone is what that resolves to, matching
Task 1's `registry.py`/`router.py` read-endpoint reasoning). Idempotency-
Key + response replay follows `attention/capacity.py`'s
`_write_side_effects`/`_load_cached` convention exactly.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from json import dumps
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session
from ecc.observability import (
    queue_lifecycle_event,
    record_audit_outbox_failure,
    record_idempotency_conflict,
)

from .tools import (
    ToolDefinition,
    ToolVersionNotFound,
    activate_tool_version,
    get_active_tool,
    tool_family_exists,
)

PromptStatus = Literal["draft", "active", "retired"]

_PROMPT_FIELDS = """
    id, prompt_id, version, template, template_hash, input_schema_ref,
    output_schema_ref, status
"""


@dataclass(frozen=True, slots=True)
class PromptVersion:
    id: UUID
    prompt_id: str
    version: int
    template: str
    template_hash: str
    input_schema_ref: str
    output_schema_ref: str
    status: PromptStatus


@dataclass(frozen=True, slots=True)
class PromptVersionNotFound:
    """No `prompt_versions` row exists for the given `(prompt_id, version)`
    pair -- distinct from "not currently active" (`get_active_prompt`
    returning `None`), mirroring `tools.ToolVersionNotFound`.
    """

    prompt_id: str
    version: int


def compute_template_hash(*, template: str, input_schema_ref: str, output_schema_ref: str) -> str:
    """`sha256` over the canonical (UTF-8, sorted-object-keys) JSON bytes of
    `{template, input_schema_ref, output_schema_ref}` -- design doc
    Decision 3's hashing scheme for prompts. Mirrored (not imported, per
    this codebase's migration-self-containment convention) by migration
    `0029_phase4_prompt_tool_versions.py`'s `_canonical_hash` for the seed
    row; keep both in sync if this scheme ever changes.
    """
    material = {
        "template": template,
        "input_schema_ref": input_schema_ref,
        "output_schema_ref": output_schema_ref,
    }
    canonical = dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return sha256(canonical.encode("utf-8")).hexdigest()


def _row_to_prompt(row: dict[str, Any]) -> PromptVersion:
    return PromptVersion(
        id=row["id"],
        prompt_id=row["prompt_id"],
        version=row["version"],
        template=row["template"],
        template_hash=row["template_hash"],
        input_schema_ref=row["input_schema_ref"],
        output_schema_ref=row["output_schema_ref"],
        status=row["status"],
    )


def get_active_prompt(session: Session, prompt_id: str) -> PromptVersion | None:
    """The currently active version of a prompt, or `None` if the prompt
    has no active version. Never raises on a missing row, matching Task
    1's `registry.py:get_model` convention.
    """
    row = (
        session.execute(
            text(
                f"SELECT {_PROMPT_FIELDS} FROM prompt_versions "
                "WHERE prompt_id = :prompt_id AND status = 'active'"
            ),
            {"prompt_id": prompt_id},
        )
        .mappings()
        .one_or_none()
    )
    return _row_to_prompt(dict(row)) if row is not None else None


def get_prompt_version(session: Session, prompt_id: str, version: int) -> PromptVersion | None:
    """A specific `(prompt_id, version)` row regardless of status -- used
    by `activate_prompt_version` to find the row it is about to activate.
    """
    row = (
        session.execute(
            text(
                f"SELECT {_PROMPT_FIELDS} FROM prompt_versions "
                "WHERE prompt_id = :prompt_id AND version = :version"
            ),
            {"prompt_id": prompt_id, "version": version},
        )
        .mappings()
        .one_or_none()
    )
    return _row_to_prompt(dict(row)) if row is not None else None


def prompt_family_exists(session: Session, prompt_id: str) -> bool:
    """Whether any row (any status) exists for this `prompt_id` -- used by
    `_resolve_policy_kind` below to decide whether the activate endpoint's
    path parameter names a prompt or a tool.
    """
    return (
        session.execute(
            text("SELECT 1 FROM prompt_versions WHERE prompt_id = :prompt_id LIMIT 1"),
            {"prompt_id": prompt_id},
        ).first()
        is not None
    )


def activate_prompt_version(
    session: Session, prompt_id: str, version: int
) -> PromptVersion | PromptVersionNotFound:
    """Explicit administrative activation (design doc Decision 3): retires
    whichever version is currently `active` for `prompt_id` (if any and if
    it is not already the target row) and marks the target row `active`,
    each via an `UPDATE` touching only `status`/`updated_at` -- the two
    columns `trg_prompt_versions_immutability` never guards, confirmed by
    reading the trigger this module's migration creates (it only rejects
    changes to `template`/`template_hash`/`input_schema_ref`/`output_
    schema_ref` once `OLD.status <> 'draft'`). Never edits `template`
    content of any row. Caller (the HTTP endpoint below) owns the
    surrounding transaction, idempotency key and audit event -- this
    function is the pure data-layer mutation only.

    `FOR UPDATE` locks both the target row and the current active row (if
    distinct) for the rest of the caller's transaction, mirroring `tools.
    py:activate_tool_version`'s identical race-closing rationale.
    """
    target_row = (
        session.execute(
            text(
                f"SELECT {_PROMPT_FIELDS} FROM prompt_versions "
                "WHERE prompt_id = :prompt_id AND version = :version FOR UPDATE"
            ),
            {"prompt_id": prompt_id, "version": version},
        )
        .mappings()
        .one_or_none()
    )
    if target_row is None:
        return PromptVersionNotFound(prompt_id=prompt_id, version=version)

    now = datetime.now(UTC)
    if target_row["status"] != "active":
        current_active = (
            session.execute(
                text(
                    "SELECT id FROM prompt_versions "
                    "WHERE prompt_id = :prompt_id AND status = 'active' FOR UPDATE"
                ),
                {"prompt_id": prompt_id},
            )
            .mappings()
            .one_or_none()
        )
        if current_active is not None and current_active["id"] != target_row["id"]:
            session.execute(
                text(
                    "UPDATE prompt_versions SET status = 'retired', updated_at = :now "
                    "WHERE id = :id"
                ),
                {"id": current_active["id"], "now": now},
            )
        session.execute(
            text("UPDATE prompt_versions SET status = 'active', updated_at = :now WHERE id = :id"),
            {"id": target_row["id"], "now": now},
        )

    final_row = (
        session.execute(
            text(f"SELECT {_PROMPT_FIELDS} FROM prompt_versions WHERE id = :id"),
            {"id": target_row["id"]},
        )
        .mappings()
        .one()
    )
    return _row_to_prompt(dict(final_row))


# --- POST /api/v1/ai/policies/{prompt_id_or_tool_name}/activate ------------

router = APIRouter(prefix="/api/v1/ai", tags=["ai-runtime"])
SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]


class PolicyActivateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    # Optimistic-concurrency guard (`Global constraints`: every mutation
    # "uses ... optimistic versioning"): the version the caller last
    # observed as active, or `None` if the caller believes nothing is
    # active yet. A mismatch against the row actually read under lock
    # raises `VERSION_CONFLICT` before any write happens.
    expected_active_version: int | None = Field(default=None, ge=1)


class PolicyActivateResponse(BaseModel):
    kind: Literal["prompt", "tool"]
    name: str
    active_version: int
    status: Literal["active"]


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
) -> PolicyActivateResponse | None:
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
    return PolicyActivateResponse.model_validate(row["response_body"])


def _store_idempotency(
    session: Session,
    auth: AuthContext,
    key: str,
    request_hash: str,
    response: PolicyActivateResponse,
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


# Task 5 addition: the exact set of gated prompt families -- design doc
# Decision 9's promotion rule ("Promotion of prompt_versions/routing_
# policies changes for this task always re-runs the full 20-example set
# and requires the table above to pass in full before the new version can
# become active") applies "specifically for the attention.explain_item
# prompt" (Task 5's own scope), not to every prompt family this codebase
# might ever register. `attention.explain_item.v1` is the family's stable
# slug (Decision 3: the slug never changes across versions -- only the
# integer `version` column does), the same literal migration
# 0029_phase4_prompt_tool_versions.py seeds. `activate_tool_version`'s
# path (kind == "tool") is never gated -- Task 5's scope is this one prompt
# family only.
_GATED_PROMPT_IDS = frozenset({"attention.explain_item.v1"})


def _prompt_evaluation_floor_met(
    session: Session, auth: AuthContext, *, prompt_id: str, version: int
) -> bool:
    """Design doc Decision 9 / `EVALUATION-CONTRACT.md`: before a gated
    prompt version may become `active`, a `evaluation_runs` row for this
    exact `(task_type, prompt_id, version)` triple, in the acting
    administrator's own workspace, must already exist and must satisfy
    `evaluation.check_promotion_floors`. Only ever called for names in
    `_GATED_PROMPT_IDS` -- `activate_policy` below checks that first.

    **Imported locally, not at module load time.** `evaluation.py` imports
    `runtime.py` (to call `execute_run`), and `runtime.py` imports this
    module (`from .prompts import get_active_prompt`) -- a module-level
    `from .evaluation import ...` here would start importing `evaluation.py`
    before this module finishes defining `get_active_prompt`, which
    `runtime.py` (itself imported by `evaluation.py`) needs already defined.
    Deferring the import to call time breaks that cycle safely: by the time
    any HTTP request reaches `activate_policy`, every module in this
    package has already finished importing (`ecc.main` imports all of
    `prompts`/`registry`/`router`/`runtime`/`evaluation` at process start),
    so the import below always succeeds and costs nothing beyond the first
    call (Python caches the module).
    """
    from .evaluation import check_promotion_floors, get_latest_evaluation_run

    task_type = _GATED_PROMPT_ID_TASK_TYPES.get(prompt_id)
    if task_type is None:
        return True  # not actually gated -- defensive, _GATED_PROMPT_IDS already filtered this.

    latest = get_latest_evaluation_run(
        session, auth, task_type=task_type, prompt_id=prompt_id, prompt_version=version
    )
    if latest is None:
        return False
    return check_promotion_floors(latest)


# prompt_id -> task_type for every gated family (Task 5 scope: exactly
# one). Not imported from `runtime.TASK_PORTS` to avoid the same
# module-level circular import `_prompt_evaluation_floor_met`'s docstring
# explains -- kept in sync by convention with `runtime.TASK_PORTS[
# "attention.explain_item"].prompt_id`, mirroring this codebase's
# established hash-duplication convention (`prompts.py`'s own `compute_
# template_hash` docstring) for exactly this kind of "two modules, one
# fact" situation.
_GATED_PROMPT_ID_TASK_TYPES = {"attention.explain_item.v1": "attention.explain_item"}


def _resolve_policy_kind(session: Session, name: str) -> Literal["prompt", "tool"] | None:
    """Which table `prompt_id_or_tool_name` names, checked against *any*
    status row (not just active) -- a caller activating a fresh `draft`
    version of an existing family must still resolve correctly. Tools are
    checked first only because that happens to match this activation's two
    seeded families' alphabetical/registration order; there is no name
    collision possible between the two tables in practice (`attention.get_
    item`/`knowledge.get_entity` vs. `attention.explain_item.v1`), so the
    order carries no real precedence meaning.
    """
    if tool_family_exists(session, name):
        return "tool"
    if prompt_family_exists(session, name):
        return "prompt"
    return None


def _current_active_version(
    session: Session, kind: Literal["prompt", "tool"], name: str
) -> int | None:
    active: PromptVersion | ToolDefinition | None
    if kind == "tool":
        active = get_active_tool(session, name)
    else:
        active = get_active_prompt(session, name)
    return active.version if active is not None else None


def _write_activation_audit(
    session: Session,
    auth: AuthContext,
    request: Request,
    *,
    kind: Literal["prompt", "tool"],
    aggregate_id: UUID,
    name: str,
    version: int,
    now: datetime,
) -> None:
    """Audit + outbox for a policy activation, matching `attention/
    capacity.py`'s `_write_side_effects` pattern exactly. `prompt_versions`/
    `tool_definitions` are global platform data with no `workspace_id` of
    their own (see this module's docstring), so `auth.workspace_id` here
    records the acting administrator's own workspace context -- required
    because `audit_events.workspace_id` is `NOT NULL` -- not a claim that
    the mutated row itself belongs to that workspace.
    """
    request_id, correlation_id = _request_ids(request)
    event_type = f"ai_{kind}.activated"
    aggregate_type = "prompt_version" if kind == "prompt" else "tool_definition"
    try:
        session.execute(
            text(
                """
                INSERT INTO audit_events (
                    id, workspace_id, event_type, aggregate_type, aggregate_id,
                    aggregate_version, actor_id, request_id, correlation_id,
                    changed_fields, authorization_result, source, metadata, occurred_at
                ) VALUES (
                    :id, :workspace_id, :event_type, :aggregate_type, :aggregate_id,
                    :aggregate_version, :actor_id, :request_id, :correlation_id,
                    ARRAY['status'], 'allowed', 'user', :metadata, :occurred_at
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": auth.workspace_id,
                "event_type": event_type,
                "aggregate_type": aggregate_type,
                "aggregate_id": aggregate_id,
                "aggregate_version": version,
                "actor_id": auth.user_id,
                "request_id": request_id,
                "correlation_id": correlation_id,
                "metadata": dumps({"name": name}),
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
                "payload": dumps({"name": name, "version": version}),
                "occurred_at": now,
            },
        )
    except SQLAlchemyError:
        record_audit_outbox_failure("ai_runtime")
        raise
    queue_lifecycle_event(session, "ai_runtime", event_type, "allowed")


@router.post("/policies/{prompt_id_or_tool_name}/activate", response_model=PolicyActivateResponse)
def activate_policy(
    prompt_id_or_tool_name: str,
    payload: PolicyActivateRequest,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> PolicyActivateResponse:
    """`API-SCHEMAS.md`: "the explicit administrative action that flips
    which `prompt_versions`/`tool_definitions` row is `active`; ... requires
    local-owner authority ... and writes an audit event. It never mutates
    an already-`active` or `retired` row -- activation always points the
    'current' pointer at an existing immutable version, never edits one."
    `prompt_id_or_tool_name` is resolved server-side against both tables
    (`_resolve_policy_kind`) -- the caller never declares which table it
    means, closing off a caller-supplied-kind bypass of that resolution.
    """
    request_hash = _request_hash(payload, f"activate:{prompt_id_or_tool_name}")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return cached

        kind = _resolve_policy_kind(session, prompt_id_or_tool_name)
        if kind is None:
            raise HTTPException(status_code=404, detail="POLICY_NOT_FOUND")

        current_version = _current_active_version(session, kind, prompt_id_or_tool_name)
        if (
            payload.expected_active_version is not None
            and payload.expected_active_version != current_version
        ):
            raise HTTPException(
                status_code=409,
                detail={"code": "VERSION_CONFLICT", "current_active_version": current_version},
            )

        if kind == "prompt" and prompt_id_or_tool_name in _GATED_PROMPT_IDS:
            # Design doc Decision 9 / EVALUATION-CONTRACT.md: a gated
            # prompt version may only become active if a passing
            # evaluation_runs row already exists for this exact
            # (prompt_id, version) pair -- activate_tool_version's path
            # (kind == "tool") is never reached by this branch.
            if not _prompt_evaluation_floor_met(
                session, auth, prompt_id=prompt_id_or_tool_name, version=payload.version
            ):
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "EVALUATION_FLOORS_NOT_MET",
                        "prompt_id": prompt_id_or_tool_name,
                        "version": payload.version,
                    },
                )

        result: PromptVersion | ToolDefinition | PromptVersionNotFound | ToolVersionNotFound
        if kind == "tool":
            result = activate_tool_version(session, prompt_id_or_tool_name, payload.version)
        else:
            result = activate_prompt_version(session, prompt_id_or_tool_name, payload.version)

        if isinstance(result, PromptVersionNotFound | ToolVersionNotFound):
            raise HTTPException(status_code=404, detail="POLICY_NOT_FOUND")

        response = PolicyActivateResponse(
            kind=kind,
            name=prompt_id_or_tool_name,
            active_version=result.version,
            status="active",
        )
        _write_activation_audit(
            session,
            auth,
            request,
            kind=kind,
            aggregate_id=result.id,
            name=prompt_id_or_tool_name,
            version=result.version,
            now=now,
        )
        _store_idempotency(session, auth, idempotency_key, request_hash, response, now)
        return response
