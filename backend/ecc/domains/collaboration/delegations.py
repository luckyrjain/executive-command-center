"""`ecc.domains.collaboration.delegations` -- Phase 8 Task 6
(`docs/superpowers/plans/2026-08-01-phase-8-multi-user.md` Task 6,
`docs/phases/phase-008/DELEGATION-CONTRACT.md`). `GET|POST /delegations`,
`POST /delegations/{id}/accept|reject|revoke|complete`.

**State machine exactly as the contract names, with one disclosed gap.**
`proposed -> accepted|rejected|expired`; `accepted -> completed|revoked|
cancelled`. `revoke` is therefore only ever valid from `accepted` (matching
the diagram literally) -- there is no route in this task's own scope
(`API-SCHEMAS.md`'s route list has no `/cancel`, and the diagram gives
`proposed` no `revoked` exit) for a delegator to withdraw a still-`proposed`
delegation before the recipient acts on it; they can only wait for
`reject`/`expired`. `cancelled` is reserved entirely for a future
*system*-initiated transition (Task 8's member-removal flow resolving an
active delegation involving a removed member) -- no endpoint here can ever
produce it, the identical `superseded`-on-`DecisionStatus` precedent
(`decisions_incidents.py`).

**Who may act at each transition is a disclosed judgment call**, since
neither the contract nor the plan states it explicitly: any active member
may *propose* a delegation (not role-gated beyond membership -- this isn't
"create a workspace resource" in the same sense `require_role_action`
gates elsewhere), but only if they hold `write` access to the obligation
resource itself (`authz.authorize`, two-phase read-then-write, the
established 404-before-403 shape) -- delegating responsibility for
something implies control over it, and a `viewer`'s baseline lacks `write`
so this also naturally excludes them from proposing at all. `accept`/
`reject`/`complete` are recipient-only (only the recipient can know
whether they're taking it on, declining it, or have finished it);
`revoke` is delegator-only (the same "whoever creates a grant/obligation
can undo it" principle `resource_grants` revocation already uses).
`owner`/`admin` additionally see every delegation in the workspace on
`GET /delegations`/`GET /delegations/{id}` (oversight, mirroring `authz.
list_grants_endpoint`'s identical "owner/admin see everything, everyone
else sees only what names them" split) but cannot accept/reject/revoke/
complete one they are not a party to.

**Evidence and its grants.** A delegation's `evidence` (`delegation_
evidence`, a real addition beyond the plan's own literal migration bullet
-- see migration `0064`'s own docstring for why) always includes the
obligation resource itself (added server-side, so a recipient can always
at least see what they're being asked to act on) plus whatever the
delegator names explicitly, each requiring the delegator hold `read`
access to it at proposal time. Acceptance creates one read-only (`actions
= ['read']`, never broader) `resource_grants` row per evidence item,
naming exactly that set -- the contract's own "never a broader grant"
line. Each such grant auto-widens its resource from `private` to
`shared_explicitly` if needed (the identical strict-widening half of
`ecc.platform.authz.create_grant_endpoint`'s own Task 5 fix -- a
conditional `UPDATE ... WHERE visibility = 'private'`, a safe no-op
otherwise) but never narrows an already-`workspace`-visible resource --
unlike Task 5's own explicit, UI-mediated `narrow_visibility` flow, an
`accept` call has no way to surface "N members will lose default access"
for confirmation, so it simply leaves a `workspace`-visibility evidence
resource as is (the grant becomes redundant but harmless, since the
recipient already reads it via role). These grants carry `expires_at =
NULL` -- the contract's "auto-revoked the moment the delegation reaches
any terminal state" is enforced by directly revoking them (`revoked_at`
set) when the delegation reaches `completed`/`revoked`, not by a time
limit (`rejected`/`expired` both only ever happen from `proposed`, before
any grant exists, so neither needs a revocation step).

**Lazy expiry, no background job.** `_maybe_expire_single`/`_expire_due`
transition an overdue `proposed` delegation to `expired` the moment any
endpoint next reads or mutates it -- no scheduler is named in this task's
own scope. `GET /delegations` bulk-expires every overdue delegation
visible to the caller in one statement (`owner`/`admin`: every overdue
delegation in the workspace; everyone else: only ones naming their own
account) rather than one `UPDATE` per row, avoiding the exact N+1 query
shape a Task 4 CI failure already caught in a different domain
(`test_waiting_link_rejects_deep_circular_blocked_by_chain`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from json import dumps
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session
from ecc.observability import record_idempotency_conflict
from ecc.platform import authz
from ecc.platform.authz import UnknownResourceTypeError

router = APIRouter(prefix="/api/v1/delegations", tags=["delegations"])
SessionDep = Annotated[Session, Depends(get_session)]
IdempotencyHeader = Annotated[
    str,
    Header(alias="Idempotency-Key", min_length=1, max_length=255),
]

_IDEMPOTENCY_TTL = timedelta(hours=24)

_DELEGATION_FIELDS = (
    "id, delegator_account_id, recipient_account_id, obligation_type, "
    "obligation_resource_id, expected_outcome, due_at, status, created_at, updated_at"
)


class EvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resource_type: str
    resource_id: UUID


class DelegationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    recipient_account_id: UUID
    obligation_type: str
    obligation_resource_id: UUID
    expected_outcome: str = Field(min_length=1, max_length=4000)
    due_at: datetime
    evidence: list[EvidenceRef] = Field(default_factory=list)


class DelegationResponse(BaseModel):
    id: UUID
    delegator_account_id: UUID
    recipient_account_id: UUID
    obligation_type: str
    obligation_resource_id: UUID
    expected_outcome: str
    due_at: datetime
    status: str
    evidence: list[EvidenceRef]
    created_at: datetime
    updated_at: datetime


class DelegationListResponse(BaseModel):
    delegations: list[DelegationResponse]


def _account_id_for(session: Session, *, workspace_id: UUID, users_id: UUID) -> UUID:
    row = (
        session.execute(
            text("SELECT account_id FROM users WHERE workspace_id = :workspace_id AND id = :id"),
            {"workspace_id": workspace_id, "id": users_id},
        )
        .mappings()
        .one()
    )
    result: UUID = row["account_id"]
    return result


def _users_id_for_account(session: Session, *, workspace_id: UUID, account_id: UUID) -> UUID:
    """The inverse of `_account_id_for` -- `resource_grants.granted_by` FKs
    to `users.(workspace_id, id)`, not `accounts.id`, so `_grant_evidence`
    (called from `accept_delegation_endpoint`, where `auth.user_id` is the
    *recipient's* own `users_id`, not the delegator's) needs this to stamp
    `granted_by` as the delegator -- the party who actually decided this
    evidence should be shared -- rather than the recipient granting
    themselves access, which `revoke_grant_endpoint`'s "the original
    granter may always revoke" rule would then misattribute.
    """
    row = (
        session.execute(
            text(
                "SELECT id FROM users "
                "WHERE workspace_id = :workspace_id AND account_id = :account_id"
            ),
            {"workspace_id": workspace_id, "account_id": account_id},
        )
        .mappings()
        .one()
    )
    result: UUID = row["id"]
    return result


def _request_hash(action: str, payload: BaseModel | None = None) -> str:
    material = {"action": action, "payload": payload.model_dump(mode="json") if payload else None}
    return sha256(dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _lock_idempotency(session: Session, auth: AuthContext, key: str) -> None:
    lock_key = f"{auth.workspace_id}:{auth.user_id}:{key}"
    session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
        {"lock_key": lock_key},
    )


def _load_cached(
    session: Session, auth: AuthContext, key: str, request_hash: str
) -> dict[str, Any] | None:
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
        record_idempotency_conflict("collaboration_delegations")
        raise HTTPException(status_code=409, detail="IDEMPOTENCY_CONFLICT")
    result: dict[str, Any] = row["response_body"]
    return result


def _store_idempotency(
    session: Session,
    auth: AuthContext,
    key: str,
    request_hash: str,
    response_body: dict[str, Any],
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
            "response_body": dumps(response_body),
            "created_at": now,
            "expires_at": now + _IDEMPOTENCY_TTL,
        },
    )


def _get_delegation(
    session: Session, workspace_id: UUID, delegation_id: UUID
) -> dict[str, Any] | None:
    row = (
        session.execute(
            text(
                f"SELECT {_DELEGATION_FIELDS} FROM delegations "  # noqa: S608
                "WHERE workspace_id = :workspace_id AND id = :id"
            ),
            {"workspace_id": workspace_id, "id": delegation_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _evidence_for(session: Session, delegation_id: UUID) -> list[EvidenceRef]:
    rows = (
        session.execute(
            text(
                "SELECT resource_type, resource_id FROM delegation_evidence "
                "WHERE delegation_id = :id ORDER BY created_at ASC"
            ),
            {"id": delegation_id},
        )
        .mappings()
        .all()
    )
    return [
        EvidenceRef(resource_type=r["resource_type"], resource_id=r["resource_id"]) for r in rows
    ]


def _to_response(session: Session, row: dict[str, Any]) -> DelegationResponse:
    return DelegationResponse(**row, evidence=_evidence_for(session, row["id"]))


def _write_event(
    session: Session,
    delegation_id: UUID,
    event_type: str,
    actor_account_id: UUID | None,
    now: datetime,
    detail: str | None = None,
) -> None:
    session.execute(
        text(
            "INSERT INTO delegation_events (id, delegation_id, event_type, actor_account_id, "
            "occurred_at, detail) VALUES (:id, :delegation_id, :event_type, :actor_account_id, "
            ":now, :detail)"
        ),
        {
            "id": uuid4(),
            "delegation_id": delegation_id,
            "event_type": event_type,
            "actor_account_id": actor_account_id,
            "now": now,
            "detail": detail,
        },
    )


def _notify_member(
    session: Session,
    *,
    workspace_id: UUID,
    account_id: UUID,
    notification_type: str,
    resource_ref: str,
    now: datetime,
) -> None:
    """Private, duplicated per-module rather than imported from
    `ecc.platform.notifications` -- see that module's own docstring for the
    layering reason (and `ecc.platform.authz`'s identical copy, the same
    per-module-duplication convention this file's own idempotency helpers
    already follow).
    """
    session.execute(
        text(
            """
            INSERT INTO member_notifications (
                id, workspace_id, account_id, notification_type, resource_ref, created_at
            ) VALUES (
                :id, :workspace_id, :account_id, :notification_type, :resource_ref, :now
            )
            ON CONFLICT (workspace_id, account_id, notification_type, resource_ref) DO NOTHING
            """
        ),
        {
            "id": uuid4(),
            "workspace_id": workspace_id,
            "account_id": account_id,
            "notification_type": notification_type,
            "resource_ref": resource_ref,
            "now": now,
        },
    )


def _notify_expired(
    session: Session, *, workspace_id: UUID, delegation: dict[str, Any], now: datetime
) -> None:
    """Both parties, not just one -- unlike every other transition (each
    caused by one specific party's own action, so only the *other* party
    needs telling), expiry is system-initiated and equally news to both.
    Also this module's own concrete test fixture for the migration's
    "duplicate underlying event does not double-notify" guarantee: two
    concurrent requests racing `_maybe_expire_single`/`_expire_due` on the
    same overdue delegation only ever produce one `delegation.expired`
    notification per party, via `member_notifications`'s own `ON CONFLICT
    DO NOTHING`, not this function taking any lock itself.
    """
    ref = f"delegations:{delegation['id']}"
    for account_id in (delegation["delegator_account_id"], delegation["recipient_account_id"]):
        _notify_member(
            session,
            workspace_id=workspace_id,
            account_id=account_id,
            notification_type="delegation.expired",
            resource_ref=ref,
            now=now,
        )


def _maybe_expire_single(
    session: Session, *, workspace_id: UUID, delegation: dict[str, Any], now: datetime
) -> dict[str, Any]:
    if delegation["status"] != "proposed" or delegation["due_at"] >= now:
        return delegation
    updated = (
        session.execute(
            text(
                f"UPDATE delegations SET status = 'expired', updated_at = :now "  # noqa: S608
                f"WHERE id = :id AND status = 'proposed' RETURNING {_DELEGATION_FIELDS}"
            ),
            {"id": delegation["id"], "now": now},
        )
        .mappings()
        .one_or_none()
    )
    if updated is None:
        # Lost a race with a concurrent transition -- re-read fresh rather
        # than trust the now-stale `delegation` dict.
        refreshed = _get_delegation(session, workspace_id, delegation["id"])
        assert refreshed is not None  # the row cannot vanish, only change status
        return refreshed
    _write_event(session, delegation["id"], "expired", None, now)
    _notify_expired(session, workspace_id=workspace_id, delegation=dict(updated), now=now)
    return dict(updated)


def _expire_due(
    session: Session, *, workspace_id: UUID, account_id: UUID | None, now: datetime
) -> None:
    """`account_id=None` bulk-expires every overdue `proposed` delegation
    in the workspace (`owner`/`admin`'s own broader oversight visibility);
    otherwise scoped to delegations naming `account_id` as either party.
    """
    clause = (
        ""
        if account_id is None
        else "AND (delegator_account_id = :account_id OR recipient_account_id = :account_id) "
    )
    params: dict[str, Any] = {"workspace_id": workspace_id, "now": now}
    if account_id is not None:
        params["account_id"] = account_id
    expired_rows = (
        session.execute(
            text(
                "UPDATE delegations SET status = 'expired', updated_at = :now "
                "WHERE workspace_id = :workspace_id AND status = 'proposed' AND due_at < :now "
                f"{clause}RETURNING id, delegator_account_id, recipient_account_id"
            ),
            params,
        )
        .mappings()
        .all()
    )
    for row in expired_rows:
        _write_event(session, row["id"], "expired", None, now)
        _notify_expired(
            session,
            workspace_id=workspace_id,
            delegation={
                "id": row["id"],
                "delegator_account_id": row["delegator_account_id"],
                "recipient_account_id": row["recipient_account_id"],
            },
            now=now,
        )


def cancel_delegations_for_removed_member(
    session: Session, *, workspace_id: UUID, account_id: UUID, now: datetime
) -> None:
    """Phase 8 Task 8's own call site -- `ecc.domains.identity.membership_
    removal.remove_member_endpoint` calls this before finalizing a member's
    removal. Force-transitions every `proposed`/`accepted` delegation
    naming `account_id` as either party to `cancelled` -- migration
    `0064_phase8_delegations.py`'s own docstring reserved exactly this
    value for exactly this call site, unreachable from any user-facing
    endpoint in this module. Bulk, not one `UPDATE` per row -- the same
    N+1-avoidance shape `_expire_due` already established. An `accepted`
    delegation's evidence grants are revoked exactly like `revoke`/
    `complete` already do (`_revoke_evidence_grants`, defined below); a
    `proposed` one never had any to revoke.
    """
    rows = (
        session.execute(
            text(
                "UPDATE delegations SET status = 'cancelled', updated_at = :now "
                "WHERE workspace_id = :workspace_id AND status IN ('proposed', 'accepted') "
                "AND (delegator_account_id = :account_id OR recipient_account_id = :account_id) "
                "RETURNING id, recipient_account_id, status"
            ),
            {"workspace_id": workspace_id, "account_id": account_id, "now": now},
        )
        .mappings()
        .all()
    )
    for row in rows:
        _write_event(session, row["id"], "cancelled", None, now)
        _revoke_evidence_grants(
            session,
            workspace_id=workspace_id,
            delegation_id=row["id"],
            recipient_account_id=row["recipient_account_id"],
            now=now,
        )


def _grant_evidence(
    session: Session,
    *,
    workspace_id: UUID,
    delegation_id: UUID,
    recipient_account_id: UUID,
    granted_by: UUID,
    now: datetime,
) -> None:
    """`create_delegation_endpoint` already checked the delegator's own
    `read` access to every evidence item once, at proposal time -- but
    `proposed` can sit for however long `due_at` allows, and nothing
    re-checks that access before this function (called from
    `accept_delegation_endpoint`, potentially much later) actually grants
    it to the recipient. Found in the second whole-phase review: without
    this re-check, a delegator who loses access to an evidence resource
    between proposing and the recipient accepting (e.g. its ownership was
    transferred away from them in the interim) would still have that
    resource silently granted to the recipient, `granted_by` stamped with
    the now-access-less delegator -- the recipient ending up with read
    access neither the delegator nor the resource's real current owner
    ever actually approved. `authorize()` is re-run per item against a
    fresh `AuthContext` for the delegator (not the recipient calling this
    endpoint); an item that no longer passes is silently skipped rather
    than failing the whole accept, since the delegation's other evidence
    and its underlying obligation may still be perfectly valid.
    """
    # `timezone` is a placeholder -- `authorize()`'s six-step decision never
    # reads it, so fetching the delegator's real one would be a wasted query.
    delegator_auth = AuthContext(workspace_id=workspace_id, user_id=granted_by, timezone="UTC")
    for item in _evidence_for(session, delegation_id):
        authz.require_known_resource_type(item.resource_type)  # defense in depth
        if not authz.authorize(
            session,
            delegator_auth,
            resource_type=item.resource_type,
            resource_id=item.resource_id,
            action="read",
        ):
            continue
        session.execute(
            text(
                f"UPDATE {item.resource_type} SET visibility = 'shared_explicitly' "  # noqa: S608
                "WHERE id = :id AND visibility = 'private'"
            ),
            {"id": item.resource_id},
        )
        session.execute(
            text(
                """
                INSERT INTO resource_grants (
                    id, workspace_id, grantee_account_id, resource_type, resource_id,
                    actions, granted_by, expires_at, created_at, delegation_id
                ) VALUES (
                    :id, :workspace_id, :grantee_account_id, :resource_type, :resource_id,
                    :actions, :granted_by, NULL, :now, :delegation_id
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "grantee_account_id": recipient_account_id,
                "resource_type": item.resource_type,
                "resource_id": item.resource_id,
                "actions": ["read"],
                "granted_by": granted_by,
                "now": now,
                "delegation_id": delegation_id,
            },
        )


def _revoke_evidence_grants(
    session: Session,
    *,
    workspace_id: UUID,
    delegation_id: UUID,
    recipient_account_id: UUID,
    now: datetime,
) -> None:
    """Scoped by `delegation_id`, not merely by
    `(grantee_account_id, resource_type, resource_id)` -- if the same
    recipient is delegated two different obligations that both name the
    same evidence resource (a realistic scenario), matching on resource
    identity alone would revoke the *other*, still-`accepted` delegation's
    own evidence access too. `delegation_id` (migration
    `0067_phase8_grant_delegation_id.py`) makes each
    delegation's own evidence grants independently revocable.
    `recipient_account_id`/`resource_type`/`resource_id` are still checked
    too, defensively -- a delegation should only ever revoke grants that
    are actually its own and actually name its own evidence, not merely
    trust `delegation_id` alone.
    """
    for item in _evidence_for(session, delegation_id):
        session.execute(
            text(
                "UPDATE resource_grants SET revoked_at = :now "
                "WHERE workspace_id = :workspace_id AND grantee_account_id = :account_id "
                "AND resource_type = :resource_type AND resource_id = :resource_id "
                "AND delegation_id = :delegation_id AND revoked_at IS NULL"
            ),
            {
                "now": now,
                "workspace_id": workspace_id,
                "account_id": recipient_account_id,
                "resource_type": item.resource_type,
                "resource_id": item.resource_id,
                "delegation_id": delegation_id,
            },
        )


@router.post("", response_model=DelegationResponse, status_code=status.HTTP_201_CREATED)
def create_delegation_endpoint(
    payload: DelegationCreateRequest,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> DelegationResponse:
    authz.require_active_role(session, auth)
    now = datetime.now(UTC)
    if payload.due_at <= now:
        raise HTTPException(status_code=422, detail="DUE_AT_IN_PAST")

    try:
        authz.require_grantable(payload.obligation_type)
        for item in payload.evidence:
            authz.require_grantable(item.resource_type)
    except UnknownResourceTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="RESOURCE_TYPE_NOT_GRANTABLE"
        ) from exc

    request_hash = _request_hash("create", payload)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return DelegationResponse.model_validate(cached)

        delegator_account_id = _account_id_for(
            session, workspace_id=auth.workspace_id, users_id=auth.user_id
        )
        if payload.recipient_account_id == delegator_account_id:
            raise HTTPException(status_code=422, detail="CANNOT_DELEGATE_TO_SELF")

        if not authz.authorize(
            session,
            auth,
            resource_type=payload.obligation_type,
            resource_id=payload.obligation_resource_id,
            action="read",
        ):
            raise HTTPException(status_code=404, detail="OBLIGATION_NOT_FOUND")
        if not authz.authorize(
            session,
            auth,
            resource_type=payload.obligation_type,
            resource_id=payload.obligation_resource_id,
            action="write",
        ):
            raise HTTPException(status_code=403, detail="INSUFFICIENT_ROLE")

        recipient_membership = (
            session.execute(
                text(
                    "SELECT 1 FROM workspace_memberships "
                    "WHERE workspace_id = :workspace_id AND account_id = :account_id "
                    "AND status = 'active'"
                ),
                {"workspace_id": auth.workspace_id, "account_id": payload.recipient_account_id},
            )
            .mappings()
            .one_or_none()
        )
        if recipient_membership is None:
            raise HTTPException(status_code=404, detail="RECIPIENT_NOT_FOUND")

        for item in payload.evidence:
            if not authz.authorize(
                session,
                auth,
                resource_type=item.resource_type,
                resource_id=item.resource_id,
                action="read",
            ):
                raise HTTPException(status_code=404, detail="EVIDENCE_NOT_FOUND")

        delegation_id = uuid4()
        session.execute(
            text(
                """
                INSERT INTO delegations (
                    id, workspace_id, delegator_account_id, recipient_account_id,
                    obligation_type, obligation_resource_id, expected_outcome, due_at,
                    status, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, :delegator_account_id, :recipient_account_id,
                    :obligation_type, :obligation_resource_id, :expected_outcome, :due_at,
                    'proposed', :now, :now
                )
                """
            ),
            {
                "id": delegation_id,
                "workspace_id": auth.workspace_id,
                "delegator_account_id": delegator_account_id,
                "recipient_account_id": payload.recipient_account_id,
                "obligation_type": payload.obligation_type,
                "obligation_resource_id": payload.obligation_resource_id,
                "expected_outcome": payload.expected_outcome,
                "due_at": payload.due_at,
                "now": now,
            },
        )

        # The obligation resource is always evidence too, deduped against
        # anything the caller also named explicitly -- see this module's
        # own docstring for why.
        evidence_set = {(payload.obligation_type, payload.obligation_resource_id)}
        evidence_set.update((item.resource_type, item.resource_id) for item in payload.evidence)
        for resource_type, resource_id in evidence_set:
            session.execute(
                text(
                    "INSERT INTO delegation_evidence "
                    "(id, delegation_id, resource_type, resource_id, created_at) "
                    "VALUES (:id, :delegation_id, :resource_type, :resource_id, :now)"
                ),
                {
                    "id": uuid4(),
                    "delegation_id": delegation_id,
                    "resource_type": resource_type,
                    "resource_id": resource_id,
                    "now": now,
                },
            )

        _write_event(session, delegation_id, "proposed", delegator_account_id, now)
        _notify_member(
            session,
            workspace_id=auth.workspace_id,
            account_id=payload.recipient_account_id,
            notification_type="delegation.proposed",
            resource_ref=f"delegations:{delegation_id}",
            now=now,
        )

        row = _get_delegation(session, auth.workspace_id, delegation_id)
        assert row is not None
        response = _to_response(session, row)
        _store_idempotency(
            session, auth, idempotency_key, request_hash, response.model_dump(mode="json"), now, 201
        )
        return response


@router.get("", response_model=DelegationListResponse)
def list_delegations_endpoint(auth: AuthDep, session: SessionDep) -> DelegationListResponse:
    role = authz.require_active_role(session, auth)
    now = datetime.now(UTC)
    with session.begin():
        account_id = _account_id_for(session, workspace_id=auth.workspace_id, users_id=auth.user_id)
        if role in {"owner", "admin"}:
            _expire_due(session, workspace_id=auth.workspace_id, account_id=None, now=now)
            rows = (
                session.execute(
                    text(
                        f"SELECT {_DELEGATION_FIELDS} FROM delegations "  # noqa: S608
                        "WHERE workspace_id = :workspace_id ORDER BY created_at DESC"
                    ),
                    {"workspace_id": auth.workspace_id},
                )
                .mappings()
                .all()
            )
        else:
            _expire_due(session, workspace_id=auth.workspace_id, account_id=account_id, now=now)
            rows = (
                session.execute(
                    text(
                        f"SELECT {_DELEGATION_FIELDS} FROM delegations "  # noqa: S608
                        "WHERE workspace_id = :workspace_id AND "
                        "(delegator_account_id = :account_id "
                        "OR recipient_account_id = :account_id) "
                        "ORDER BY created_at DESC"
                    ),
                    {"workspace_id": auth.workspace_id, "account_id": account_id},
                )
                .mappings()
                .all()
            )
        delegations = [_to_response(session, dict(r)) for r in rows]
    return DelegationListResponse(delegations=delegations)


@router.get("/{delegation_id}", response_model=DelegationResponse)
def get_delegation_endpoint(
    delegation_id: UUID, auth: AuthDep, session: SessionDep
) -> DelegationResponse:
    role = authz.require_active_role(session, auth)
    now = datetime.now(UTC)
    with session.begin():
        account_id = _account_id_for(session, workspace_id=auth.workspace_id, users_id=auth.user_id)
        row = _get_delegation(session, auth.workspace_id, delegation_id)
        if row is None:
            raise HTTPException(status_code=404, detail="DELEGATION_NOT_FOUND")
        is_party = account_id in (row["delegator_account_id"], row["recipient_account_id"])
        if role not in {"owner", "admin"} and not is_party:
            raise HTTPException(status_code=404, detail="DELEGATION_NOT_FOUND")
        row = _maybe_expire_single(session, workspace_id=auth.workspace_id, delegation=row, now=now)
        return _to_response(session, row)


@router.post("/{delegation_id}/accept", response_model=DelegationResponse)
def accept_delegation_endpoint(
    delegation_id: UUID,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> DelegationResponse:
    request_hash = _request_hash(f"accept:{delegation_id}")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return DelegationResponse.model_validate(cached)

        account_id = _account_id_for(session, workspace_id=auth.workspace_id, users_id=auth.user_id)
        row = _get_delegation(session, auth.workspace_id, delegation_id)
        if row is None or account_id not in (
            row["delegator_account_id"],
            row["recipient_account_id"],
        ):
            raise HTTPException(status_code=404, detail="DELEGATION_NOT_FOUND")
        row = _maybe_expire_single(session, workspace_id=auth.workspace_id, delegation=row, now=now)
        if row["recipient_account_id"] != account_id:
            raise HTTPException(status_code=403, detail="INSUFFICIENT_ROLE")
        if row["status"] != "proposed":
            raise HTTPException(status_code=409, detail="DELEGATION_NOT_PROPOSED")

        updated = (
            session.execute(
                text(
                    f"UPDATE delegations SET status = 'accepted', updated_at = :now "  # noqa: S608
                    f"WHERE id = :id AND status = 'proposed' RETURNING {_DELEGATION_FIELDS}"
                ),
                {"id": delegation_id, "now": now},
            )
            .mappings()
            .one_or_none()
        )
        if updated is None:
            raise HTTPException(status_code=409, detail="DELEGATION_NOT_PROPOSED")

        delegator_users_id = _users_id_for_account(
            session, workspace_id=auth.workspace_id, account_id=row["delegator_account_id"]
        )
        _grant_evidence(
            session,
            workspace_id=auth.workspace_id,
            delegation_id=delegation_id,
            recipient_account_id=account_id,
            granted_by=delegator_users_id,
            now=now,
        )
        _write_event(session, delegation_id, "accepted", account_id, now)
        _notify_member(
            session,
            workspace_id=auth.workspace_id,
            account_id=row["delegator_account_id"],
            notification_type="delegation.accepted",
            resource_ref=f"delegations:{delegation_id}",
            now=now,
        )

        response = _to_response(session, dict(updated))
        _store_idempotency(
            session, auth, idempotency_key, request_hash, response.model_dump(mode="json"), now
        )
        return response


@router.post("/{delegation_id}/reject", response_model=DelegationResponse)
def reject_delegation_endpoint(
    delegation_id: UUID,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> DelegationResponse:
    request_hash = _request_hash(f"reject:{delegation_id}")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return DelegationResponse.model_validate(cached)

        account_id = _account_id_for(session, workspace_id=auth.workspace_id, users_id=auth.user_id)
        row = _get_delegation(session, auth.workspace_id, delegation_id)
        if row is None or account_id not in (
            row["delegator_account_id"],
            row["recipient_account_id"],
        ):
            raise HTTPException(status_code=404, detail="DELEGATION_NOT_FOUND")
        row = _maybe_expire_single(session, workspace_id=auth.workspace_id, delegation=row, now=now)
        if row["recipient_account_id"] != account_id:
            raise HTTPException(status_code=403, detail="INSUFFICIENT_ROLE")
        if row["status"] != "proposed":
            raise HTTPException(status_code=409, detail="DELEGATION_NOT_PROPOSED")

        updated = (
            session.execute(
                text(
                    f"UPDATE delegations SET status = 'rejected', updated_at = :now "  # noqa: S608
                    f"WHERE id = :id AND status = 'proposed' RETURNING {_DELEGATION_FIELDS}"
                ),
                {"id": delegation_id, "now": now},
            )
            .mappings()
            .one_or_none()
        )
        if updated is None:
            raise HTTPException(status_code=409, detail="DELEGATION_NOT_PROPOSED")

        _write_event(session, delegation_id, "rejected", account_id, now)
        _notify_member(
            session,
            workspace_id=auth.workspace_id,
            account_id=row["delegator_account_id"],
            notification_type="delegation.rejected",
            resource_ref=f"delegations:{delegation_id}",
            now=now,
        )

        response = _to_response(session, dict(updated))
        _store_idempotency(
            session, auth, idempotency_key, request_hash, response.model_dump(mode="json"), now
        )
        return response


@router.post("/{delegation_id}/revoke", response_model=DelegationResponse)
def revoke_delegation_endpoint(
    delegation_id: UUID,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> DelegationResponse:
    request_hash = _request_hash(f"revoke:{delegation_id}")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return DelegationResponse.model_validate(cached)

        account_id = _account_id_for(session, workspace_id=auth.workspace_id, users_id=auth.user_id)
        row = _get_delegation(session, auth.workspace_id, delegation_id)
        if row is None or account_id not in (
            row["delegator_account_id"],
            row["recipient_account_id"],
        ):
            raise HTTPException(status_code=404, detail="DELEGATION_NOT_FOUND")
        if row["delegator_account_id"] != account_id:
            raise HTTPException(status_code=403, detail="INSUFFICIENT_ROLE")
        # No lazy-expiry check here -- revoke is only ever valid from
        # `accepted`, a state `_maybe_expire_single` never transitions out
        # of (it only ever touches `proposed`).
        if row["status"] != "accepted":
            raise HTTPException(status_code=409, detail="DELEGATION_NOT_ACCEPTED")

        updated = (
            session.execute(
                text(
                    f"UPDATE delegations SET status = 'revoked', updated_at = :now "  # noqa: S608
                    f"WHERE id = :id AND status = 'accepted' RETURNING {_DELEGATION_FIELDS}"
                ),
                {"id": delegation_id, "now": now},
            )
            .mappings()
            .one_or_none()
        )
        if updated is None:
            raise HTTPException(status_code=409, detail="DELEGATION_NOT_ACCEPTED")

        _revoke_evidence_grants(
            session,
            workspace_id=auth.workspace_id,
            delegation_id=delegation_id,
            recipient_account_id=row["recipient_account_id"],
            now=now,
        )
        _write_event(session, delegation_id, "revoked", account_id, now)
        _notify_member(
            session,
            workspace_id=auth.workspace_id,
            account_id=row["recipient_account_id"],
            notification_type="delegation.revoked",
            resource_ref=f"delegations:{delegation_id}",
            now=now,
        )

        response = _to_response(session, dict(updated))
        _store_idempotency(
            session, auth, idempotency_key, request_hash, response.model_dump(mode="json"), now
        )
        return response


@router.post("/{delegation_id}/complete", response_model=DelegationResponse)
def complete_delegation_endpoint(
    delegation_id: UUID,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
    idempotency_key: IdempotencyHeader,
) -> DelegationResponse:
    request_hash = _request_hash(f"complete:{delegation_id}")
    now = datetime.now(UTC)
    with session.begin():
        _lock_idempotency(session, auth, idempotency_key)
        cached = _load_cached(session, auth, idempotency_key, request_hash)
        if cached is not None:
            return DelegationResponse.model_validate(cached)

        account_id = _account_id_for(session, workspace_id=auth.workspace_id, users_id=auth.user_id)
        row = _get_delegation(session, auth.workspace_id, delegation_id)
        if row is None or account_id not in (
            row["delegator_account_id"],
            row["recipient_account_id"],
        ):
            raise HTTPException(status_code=404, detail="DELEGATION_NOT_FOUND")
        if row["recipient_account_id"] != account_id:
            raise HTTPException(status_code=403, detail="INSUFFICIENT_ROLE")
        if row["status"] != "accepted":
            raise HTTPException(status_code=409, detail="DELEGATION_NOT_ACCEPTED")

        updated = (
            session.execute(
                text(
                    f"UPDATE delegations SET status = 'completed', updated_at = :now "  # noqa: S608
                    f"WHERE id = :id AND status = 'accepted' RETURNING {_DELEGATION_FIELDS}"
                ),
                {"id": delegation_id, "now": now},
            )
            .mappings()
            .one_or_none()
        )
        if updated is None:
            raise HTTPException(status_code=409, detail="DELEGATION_NOT_ACCEPTED")

        _revoke_evidence_grants(
            session,
            workspace_id=auth.workspace_id,
            delegation_id=delegation_id,
            recipient_account_id=row["recipient_account_id"],
            now=now,
        )
        _write_event(session, delegation_id, "completed", account_id, now)
        _notify_member(
            session,
            workspace_id=auth.workspace_id,
            account_id=row["delegator_account_id"],
            notification_type="delegation.completed",
            resource_ref=f"delegations:{delegation_id}",
            now=now,
        )

        response = _to_response(session, dict(updated))
        _store_idempotency(
            session, auth, idempotency_key, request_hash, response.model_dump(mode="json"), now
        )
        return response
