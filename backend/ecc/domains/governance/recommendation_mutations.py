from collections.abc import Callable
from datetime import UTC, datetime
from json import dumps
from types import SimpleNamespace
from typing import Annotated, Any, Literal, cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session
from ecc.domains.governance.recommendation_events import record_event, record_feedback
from ecc.domains.governance.recommendation_models import (
    ConfirmAction,
    DeferAction,
    PinAction,
    RecommendationCreate,
    RecommendationResponse,
    RejectAction,
    VersionAction,
)
from ecc.domains.governance.recommendation_storage import (
    FIELDS,
    check_version,
    expire_if_needed,
    get_row,
    load_cached,
    lock_idempotency,
    project,
    request_hash,
    save_cached,
)
from ecc.domains.governance.recommendation_targets import (
    execute_target,
    target_version,
    validate_action,
)
from ecc.platform import authz
from ecc.platform.connector_security import (
    EMAIL_RECOMMENDATION_TYPE,
    EmailConsentInactiveError,
    personal_derived_row_scope,
    require_active_members_locked,
)

router = APIRouter(prefix="/api/v1/recommendations", tags=["recommendations"])
SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]


def _start(
    session: Session,
    auth: AuthContext,
    idempotency_key: str,
    digest: str,
) -> RecommendationResponse | None:
    lock_idempotency(session, auth, idempotency_key)
    return load_cached(session, auth, idempotency_key, digest)


def synthetic_request(request_id: UUID, correlation_id: UUID) -> Request:
    """A minimal `Request`-shaped stand-in for `create_recommendation`'s
    internal (non-HTTP) callers -- Phase 10 Task 5's own sync-pipeline hook
    (`gmail_adapter.py`), which has no real inbound HTTP request to thread
    through, the same "no live request" gap `recommendation_targets.py`'s
    own `_request_ids` fallback-to-`uuid4()` already handles for a
    *missing* id pair. `record_event` only ever reads `request.state.
    request_id`/`request.state.correlation_id` (never any other `Request`
    attribute) across every call site in this module, so a `SimpleNamespace`
    satisfying just that shape is sufficient; `cast` tells mypy to trust it
    structurally matches `Request` for this one narrow use.
    """
    return cast(
        Request,
        SimpleNamespace(
            state=SimpleNamespace(request_id=str(request_id), correlation_id=str(correlation_id))
        ),
    )


def create_recommendation(
    session: Session,
    auth: AuthContext,
    payload: RecommendationCreate,
    request: Request,
    idempotency_key: str,
    *,
    visibility: Literal["workspace", "private"] = "workspace",
    require_active_actor: bool = False,
    write_guard: Callable[[Session], None] | None = None,
) -> RecommendationResponse:
    """`generate_recommendation`'s full body, factored out so a non-HTTP
    caller can create a recommendation the exact same way `POST /api/v1/
    recommendations` does -- Task 4's own "no second insert path" discipline
    (`insert_task`/`insert_commitment`/`insert_risk`) applied one layer up,
    to recommendation creation itself, for Phase 10 Task 5's proactive
    `email.detect_action` sync-pipeline hook (the first non-HTTP caller).
    The HTTP endpoint below is now a thin wrapper supplying `AuthDep`/
    `CsrfDep`/the real inbound `Request` this function itself has no
    opinion on; `request` here is used only for `record_event`'s
    `request_id`/`correlation_id` (see `synthetic_request` above for the
    non-HTTP caller's own supplied value), and `idempotency_key` is passed
    explicitly rather than resolved from an HTTP header, so a system caller
    can synthesize its own stable, replay-safe key (e.g. `f"email-detect-
    action:{message_id}"`) instead of requiring a browser-originated
    `Idempotency-Key` header that does not exist for it.

    `visibility` (Spec A S1.8(a)): `"workspace"` for every caller except
    the Gmail action-detection hook, which passes `"private"` when
    `ECC_PERSONAL_DATA_ISOLATION` is on. The row's `owner_id` is always
    `auth.user_id` -- exactly what the `created_by` default-owner trigger
    already assigned -- never a separately supplied user, so a caller
    cannot make a private row owned by someone other than the actor it
    authenticated as (the detection hook builds `auth` from the connector
    account's own owner, i.e. the mailbox owner).

    `write_guard` (FX5): called with `session` after the idempotency lock
    and before any row lock or write; whatever it raises propagates.
    """
    authz.require_role_action(session, auth, "write")
    validate_action(payload.target_type, payload.proposed_action)
    is_create = payload.proposed_action.get("operation") == "create"
    digest = request_hash(payload, "generate")
    if require_active_actor:
        # Spec A S1.11 (opt-in; the Gmail action-detection hook): the role
        # check above is not locked -- re-check the actor (the mailbox
        # owner, who owns the row) under the shared membership lock, taken
        # before `_start`'s idempotency lock (lock order: membership ->
        # idempotency -> rows). Inactive -> `MembershipInactiveError`.
        require_active_members_locked(
            session, workspace_id=auth.workspace_id, users_ids=[auth.user_id]
        )
    cached = _start(session, auth, idempotency_key, digest)
    if cached is not None:
        return cached
    if write_guard is not None:
        # FX5 (opt-in; the Gmail action-detection hook's consent re-check):
        # after the membership and idempotency locks (lock order:
        # membership -> idempotency -> rows), before any row lock or
        # write. Raising leaves nothing written.
        write_guard(session)
    if not is_create:
        # `operation="create"` proposes a brand-new row -- there is no
        # existing target to serialize concurrent generation against or
        # to check a version for (`target_id`/`expected_version` are both
        # `None`, enforced by `RecommendationCreate`'s own model
        # validator), so this whole block is skipped for it. The
        # validator also guarantees both are set here; the local `target_id`
        # narrows the type for mypy without weakening that guarantee.
        target_id = payload.target_id
        assert target_id is not None
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {
                "key": (
                    f"recommendation-target:{auth.workspace_id}:{payload.target_type}:{target_id}"
                )
            },
        )
        current_version = target_version(
            session,
            auth.workspace_id,
            payload.target_type,
            target_id,
        )
        if current_version is None:
            raise HTTPException(status_code=404, detail="TARGET_NOT_FOUND")
        if current_version != payload.expected_version:
            raise HTTPException(status_code=409, detail="TARGET_VERSION_CONFLICT")
    now = datetime.now(UTC)
    superseded = (
        session.execute(
            text(
                f"""
                UPDATE recommendations
                SET status='superseded', version=version+1,
                    updated_at=:now, updated_by=:actor_id
                WHERE workspace_id=:workspace_id
                  AND target_type=:target_type
                  AND target_id=:target_id
                  AND status IN ('proposed','pending_confirmation')
                  AND archived_at IS NULL
                RETURNING {FIELDS}
                """
            ),
            {
                "now": now,
                "actor_id": auth.user_id,
                "workspace_id": auth.workspace_id,
                "target_type": payload.target_type,
                "target_id": payload.target_id,
            },
        )
        .mappings()
        .all()
    )
    for previous in superseded:
        previous_row = dict(previous)
        record_event(
            request,
            session,
            auth,
            previous_row,
            "recommendation.superseded",
            {"status": "active"},
            ["status"],
        )
    row = (
        session.execute(
            text(
                f"""
            INSERT INTO recommendations (
                id, workspace_id, recommendation_type, target_type, target_id,
                proposed_action, proposed_fields, expected_version, rationale,
                confidence, status, evidence_ids, expires_at, source, pinned,
                created_by, updated_by, created_at, updated_at, version,
                owner_id, visibility
            ) VALUES (
                :id, :workspace_id, :recommendation_type, :target_type, :target_id,
                CAST(:proposed_action AS jsonb), CAST(:proposed_fields AS jsonb),
                :expected_version, :rationale, :confidence, 'proposed',
                :evidence_ids, :expires_at, :source, false, :actor_id, :actor_id,
                :created_at, :created_at, 1, :actor_id, :visibility
            ) RETURNING {FIELDS}
            """
            ),
            {
                "id": uuid4(),
                "workspace_id": auth.workspace_id,
                "recommendation_type": payload.recommendation_type,
                "target_type": payload.target_type,
                "target_id": payload.target_id,
                "proposed_action": dumps(payload.proposed_action),
                "proposed_fields": (
                    dumps(payload.proposed_fields) if payload.proposed_fields is not None else None
                ),
                "expected_version": payload.expected_version,
                "rationale": payload.rationale,
                "confidence": payload.confidence,
                "evidence_ids": payload.evidence_ids,
                "expires_at": payload.expires_at,
                "source": payload.source,
                "actor_id": auth.user_id,
                "created_at": now,
                "visibility": visibility,
            },
        )
        .mappings()
        .one()
    )
    current = dict(row)
    response = project(current)
    record_event(
        request,
        session,
        auth,
        current,
        "recommendation.generated",
        None,
        ["status"],
        {
            "recommendation_id": str(current["id"]),
            "source": current["source"],
            "evidence_ids": [str(value) for value in current["evidence_ids"]],
            "confidence": float(current["confidence"]),
        },
    )
    save_cached(session, auth, idempotency_key, digest, response, 201, now)
    session.commit()
    return response


@router.post("", response_model=RecommendationResponse, status_code=201)
def generate_recommendation(
    payload: RecommendationCreate,
    request: Request,
    auth: AuthDep,
    _: CsrfDep,
    session: SessionDep,
    idempotency_key: IdempotencyHeader,
) -> RecommendationResponse:
    # Spec A plan note N19(2): `email_action_detected` is reserved for the
    # Gmail action-detection hook (the only writer of a real, Gmail-derived,
    # mailbox-owner-private one). A member-created row of that type would
    # match the personal-data predicate -- share-refused and not blocking
    # the creator's removal -- while being neither private nor from Gmail.
    # Refused whatever `ECC_PERSONAL_DATA_ISOLATION` says, in the same
    # `VALIDATION_ERROR` shape a `RecommendationCreate` field violation has
    # (the model itself is shared with the hook, so it cannot refuse it).
    if payload.recommendation_type == EMAIL_RECOMMENDATION_TYPE:
        raise HTTPException(
            status_code=422,
            detail=[
                {
                    "type": "value_error",
                    "loc": ["body", "recommendation_type"],
                    "msg": "recommendation_type is reserved",
                }
            ],
        )
    return create_recommendation(session, auth, payload, request, idempotency_key)


def _transition(
    recommendation_id: UUID,
    payload: VersionAction,
    request: Request,
    auth: AuthContext,
    session: Session,
    idempotency_key: str,
    *,
    action_name: str,
    allowed_statuses: set[str],
    updates: dict[str, Any],
    feedback_action: str | None = None,
    feedback_reason: str | None = None,
    feedback_defer_until: datetime | None = None,
) -> RecommendationResponse:
    digest = request_hash(payload, action_name)
    cached = _start(session, auth, idempotency_key, digest)
    if cached is not None:
        return cached
    # Lock before authorizing: an ownership transfer that commits while
    # this request waits on the row lock must be seen by the checks below
    # (READ COMMITTED: each later statement reads the committed row), not
    # by checks that ran against the pre-transfer row.
    locked = get_row(session, auth, recommendation_id, for_update=True)
    if not authz.authorize(
        session, auth, resource_type="recommendations", resource_id=recommendation_id, action="read"
    ):
        raise HTTPException(status_code=404, detail="RECOMMENDATION_NOT_FOUND")
    if not authz.authorize(
        session,
        auth,
        resource_type="recommendations",
        resource_id=recommendation_id,
        action="write",
    ):
        raise HTTPException(status_code=403, detail="INSUFFICIENT_ROLE")
    row = expire_if_needed(session, auth, locked, request=request)
    check_version(row, int(payload.expected_version))
    if row["status"] not in allowed_statuses:
        raise HTTPException(status_code=409, detail="INVALID_RECOMMENDATION_STATE")
    before = {
        "status": row["status"],
        "pinned": row["pinned"],
        "deferred_until": row["deferred_until"].isoformat() if row["deferred_until"] else None,
    }
    clauses = ["version=version+1", "updated_at=:updated_at", "updated_by=:actor_id"]
    params: dict[str, Any] = {
        "workspace_id": auth.workspace_id,
        "recommendation_id": recommendation_id,
        "updated_at": datetime.now(UTC),
        "actor_id": auth.user_id,
    }
    for field, value in updates.items():
        clauses.append(f"{field}=:{field}")
        params[field] = value
    updated = (
        session.execute(
            text(
                f"UPDATE recommendations SET {', '.join(clauses)} "  # noqa: S608 -- SET keys are literals from callers; values bound
                f"WHERE workspace_id=:workspace_id AND id=:recommendation_id RETURNING {FIELDS}"
            ),
            params,
        )
        .mappings()
        .one()
    )
    current = dict(updated)
    if feedback_action is not None:
        record_feedback(
            session,
            auth,
            recommendation_id,
            feedback_action,
            reason=feedback_reason,
            defer_until=feedback_defer_until,
        )
    record_event(
        request,
        session,
        auth,
        current,
        action_name,
        before,
        list(updates),
    )
    response = project(current)
    save_cached(
        session,
        auth,
        idempotency_key,
        digest,
        response,
        200,
        params["updated_at"],
    )
    session.commit()
    return response


@router.post("/{recommendation_id}/publish", response_model=RecommendationResponse)
def publish_recommendation(
    recommendation_id: UUID,
    payload: VersionAction,
    request: Request,
    auth: AuthDep,
    _: CsrfDep,
    session: SessionDep,
    idempotency_key: IdempotencyHeader,
) -> RecommendationResponse:
    return _transition(
        recommendation_id,
        payload,
        request,
        auth,
        session,
        idempotency_key,
        action_name="recommendation.confirmation_requested",
        allowed_statuses={"proposed"},
        updates={"status": "pending_confirmation"},
    )


@router.post("/{recommendation_id}/reject", response_model=RecommendationResponse)
def reject_recommendation(
    recommendation_id: UUID,
    payload: RejectAction,
    request: Request,
    auth: AuthDep,
    _: CsrfDep,
    session: SessionDep,
    idempotency_key: IdempotencyHeader,
) -> RecommendationResponse:
    return _transition(
        recommendation_id,
        payload,
        request,
        auth,
        session,
        idempotency_key,
        action_name="recommendation.rejected",
        allowed_statuses={"pending_confirmation"},
        updates={"status": "rejected"},
        feedback_action="reject",
        feedback_reason=payload.reason,
    )


@router.post("/{recommendation_id}/defer", response_model=RecommendationResponse)
def defer_recommendation(
    recommendation_id: UUID,
    payload: DeferAction,
    request: Request,
    auth: AuthDep,
    _: CsrfDep,
    session: SessionDep,
    idempotency_key: IdempotencyHeader,
) -> RecommendationResponse:
    if payload.defer_until <= datetime.now(UTC):
        raise HTTPException(status_code=422, detail="DEFER_UNTIL_MUST_BE_FUTURE")
    return _transition(
        recommendation_id,
        payload,
        request,
        auth,
        session,
        idempotency_key,
        action_name="recommendation.deferred",
        allowed_statuses={"proposed", "pending_confirmation"},
        updates={"deferred_until": payload.defer_until},
        feedback_action="defer",
        feedback_defer_until=payload.defer_until,
    )


@router.post("/{recommendation_id}/pin", response_model=RecommendationResponse)
def pin_recommendation(
    recommendation_id: UUID,
    payload: PinAction,
    request: Request,
    auth: AuthDep,
    _: CsrfDep,
    session: SessionDep,
    idempotency_key: IdempotencyHeader,
) -> RecommendationResponse:
    return _transition(
        recommendation_id,
        payload,
        request,
        auth,
        session,
        idempotency_key,
        action_name="recommendation.pinned",
        allowed_statuses={"proposed", "pending_confirmation"},
        updates={"pinned": payload.pinned},
        feedback_action="pin",
    )


def _require_email_consent_for_confirm(
    session: Session, auth: AuthContext, recommendation_id: UUID
) -> None:
    """FX5 round 3: confirming an `email_action_detected` recommendation
    copies its Gmail-derived content into a new task and marks it
    `executed` -- a row the revocation cascade only redacts. So its owner's
    `email` consent is re-checked under the same locks every Gmail write
    takes (`gmail_shared.require_email_consent_locked`): the owner's `email`
    domain row, then their Gmail connector row(s), `FOR KEY SHARE`.

    Called after the idempotency lock and BEFORE the recommendation row's
    `FOR UPDATE`, keeping the cascade's order (domain row -> connector rows
    -> recommendation rows); taking it after the row lock would create a
    deadlock cycle with the cascade. A cascade that committed first is seen
    (403 `EMAIL_CONSENT_NOT_ACTIVE`, nothing written -- the caller's
    session rolls back); one that starts later waits for this confirm to
    commit and then redacts the `executed` row.

    Owner-wide connector form (`connector_account_id=None`), a deliberate
    choice: a recommendation does not record which connector produced it,
    so every one of the owner's Gmail rows is locked. Side effect: this
    confirm can wait on a sync's phase-1 `FOR UPDATE` held across a token
    refresh (known limitation, see `connector_accounts._run_connector_sync`):
    the wait can exceed the 5s statement timeout, giving a generic 500 with
    nothing written; the caller retries after a few seconds.

    The type/owner read here is unlocked. `recommendation_type` never
    changes. `owner_id` can change only through an ownership transfer, which
    `ECC_PERSONAL_DATA_ISOLATION` refuses for personal data; with the flag
    off an email-derived recommendation can be transferred, and this then
    checks the NEW owner's consent. That is not a widening: the cascade
    purges by the owner at purge time (`recommendations.owner_id`), so a
    transferred row is outside the old mailbox owner's cascade either way,
    and the check can only refuse more, never allow a write the cascade
    would otherwise have to clean up. A transfer racing this unlocked read
    at worst checks the previous owner. No refusal audit: none of this
    endpoint's other 4xx refusals write one either.
    """
    # Deferred import: `personal` already imports this module (the
    # detection hook calls `create_recommendation`), so importing it back at
    # module level would be a cycle-prone domain back-reference.
    from ecc.domains.personal.gmail_shared import require_email_consent_locked

    target = session.execute(
        text(
            "SELECT recommendation_type, owner_id FROM recommendations "
            "WHERE workspace_id = :workspace_id AND id = :recommendation_id"
        ),
        {"workspace_id": auth.workspace_id, "recommendation_id": recommendation_id},
    ).one_or_none()
    if target is None or target[0] != EMAIL_RECOMMENDATION_TYPE or target[1] is None:
        return
    try:
        require_email_consent_locked(
            session,
            workspace_id=auth.workspace_id,
            owner_id=target[1],
            connector_account_id=None,
        )
    except EmailConsentInactiveError:
        raise HTTPException(status_code=403, detail=EmailConsentInactiveError.code) from None


@router.post("/{recommendation_id}/confirm", response_model=RecommendationResponse)
def confirm_recommendation(
    recommendation_id: UUID,
    payload: ConfirmAction,
    request: Request,
    auth: AuthDep,
    _: CsrfDep,
    session: SessionDep,
    idempotency_key: IdempotencyHeader,
) -> RecommendationResponse:
    digest = request_hash(payload, "recommendation.confirm")
    cached = _start(session, auth, idempotency_key, digest)
    if cached is not None:
        return cached
    if not authz.authorize(
        session, auth, resource_type="recommendations", resource_id=recommendation_id, action="read"
    ):
        raise HTTPException(status_code=404, detail="RECOMMENDATION_NOT_FOUND")
    if not authz.authorize(
        session,
        auth,
        resource_type="recommendations",
        resource_id=recommendation_id,
        action="write",
    ):
        raise HTTPException(status_code=403, detail="INSUFFICIENT_ROLE")
    _require_email_consent_for_confirm(session, auth, recommendation_id)
    # Lock before authorizing: an ownership transfer that commits while
    # this request waits on the row lock must be seen by the checks below
    # (READ COMMITTED: each later statement reads the committed row), not
    # by checks that ran against the pre-transfer row.
    # The pair above stays: the consent check must run before this lock
    # (see `_require_email_consent_for_confirm`) and must not answer a
    # caller who cannot see the recommendation; the pair is re-run here.
    locked = get_row(session, auth, recommendation_id, for_update=True)
    if not authz.authorize(
        session, auth, resource_type="recommendations", resource_id=recommendation_id, action="read"
    ):
        raise HTTPException(status_code=404, detail="RECOMMENDATION_NOT_FOUND")
    if not authz.authorize(
        session,
        auth,
        resource_type="recommendations",
        resource_id=recommendation_id,
        action="write",
    ):
        raise HTTPException(status_code=403, detail="INSUFFICIENT_ROLE")
    row = expire_if_needed(session, auth, locked, request=request)
    check_version(row, payload.expected_version)
    is_create = row["proposed_action"].get("operation") == "create"
    if is_create:
        # No existing target to have a version of -- `RecommendationCreate`'s
        # own model validator already guarantees `row["expected_version"]`
        # is `None` for this row, so there is nothing for the caller to
        # echo back.
        if payload.target_expected_version is not None:
            raise HTTPException(status_code=422, detail="TARGET_EXPECTED_VERSION_NOT_ALLOWED")
    else:
        if payload.target_expected_version is None:
            raise HTTPException(status_code=422, detail="TARGET_EXPECTED_VERSION_REQUIRED")
        if payload.target_expected_version != int(row["expected_version"]):
            raise HTTPException(status_code=409, detail="TARGET_VERSION_CONFLICT")
    if row["status"] != "pending_confirmation":
        raise HTTPException(status_code=409, detail="INVALID_RECOMMENDATION_STATE")
    if row["deferred_until"] is not None and row["deferred_until"] > datetime.now(UTC):
        raise HTTPException(status_code=409, detail="RECOMMENDATION_DEFERRED")
    accepted_at = datetime.now(UTC)
    accepted = (
        session.execute(
            text(
                f"""
            UPDATE recommendations
            SET status='accepted', confirmed_by=:actor_id, confirmed_at=:confirmed_at,
                version=version+1, updated_at=:confirmed_at, updated_by=:actor_id
            WHERE workspace_id=:workspace_id AND id=:recommendation_id
            RETURNING {FIELDS}
            """
            ),
            {
                "actor_id": auth.user_id,
                "confirmed_at": accepted_at,
                "workspace_id": auth.workspace_id,
                "recommendation_id": recommendation_id,
            },
        )
        .mappings()
        .one()
    )
    accepted_row = dict(accepted)
    record_feedback(session, auth, recommendation_id, "accept")
    record_event(
        request,
        session,
        auth,
        accepted_row,
        "recommendation.accepted",
        {"status": "pending_confirmation"},
        ["status", "confirmed_by", "confirmed_at"],
    )
    execution_result = execute_target(
        session,
        auth,
        request,
        recommendation_id,
        row["target_type"],
        row["target_id"],
        row["proposed_action"],
        int(row["expected_version"]) if row["expected_version"] is not None else None,
        row["proposed_fields"],
        # Spec A plan note N23: a personal-data recommendation's created
        # target copies its email-derived content -- with the flag on it is
        # the recommendation owner's and `private`, never workspace-visible.
        personal_derived_row_scope(session, "recommendations", recommendation_id),
    )
    executed_at = datetime.now(UTC)
    executed = (
        session.execute(
            text(
                f"""
            UPDATE recommendations
            SET status='executed', execution_result=CAST(:execution_result AS jsonb),
                version=version+1, updated_at=:updated_at, updated_by=:actor_id
            WHERE workspace_id=:workspace_id AND id=:recommendation_id
            RETURNING {FIELDS}
            """
            ),
            {
                "execution_result": dumps(execution_result),
                "updated_at": executed_at,
                "actor_id": auth.user_id,
                "workspace_id": auth.workspace_id,
                "recommendation_id": recommendation_id,
            },
        )
        .mappings()
        .one()
    )
    current = dict(executed)
    record_event(
        request,
        session,
        auth,
        current,
        "recommendation.executed",
        {"status": "accepted"},
        ["status", "execution_result"],
        {"recommendation_id": str(recommendation_id), **execution_result},
    )
    response = project(current)
    save_cached(session, auth, idempotency_key, digest, response, 200, executed_at)
    session.commit()
    return response
