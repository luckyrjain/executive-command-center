"""`ecc.platform.notifications` -- Phase 8 Task 7
(`docs/superpowers/plans/2026-08-01-phase-8-multi-user.md` Task 7,
`docs/phases/phase-008/PERMISSION-CONTRACT.md`). `GET /notifications`,
`POST /notifications/{id}/read`, `GET /shared/activity`.

**No shared `notify()` helper exported from here.** Writing a
`member_notifications` row is a two-line `INSERT ... ON CONFLICT DO
NOTHING`; rather than have `ecc.platform.authz` (which also needs to write
one, from `create_grant_endpoint`) import this module -- which would need
to import `authz` right back for `/shared/activity`'s own use of
`authorize()`, a real circular import -- each writer (`authz.py`'s
`create_grant_endpoint`, `ecc.domains.collaboration.delegations`'s six
transition endpoints) carries its own private copy, the identical
per-module-duplication convention this codebase already uses for the
idempotency helpers (`_lock_idempotency`/`_load_cached`/`_store_
idempotency`, reimplemented in every domain rather than shared). This
module owns only the *read* side of `member_notifications` plus the
separate, read-only `/shared/activity` feed.
"""

from __future__ import annotations

from base64 import urlsafe_b64decode, urlsafe_b64encode
from datetime import UTC, datetime
from hmac import compare_digest, new
from json import dumps, loads
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.orm import Session

from ecc.auth import AuthDep, CsrfDep
from ecc.config import get_settings
from ecc.database import get_session
from ecc.platform import authz

router = APIRouter(prefix="/api/v1", tags=["notifications"])
SessionDep = Annotated[Session, Depends(get_session)]

# ---------------------------------------------------------------------------
# Signed opaque cursor -- duplicated from
# `ecc.domains.platform.audit_queries`'s own identical helper rather than
# imported, matching this module's own docstring on per-module duplication.
# ---------------------------------------------------------------------------


def _sign(payload: dict[str, str]) -> str:
    body = urlsafe_b64encode(dumps(payload, separators=(",", ":")).encode()).decode()
    signature = new(get_settings().session_secret.encode(), body.encode(), "sha256").hexdigest()
    return f"{body}.{signature}"


def _decode(cursor: str) -> tuple[datetime, UUID]:
    try:
        body, signature = cursor.rsplit(".", 1)
        expected = new(get_settings().session_secret.encode(), body.encode(), "sha256").hexdigest()
        if not compare_digest(signature, expected):
            raise ValueError
        payload = loads(urlsafe_b64decode(body.encode()).decode())
        ts = datetime.fromisoformat(payload["ts"])
        if ts.utcoffset() is None:
            raise ValueError
        return ts, UUID(payload["id"])
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="INVALID_CURSOR") from None


def _account_id_for(session: Session, *, workspace_id: UUID, users_id: UUID) -> UUID:
    """Thin, raising wrapper around `authz.account_id_for` -- every call
    site here passes an `auth.user_id` from an already-authenticated
    session, so the row is always expected to exist; the assert converts
    that expectation into a hard failure rather than silently propagating
    `None` (candidate 6's dedup: this used to be its own private copy of
    `authz.account_id_for`'s query, see that function's own docstring).
    """
    account_id = authz.account_id_for(session, workspace_id=workspace_id, users_id=users_id)
    assert account_id is not None  # an authenticated caller's own users row always exists
    return account_id


# ---------------------------------------------------------------------------
# GET /notifications, POST /notifications/{id}/read
# ---------------------------------------------------------------------------


class NotificationResponse(BaseModel):
    id: UUID
    notification_type: str
    resource_ref: str
    read_at: datetime | None
    created_at: datetime


class NotificationListResponse(BaseModel):
    notifications: list[NotificationResponse]
    next_cursor: str | None


@router.get("/notifications", response_model=NotificationListResponse)
def list_notifications_endpoint(
    auth: AuthDep,
    session: SessionDep,
    unread_only: bool = False,
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
) -> NotificationListResponse:
    """Always scoped to the caller's own `account_id` -- there is no
    `owner`/`admin` "see everyone's notifications" broadening here, unlike
    `authz_grants.list_grants_endpoint`/`delegations.list_delegations_endpoint`'s
    own oversight carve-out: a notification is inherently addressed to one
    account, not a workspace resource an administrator oversees.
    """
    authz.require_active_role(session, auth)
    account_id = _account_id_for(session, workspace_id=auth.workspace_id, users_id=auth.user_id)

    conditions = ["workspace_id = :workspace_id", "account_id = :account_id"]
    params: dict[str, Any] = {
        "workspace_id": auth.workspace_id,
        "account_id": account_id,
        "limit": limit + 1,
    }
    if unread_only:
        conditions.append("read_at IS NULL")
    if cursor is not None:
        cursor_ts, cursor_id = _decode(cursor)
        conditions.append("(created_at, id) < (:cursor_ts, :cursor_id)")
        params.update(cursor_ts=cursor_ts, cursor_id=cursor_id)

    rows = (
        session.execute(
            text(
                f"""
                SELECT id, notification_type, resource_ref, read_at, created_at
                FROM member_notifications
                WHERE {" AND ".join(conditions)}
                ORDER BY created_at DESC, id DESC
                LIMIT :limit
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    session.rollback()
    has_more = len(rows) > limit
    visible = rows[:limit]
    next_cursor = None
    if has_more and visible:
        tail = visible[-1]
        next_cursor = _sign({"ts": tail["created_at"].isoformat(), "id": str(tail["id"])})
    return NotificationListResponse(
        notifications=[NotificationResponse(**dict(r)) for r in visible],
        next_cursor=next_cursor,
    )


@router.post("/notifications/{notification_id}/read", response_model=NotificationResponse)
def mark_notification_read_endpoint(
    notification_id: UUID,
    auth: AuthDep,
    session: SessionDep,
    _csrf: CsrfDep,
) -> NotificationResponse:
    """Idempotent -- marking an already-read notification read again is a
    no-op that returns the same response, not a `409`. Scoped by
    `account_id` in the same `WHERE` clause as the lookup itself (never a
    separate ownership check after the fact), so a notification addressed
    to a different account 404s exactly like a nonexistent id -- the same
    "never leak existence" shape every single-resource route in this
    codebase already holds to.
    """
    authz.require_active_role(session, auth)
    with session.begin():
        account_id = _account_id_for(session, workspace_id=auth.workspace_id, users_id=auth.user_id)
        row = (
            session.execute(
                text(
                    "SELECT id, notification_type, resource_ref, read_at, created_at "
                    "FROM member_notifications "
                    "WHERE id = :id AND workspace_id = :workspace_id AND account_id = :account_id"
                ),
                {
                    "id": notification_id,
                    "workspace_id": auth.workspace_id,
                    "account_id": account_id,
                },
            )
            .mappings()
            .one_or_none()
        )
        if row is None:
            raise HTTPException(status_code=404, detail="NOTIFICATION_NOT_FOUND")
        if row["read_at"] is not None:
            return NotificationResponse(**dict(row))

        updated = (
            session.execute(
                text(
                    "UPDATE member_notifications SET read_at = :now "
                    "WHERE id = :id AND read_at IS NULL "
                    "RETURNING id, notification_type, resource_ref, read_at, created_at"
                ),
                {"now": datetime.now(UTC), "id": notification_id},
            )
            .mappings()
            .one_or_none()
        )
        if updated is None:
            # Lost a race with a concurrent mark-read -- re-read fresh
            # rather than trust the now-stale `row` dict.
            refreshed = (
                session.execute(
                    text(
                        "SELECT id, notification_type, resource_ref, read_at, created_at "
                        "FROM member_notifications WHERE id = :id"
                    ),
                    {"id": notification_id},
                )
                .mappings()
                .one()
            )
            return NotificationResponse(**dict(refreshed))
        return NotificationResponse(**dict(updated))


# ---------------------------------------------------------------------------
# GET /shared/activity
# ---------------------------------------------------------------------------

# Explicit, closed map from `audit_events.aggregate_type` (each domain's own
# ad hoc vocabulary, e.g. `"incident"`) to the `resource_type` string
# `authz.authorize` understands (the plural table name, e.g. `"incidents"`).
# Deliberately NOT derived by guessing a pluralization rule -- grepping
# every `aggregate_type=` call site across the ~35 domain files that write
# `audit_events` found no shared convention (`"incident"` vs `"incidents"`,
# `"engineering_work_item"` vs `"engineering_work_items"`, etc. -- each
# domain chose its own singular/plural/synonym independently). A wrong
# guess here could silently map one resource's events onto an unrelated
# table with a colliding UUID and leak them -- far worse than the safe
# failure mode. Any `aggregate_type` not in this map is excluded from
# `/shared/activity` entirely (fails closed), never shown unfiltered.
#
# **Disclosed scope boundary**: this covers the `engineering` domain (Task
# 3's own original reference domain) plus the Phase 7 personal-domain
# aggregate types (useful precisely because they are `UNGRANTABLE_
# RESOURCE_TYPES` -- hardcoded `private` -- making them the concrete
# fixture this endpoint's own redaction test exercises: their activity
# must never appear in another member's feed, in any form). Widening this
# map to the remaining ~30 audit-emitting domains is incremental follow-up
# work, not a structural limitation of the mechanism -- adding a domain
# here requires no change to the filtering logic itself, only one more
# dict entry once that domain's own `aggregate_type` convention is
# confirmed.
#
# **`workspace_membership` (added in the third whole-phase review's
# `membership_removal.py` audit-trail fix) is deliberately excluded here,
# unlike every other entry.** Every other row in this map names a
# `resource_type` this same value already exists in `authz.py`'s
# `_RESOURCE_TABLES` -- a real, generically ownable/grantable resource this
# map's `authz.authorize(resource_type=..., action="read")` check can
# evaluate. `workspace_memberships` is identity infrastructure with no
# entry in `_RESOURCE_TABLES` at all (`membership_removal.py`'s own
# docstring: "`GET .../members` is readable by any active member,"
# unconditionally, not gated by ownership/visibility/grants the way every
# resource in this map is) -- reusing this map's generic authorize()-based
# filter for it would either reject every row (no matching resource_type
# ever authorizes) or require a special-cased always-visible-to-any-active-
# member rule this map's own closed, uniform contract does not have a slot
# for. Surfacing `member.role_changed`/`member.removed` through `/shared/
# activity` remains a real, disclosed gap -- closing it needs a small
# extension to this filtering mechanism, not just one more dict entry.
_AGGREGATE_TYPE_TO_RESOURCE_TYPE: dict[str, str] = {
    "incident": "incidents",
    "engineering_decision": "engineering_decisions",
    "engineering_work_item": "engineering_work_items",
    "repository": "repositories",
    "personal_domain": "personal_domains",
    "domain_record": "domain_records",
    "cross_domain_grant": "cross_domain_grants",
}

_ACTIVITY_CANDIDATE_MULTIPLIER = 4
_ACTIVITY_MAX_SCAN_ITERATIONS = 5


class ActivityEventResponse(BaseModel):
    id: UUID
    event_type: str
    aggregate_type: str
    aggregate_id: UUID
    actor_id: UUID | None
    occurred_at: datetime


class ActivityListResponse(BaseModel):
    items: list[ActivityEventResponse]
    next_cursor: str | None


def _fetch_audit_candidates(
    session: Session,
    *,
    workspace_id: UUID,
    before: tuple[datetime, UUID] | None,
    batch_size: int,
) -> list[dict[str, Any]]:
    conditions = ["workspace_id = :workspace_id"]
    params: dict[str, Any] = {"workspace_id": workspace_id, "limit": batch_size}
    if before is not None:
        conditions.append("(occurred_at, id) < (:before_ts, :before_id)")
        params.update(before_ts=before[0], before_id=before[1])
    rows = (
        session.execute(
            text(
                f"""
                SELECT id, event_type, aggregate_type, aggregate_id, actor_id, occurred_at
                FROM audit_events
                WHERE {" AND ".join(conditions)}
                ORDER BY occurred_at DESC, id DESC
                LIMIT :limit
                """
            ),
            params,
        )
        .mappings()
        .all()
    )
    return [dict(r) for r in rows]


@router.get("/shared/activity", response_model=ActivityListResponse)
def list_shared_activity_endpoint(
    auth: AuthDep,
    session: SessionDep,
    cursor: str | None = None,
    limit: int = Query(default=20, ge=1, le=100),
) -> ActivityListResponse:
    """`PERMISSION-CONTRACT.md`'s "affected members see redacted audit/
    activity without private-content leakage" -- built directly on
    `authz.authorize`'s own six-step decision, re-run fresh for every
    candidate row against the resource's CURRENT `owner_id`/`visibility`/
    grants. Deliberately never uses `audit_events.owner_id`/`visibility`
    directly (columns migration `0063_phase8_authz_visibility.py` added to
    every table it covers, `audit_events` included): every domain's own
    `_write_side_effects`-equivalent writes those two columns as the
    *actor's own id* and the literal string `'workspace'`, unconditionally,
    on every single event -- a placeholder satisfying that migration's own
    `NOT NULL` backfill, never a meaningful per-event copy of the real
    underlying resource's visibility. Filtering on them directly would have
    made every event visible to every active member regardless of the
    underlying resource's real, current visibility -- exactly the leak
    `PERMISSION-CONTRACT.md` requires this endpoint to prevent, and exactly
    why this re-derives the answer from `authorize()` fresh per row
    instead.

    Deliberately narrower than `GET /api/v1/audit` (the `owner`/`admin`-only,
    workspace-wide admin audit log `ecc.domains.platform.audit_queries`
    already exposes -- role-gated since the second whole-phase review found
    it had none at all): this response omits `before`/`after`/`metadata`
    entirely -- "there was activity on this resource," not a field-level
    diff -- and only ever returns rows whose `aggregate_type` is in
    `_AGGREGATE_TYPE_TO_RESOURCE_TYPE` and whose underlying resource the
    caller can currently read.

    Pagination note: because filtering happens after the underlying query
    (an unknown fraction of candidate rows may be redacted per page), this
    scans up to `limit * 4` candidate audit rows per underlying query,
    across up to 5 queries, trying to fill one page -- a bounded
    best-effort, not a guarantee. In a workspace where a caller's visible
    activity is sparse relative to total activity, a single page may
    legitimately return fewer than `limit` items even though more visible
    items exist further back; a non-null `next_cursor` means "keep going,"
    not "this many more exist."
    """
    authz.require_active_role(session, auth)
    remaining = limit
    collected: list[dict[str, Any]] = []
    walk_cursor = _decode(cursor) if cursor is not None else None
    exhausted = False

    for _ in range(_ACTIVITY_MAX_SCAN_ITERATIONS):
        if remaining <= 0:
            break
        batch_size = remaining * _ACTIVITY_CANDIDATE_MULTIPLIER
        batch = _fetch_audit_candidates(
            session, workspace_id=auth.workspace_id, before=walk_cursor, batch_size=batch_size
        )
        if not batch:
            exhausted = True
            break
        for row in batch:
            walk_cursor = (row["occurred_at"], row["id"])
            resource_type = _AGGREGATE_TYPE_TO_RESOURCE_TYPE.get(row["aggregate_type"])
            if resource_type is not None and authz.authorize(
                session,
                auth,
                resource_type=resource_type,
                resource_id=row["aggregate_id"],
                action="read",
            ):
                collected.append(row)
                remaining -= 1
                if remaining <= 0:
                    break
        if len(batch) < batch_size:
            exhausted = True
            break

    session.rollback()
    next_cursor = (
        None
        if exhausted or walk_cursor is None
        else _sign({"ts": walk_cursor[0].isoformat(), "id": str(walk_cursor[1])})
    )
    return ActivityListResponse(
        items=[
            ActivityEventResponse(
                id=r["id"],
                event_type=r["event_type"],
                aggregate_type=r["aggregate_type"],
                aggregate_id=r["aggregate_id"],
                actor_id=r["actor_id"],
                occurred_at=r["occurred_at"],
            )
            for r in collected
        ],
        next_cursor=next_cursor,
    )
