"""`ecc.domains.identity.membership_removal` -- Phase 8 Task 8
(`docs/superpowers/plans/2026-08-01-phase-8-multi-user.md` Task 8,
`docs/superpowers/specs/2026-08-01-phase-8-multi-user-design.md` Decision
1). `GET /identity/workspaces/{id}/members`, `PATCH /identity/workspaces/
{id}/members/{user_id}`, `DELETE /identity/workspaces/{id}/members/
{user_id}`.

A separate `APIRouter` sharing `ecc.domains.identity.accounts`' own
`/api/v1/identity` prefix -- the identical pattern `ecc.domains.identity.
invitations` already established (its own module docstring), not a new
convention.

**`GET .../members` is readable by any active member, not owner/admin
only** -- a disclosed judgment call, since neither the plan nor
`PERMISSION-CONTRACT.md` states who may see the team roster. "Who's on
this workspace" is low-sensitivity, ordinary team-directory information
(a name/role list, not a resource), unlike creating an invitation or
managing a grant, which this codebase already reserves to `owner`/
`admin` -- gating a read-only roster the same way would make Task 9's own
"members/invitations panel" unusable for a `member`/`viewer` role. `PATCH`/
`DELETE` (the actual mutations) stay `owner`/`admin`-gated, plus `DELETE`
additionally permits self-removal (see that endpoint's own docstring).

**Two structural invariants enforced identically by `PATCH` and
`DELETE`, since neither the plan nor any contract names them explicitly
but both are necessary to avoid an unrecoverable workspace:** (1) the
workspace's last remaining `active`, `owner`-role membership can never be
demoted or removed -- there would be no one left who could ever promote a
new owner or reverse the action; (2) a member who still owns at least one
workspace resource (`ecc.platform.authz.owned_resource_summary`, scanning
every grantable, non-`UNGRANTABLE_RESOURCE_TYPES` table) cannot be removed
until every such resource has been reassigned via `POST /ownership/
transfers` first -- the plan's own "reassigns or blocks removal pending
ownership resolution for records only they own" line, made concrete.
Phase 7 personal-domain data is deliberately excluded from this check --
it is never workspace-transferable by design (`UNGRANTABLE_RESOURCE_
TYPES`), so a removed member's own private vault content is governed
entirely by `ecc.domains.personal.export_deletion`'s own separate flow,
not by this one.

**"Offers export before finalizing" (the plan's own phrase) is the
removal response's own `export` field** -- a snapshot of the removed
member's identity/membership record (`account_id`, `email`,
`display_name`, `role`, `joined_at`, `removed_at`) returned in the same
response that finalizes removal, not a separate export subsystem. This is
a disclosed, deliberately modest interpretation: workspace resources the
member owned are already reassigned via `POST /ownership/transfers`
before removal is ever allowed to proceed, so there is no workspace data
left needing a bulk export at removal time -- what "offers export" most
plausibly protects here is the administrative record of *who this removed
member was*, not their resources (which never belonged to them personally
in the way Phase 7's own vault content does).

**Removal is a second, independent, explicit propagation path for
revoking that member's live sessions -- not merely relying on
`workspace_memberships.status` no longer being `'active'`.**
`PERMISSION-CONTRACT.md`'s own line ("membership revocation additionally
revokes every live session for that membership in the same transaction as
a second, independent propagation path") is implemented here directly:
`UPDATE sessions SET revoked_at = :now WHERE ... AND revoked_at IS NULL`,
in the same transaction as the `workspace_memberships` update. Every
subsequent request from that member's already-issued session would have
been denied by every domain's own `authz.require_active_role`/
`authz.authorize` regardless (both read `workspace_memberships.status`
fresh every time), but `ecc.auth.require_auth_context`'s own session
lookup does not join `workspace_memberships` at all (only `sessions.
revoked_at`/`expires_at` and `accounts.disabled_at`) -- so without this,
a removed member's session would still resolve to a valid `AuthContext`
at the auth layer, only to be denied later by role checks. Revoking the
session directly closes that gap at the earliest possible layer, matching
how `accounts.disabled_at` already does the same for account-level
revocation.

**Active delegations are force-cancelled, not silently orphaned or left
`proposed`/`accepted` forever** -- `ecc.domains.collaboration.delegations.
cancel_delegations_for_removed_member` (called here, not reimplemented)
transitions every delegation naming the removed member as either party to
`cancelled`, the exact system-initiated transition migration
`0064_phase8_delegations.py`'s own docstring reserved for this call site.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthDep, CsrfDep
from ecc.database import get_session
from ecc.domains.automation.worker import cancel_runs_for_removed_member
from ecc.domains.collaboration.delegations import cancel_delegations_for_removed_member
from ecc.observability import queue_lifecycle_event
from ecc.platform import audit_outbox, authz

router = APIRouter(prefix="/api/v1/identity", tags=["identity"])
SessionDep = Annotated[Session, Depends(get_session)]


class MemberResponse(BaseModel):
    user_id: UUID
    account_id: UUID
    email: str
    display_name: str
    role: str
    status: str
    created_at: datetime
    removed_at: datetime | None


class MemberListResponse(BaseModel):
    members: list[MemberResponse]


class MemberRoleUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: str = Field(pattern="^(owner|admin|member|viewer)$")


class MemberExportSnapshot(BaseModel):
    account_id: UUID
    email: str
    display_name: str
    role: str
    joined_at: datetime
    removed_at: datetime


class MemberRemovalResponse(BaseModel):
    user_id: UUID
    export: MemberExportSnapshot


def _member_row(session: Session, *, workspace_id: UUID, user_id: UUID) -> dict[str, Any] | None:
    row = (
        session.execute(
            text(
                """
                SELECT wm.id AS membership_id, wm.users_id, wm.account_id, wm.role,
                       wm.status, wm.created_at, wm.removed_at,
                       a.email, a.display_name
                FROM workspace_memberships wm
                JOIN accounts a ON a.id = wm.account_id
                WHERE wm.workspace_id = :workspace_id AND wm.users_id = :user_id
                """
            ),
            {"workspace_id": workspace_id, "user_id": user_id},
        )
        .mappings()
        .one_or_none()
    )
    return dict(row) if row is not None else None


def _is_sole_active_owner(session: Session, *, workspace_id: UUID, users_id: UUID) -> bool:
    other_owners = session.execute(
        text(
            "SELECT count(*) FROM workspace_memberships "
            "WHERE workspace_id = :workspace_id AND role = 'owner' AND status = 'active' "
            "AND users_id != :users_id"
        ),
        {"workspace_id": workspace_id, "users_id": users_id},
    ).scalar_one()
    return bool(other_owners == 0)


@router.get("/workspaces/{workspace_id}/members", response_model=MemberListResponse)
def list_members_endpoint(
    workspace_id: UUID, auth: AuthDep, session: SessionDep
) -> MemberListResponse:
    if workspace_id != auth.workspace_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="WORKSPACE_NOT_FOUND")
    authz.require_active_role(session, auth)
    rows = (
        session.execute(
            text(
                """
                SELECT wm.users_id, wm.account_id, wm.role, wm.status, wm.created_at,
                       wm.removed_at, a.email, a.display_name
                FROM workspace_memberships wm
                JOIN accounts a ON a.id = wm.account_id
                WHERE wm.workspace_id = :workspace_id AND wm.status = 'active'
                ORDER BY wm.created_at ASC
                """
            ),
            {"workspace_id": auth.workspace_id},
        )
        .mappings()
        .all()
    )
    session.rollback()
    return MemberListResponse(
        members=[
            MemberResponse(
                user_id=r["users_id"],
                account_id=r["account_id"],
                email=r["email"],
                display_name=r["display_name"],
                role=r["role"],
                status=r["status"],
                created_at=r["created_at"],
                removed_at=r["removed_at"],
            )
            for r in rows
        ]
    )


@router.patch("/workspaces/{workspace_id}/members/{user_id}", response_model=MemberResponse)
def update_member_role_endpoint(
    workspace_id: UUID,
    user_id: UUID,
    payload: MemberRoleUpdateRequest,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
) -> MemberResponse:
    if workspace_id != auth.workspace_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="WORKSPACE_NOT_FOUND")
    role = authz.require_active_role(session, auth)
    if role not in {"owner", "admin"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="INSUFFICIENT_ROLE")
    # Mirrors `invitations.py`'s own `create_invitation_endpoint` guard: an
    # `admin` may change a member's role to anything up to (not including)
    # `owner` -- unrestricted, an admin could unilaterally promote
    # themselves (or anyone) to co-owner with no existing owner ever having
    # approved it. Only an existing `owner` may set `role: "owner"`.
    if payload.role == "owner" and role != "owner":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="INSUFFICIENT_ROLE")

    with session.begin():
        # Found in the third whole-phase review: `_is_sole_active_owner`'s own
        # count and this endpoint's `UPDATE` are two separate statements with
        # no row lock spanning them, so two concurrent demotions of two
        # *different* owners (when exactly two are active) could each see
        # "an owner other than me still exists" and both proceed, leaving
        # zero. Serializing every membership mutation for one workspace
        # behind a single advisory lock -- the same `pg_advisory_xact_lock`
        # technique `attention.py`'s `regenerate_attention` already uses for
        # its own cross-statement race -- closes it without a `SELECT ...
        # FOR UPDATE` across an unbounded number of owner rows.
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"membership-mutation:{auth.workspace_id}"},
        )
        member = _member_row(session, workspace_id=auth.workspace_id, user_id=user_id)
        if member is None or member["status"] != "active":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MEMBER_NOT_FOUND")
        # Found in the second whole-phase review: the `payload.role ==
        # "owner"` guard above only stops an admin from *promoting* someone
        # to owner. Nothing stopped an admin from *demoting* an existing
        # owner instead -- the same trust boundary applied asymmetrically.
        # An owner may always change another owner's role (including their
        # own); an admin may never change an owner's role at all.
        if member["role"] == "owner" and role != "owner":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="INSUFFICIENT_ROLE")
        if (
            member["role"] == "owner"
            and payload.role != "owner"
            and _is_sole_active_owner(session, workspace_id=auth.workspace_id, users_id=user_id)
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="LAST_OWNER_CANNOT_BE_DEMOTED"
            )

        now = datetime.now(UTC)
        session.execute(
            text("UPDATE workspace_memberships SET role = :role, updated_at = :now WHERE id = :id"),
            {"role": payload.role, "now": now, "id": member["membership_id"]},
        )
        # Found in the third whole-phase review: this endpoint wrote zero
        # audit trail -- every other identity mutation in this package
        # (`invitations.py`/`accounts.py`) already calls this same shared
        # helper. `aggregate_type="workspace_membership"` is new here, not
        # yet wired into `notifications.py`'s own `_AGGREGATE_TYPE_TO_
        # RESOURCE_TYPE` map -- see that module's own docstring note on why
        # this one is disclosed rather than wired up in the same pass.
        audit_outbox.write_audit_and_outbox(
            session,
            auth,
            request,
            event_type="member.role_changed",
            aggregate_type="workspace_membership",
            aggregate_id=member["membership_id"],
            aggregate_version=1,
            changed_fields=["*"],
            payload={"aggregate_id": str(member["membership_id"]), "version": 1},
            now=now,
            domain="identity",
        )
        queue_lifecycle_event(session, "identity", "member.role_changed", "allowed")
    return MemberResponse(
        user_id=user_id,
        account_id=member["account_id"],
        email=member["email"],
        display_name=member["display_name"],
        role=payload.role,
        status="active",
        created_at=member["created_at"],
        removed_at=None,
    )


@router.delete("/workspaces/{workspace_id}/members/{user_id}", response_model=MemberRemovalResponse)
def remove_member_endpoint(
    workspace_id: UUID,
    user_id: UUID,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
) -> MemberRemovalResponse:
    """`owner`/`admin` may remove any member; a member may also remove
    themselves (self-service leave) -- a disclosed judgment call, since
    neither the plan nor any contract names who may initiate removal.
    """
    if workspace_id != auth.workspace_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="WORKSPACE_NOT_FOUND")
    role = authz.require_active_role(session, auth)
    is_self = user_id == auth.user_id
    if role not in {"owner", "admin"} and not is_self:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="INSUFFICIENT_ROLE")

    with session.begin():
        # See `update_member_role_endpoint`'s own comment on this same lock:
        # closes the identical concurrent-owner-removal race for `DELETE`.
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"membership-mutation:{auth.workspace_id}"},
        )
        member = _member_row(session, workspace_id=auth.workspace_id, user_id=user_id)
        if member is None or member["status"] != "active":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="MEMBER_NOT_FOUND")
        # Symmetric with `update_member_role_endpoint`'s own admin-cannot-
        # touch-an-owner guard, found in the same second-review pass: an
        # admin removing an owner is just as much an unapproved strip of
        # that owner's authority as demoting them would be. Self-removal is
        # unaffected -- `role` here is the caller's own current role, which
        # equals `member["role"]` whenever `is_self`, so an owner can always
        # still leave on their own.
        if member["role"] == "owner" and role != "owner":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="INSUFFICIENT_ROLE")
        if member["role"] == "owner" and _is_sole_active_owner(
            session, workspace_id=auth.workspace_id, users_id=user_id
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="LAST_OWNER_CANNOT_BE_REMOVED"
            )

        owned = authz.owned_resource_summary(
            session, workspace_id=auth.workspace_id, users_id=user_id
        )
        if owned:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={"code": "OWNED_RESOURCES_BLOCK_REMOVAL", "owned_resources": owned},
            )

        now = datetime.now(UTC)
        cancel_delegations_for_removed_member(
            session, workspace_id=auth.workspace_id, account_id=member["account_id"], now=now
        )
        # Found in the second whole-phase review, mirroring the delegation
        # cascade immediately above: `worker.py`'s own docstring already
        # discloses that a run is authorized once, at enqueue, and never
        # re-checks `created_by`'s live membership -- without this, a
        # removed member's still-running (or paused/awaiting-approval)
        # automation kept right on executing real side effects attributed
        # to someone no longer in the workspace.
        cancel_runs_for_removed_member(session, workspace_id=auth.workspace_id, users_id=user_id)
        session.execute(
            text(
                "UPDATE sessions SET revoked_at = :now "
                "WHERE workspace_id = :workspace_id AND user_id = :user_id AND revoked_at IS NULL"
            ),
            {"now": now, "workspace_id": auth.workspace_id, "user_id": user_id},
        )
        session.execute(
            text(
                "UPDATE workspace_memberships SET status = 'removed', removed_at = :now, "
                "updated_at = :now WHERE id = :id"
            ),
            {"now": now, "id": member["membership_id"]},
        )
        # See `update_member_role_endpoint`'s own comment on this same call.
        audit_outbox.write_audit_and_outbox(
            session,
            auth,
            request,
            event_type="member.removed",
            aggregate_type="workspace_membership",
            aggregate_id=member["membership_id"],
            aggregate_version=1,
            changed_fields=["*"],
            payload={"aggregate_id": str(member["membership_id"]), "version": 1},
            now=now,
            domain="identity",
        )
        queue_lifecycle_event(session, "identity", "member.removed", "allowed")
    return MemberRemovalResponse(
        user_id=user_id,
        export=MemberExportSnapshot(
            account_id=member["account_id"],
            email=member["email"],
            display_name=member["display_name"],
            role=member["role"],
            joined_at=member["created_at"],
            removed_at=now,
        ),
    )
