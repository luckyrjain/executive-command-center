"""`ecc.platform.authz` -- Phase 8 Task 3's authorization engine
(`docs/superpowers/specs/2026-08-01-phase-8-multi-user-design.md`
Decision 2, `docs/phases/phase-008/PERMISSION-CONTRACT.md`).

`authorize()` is the single, closed decision point every domain's list/
get/mutate endpoint calls before touching a resource -- mirroring how
`require_csrf`/`require_auth_context` (`ecc/auth.py`) are already the one
point every mutating endpoint goes through, not a pattern reinvented per
domain. This task wires it end to end into exactly one reference domain
(`engineering`, `ecc.domains.engineering.connector_accounts`/
`decisions_incidents`) to prove the mechanism; Task 4 repeats the same
wiring across every remaining domain -- no new mechanism there, per the
design doc's own "mechanical repetition, not new design" framing.

**Roles are a closed, bounded enum with a fixed permission set each --
not a customizable ACL builder.** `owner`/`admin`/`member` share the same
baseline (`read`, `write`) for `workspace`-visibility resources in this
phase's own scope (Decision 2's "roles do not grant private-vault access"
boundary is enforced by visibility, not by narrowing member's baseline
below admin's); `viewer` reads only. Membership/invitation/role
management itself is a *separate* authority, already gated directly
against `workspace_memberships.role` by `ecc.domains.identity.accounts`/
`invitations` (Task 1/2) -- `authorize()` governs *resource* access, not
who can invite or promote members.

**Signature note.** The design doc's own shorthand is
`authorize(auth, resource_type, resource_id, action) -> bool`; this
module's actual signature leads with `session` first, matching every
other DB-touching function's parameter order elsewhere in this codebase
(`_require_owner_or_admin`, `_write_identity_audit_event`, etc.) rather
than a literal transcription of the design doc's illustrative shorthand.

**`resource_type` is validated against a closed allowlist before ever
being interpolated into SQL** (`_RESOURCE_TABLES` -- table names cannot be
bind parameters) -- the exact 69-table union migration
`0063_phase8_authz_visibility.py` added `owner_id`/`visibility` to (its
own docstring enumerates and justifies every inclusion/exclusion
decision). An unrecognized `resource_type` raises rather than silently
querying an attacker-controlled table name.

**Every `authorize()` call reads fresh from Postgres -- no cache
anywhere to invalidate**, the structural guarantee Decision 4's
revocation-propagation SLO depends on: `workspace_memberships.status`,
the resource's own `owner_id`/`visibility`, and `resource_grants.
revoked_at`/`expires_at` are all read within this one call, never
memoized across requests or across a background job's own lifetime --
callers in `ecc.domains.automation`/`ecc.domains.engineering`'s
background/sync code must call `authorize()` again immediately before
each side-effecting step, not once at job-start (Decision 2's own
"background jobs re-check, never cache authority" line).

**`GET|POST /sharing/grants`, `DELETE /sharing/grants/{id}`** are this
module's own direct API for the mechanism above -- Task 5 is the
sharing-review UX and broader product surface, per the implementation
plan's own split. **Who may create a grant is a disclosed judgment
call**: neither the design doc nor `PERMISSION-CONTRACT.md` states it
explicitly, so this module requires the caller to be either the
resource's own `owner_id` or hold `owner`/`admin` role in the workspace
(the same bar `create_invitation_endpoint` already holds invitation
creation to) -- a grant widens who can see a specific resource, which is
exactly the kind of decision this codebase already reserves to a
resource's own owner or the workspace's own administrators elsewhere, not
opened to every member. The grantee must already hold an `active`
membership in this workspace (a grant to a non-member has no requester
context `authorize()` could ever evaluate it against). Revocation
(`DELETE`) is available to the same set, plus the grant's own original
`granted_by` actor -- whoever created a grant can always undo it, even if
their role changes afterward.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthContext, AuthDep, CsrfDep
from ecc.database import get_session

Role = Literal["owner", "admin", "member", "viewer"]
Action = Literal["read", "write"]

ROLES: tuple[Role, ...] = ("owner", "admin", "member", "viewer")
ACTIONS: tuple[Action, ...] = ("read", "write")

# Migration `0063_phase8_authz_visibility.py`'s own `_ORIGINAL_WORKSPACE_
# USER_SUBQUERY` formula, quoted verbatim, for callers writing a NEW row
# into a table that migration added `owner_id` to (its `_NEW_OWNER_FROM_
# WORKSPACE_ORIGINAL_USER` group) -- the identical formula that migration's
# own BEFORE INSERT trigger safety net uses when `owner_id` is left NULL
# (see that migration's own docstring, "Why every new/widened column also
# gets a DB-level default"). A domain's own INSERT should supply this
# explicitly, not rely on the trigger, whenever it bulk-inserts many rows
# in one statement -- a per-row trigger firing thousands of times inside a
# single bulk `INSERT ... SELECT` measurably regressed `ecc.domains.
# attention.attention`'s `regenerate` endpoint (a CI-budgeted p95 test
# caught it) purely from trigger-invocation overhead, even though the
# underlying subquery itself is cheap. Written as a **non-correlated**
# subquery (references only `:workspace_id`, never a per-row column of the
# statement it's embedded in) specifically so Postgres evaluates it once
# per statement, not once per row, when embedded in a bulk `INSERT ...
# SELECT`.
WORKSPACE_ORIGINAL_OWNER_SQL = (
    "(SELECT u.id FROM users AS u WHERE u.workspace_id = :workspace_id "
    "ORDER BY u.created_at ASC LIMIT 1)"
)

# Fixed permission baseline per role for `workspace`-visibility resources
# (Decision 2's own illustrative baseline, finalized here). Membership/
# invitation/role management is governed separately (Task 1/2's own
# direct `workspace_memberships.role` checks), not by this table.
_ROLE_PERMISSIONS: dict[Role, frozenset[Action]] = {
    "owner": frozenset({"read", "write"}),
    "admin": frozenset({"read", "write"}),
    "member": frozenset({"read", "write"}),
    "viewer": frozenset({"read"}),
}

# Decision 2 item 3's own explicit list, quoted verbatim -- no
# `resource_grants` row may ever name one of these as `resource_type`,
# regardless of the requesting role, enforced in `require_grantable`
# below before any grant insert.
UNGRANTABLE_RESOURCE_TYPES: frozenset[str] = frozenset(
    {
        "personal_domains",
        "domain_consents",
        "domain_sources",
        "domain_records",
        "goals",
        "routines",
        "check_ins",
        "personal_insights",
        "cross_domain_grants",
        "personal_insight_feedback",
    }
)

# The full set of resource_types authorize() can evaluate -- exactly the
# tables migration 0063_phase8_authz_visibility.py gave owner_id/visibility
# to (its own docstring is the source of truth for why each table is or
# isn't included). Kept as one literal tuple here, not derived from the
# migration at runtime, so this module has no import-time dependency on
# migration internals.
_RESOURCE_TABLES: frozenset[str] = frozenset(
    {
        # Phase 1 + deletion_jobs (pre-existing owner_id, visibility only).
        "tasks",
        "commitments",
        "notes",
        "risks",
        "deletion_jobs",
        # Phase 7 personal-domain tables (pre-existing owner_id, hardcoded
        # private -- also the UNGRANTABLE_RESOURCE_TYPES set above).
        "personal_domains",
        "domain_consents",
        "domain_sources",
        "domain_records",
        "goals",
        "routines",
        "check_ins",
        "personal_insights",
        "cross_domain_grants",
        "personal_insight_feedback",
        # New owner_id backfilled from an existing user_id column.
        "morning_briefs",
        "capacity_profiles",
        "planning_constraints",
        "plans",
        # New owner_id backfilled from the workspace's original user.
        "pkos_nodes",
        "pkos_edges",
        "pkos_evidence",
        "audit_events",
        "calendar_events",
        "meetings",
        "attention_items",
        "recommendations",
        "recommendation_feedback",
        "entity_aliases",
        "knowledge_claims",
        "timeline_entries",
        "resolution_candidates",
        "entity_operations",
        "retrieval_documents",
        "embedding_projections",
        "attention_feedback",
        "waiting_links",
        "risk_reviews",
        "plan_blocks",
        "meeting_participants",
        "meeting_packs",
        "ai_runs",
        "ai_run_steps",
        "evaluation_runs",
        "generated_artifacts",
        "workflow_definitions",
        "workflow_versions",
        "automation_policies",
        "triggers",
        "workflow_runs",
        "workflow_run_steps",
        "approval_requests",
        "compensation_steps",
        "automation_kill_switches",
        "connector_accounts",
        "sync_cursors",
        "sync_runs",
        "repositories",
        "engineering_work_items",
        "changes",
        "reviews",
        "delivery_metric_snapshots",
        "incidents",
        "engineering_decisions",
        "incident_changes",
        "decision_changes",
        "datadog_monitors",
        "datadog_service_definitions",
        "datadog_dashboards",
    }
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


class UnknownResourceTypeError(ValueError):
    """Raised when `resource_type` is not one of `_RESOURCE_TABLES` --
    programmer error (a typo'd or unregistered resource_type), not a
    request-shaped error any endpoint should catch and translate to a
    4xx; an endpoint passing this should have validated its own
    resource_type constant already.
    """


@dataclass(frozen=True)
class ResourceRef:
    workspace_id: UUID
    owner_id: UUID
    visibility: str


_IDENTIFIER_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def require_safe_sql_identifier(value: str, *, label: str) -> None:
    """Every current `table_alias` caller passes a hardcoded literal (e.g.
    `"incidents"`), so this is defense-in-depth, not a fix for a live
    injection path -- but `visible_resource_filter_sql` f-string-
    interpolates `table_alias` directly, unlike `resource_type` (validated
    by `require_known_resource_type` against a closed allowlist), so
    nothing today stops a future caller from building it from
    request-influenced input. Raising here fails the same way an unknown
    `resource_type` does: a programmer error to fix at the call site, not
    a request-shaped error for an endpoint to catch.
    """
    if not _IDENTIFIER_RE.match(value):
        raise UnknownResourceTypeError(f"{label} is not a safe SQL identifier: {value!r}")


def require_known_resource_type(resource_type: str) -> None:
    if resource_type not in _RESOURCE_TABLES:
        raise UnknownResourceTypeError(f"unknown resource_type: {resource_type!r}")


def require_grantable(resource_type: str) -> None:
    """The application-level `_UNGRANTABLE_RESOURCE_TYPES` check Decision 2
    item 3 names -- called before any `resource_grants` insert, not merely
    relied on as a convention no grant-creation code path happens to
    violate.
    """
    require_known_resource_type(resource_type)
    if resource_type in UNGRANTABLE_RESOURCE_TYPES:
        raise UnknownResourceTypeError(
            f"resource_type {resource_type!r} is ungrantable (Phase 7 personal-domain table)"
        )


def _current_role(session: Session, *, workspace_id: UUID, users_id: UUID) -> Role | None:
    """Read-only. Deliberately never calls `session.rollback()` -- every
    function in this module is called from both plain read-only GET
    endpoints (no open transaction) *and* from inside a mutating
    endpoint's own `with session.begin(): ...` block. Rolling back here
    would prematurely end that outer transaction -- and, since this
    codebase's advisory locks (`pg_advisory_xact_lock`) are
    transaction-scoped, would silently release a lock a caller like
    `_lock_idempotency` already took earlier in the same transaction. A
    GET-only endpoint with no open transaction needs no explicit rollback
    either -- `ecc.database.get_session`'s own `with SessionFactory() as
    session: yield session` closes (and thus rolls back) any still-open
    transaction at the request boundary regardless.
    """
    row = (
        session.execute(
            text(
                "SELECT role FROM workspace_memberships "
                "WHERE workspace_id = :workspace_id AND users_id = :users_id "
                "AND status = 'active'"
            ),
            {"workspace_id": workspace_id, "users_id": users_id},
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else row["role"]


def _account_id_for(session: Session, *, workspace_id: UUID, users_id: UUID) -> UUID | None:
    """Read-only -- see `_current_role`'s own docstring for why this never
    calls `session.rollback()`.
    """
    row = (
        session.execute(
            text(
                "SELECT account_id FROM users WHERE workspace_id = :workspace_id AND id = :users_id"
            ),
            {"workspace_id": workspace_id, "users_id": users_id},
        )
        .mappings()
        .one_or_none()
    )
    return None if row is None else row["account_id"]


def _users_id_for_account(session: Session, *, workspace_id: UUID, account_id: UUID) -> UUID | None:
    """The inverse of `_account_id_for` -- resolves an `accounts.id` to its
    `users.id` anchor within one workspace. `owner_id` on every table
    `_RESOURCE_TABLES` names FKs `users.(workspace_id, id)`, not
    `accounts.id` (migration `0063_phase8_authz_visibility.py`'s own FK
    topology), so `create_ownership_transfer_endpoint` needs this to stamp
    the transferred resource's new `owner_id` from the request's
    account-scoped `to_account_id`. Read-only -- see `_current_role`'s own
    docstring for why this never calls `session.rollback()`.
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
        .one_or_none()
    )
    return None if row is None else row["id"]


_UNOWNABLE_FOR_REMOVAL_PURPOSES: frozenset[str] = frozenset({"audit_events"})


def owned_resource_summary(
    session: Session, *, workspace_id: UUID, users_id: UUID
) -> list[dict[str, object]]:
    """Every grantable `resource_type` (excluding `UNGRANTABLE_RESOURCE_
    TYPES` -- Phase 7 personal-domain data is never workspace-transferable,
    a member-removal decision made once here rather than re-litigated at
    every call site -- and `audit_events`, see below) this `users_id`
    currently owns at least one row of, as `[{"resource_type": str, "count":
    int}]`. This is `ecc.domains.identity.membership_removal.remove_member_
    endpoint`'s own "records only they own" check -- iterating every table
    with one `SELECT count(*)` each is the same scanning shape `scripts/
    verify_restore.sh` already uses across every `workspace_id`-scoped
    table, acceptable here for the same reason: an administrative,
    infrequent operation, not a hot path.

    **`audit_events` is excluded, a real bug found and fixed against this
    function's own first test run, not a design choice made up front.**
    `audit_events` is itself one of the 69 tables in `_RESOURCE_TABLES`, but
    `ecc.platform.notifications`'s own `list_shared_activity_endpoint`
    docstring already established that every domain's `_write_side_effects`-
    equivalent writes `audit_events.owner_id` as the *acting user's own id*
    unconditionally, on every single event -- never a meaningful signal of
    who owns anything. Scanning it here without exclusion meant *any* active
    member who had ever created or mutated *any* resource (i.e. essentially
    every member) would show up as "owning" an `audit_events` row, making
    removal spuriously blocked almost universally -- caught directly by
    `tests/test_identity_membership_removal_postgres.py`'s own first CI run
    (`OWNED_RESOURCES_BLOCK_REMOVAL` on `audit_events` after the real,
    intentionally-owned `incidents` row had already been transferred away).

    Read-only -- see `_current_role`'s own docstring for why this never
    calls `session.rollback()`.
    """
    summary: list[dict[str, object]] = []
    excluded = UNGRANTABLE_RESOURCE_TYPES | _UNOWNABLE_FOR_REMOVAL_PURPOSES
    for resource_type in sorted(_RESOURCE_TABLES - excluded):
        count = session.execute(
            text(
                f"SELECT count(*) FROM {resource_type} "  # noqa: S608
                "WHERE workspace_id = :workspace_id AND owner_id = :users_id"
            ),
            {"workspace_id": workspace_id, "users_id": users_id},
        ).scalar_one()
        if count:
            summary.append({"resource_type": resource_type, "count": count})
    return summary


def _load_resource(
    session: Session, *, resource_type: str, resource_id: UUID
) -> ResourceRef | None:
    """Read-only -- see `_current_role`'s own docstring for why this never
    calls `session.rollback()`.
    """
    require_known_resource_type(resource_type)
    row = (
        session.execute(
            text(f"SELECT workspace_id, owner_id, visibility FROM {resource_type} WHERE id = :id"),  # noqa: S608
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


def _load_resource_for_update(
    session: Session, *, resource_type: str, resource_id: UUID
) -> ResourceRef | None:
    """Locked variant of `_load_resource`, for any endpoint that both
    authorizes against AND then mutates the same resource row in one
    transaction. Never call this from `authorize()`'s own read-only path
    (`_load_resource`'s own docstring explains why that path must stay
    unlocked).

    **Found in Phase 8's second whole-phase review, not the first.** The
    first review's fix to `create_ownership_transfer_endpoint` added a
    `SELECT ... FOR UPDATE` *after* `_require_owner_admin_or_resource_owner`
    had already run against an earlier, unlocked `_load_resource` read --
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


def _active_grant_exists(
    session: Session,
    *,
    workspace_id: UUID,
    account_id: UUID,
    resource_type: str,
    resource_id: UUID,
    action: Action,
    now: datetime,
) -> bool:
    """Read-only -- see `_current_role`'s own docstring for why this never
    calls `session.rollback()`.
    """
    row = (
        session.execute(
            text(
                """
                SELECT 1 FROM resource_grants
                WHERE workspace_id = :workspace_id AND grantee_account_id = :account_id
                  AND resource_type = :resource_type AND resource_id = :resource_id
                  AND :action = ANY(actions)
                  AND revoked_at IS NULL
                  AND (expires_at IS NULL OR expires_at > :now)
                """
            ),
            {
                "workspace_id": workspace_id,
                "account_id": account_id,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "action": action,
                "now": now,
            },
        )
        .mappings()
        .one_or_none()
    )
    return row is not None


def _active_grant_actions_for(
    session: Session,
    *,
    workspace_id: UUID,
    account_id: UUID,
    resource_type: str,
    resource_id: UUID,
    now: datetime,
) -> frozenset[Action]:
    """The union of `actions` across every currently-active grant naming
    `account_id` on this exact resource -- deliberately a union over *all*
    matching rows, not just the newest one, because nothing stops two
    separate grants (e.g. an earlier `read`-only grant, a later `write`
    grant layered on top without revoking the first) from coexisting.
    `_active_grant_exists` above stays a single-action, single-query
    `EXISTS`-shaped check for `authorize()`'s own hot path; this is the
    richer read `effective_permissions`/the sharing-review preview need
    and is not on `authorize()`'s call path, so the extra aggregation cost
    here never touches it. Read-only -- see `_current_role`'s own
    docstring for why this never calls `session.rollback()`.
    """
    rows = (
        session.execute(
            text(
                """
                SELECT actions FROM resource_grants
                WHERE workspace_id = :workspace_id AND grantee_account_id = :account_id
                  AND resource_type = :resource_type AND resource_id = :resource_id
                  AND revoked_at IS NULL
                  AND (expires_at IS NULL OR expires_at > :now)
                """
            ),
            {
                "workspace_id": workspace_id,
                "account_id": account_id,
                "resource_type": resource_type,
                "resource_id": resource_id,
                "now": now,
            },
        )
        .mappings()
        .all()
    )
    combined: set[Action] = set()
    for row in rows:
        combined.update(row["actions"])
    return frozenset(combined)


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


class EffectivePermissions(BaseModel):
    """`API-SCHEMAS.md`'s "resource responses expose effective permissions
    (which visibility tier and, where relevant, which grant is why the
    caller can see this resource)" requirement, made concrete. No field
    shape is spec'd anywhere else in the design doc/contracts -- this is
    the one shape every domain response embeds or every caller fetches
    standalone via `GET /sharing/resources/{resource_type}/{resource_id}`.
    """

    resource_type: str
    resource_id: UUID
    visibility: str
    owner_id: UUID
    is_owner: bool
    role: Role
    via: Literal["owner", "workspace_role", "resource_grant"]
    granted_actions: list[Action]


def effective_permissions(
    session: Session, auth: AuthContext, *, resource_type: str, resource_id: UUID
) -> EffectivePermissions | None:
    """The richer, response-shaped counterpart to `authorize()`'s plain
    `bool` -- same six-step decision (kept in the same order deliberately,
    so the two are easy to eyeball against each other and never drift),
    but returns *why* access is allowed and exactly which actions it
    covers, rather than a single action's pass/fail. Returns `None` under
    the identical conditions `authorize()` returns `False` for -- no active
    membership, unknown/cross-workspace/nonexistent resource, `private`
    and not the owner, or `shared_explicitly` with no active grant -- so
    every caller translates `None` to `404` exactly like every other
    single-resource route already does, never a different failure shape
    that could itself leak existence.
    """
    role = _current_role(session, workspace_id=auth.workspace_id, users_id=auth.user_id)
    if role is None:
        return None
    resource = _load_resource(session, resource_type=resource_type, resource_id=resource_id)
    if resource is None or resource.workspace_id != auth.workspace_id:
        return None
    if resource.owner_id == auth.user_id:
        return EffectivePermissions(
            resource_type=resource_type,
            resource_id=resource_id,
            visibility=resource.visibility,
            owner_id=resource.owner_id,
            is_owner=True,
            role=role,
            via="owner",
            granted_actions=list(ACTIONS),
        )
    if resource.visibility == "private":
        return None
    if resource.visibility == "shared_explicitly":
        account_id = _account_id_for(session, workspace_id=auth.workspace_id, users_id=auth.user_id)
        if account_id is None:
            return None
        actions = _active_grant_actions_for(
            session,
            workspace_id=auth.workspace_id,
            account_id=account_id,
            resource_type=resource_type,
            resource_id=resource_id,
            now=datetime.now(UTC),
        )
        if not actions:
            return None
        return EffectivePermissions(
            resource_type=resource_type,
            resource_id=resource_id,
            visibility=resource.visibility,
            owner_id=resource.owner_id,
            is_owner=False,
            role=role,
            via="resource_grant",
            granted_actions=sorted(actions),
        )
    if resource.visibility == "workspace":
        return EffectivePermissions(
            resource_type=resource_type,
            resource_id=resource_id,
            visibility=resource.visibility,
            owner_id=resource.owner_id,
            is_owner=False,
            role=role,
            via="workspace_role",
            granted_actions=sorted(_ROLE_PERMISSIONS[role]),
        )
    return None


def require_active_role(session: Session, auth: AuthContext) -> Role:
    """Returns the caller's current active role, or raises `403
    INSUFFICIENT_ROLE` if they hold no active membership in
    `auth.workspace_id` at all. For endpoints that create a brand-new
    resource -- there is no existing `owner_id`/`visibility` yet for
    `authorize()`'s own per-resource decision to run against, so creation
    is gated on role alone, mirroring `ecc.domains.identity.invitations`'
    own `_require_owner_or_admin` shape (a shared check, not reimplemented
    per domain).

    Unlike `authorize()`/`visible_resource_filter_sql` (called from both
    plain reads *and* from inside an already-open `with session.begin():`
    block, so they must never touch transaction state themselves), this
    function's own contract is always "called before the caller's own
    transaction begins" -- every call site in this codebase gates a
    *create* endpoint's very first line, before any `with session.begin():`.
    It rolls back its own read here so that guarantee holds structurally:
    without this, SQLAlchemy's autobegin would leave an open transaction
    from this function's own `SELECT`, and the caller's subsequent `with
    session.begin():` would raise `InvalidRequestError: A transaction is
    already begun on this Session` -- confirmed by hitting exactly that
    error while wiring this into `ecc.domains.engineering` and fixing it
    here rather than at every call site individually.
    """
    role = _current_role(session, workspace_id=auth.workspace_id, users_id=auth.user_id)
    session.rollback()
    if role is None:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="INSUFFICIENT_ROLE")
    return role


def require_role_action(session: Session, auth: AuthContext, action: Action) -> None:
    """Role-based gate for creating a brand-new `workspace`-visibility
    resource. Raises `403 INSUFFICIENT_ROLE` if the caller's current
    role's fixed permission set does not include `action` -- every role in
    `_ROLE_PERMISSIONS` includes `read` today, so this is only ever
    consequential for `write` (a `viewer` may not create resources).
    """
    role = require_active_role(session, auth)
    if action not in _ROLE_PERMISSIONS[role]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="INSUFFICIENT_ROLE")


def authorize(
    session: Session,
    auth: AuthContext,
    *,
    resource_type: str,
    resource_id: UUID,
    action: Action,
) -> bool:
    """Decision 2's six-step function, every branch reachable, none
    implicit. Returns `False` (never raises for an ordinary "no access")
    so callers can uniformly translate a `False` result to `404` for a
    single-resource `GET` without a try/except at every call site.

    For a mutate endpoint that needs both a read-visibility check and a
    write-permission check, call this twice -- once with `action="read"`
    (a `False` result means "doesn't exist, isn't visible to you, or
    you're not an active member here" -- report `404`), then, only after
    that has already succeeded, again with `action="write"` (a `False`
    result here means the resource IS visible to the caller but their role/
    grant doesn't cover writing to it -- safe to report `403`, since the
    read check already established the caller can see it, so `403` reveals
    nothing new). Calling this once with `action="write"` alone and
    translating a `False` result straight to `403` -- skipping the read
    check -- lets an unauthorized caller distinguish "does not exist" from
    "exists but you can't see it" by comparing 404 against 403, exactly the
    leak this two-call pattern (see `connector_accounts.py`'s call sites
    for the canonical shape) exists to prevent.
    """
    role = _current_role(session, workspace_id=auth.workspace_id, users_id=auth.user_id)
    if role is None:
        return False  # step 1: not an active member of this workspace at all

    resource = _load_resource(session, resource_type=resource_type, resource_id=resource_id)
    if resource is None or resource.workspace_id != auth.workspace_id:
        return False  # no such resource in the caller's own workspace

    if resource.owner_id == auth.user_id:
        return True  # step 2: the owner is always allowed

    if resource.visibility == "private":
        return False  # step 3

    if resource.visibility == "shared_explicitly":
        account_id = _account_id_for(session, workspace_id=auth.workspace_id, users_id=auth.user_id)
        if account_id is None:
            return False
        return _active_grant_exists(
            session,
            workspace_id=auth.workspace_id,
            account_id=account_id,
            resource_type=resource_type,
            resource_id=resource_id,
            action=action,
            now=datetime.now(UTC),
        )  # step 4

    if resource.visibility == "workspace":
        return action in _ROLE_PERMISSIONS[role]  # step 5

    return False  # step 6: no implicit allow path exists anywhere in this function


def visible_resource_filter_sql(
    session: Session,
    auth: AuthContext,
    *,
    resource_type: str,
    action: Action,
    table_alias: str,
) -> tuple[str, dict[str, object]]:
    """The list-endpoint counterpart to `authorize()` -- Decision 2's own
    "a denied list endpoint filters visibility server-side in the WHERE
    clause... never client-side" requirement, expressed once here so every
    domain's list endpoint embeds the identical filter shape instead of
    re-deriving it (Task 4 reuses this unchanged for every remaining
    domain, per the design doc's own "no new mechanism" framing).

    Returns `(sql_fragment, params)` -- the caller embeds `sql_fragment`
    into their own query's `WHERE` clause (already parenthesized, safe to
    `AND`/`OR` with other conditions) using `table_alias` as the alias for
    `resource_type`'s own table in that query, and merges `params` into
    their own bind-parameter dict. Every value is a bind parameter; only
    `table_alias`/`resource_type`-derived identifiers are ever
    interpolated into the SQL text, and both are validated first --
    `resource_type` against the closed `_RESOURCE_TABLES` allowlist,
    `table_alias` against `require_safe_sql_identifier`'s bare-identifier
    shape (every current caller passes a hardcoded literal alias, so this
    is defense-in-depth against a future caller building one dynamically,
    not a fix for a live injection path).
    """
    require_known_resource_type(resource_type)
    require_safe_sql_identifier(table_alias, label="table_alias")
    role = _current_role(session, workspace_id=auth.workspace_id, users_id=auth.user_id)
    if role is None:
        # Not an active member -- no row should ever match; a false
        # literal predicate is simpler and safer than asking every caller
        # to special-case an empty-role result themselves.
        return "FALSE", {}

    role_permits_workspace = action in _ROLE_PERMISSIONS[role]
    account_id = _account_id_for(session, workspace_id=auth.workspace_id, users_id=auth.user_id)

    workspace_clause = "TRUE" if role_permits_workspace else "FALSE"
    sql = (
        f"({table_alias}.owner_id = :__authz_user_id "
        f"OR ({table_alias}.visibility = 'workspace' AND {workspace_clause}) "
        f"OR ({table_alias}.visibility = 'shared_explicitly' AND {table_alias}.id IN ("
        "SELECT resource_id FROM resource_grants "
        "WHERE workspace_id = :__authz_workspace_id AND grantee_account_id = :__authz_account_id "
        "AND resource_type = :__authz_resource_type AND :__authz_action = ANY(actions) "
        "AND revoked_at IS NULL AND (expires_at IS NULL OR expires_at > :__authz_now)"
        ")))"
    )
    params: dict[str, object] = {
        "__authz_user_id": auth.user_id,
        "__authz_workspace_id": auth.workspace_id,
        "__authz_account_id": account_id,
        "__authz_resource_type": resource_type,
        "__authz_action": action,
        "__authz_now": datetime.now(UTC),
    }
    return sql, params


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
    role = _current_role(session, workspace_id=auth.workspace_id, users_id=auth.user_id)
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
    role = _current_role(session, workspace_id=auth.workspace_id, users_id=auth.user_id)
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
        account_id = _account_id_for(session, workspace_id=auth.workspace_id, users_id=auth.user_id)
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
            role = _current_role(session, workspace_id=auth.workspace_id, users_id=auth.user_id)
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

    resource = _load_resource(
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
    existing_grant_actions = _active_grant_actions_for(
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
        current_access = _ROLE_PERMISSIONS[grantee_role]
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

        to_users_id = _users_id_for_account(
            session, workspace_id=auth.workspace_id, account_id=payload.to_account_id
        )
        assert to_users_id is not None  # active membership implies a users row exists

        from_account_id = _account_id_for(
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
    role = _current_role(session, workspace_id=auth.workspace_id, users_id=auth.user_id)
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
        account_id = _account_id_for(session, workspace_id=auth.workspace_id, users_id=auth.user_id)
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
