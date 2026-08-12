"""`ecc.platform.authz_grants` -- the grants and ownership-transfer HTTP
feature: `GET|POST /sharing/grants`, `DELETE /sharing/grants/{id}`,
`POST /sharing/grants/preview`, `GET /sharing/resources/{type}/{id}`, and
`POST|GET /ownership/transfers`. Split out of `authz.py` (an architecture-
review deepening) -- `authz.py` is the decision engine 40+ callers across
every domain import and never touched by this split; this module is a
*consumer* of it, the same as any other domain, not part of the engine
itself. See `authz.py`'s own module docstring for why `require_grantable`
and `_active_grant_exists` stayed there despite one reading like an engine
name and the other like a grants one -- the actual call graph, not the
name, decided which module each function belongs to.

**Who may create a grant is a disclosed judgment call**: neither the
design doc nor `PERMISSION-CONTRACT.md` states it explicitly, so this
module requires the caller to be either the resource's own `owner_id` or
hold `owner`/`admin` role in the workspace (the same bar `create_
invitation_endpoint` already holds invitation creation to) -- a grant
widens who can see a specific resource, which is exactly the kind of
decision this codebase already reserves to a resource's own owner or the
workspace's own administrators elsewhere, not opened to every member. The
grantee must already hold an `active` membership in this workspace (a
grant to a non-member has no requester context `authorize()` could ever
evaluate it against). Revocation (`DELETE`) is available to the same set,
plus the grant's own original `granted_by` actor -- whoever created a
grant can always undo it, even if their role changes afterward.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session

from .authz import (
    ROLE_PERMISSIONS,
    Action,
    EffectivePermissions,
    ResourceRef,
    UnknownResourceTypeError,
    account_id_for,
    active_grant_actions_for,
    current_role,
    effective_permissions,
    load_resource,
    require_grantable,
    require_known_resource_type,
    users_id_for_account,
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
    `ecc.platform.notifications` -- see that module's own docstring for why
    (importing it here would create a circular import, since it needs
    `authz` right back for `/shared/activity`). `ON CONFLICT DO NOTHING`
    against `uq_member_notifications_dedup` is this helper's own answer to
    "a duplicate underlying event does not double-notify" -- Task 7's own
    migration docstring has the full rationale.
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


def _load_resource_for_update(
    session: Session, *, resource_type: str, resource_id: UUID
) -> ResourceRef | None:
    """Locked variant of `load_resource`, for any endpoint that both
    authorizes against AND then mutates the same resource row in one
    transaction. Never call this from `authorize()`'s own read-only path
    (`load_resource`'s own docstring explains why that path must stay
    unlocked).

    **Found in Phase 8's second whole-phase review, not the first.** The
    first review's fix to `create_ownership_transfer_endpoint` added a
    `SELECT ... FOR UPDATE` *after* `_require_owner_admin_or_resource_owner`
    had already run against an earlier, unlocked `load_resource` read --
    it made `from_account_id` accurate against the locked value but never
    re-ran the authorization decision itself against that value. A
    non-owner/admin caller who owns a resource at the moment of their own
    unlocked read, but no longer owns it by the time their transaction's
    mutation actually commits (because a concurrent transfer or grant beat
    them to it), still passed the check. Locking first and authorizing
    against the *locked* row -- what every call site below now does --
    closes that: the authorization decision and the mutation always see
    the same, serialized value.
    """
    require_known_resource_type(resource_type)
    row = (
        session.execute(
            text(
                f"SELECT workspace_id, owner_id, visibility FROM {resource_type} "  # noqa: S608
                "WHERE id = :id FOR UPDATE"
            ),
            {"id": resource_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    return ResourceRef(
        workspace_id=row["workspace_id"], owner_id=row["owner_id"], visibility=row["visibility"]
    )


def _members_losing_default_access(
    session: Session,
    *,
    workspace_id: UUID,
    resource_owner_id: UUID,
    grantee_account_id: UUID,
    resource_type: str,
    resource_id: UUID,
    now: datetime,
) -> list[UUID]:
    """Every active member's `account_id` in this workspace -- other than
    the resource's own owner (always allowed, `authorize()` step 2) and the
    prospective grantee (covered by the new grant instead) -- who does not
    already hold their own active explicit grant on this exact resource:
    the set that would lose today's default `workspace`-visibility access
    the instant this resource narrows to `shared_explicitly`. The concrete
    answer to the *losing* half of `UX-STATES.md`'s "sharing previews
    exactly what becomes visible" -- the gaining half (the grantee) is
    already the request's own subject.
    """
    rows = (
        session.execute(
            text(
                """
                SELECT wm.account_id
                FROM workspace_memberships wm
                JOIN users u ON u.workspace_id = wm.workspace_id AND u.id = wm.users_id
                WHERE wm.workspace_id = :workspace_id AND wm.status = 'active'
                  AND u.id != :resource_owner_id
                  AND wm.account_id != :grantee_account_id
                  AND NOT EXISTS (
                      SELECT 1 FROM resource_grants rg
                      WHERE rg.workspace_id = :workspace_id
                        AND rg.grantee_account_id = wm.account_id
                        AND rg.resource_type = :resource_type
                        AND rg.resource_id = :resource_id
                        AND rg.revoked_at IS NULL
                        AND (rg.expires_at IS NULL OR rg.expires_at > :now)
                  )
                """
            ),
            {
                "workspace_id": workspace_id,
                "resource_owner_id": resource_owner_id,
                "grantee_account_id": grantee_account_id,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "now": now,
            },
        )
        .mappings()
        .all()
    )
    return [r["account_id"] for r in rows]


# ---------------------------------------------------------------------------
# GET|POST /sharing/grants, DELETE /sharing/grants/{id}
# ---------------------------------------------------------------------------

router = APIRouter(prefix="/api/v1/sharing", tags=["sharing"])
SessionDep = Annotated[Session, Depends(get_session)]


class GrantCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resource_type: str
    resource_id: UUID
    grantee_account_id: UUID
    actions: list[Action] = Field(min_length=1)
    expires_at: datetime | None = None
    narrow_visibility: bool = False

    @field_validator("actions")
    @classmethod
    def _dedupe(cls, value: list[Action]) -> list[Action]:
        return sorted(set(value))


class GrantResponse(BaseModel):
    id: UUID
    resource_type: str
    resource_id: UUID
    grantee_account_id: UUID
    actions: list[str]
    granted_by: UUID
    expires_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class GrantListResponse(BaseModel):
    grants: list[GrantResponse]


def _require_owner_admin_or_resource_owner(
    session: Session, auth: AuthContext, resource: ResourceRef
) -> None:
    if resource.owner_id == auth.user_id:
        return
    role = current_role(session, workspace_id=auth.workspace_id, users_id=auth.user_id)
    if role in {"owner", "admin"}:
        return
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="INSUFFICIENT_ROLE")


@router.post("/grants", response_model=GrantResponse, status_code=status.HTTP_201_CREATED)
def create_grant_endpoint(
    payload: GrantCreateRequest,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
) -> GrantResponse:
    try:
        require_grantable(payload.resource_type)
    except UnknownResourceTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="RESOURCE_TYPE_NOT_GRANTABLE"
        ) from exc

    with session.begin():
        # Locked first, authorized against the locked value second -- see
        # `_load_resource_for_update`'s own docstring. Without this, a
        # concurrent ownership transfer away from this caller (a non-
        # owner/admin member) between their own unlocked read and this
        # transaction's `UPDATE ... SET visibility` could let them force a
        # visibility change and grant on a resource they no longer own by
        # the time this commits.
        resource = _load_resource_for_update(
            session, resource_type=payload.resource_type, resource_id=payload.resource_id
        )
        if resource is None or resource.workspace_id != auth.workspace_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="RESOURCE_NOT_FOUND")
        _require_owner_admin_or_resource_owner(session, auth, resource)

        grantee_membership = (
            session.execute(
                text(
                    "SELECT 1 FROM workspace_memberships "
                    "WHERE workspace_id = :workspace_id AND account_id = :account_id "
                    "AND status = 'active'"
                ),
                {"workspace_id": auth.workspace_id, "account_id": payload.grantee_account_id},
            )
            .mappings()
            .one_or_none()
        )
        if grantee_membership is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="GRANTEE_NOT_FOUND")

        # A grant is only ever load-bearing when the resource's own
        # visibility is `shared_explicitly` -- `authorize()`'s step 5 never
        # consults `resource_grants` for a `workspace`-visibility resource
        # (every active member already reads it via their role), and step 3
        # denies a `private` resource unconditionally regardless of any
        # grant. Without this block, `POST /grants` would happily insert a
        # row that changes nothing -- confirmed by grep: before this task,
        # no endpoint anywhere ever transitioned a resource's `visibility`,
        # so every grant ever created was inert. Two cases:
        #  - `private` -> `shared_explicitly` is a strict widening (only the
        #    owner could see it before; now the owner plus named grantees
        #    can) and is applied automatically -- the owner's act of
        #    granting IS the deliberate confirmation `PERMISSION-CONTRACT.md`
        #    asks for.
        #  - `workspace` -> `shared_explicitly` is a *narrowing* for every
        #    OTHER active member (they lose their current default,
        #    role-based access the instant this flips, unless they also
        #    hold their own explicit grant) -- too consequential to happen
        #    as a side effect of an ordinary grant, so it requires the
        #    caller to have already reviewed `POST /grants/preview`'s
        #    `members_losing_default_access` and opted in via
        #    `narrow_visibility=True`; otherwise this 409s rather than
        #    silently locking other members out.
        if resource.visibility == "workspace" and not payload.narrow_visibility:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="GRANT_REQUIRES_NARROW_VISIBILITY"
            )
        if resource.visibility != "shared_explicitly":
            session.execute(
                text(
                    f"UPDATE {payload.resource_type} SET visibility = 'shared_explicitly' "  # noqa: S608
                    "WHERE id = :id"
                ),
                {"id": payload.resource_id},
            )

        grant_id = uuid4()
        now = datetime.now(UTC)
        session.execute(
            text(
                """
                INSERT INTO resource_grants (
                    id, workspace_id, grantee_account_id, resource_type, resource_id,
                    actions, granted_by, expires_at, created_at
                ) VALUES (
                    :id, :workspace_id, :grantee_account_id, :resource_type, :resource_id,
                    :actions, :granted_by, :expires_at, :now
                )
                """
            ),
            {
                "id": grant_id,
                "workspace_id": auth.workspace_id,
                "grantee_account_id": payload.grantee_account_id,
                "resource_type": payload.resource_type,
                "resource_id": payload.resource_id,
                "actions": payload.actions,
                "granted_by": auth.user_id,
                "expires_at": payload.expires_at,
                "now": now,
            },
        )
        _notify_member(
            session,
            workspace_id=auth.workspace_id,
            account_id=payload.grantee_account_id,
            notification_type="grant.created",
            resource_ref=f"resource_grants:{grant_id}",
            now=now,
        )
    return GrantResponse(
        id=grant_id,
        resource_type=payload.resource_type,
        resource_id=payload.resource_id,
        grantee_account_id=payload.grantee_account_id,
        actions=list(payload.actions),
        granted_by=auth.user_id,
        expires_at=payload.expires_at,
        revoked_at=None,
        created_at=now,
    )


@router.get("/grants", response_model=GrantListResponse)
def list_grants_endpoint(auth: AuthDep, session: SessionDep) -> GrantListResponse:
    """`owner`/`admin` see every grant in the workspace (the sharing-review
    surface Task 5 builds a UI for); every other role sees only grants
    naming their own account as grantee -- "what has been shared with
    me," not "what has anyone shared with anyone."
    """
    role = current_role(session, workspace_id=auth.workspace_id, users_id=auth.user_id)
    if role in {"owner", "admin"}:
        rows = (
            session.execute(
                text(
                    "SELECT id, resource_type, resource_id, grantee_account_id, actions, "
                    "granted_by, expires_at, revoked_at, created_at "
                    "FROM resource_grants WHERE workspace_id = :workspace_id "
                    "ORDER BY created_at DESC"
                ),
                {"workspace_id": auth.workspace_id},
            )
            .mappings()
            .all()
        )
    else:
        account_id = account_id_for(session, workspace_id=auth.workspace_id, users_id=auth.user_id)
        rows = (
            session.execute(
                text(
                    "SELECT id, resource_type, resource_id, grantee_account_id, actions, "
                    "granted_by, expires_at, revoked_at, created_at "
                    "FROM resource_grants "
                    "WHERE workspace_id = :workspace_id AND grantee_account_id = :account_id "
                    "ORDER BY created_at DESC"
                ),
                {"workspace_id": auth.workspace_id, "account_id": account_id},
            )
            .mappings()
            .all()
        )
    session.rollback()
    return GrantListResponse(
        grants=[GrantResponse(**dict(r), actions=list(r["actions"])) for r in rows]
    )


@router.delete("/grants/{grant_id}", response_model=GrantResponse)
def revoke_grant_endpoint(
    grant_id: UUID,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
) -> GrantResponse:
    with session.begin():
        grant = (
            session.execute(
                text(
                    "SELECT id, workspace_id, grantee_account_id, resource_type, resource_id, "
                    "actions, granted_by, expires_at, revoked_at, created_at "
                    "FROM resource_grants WHERE id = :id FOR UPDATE"
                ),
                {"id": grant_id},
            )
            .mappings()
            .one_or_none()
        )
        if grant is None or grant["workspace_id"] != auth.workspace_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="GRANT_NOT_FOUND")
        if grant["revoked_at"] is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="GRANT_ALREADY_REVOKED"
            )

        if grant["granted_by"] != auth.user_id:
            # Locked, consistent with `create_grant_endpoint`'s own fix --
            # the grant row itself is already locked above, so this
            # additionally serializes against a concurrent ownership
            # transfer of the underlying resource while this revoke is in
            # flight.
            resource = _load_resource_for_update(
                session, resource_type=grant["resource_type"], resource_id=grant["resource_id"]
            )
            role = current_role(session, workspace_id=auth.workspace_id, users_id=auth.user_id)
            is_resource_owner = resource is not None and resource.owner_id == auth.user_id
            if role not in {"owner", "admin"} and not is_resource_owner:
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN, detail="INSUFFICIENT_ROLE"
                )

        now = datetime.now(UTC)
        session.execute(
            text("UPDATE resource_grants SET revoked_at = :now WHERE id = :id"),
            {"now": now, "id": grant_id},
        )
    return GrantResponse(
        id=grant["id"],
        resource_type=grant["resource_type"],
        resource_id=grant["resource_id"],
        grantee_account_id=grant["grantee_account_id"],
        actions=list(grant["actions"]),
        granted_by=grant["granted_by"],
        expires_at=grant["expires_at"],
        revoked_at=now,
        created_at=grant["created_at"],
    )


# ---------------------------------------------------------------------------
# POST /sharing/grants/preview, GET /sharing/resources/{type}/{id}
# ---------------------------------------------------------------------------


class GrantPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resource_type: str
    resource_id: UUID
    grantee_account_id: UUID
    actions: list[Action] = Field(min_length=1)

    @field_validator("actions")
    @classmethod
    def _dedupe(cls, value: list[Action]) -> list[Action]:
        return sorted(set(value))


class GrantPreviewResponse(BaseModel):
    resource_type: str
    resource_id: UUID
    grantee_account_id: UUID
    current_visibility: str
    proposed_visibility: str
    requires_narrow_visibility_confirmation: bool
    members_losing_default_access: list[UUID]
    grantee_already_has_access: bool
    grantee_gains_actions: list[Action]


@router.post("/grants/preview", response_model=GrantPreviewResponse)
def preview_grant_endpoint(
    payload: GrantPreviewRequest, auth: AuthDep, session: SessionDep, _csrf: CsrfDep
) -> GrantPreviewResponse:
    """Read-only dry run of `POST /grants` -- `UX-STATES.md`'s "Sharing
    previews exactly what becomes visible" requirement, computed from the
    exact same tables `authorize()`/`create_grant_endpoint` read, never a
    client-side approximation. `PERMISSION-CONTRACT.md`'s "checks occur in
    service and query boundaries... UI hiding is not security" applies
    equally to a preview: an inaccurate one is worse than none, since the
    sharing-review screen exists specifically so it can be trusted.

    Same authorization gate as `POST /grants` itself (only the resource's
    owner or workspace `owner`/`admin` may preview a grant on it) --
    otherwise this would let any member probe another member's workspace
    membership existence via `GRANTEE_NOT_FOUND`, or discover a resource's
    current visibility tier, neither of which they're entitled to learn
    about a resource they don't control sharing for.

    Mutates nothing -- `CsrfDep` but no `Idempotency-Key`, the identical
    reasoning `simulate_workflow_endpoint`'s own docstring gives for a
    mutating-method route with no state change to replay-protect.
    """
    try:
        require_grantable(payload.resource_type)
    except UnknownResourceTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="RESOURCE_TYPE_NOT_GRANTABLE"
        ) from exc

    resource = load_resource(
        session, resource_type=payload.resource_type, resource_id=payload.resource_id
    )
    if resource is None or resource.workspace_id != auth.workspace_id:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="RESOURCE_NOT_FOUND")
    _require_owner_admin_or_resource_owner(session, auth, resource)

    grantee_membership = (
        session.execute(
            text(
                "SELECT 1 FROM workspace_memberships "
                "WHERE workspace_id = :workspace_id AND account_id = :account_id "
                "AND status = 'active'"
            ),
            {"workspace_id": auth.workspace_id, "account_id": payload.grantee_account_id},
        )
        .mappings()
        .one_or_none()
    )
    if grantee_membership is None:
        session.rollback()
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="GRANTEE_NOT_FOUND")

    now = datetime.now(UTC)
    existing_grant_actions = active_grant_actions_for(
        session,
        workspace_id=auth.workspace_id,
        account_id=payload.grantee_account_id,
        resource_type=payload.resource_type,
        resource_id=payload.resource_id,
        now=now,
    )

    if resource.visibility == "workspace":
        # Resolve the grantee's *actual* role for the precise current-access
        # answer, rather than assuming every role's shared baseline.
        grantee_role = (
            session.execute(
                text(
                    "SELECT role FROM workspace_memberships "
                    "WHERE workspace_id = :workspace_id AND account_id = :account_id "
                    "AND status = 'active'"
                ),
                {"workspace_id": auth.workspace_id, "account_id": payload.grantee_account_id},
            )
            .mappings()
            .one()
        )["role"]
        current_access = ROLE_PERMISSIONS[grantee_role]
        requires_narrowing = True
        members_losing_default_access = _members_losing_default_access(
            session,
            workspace_id=auth.workspace_id,
            resource_owner_id=resource.owner_id,
            grantee_account_id=payload.grantee_account_id,
            resource_type=payload.resource_type,
            resource_id=payload.resource_id,
            now=now,
        )
    elif resource.visibility == "shared_explicitly":
        current_access = existing_grant_actions
        requires_narrowing = False
        members_losing_default_access = []
    else:  # private
        current_access = frozenset()
        requires_narrowing = False
        members_losing_default_access = []

    session.rollback()
    grantee_gains = sorted(set(payload.actions) - current_access)
    return GrantPreviewResponse(
        resource_type=payload.resource_type,
        resource_id=payload.resource_id,
        grantee_account_id=payload.grantee_account_id,
        current_visibility=resource.visibility,
        proposed_visibility="shared_explicitly",
        requires_narrow_visibility_confirmation=requires_narrowing,
        members_losing_default_access=members_losing_default_access,
        grantee_already_has_access=set(payload.actions) <= current_access,
        grantee_gains_actions=grantee_gains,
    )


@router.get(
    "/resources/{resource_type}/{resource_id}",
    response_model=EffectivePermissions,
)
def get_effective_permissions_endpoint(
    resource_type: str, resource_id: UUID, auth: AuthDep, session: SessionDep
) -> EffectivePermissions:
    """The standalone, resource-type-generic counterpart to embedding
    `EffectivePermissions` directly into a domain's own response model --
    always available for any of the 59 grantable `resource_type`s even
    before a given domain's own endpoints are updated to embed it inline,
    so "a viewer always knows why they can see it" (`API-SCHEMAS.md`) holds
    universally from the moment this ships, not only for the domains
    mechanically updated first.
    """
    try:
        require_known_resource_type(resource_type)
    except UnknownResourceTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="UNKNOWN_RESOURCE_TYPE"
        ) from exc
    result = effective_permissions(
        session, auth, resource_type=resource_type, resource_id=resource_id
    )
    session.rollback()
    if result is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="RESOURCE_NOT_FOUND")
    return result


# ---------------------------------------------------------------------------
# Phase 8 Task 8 -- POST|GET /api/v1/ownership/transfers
# ---------------------------------------------------------------------------
#
# Immediate, single-step reassignment of one resource's `owner_id` from one
# account to another -- the counterpart `ecc.domains.identity.membership_
# removal.remove_member_endpoint` drives before it will allow removing a
# member who still owns workspace resources (`owned_resource_summary`
# above is exactly what that endpoint checks). No bilateral confirmation
# step exists (unlike `delegations`' own contract-defined state machine) --
# see migration `0066_phase8_ownership_transfers.py`'s own docstring for
# why `status` is left open rather than hard-coded to a single value.

ownership_router = APIRouter(prefix="/api/v1/ownership", tags=["ownership"])


class OwnershipTransferCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resource_type: str
    resource_id: UUID
    to_account_id: UUID


class OwnershipTransferResponse(BaseModel):
    id: UUID
    resource_type: str
    resource_id: UUID
    from_account_id: UUID
    to_account_id: UUID
    status: str
    initiated_by: UUID
    created_at: datetime
    completed_at: datetime | None


class OwnershipTransferListResponse(BaseModel):
    transfers: list[OwnershipTransferResponse]


@ownership_router.post(
    "/transfers", response_model=OwnershipTransferResponse, status_code=status.HTTP_201_CREATED
)
def create_ownership_transfer_endpoint(
    payload: OwnershipTransferCreateRequest,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
) -> OwnershipTransferResponse:
    """Same authorization gate `POST /sharing/grants` already uses (the
    resource's own current owner, or workspace `owner`/`admin`) -- widening
    who may reassign a resource's ownership is exactly the kind of
    consequential decision this codebase already reserves to that same
    bar, not opened to every member. `require_grantable` (not merely
    `require_known_resource_type`) rejects `UNGRANTABLE_RESOURCE_TYPES`
    here too -- a Phase 7 personal-domain resource's ownership can never be
    reassigned, the identical structural guarantee that keeps it
    unshareable.
    """
    try:
        require_grantable(payload.resource_type)
    except UnknownResourceTypeError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="RESOURCE_TYPE_NOT_GRANTABLE"
        ) from exc

    with session.begin():
        # Locked FIRST, authorized against the locked value second -- see
        # `_load_resource_for_update`'s own docstring. Two concurrent
        # transfers of the same resource are now fully serialized: the
        # second transaction to reach this lock sees the first transaction's
        # already-committed `owner_id`, so its own authorization decision
        # (not merely its `from_account_id` bookkeeping) reflects who
        # actually owns the resource at that instant.
        resource = _load_resource_for_update(
            session, resource_type=payload.resource_type, resource_id=payload.resource_id
        )
        if resource is None or resource.workspace_id != auth.workspace_id:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="RESOURCE_NOT_FOUND")
        _require_owner_admin_or_resource_owner(session, auth, resource)
        locked_owner_id = resource.owner_id

        to_membership = (
            session.execute(
                text(
                    "SELECT 1 FROM workspace_memberships "
                    "WHERE workspace_id = :workspace_id AND account_id = :account_id "
                    "AND status = 'active'"
                ),
                {"workspace_id": auth.workspace_id, "account_id": payload.to_account_id},
            )
            .mappings()
            .one_or_none()
        )
        if to_membership is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="RECIPIENT_NOT_FOUND")

        to_users_id = users_id_for_account(
            session, workspace_id=auth.workspace_id, account_id=payload.to_account_id
        )
        assert to_users_id is not None  # active membership implies a users row exists

        from_account_id = account_id_for(
            session, workspace_id=auth.workspace_id, users_id=locked_owner_id
        )
        assert from_account_id is not None  # every owner_id names a real users row

        session.execute(
            text(
                f"UPDATE {payload.resource_type} SET owner_id = :owner_id "  # noqa: S608
                "WHERE id = :id"
            ),
            {"owner_id": to_users_id, "id": payload.resource_id},
        )

        transfer_id = uuid4()
        now = datetime.now(UTC)
        session.execute(
            text(
                """
                INSERT INTO ownership_transfers (
                    id, workspace_id, resource_type, resource_id, from_account_id,
                    to_account_id, status, initiated_by, created_at, completed_at
                ) VALUES (
                    :id, :workspace_id, :resource_type, :resource_id, :from_account_id,
                    :to_account_id, 'completed', :initiated_by, :now, :now
                )
                """
            ),
            {
                "id": transfer_id,
                "workspace_id": auth.workspace_id,
                "resource_type": payload.resource_type,
                "resource_id": payload.resource_id,
                "from_account_id": from_account_id,
                "to_account_id": payload.to_account_id,
                "initiated_by": auth.user_id,
                "now": now,
            },
        )
    return OwnershipTransferResponse(
        id=transfer_id,
        resource_type=payload.resource_type,
        resource_id=payload.resource_id,
        from_account_id=from_account_id,
        to_account_id=payload.to_account_id,
        status="completed",
        initiated_by=auth.user_id,
        created_at=now,
        completed_at=now,
    )


@ownership_router.get("/transfers", response_model=OwnershipTransferListResponse)
def list_ownership_transfers_endpoint(
    auth: AuthDep, session: SessionDep
) -> OwnershipTransferListResponse:
    """`owner`/`admin` see every transfer in the workspace (an audit trail
    of who reassigned what); every other role sees only transfers naming
    their own account as either party -- the identical
    `list_grants_endpoint` split.
    """
    role = current_role(session, workspace_id=auth.workspace_id, users_id=auth.user_id)
    if role in {"owner", "admin"}:
        rows = (
            session.execute(
                text(
                    "SELECT id, resource_type, resource_id, from_account_id, to_account_id, "
                    "status, initiated_by, created_at, completed_at "
                    "FROM ownership_transfers WHERE workspace_id = :workspace_id "
                    "ORDER BY created_at DESC"
                ),
                {"workspace_id": auth.workspace_id},
            )
            .mappings()
            .all()
        )
    else:
        account_id = account_id_for(session, workspace_id=auth.workspace_id, users_id=auth.user_id)
        rows = (
            session.execute(
                text(
                    "SELECT id, resource_type, resource_id, from_account_id, to_account_id, "
                    "status, initiated_by, created_at, completed_at "
                    "FROM ownership_transfers WHERE workspace_id = :workspace_id "
                    "AND (from_account_id = :account_id OR to_account_id = :account_id) "
                    "ORDER BY created_at DESC"
                ),
                {"workspace_id": auth.workspace_id, "account_id": account_id},
            )
            .mappings()
            .all()
        )
    session.rollback()
    return OwnershipTransferListResponse(
        transfers=[OwnershipTransferResponse(**dict(r)) for r in rows]
    )
