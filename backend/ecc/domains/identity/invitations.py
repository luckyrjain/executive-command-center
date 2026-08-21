"""Phase 8 Task 2: invitations -- `POST /identity/workspaces/{id}/invitations`,
`GET /identity/workspaces/{id}/invitations`, `POST /identity/invitations/{id}
/accept|reject|revoke` (`docs/phases/phase-008/API-SCHEMAS.md`).

`docs/superpowers/specs/2026-08-01-phase-8-multi-user-design.md` Decision 3.
Sibling module to `ecc.domains.identity.accounts` within the same `identity`
domain package -- imports `normalize_email`/`EMAIL_PATTERN` from there
rather than duplicating email validation. Both are public (no leading
underscore) in `accounts.py` specifically so this cross-module import is a
declared dependency, not a fragile reach into another module's internals --
`accounts.py`'s own deletion once broke this exact file when the helper it
imported was still private (round of dead-code cleanup earlier this phase).

**Two different acceptance paths, not one, because a brand-new recipient
cannot present an authenticated session that does not yet exist.**
`POST /identity/accounts` (`accounts.py`, modified by this task) now
strictly requires a valid, matching invitation `token` query parameter --
self-registration with no invitation is closed, per this task's own plan
line ("`POST /accounts` is wired to actually validate its token parameter
against a real, unaccepted, unexpired `invitations` row"). For a person
with **no existing account anywhere**, that single call registers the
account, creates the `users`/`workspace_memberships` rows, marks the
invitation accepted, and returns an authenticated session in one
transaction -- there is no separate `/accept` step for this path, because
nothing exists yet that could authenticate a follow-up call. `POST
/invitations/{id}/accept` (this module) instead serves the person who
**already holds a session from some other workspace membership** and is
accepting a new invitation to an additional workspace -- exactly the case
Decision 3's own accept-endpoint description (token match, expiry,
terminal-state and email-match checks, all inside one row-locked
transaction) applies to directly, since an authenticated caller already
exists to check those things against.

**A genuinely new company with no prior invitation from anyone has no
self-service signup path in this phase's scope.** `POST /workspaces`
(Task 1, unchanged) still lets an *already-authenticated* account create a
brand-new workspace and become its owner -- but reaching that first
authenticated state now requires a valid invitation token, full stop. This
matches `PHASE-008-multi-user.md`'s own focus on collaboration within
existing workspaces rather than initial tenant provisioning; a dedicated
"start a new company" onboarding flow, if this product needs one, is its
own separate design decision outside this phase's scope, not something to
silently smuggle back in as an unreviewed exception to Decision 3's own
"invitations are the only path in" line.

**`GET /workspaces/{id}/invitations` is `owner`/`admin`-only**, though the
implementation plan's own Task 2 line only states that restriction
explicitly for the create endpoint. Pending invitee email addresses are
sensitive enough (who has been invited, and to what role) that this
module holds every invitation endpoint to the same `owner`/`admin` bar
except accept/reject, which are recipient-initiated by design.

**An `admin` may not invite someone as `owner`.** Neither the design doc
nor the implementation plan states a restriction here, but leaving it
unrestricted would let any `admin` unilaterally mint a co-owner with no
`owner` ever approving it -- a real privilege-escalation path, caught in
this PR's own adversarial review round. `create_invitation_endpoint`
requires the caller to already be an `owner` themselves before a `role:
"owner"` invitation is accepted; `admin` can still invite at
`admin`/`member`/`viewer`. This is a minimal, conservative guard ahead of
Task 3's full authorization/role matrix, disclosed here as a judgment call
rather than left as a silent gap.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import compare_digest
from typing import Annotated
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session
from ecc.domains.identity.accounts import EMAIL_PATTERN, normalize_email
from ecc.observability import queue_lifecycle_event
from ecc.platform import audit_outbox

router = APIRouter(prefix="/api/v1/identity", tags=["identity"])
SessionDep = Annotated[Session, Depends(get_session)]

_INVITATION_MAX_AGE_SECONDS = 7 * 24 * 60 * 60


def _require_owner_or_admin(session: Session, *, workspace_id: UUID, users_id: UUID) -> str:
    membership = (
        session.execute(
            text(
                "SELECT role FROM workspace_memberships "
                "WHERE workspace_id = :workspace_id AND users_id = :users_id AND status = 'active'"
            ),
            {"workspace_id": workspace_id, "users_id": users_id},
        )
        .mappings()
        .one_or_none()
    )
    if membership is None or membership["role"] not in {"owner", "admin"}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="INSUFFICIENT_ROLE")
    return str(membership["role"])


class InvitationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str
    role: str = Field(pattern="^(owner|admin|member|viewer)$")

    @field_validator("email")
    @classmethod
    def _normalize(cls, value: str) -> str:
        normalized = normalize_email(value)
        if not EMAIL_PATTERN.match(normalized) or len(normalized) > 320:
            raise ValueError("EMAIL_INVALID")
        return normalized


class InvitationCreateResponse(BaseModel):
    id: UUID
    email: str
    role: str
    token: str
    expires_at: datetime
    created_at: datetime


class InvitationSummary(BaseModel):
    id: UUID
    email: str
    role: str
    expires_at: datetime
    accepted_at: datetime | None
    rejected_at: datetime | None
    revoked_at: datetime | None
    created_at: datetime


class InvitationListResponse(BaseModel):
    invitations: list[InvitationSummary]


class InvitationTokenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str


class InvitationActionResponse(BaseModel):
    id: UUID
    status: str
    workspace_id: UUID
    role: str | None = None


# ---------------------------------------------------------------------------
# POST|GET /identity/workspaces/{workspace_id}/invitations
# ---------------------------------------------------------------------------


@router.post(
    "/workspaces/{workspace_id}/invitations",
    response_model=InvitationCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
def create_invitation_endpoint(
    workspace_id: UUID,
    payload: InvitationCreateRequest,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
) -> InvitationCreateResponse:
    if workspace_id != auth.workspace_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="WORKSPACE_NOT_FOUND")
    now = datetime.now(UTC)
    with session.begin():
        caller_role = _require_owner_or_admin(
            session, workspace_id=workspace_id, users_id=auth.user_id
        )
        # An `admin` may invite at any role up to (not including) `owner`
        # -- unrestricted, an admin could unilaterally mint a co-owner with
        # no owner ever having approved it. Only an existing `owner` may
        # invite another `owner`. This is a minimal, conservative guard
        # ahead of Task 3's full authorization/role matrix, not a
        # substitute for it.
        if payload.role == "owner" and caller_role != "owner":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="INSUFFICIENT_ROLE")

        already_member = (
            session.execute(
                text(
                    """
                    SELECT 1
                    FROM workspace_memberships AS wm
                    JOIN users AS u ON u.workspace_id = wm.workspace_id AND u.id = wm.users_id
                    JOIN accounts AS a ON a.id = u.account_id
                    WHERE wm.workspace_id = :workspace_id AND a.email = :email
                      AND wm.status = 'active'
                    """
                ),
                {"workspace_id": workspace_id, "email": payload.email},
            )
            .mappings()
            .one_or_none()
        )
        if already_member is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="ALREADY_MEMBER")

        # `SELECT ... FOR UPDATE` alone only locks *existing* rows -- for a
        # brand-new recipient (the common case: no invitation has ever been
        # created for this workspace/email pair) it locks nothing at all,
        # so two concurrent creates would both see zero rows, both evaluate
        # `still_pending = False`, and both insert. An advisory lock on the
        # (workspace_id, email) pair itself, taken before that read,
        # serializes concurrent creates regardless of whether a row already
        # exists -- the same `pg_advisory_xact_lock(hashtextextended(...))`
        # discipline `_lock_idempotency` already uses elsewhere in this
        # codebase (e.g. `ecc.domains.scheduling.meetings`) for exactly
        # this "lock a key that may have zero rows" shape.
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
            {"lock_key": f"invitation:{workspace_id}:{payload.email}"},
        )
        # `FOR UPDATE` below is now belt-and-suspenders (the advisory lock
        # above already serializes this whole check-then-insert sequence)
        # but kept since it costs nothing and matches the migration's own
        # docstring description of the row-locking behavior.
        existing = (
            session.execute(
                text(
                    """
                    SELECT accepted_at, rejected_at, revoked_at, expires_at
                    FROM invitations
                    WHERE workspace_id = :workspace_id AND email = :email
                    FOR UPDATE
                    """
                ),
                {"workspace_id": workspace_id, "email": payload.email},
            )
            .mappings()
            .all()
        )
        still_pending = any(
            row["accepted_at"] is None
            and row["rejected_at"] is None
            and row["revoked_at"] is None
            and row["expires_at"] > now
            for row in existing
        )
        if still_pending:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="INVITATION_ALREADY_PENDING"
            )

        invitation_id = uuid4()
        raw_token = secrets.token_urlsafe(32)
        token_hash = sha256(raw_token.encode("utf-8")).hexdigest()
        expires_at = now + timedelta(seconds=_INVITATION_MAX_AGE_SECONDS)
        session.execute(
            text(
                """
                INSERT INTO invitations (
                    id, workspace_id, email, role, token_hash, invited_by,
                    expires_at, created_at
                ) VALUES (
                    :id, :workspace_id, :email, :role, :token_hash, :invited_by,
                    :expires_at, :created_at
                )
                """
            ),
            {
                "id": invitation_id,
                "workspace_id": workspace_id,
                "email": payload.email,
                "role": payload.role,
                "token_hash": token_hash,
                "invited_by": auth.user_id,
                "expires_at": expires_at,
                "created_at": now,
            },
        )
        audit_outbox.write_audit_and_outbox(
            session,
            auth,
            request,
            event_type="invitation.created",
            aggregate_type="invitation",
            aggregate_id=invitation_id,
            aggregate_version=1,
            changed_fields=["*"],
            payload={"aggregate_id": str(invitation_id), "version": 1},
            now=now,
            domain="identity",
        )
        queue_lifecycle_event(session, "identity", "invitation.created", "allowed")
    return InvitationCreateResponse(
        id=invitation_id,
        email=payload.email,
        role=payload.role,
        token=raw_token,
        expires_at=expires_at,
        created_at=now,
    )


@router.get("/workspaces/{workspace_id}/invitations", response_model=InvitationListResponse)
def list_invitations_endpoint(
    workspace_id: UUID, auth: AuthDep, session: SessionDep
) -> InvitationListResponse:
    if workspace_id != auth.workspace_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="WORKSPACE_NOT_FOUND")
    _require_owner_or_admin(session, workspace_id=workspace_id, users_id=auth.user_id)
    rows = (
        session.execute(
            text(
                """
                SELECT id, email, role, expires_at, accepted_at, rejected_at,
                       revoked_at, created_at
                FROM invitations
                WHERE workspace_id = :workspace_id
                ORDER BY created_at DESC
                """
            ),
            {"workspace_id": workspace_id},
        )
        .mappings()
        .all()
    )
    session.rollback()
    return InvitationListResponse(invitations=[InvitationSummary(**dict(r)) for r in rows])


# ---------------------------------------------------------------------------
# POST /identity/invitations/{invitation_id}/accept|reject|revoke
# ---------------------------------------------------------------------------


@router.post(
    "/invitations/{invitation_id}/accept",
    response_model=InvitationActionResponse,
)
def accept_invitation_endpoint(
    invitation_id: UUID,
    payload: InvitationTokenRequest,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
) -> InvitationActionResponse:
    """For a caller who already holds a session (from any workspace
    membership) accepting a *different* invitation. A brand-new recipient
    with no account anywhere never reaches this endpoint at all -- see this
    module's own docstring for why `POST /accounts` handles that path
    entirely on its own.
    """
    now = datetime.now(UTC)
    token_hash = sha256(payload.token.encode("utf-8")).hexdigest()
    with session.begin():
        invitation = (
            session.execute(
                text(
                    """
                    SELECT workspace_id, email, role, token_hash, invited_by, expires_at,
                           accepted_at, rejected_at, revoked_at
                    FROM invitations
                    WHERE id = :id
                    FOR UPDATE
                    """
                ),
                {"id": invitation_id},
            )
            .mappings()
            .one_or_none()
        )
        if invitation is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="INVITATION_NOT_FOUND"
            )
        if not compare_digest(invitation["token_hash"], token_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="INVITATION_TOKEN_INVALID"
            )
        if invitation["expires_at"] <= now:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="INVITATION_EXPIRED"
            )
        if (
            invitation["accepted_at"] is not None
            or invitation["rejected_at"] is not None
            or invitation["revoked_at"] is not None
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="INVITATION_ALREADY_RESOLVED"
            )

        # Found in the third whole-phase review, the same "re-authorize at
        # use-time, not trust proposal-time" bug class `delegations.py`'s own
        # `_grant_evidence` was fixed for in the second review: an
        # `owner`-role invitation's grant was checked only once, against
        # `create_invitation_endpoint`'s caller at creation time
        # (`payload.role == "owner" and caller_role != "owner"`), and never
        # re-verified against that same inviter's *current* authority here at
        # accept time -- an invitation can sit pending for up to
        # `_INVITATION_MAX_AGE_SECONDS` (7 days), long enough for the
        # inviting owner to be demoted or removed in the meantime, after
        # which accepting still mints a fresh co-owner nobody currently
        # holding owner authority approved.
        if invitation["role"] == "owner":
            inviter_membership = (
                session.execute(
                    text(
                        "SELECT role FROM workspace_memberships "
                        "WHERE workspace_id = :workspace_id AND users_id = :users_id "
                        "AND status = 'active'"
                    ),
                    {
                        "workspace_id": invitation["workspace_id"],
                        "users_id": invitation["invited_by"],
                    },
                )
                .mappings()
                .one_or_none()
            )
            if inviter_membership is None or inviter_membership["role"] != "owner":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT, detail="INVITER_NO_LONGER_AUTHORIZED"
                )

        account_row = (
            session.execute(
                text(
                    "SELECT a.email, a.id AS account_id FROM users AS u "
                    "JOIN accounts AS a ON a.id = u.account_id "
                    "WHERE u.workspace_id = :workspace_id AND u.id = :users_id"
                ),
                {"workspace_id": auth.workspace_id, "users_id": auth.user_id},
            )
            .mappings()
            .one()
        )
        if account_row["email"] != invitation["email"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="INVITATION_EMAIL_MISMATCH"
            )

        target_workspace_id = invitation["workspace_id"]
        # Scoped to `status = 'active'` deliberately -- `membership_removal.
        # py`'s own `remove_member_endpoint` never deletes a workspace_
        # memberships row, only flips it to `status = 'removed'` (Decision
        # 1: "never deletes the `users` row"). An unscoped check here would
        # make a removed member's own stale row permanently block them from
        # ever being re-invited and re-accepting, even though nothing about
        # `ALREADY_MEMBER`'s own meaning ("you already hold active
        # membership") applies to them anymore.
        existing_membership = (
            session.execute(
                text(
                    "SELECT id, status, users_id FROM workspace_memberships "
                    "WHERE workspace_id = :workspace_id AND account_id = :account_id "
                    "FOR UPDATE"
                ),
                {"workspace_id": target_workspace_id, "account_id": account_row["account_id"]},
            )
            .mappings()
            .one_or_none()
        )
        if existing_membership is not None and existing_membership["status"] == "active":
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="ALREADY_MEMBER")

        # A prior membership (now `removed`) also means a `users` row for
        # this exact (workspace_id, account_id) pair already exists --
        # `uq_users_workspace_account` would reject a second `INSERT` for
        # it, and it is the same identity's own history, not a stranger's,
        # so reusing it (rather than minting a new `users.id`) is correct,
        # not merely constraint-driven.
        if existing_membership is not None:
            new_users_id = existing_membership["users_id"]
        else:
            new_users_id = uuid4()
        # The read above is `FOR UPDATE` (narrows the common case and
        # serializes a concurrent reactivation of the *same* prior
        # membership row), but cannot itself prevent two concurrent
        # accepts of two separately-created pending invitations for a
        # recipient with no prior membership at all from both passing it
        # and racing on the inserts below. `uq_users_workspace_account`/
        # `uq_workspace_memberships_workspace_account` are the actual
        # backstop for that case; catching the violation here turns it
        # into the documented `409 ALREADY_MEMBER` instead of an unhandled
        # 500, matching how `create_account_endpoint` already handles its
        # own analogous `accounts.email` race.
        try:
            if existing_membership is None:
                session.execute(
                    text(
                        "INSERT INTO users (id, workspace_id, account_id, created_at) "
                        "VALUES (:id, :workspace_id, :account_id, :now)"
                    ),
                    {
                        "id": new_users_id,
                        "workspace_id": target_workspace_id,
                        "account_id": account_row["account_id"],
                        "now": now,
                    },
                )
            # `invited_by` must be a `users.id` within `target_workspace_id`
            # -- `auth.user_id` is the accepting caller's own users_id,
            # scoped to whichever *other* workspace their session belongs
            # to (this endpoint exists precisely because the caller is not
            # yet an *active* member of `target_workspace_id`), so it can
            # never satisfy `fk_workspace_memberships_workspace_invited_by`.
            # The invitation's own `invited_by` (the real inviter, already
            # a member of `target_workspace_id`) is the only value that
            # can.
            if existing_membership is not None:
                session.execute(
                    text(
                        """
                        UPDATE workspace_memberships
                        SET status = 'active', role = :role, invited_by = :invited_by,
                            removed_at = NULL, updated_at = :now
                        WHERE id = :id
                        """
                    ),
                    {
                        "id": existing_membership["id"],
                        "role": invitation["role"],
                        "invited_by": invitation["invited_by"],
                        "now": now,
                    },
                )
            else:
                session.execute(
                    text(
                        """
                    INSERT INTO workspace_memberships (
                        id, workspace_id, account_id, users_id, role, status,
                        invited_by, created_at, updated_at
                    ) VALUES (
                        :id, :workspace_id, :account_id, :users_id, :role, 'active',
                        :invited_by, :now, :now
                    )
                    """
                    ),
                    {
                        "id": uuid4(),
                        "workspace_id": target_workspace_id,
                        "account_id": account_row["account_id"],
                        "users_id": new_users_id,
                        "role": invitation["role"],
                        "invited_by": invitation["invited_by"],
                        "now": now,
                    },
                )
        except IntegrityError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="ALREADY_MEMBER"
            ) from exc
        session.execute(
            text("UPDATE invitations SET accepted_at = :now WHERE id = :id"),
            {"now": now, "id": invitation_id},
        )
        audit_outbox.write_audit_and_outbox(
            session,
            AuthContext(workspace_id=target_workspace_id, user_id=new_users_id, timezone="UTC"),
            request,
            event_type="invitation.accepted",
            aggregate_type="invitation",
            aggregate_id=invitation_id,
            aggregate_version=1,
            changed_fields=["*"],
            payload={"aggregate_id": str(invitation_id), "version": 1},
            now=now,
            domain="identity",
        )
        queue_lifecycle_event(session, "identity", "invitation.accepted", "allowed")
    return InvitationActionResponse(
        id=invitation_id,
        status="accepted",
        workspace_id=target_workspace_id,
        role=invitation["role"],
    )


@router.post("/invitations/{invitation_id}/reject", response_model=InvitationActionResponse)
def reject_invitation_endpoint(
    invitation_id: UUID,
    payload: InvitationTokenRequest,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
) -> InvitationActionResponse:
    now = datetime.now(UTC)
    token_hash = sha256(payload.token.encode("utf-8")).hexdigest()
    with session.begin():
        invitation = (
            session.execute(
                text(
                    """
                    SELECT workspace_id, email, token_hash, expires_at,
                           accepted_at, rejected_at, revoked_at
                    FROM invitations
                    WHERE id = :id
                    FOR UPDATE
                    """
                ),
                {"id": invitation_id},
            )
            .mappings()
            .one_or_none()
        )
        if invitation is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="INVITATION_NOT_FOUND"
            )
        if not compare_digest(invitation["token_hash"], token_hash):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="INVITATION_TOKEN_INVALID"
            )
        if invitation["expires_at"] <= now:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="INVITATION_EXPIRED"
            )
        if (
            invitation["accepted_at"] is not None
            or invitation["rejected_at"] is not None
            or invitation["revoked_at"] is not None
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="INVITATION_ALREADY_RESOLVED"
            )

        account_row = (
            session.execute(
                text(
                    "SELECT a.email FROM users AS u "
                    "JOIN accounts AS a ON a.id = u.account_id "
                    "WHERE u.workspace_id = :workspace_id AND u.id = :users_id"
                ),
                {"workspace_id": auth.workspace_id, "users_id": auth.user_id},
            )
            .mappings()
            .one()
        )
        if account_row["email"] != invitation["email"]:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="INVITATION_EMAIL_MISMATCH"
            )

        session.execute(
            text("UPDATE invitations SET rejected_at = :now WHERE id = :id"),
            {"now": now, "id": invitation_id},
        )
        # Unlike accept, reject never creates a `users` row in
        # `invitation["workspace_id"]` -- the caller has no workspace-scoped
        # identity there to record as `actor_id`, and `auth.user_id` (scoped
        # to whichever *other* workspace their session belongs to) would
        # violate `fk_audit_workspace_actor`'s composite FK. `actor_id` is
        # nullable for exactly this case.
        audit_outbox.write_audit_and_outbox(
            session,
            AuthContext(
                workspace_id=invitation["workspace_id"], user_id=auth.user_id, timezone="UTC"
            ),
            request,
            event_type="invitation.rejected",
            aggregate_type="invitation",
            aggregate_id=invitation_id,
            aggregate_version=1,
            changed_fields=["*"],
            payload={"aggregate_id": str(invitation_id), "version": 1},
            now=now,
            domain="identity",
            actor_id=None,
        )
        queue_lifecycle_event(session, "identity", "invitation.rejected", "allowed")
    return InvitationActionResponse(
        id=invitation_id, status="rejected", workspace_id=invitation["workspace_id"]
    )


@router.post("/invitations/{invitation_id}/revoke", response_model=InvitationActionResponse)
def revoke_invitation_endpoint(
    invitation_id: UUID,
    request: Request,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
) -> InvitationActionResponse:
    """Inviter/admin-initiated -- no email-match check, since the caller is
    authorized structurally by their own role in the invitation's
    workspace, not by being the invitation's recipient.
    """
    now = datetime.now(UTC)
    with session.begin():
        invitation = (
            session.execute(
                text(
                    """
                    SELECT workspace_id, accepted_at, rejected_at, revoked_at
                    FROM invitations
                    WHERE id = :id
                    FOR UPDATE
                    """
                ),
                {"id": invitation_id},
            )
            .mappings()
            .one_or_none()
        )
        if invitation is None or invitation["workspace_id"] != auth.workspace_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="INVITATION_NOT_FOUND"
            )
        _require_owner_or_admin(session, workspace_id=auth.workspace_id, users_id=auth.user_id)
        if (
            invitation["accepted_at"] is not None
            or invitation["rejected_at"] is not None
            or invitation["revoked_at"] is not None
        ):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="INVITATION_ALREADY_RESOLVED"
            )

        session.execute(
            text("UPDATE invitations SET revoked_at = :now WHERE id = :id"),
            {"now": now, "id": invitation_id},
        )
        audit_outbox.write_audit_and_outbox(
            session,
            auth,
            request,
            event_type="invitation.revoked",
            aggregate_type="invitation",
            aggregate_id=invitation_id,
            aggregate_version=1,
            changed_fields=["*"],
            payload={"aggregate_id": str(invitation_id), "version": 1},
            now=now,
            domain="identity",
        )
        queue_lifecycle_event(session, "identity", "invitation.revoked", "allowed")
    return InvitationActionResponse(
        id=invitation_id, status="revoked", workspace_id=auth.workspace_id
    )
