"""Phase 8 Task 7: `ecc.platform.notifications`
(`docs/superpowers/plans/2026-08-01-phase-8-multi-user.md` Task 7,
`docs/phases/phase-008/PERMISSION-CONTRACT.md`). Per this task's own
stated test requirements:

1. Notification idempotency: a duplicate underlying event does not
   double-notify (`test_delegation_expiry_duplicate_event_does_not_
   double_notify`, which forces the exact same delegation to expire
   twice -- the concrete shape a lazy-expiry race between two concurrent
   requests would produce -- and asserts only one `member_notifications`
   row survives).
2. Redaction: a `private`-visibility resource's activity never appears in
   another member's `/shared/activity` feed even in redacted form
   (`test_shared_activity_redacts_private_resource_from_other_members`,
   using a Phase 7 personal-domain resource -- hardcoded `private`,
   `UNGRANTABLE_RESOURCE_TYPES` -- as the fixture).

Plus coverage of the two notification-producing call sites this task wired
(`ecc.platform.authz.create_grant_endpoint`, `ecc.domains.collaboration.
delegations`'s six transitions), `GET /notifications`'s own account-scoping
and `unread_only` filter, `POST /notifications/{id}/read`'s idempotency and
"never leak existence" 404 shape, and `/shared/activity`'s positive case
(a `workspace`-visibility resource IS shown) alongside its disclosed
`_AGGREGATE_TYPE_TO_RESOURCE_TYPE` scope boundary (an unmapped
`aggregate_type`, e.g. `invitation`, is excluded rather than shown
unfiltered).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new as hmac_new
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from identity_fixtures import create_identity
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ecc.config import get_settings
from ecc.database import engine
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


def _headers(token: str, key: str | None = None) -> dict[str, str]:
    csrf = hmac_new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    headers = {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _cleanup_workspace(workspace_id: UUID) -> None:
    with engine.begin() as connection:
        # delegation_events/delegation_evidence carry no workspace_id column
        # of their own -- deleting delegations cascades to both (migration
        # 0064's own ON DELETE CASCADE). member_notifications does carry its
        # own workspace_id (migration 0065), so it needs its own DELETE.
        for table in (
            "member_notifications",
            "delegations",
            "resource_grants",
            "personal_domains",
            "invitations",
            "incidents",
            # _create_incident/_create_personal_domain (the real endpoints)
            # also write audit_events/event_outbox/idempotency_records --
            # audit_events.fk_audit_workspace_actor references users, so it
            # must be deleted before users or that FK blocks the delete.
            "event_outbox",
            "audit_events",
            "idempotency_records",
            "sessions",
            "workspace_memberships",
            "users",
        ):
            connection.execute(
                text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                {"workspace_id": workspace_id},
            )
        connection.execute(
            text("DELETE FROM workspaces WHERE id = :workspace_id"), {"workspace_id": workspace_id}
        )


@dataclass(frozen=True)
class _Actor:
    user_id: UUID
    account_id: UUID
    token: str
    client: TestClient


def _create_session(
    connection: Connection, *, workspace_id: UUID, user_id: UUID, token: str
) -> None:
    now = datetime.now(UTC)
    connection.execute(
        text(
            "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
            "expires_at, last_seen_at) "
            "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :now)"
        ),
        {
            "id": uuid4(),
            "workspace_id": workspace_id,
            "user_id": user_id,
            "token_hash": sha256(token.encode()).hexdigest(),
            "expires_at": now + timedelta(hours=1),
            "now": now,
        },
    )


def _make_actor(
    connection: Connection, *, workspace_id: UUID, role: str, status: str = "active"
) -> _Actor:
    user_id = uuid4()
    token = f"session-{uuid4()}"
    create_identity(
        connection,
        workspace_id=workspace_id,
        user_id=user_id,
        email=f"{user_id}@example.test",
        role=role,
        status=status,
    )
    _create_session(connection, workspace_id=workspace_id, user_id=user_id, token=token)
    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    account_id = connection.execute(
        text("SELECT account_id FROM users WHERE id = :id"), {"id": user_id}
    ).scalar_one()
    return _Actor(user_id=user_id, account_id=account_id, token=token, client=client)


@dataclass(frozen=True)
class _NotificationContext:
    workspace_id: UUID
    owner: _Actor
    member: _Actor
    other_member: _Actor


@pytest.fixture
def notification_context() -> Iterator[_NotificationContext]:
    workspace_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Notification Test', 'UTC', :now)"
            ),
            {"id": workspace_id, "now": now},
        )
        owner = _make_actor(connection, workspace_id=workspace_id, role="owner")
        member = _make_actor(connection, workspace_id=workspace_id, role="member")
        other_member = _make_actor(connection, workspace_id=workspace_id, role="member")

    context = _NotificationContext(
        workspace_id=workspace_id, owner=owner, member=member, other_member=other_member
    )
    try:
        yield context
    finally:
        for actor in (owner, member, other_member):
            actor.client.close()
        _cleanup_workspace(workspace_id)


def _create_incident(
    client: TestClient, token: str, *, title: str = "Obligation"
) -> dict[str, Any]:
    response = client.post(
        "/api/v1/engineering/incidents",
        json={"title": title, "severity": "high", "detected_at": datetime.now(UTC).isoformat()},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


def _propose_delegation(
    client: TestClient,
    token: str,
    *,
    recipient_account_id: UUID,
    obligation_resource_id: str,
    due_at: datetime | None = None,
) -> dict[str, Any]:
    response = client.post(
        "/api/v1/delegations",
        json={
            "recipient_account_id": str(recipient_account_id),
            "obligation_type": "incidents",
            "obligation_resource_id": obligation_resource_id,
            "expected_outcome": "Resolve the incident",
            "due_at": (due_at or (datetime.now(UTC) + timedelta(days=1))).isoformat(),
            "evidence": [],
        },
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


def _notifications_for(client: TestClient, token: str, **params: str) -> list[dict[str, Any]]:
    response = client.get("/api/v1/notifications", params=params, headers=_headers(token))
    assert response.status_code == 200, response.text
    return response.json()["notifications"]  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# GET /notifications, POST /notifications/{id}/read
# ---------------------------------------------------------------------------


def test_grant_creation_notifies_grantee(notification_context: _NotificationContext) -> None:
    ctx = notification_context
    incident = _create_incident(ctx.owner.client, ctx.owner.token)

    grant_response = ctx.owner.client.post(
        "/api/v1/sharing/grants",
        json={
            "resource_type": "incidents",
            "resource_id": incident["id"],
            "grantee_account_id": str(ctx.member.account_id),
            "actions": ["read"],
            "narrow_visibility": True,
        },
        headers=_headers(ctx.owner.token, key=str(uuid4())),
    )
    assert grant_response.status_code == 201, grant_response.text
    grant_id = grant_response.json()["id"]

    notifications = _notifications_for(ctx.member.client, ctx.member.token)
    assert [n for n in notifications if n["notification_type"] == "grant.created"] == [
        n for n in notifications if n["resource_ref"] == f"resource_grants:{grant_id}"
    ]
    matching = [n for n in notifications if n["resource_ref"] == f"resource_grants:{grant_id}"]
    assert len(matching) == 1
    assert matching[0]["read_at"] is None

    # The grantor never notifies themselves.
    owner_notifications = _notifications_for(ctx.owner.client, ctx.owner.token)
    assert all(n["resource_ref"] != f"resource_grants:{grant_id}" for n in owner_notifications)


def test_delegation_lifecycle_notifies_the_correct_party(
    notification_context: _NotificationContext,
) -> None:
    ctx = notification_context
    incident = _create_incident(ctx.owner.client, ctx.owner.token)
    delegation = _propose_delegation(
        ctx.owner.client,
        ctx.owner.token,
        recipient_account_id=ctx.member.account_id,
        obligation_resource_id=incident["id"],
    )
    ref = f"delegations:{delegation['id']}"

    # Proposing notifies the recipient, not the delegator.
    recipient_notifications = _notifications_for(ctx.member.client, ctx.member.token)
    assert any(
        n["resource_ref"] == ref and n["notification_type"] == "delegation.proposed"
        for n in recipient_notifications
    )
    owner_notifications = _notifications_for(ctx.owner.client, ctx.owner.token)
    assert all(n["resource_ref"] != ref for n in owner_notifications)

    accept = ctx.member.client.post(
        f"/api/v1/delegations/{delegation['id']}/accept",
        headers=_headers(ctx.member.token, key=str(uuid4())),
    )
    assert accept.status_code == 200, accept.text

    # Accepting notifies the delegator, not the recipient (who already knows).
    owner_notifications = _notifications_for(ctx.owner.client, ctx.owner.token)
    assert any(
        n["resource_ref"] == ref and n["notification_type"] == "delegation.accepted"
        for n in owner_notifications
    )

    complete = ctx.member.client.post(
        f"/api/v1/delegations/{delegation['id']}/complete",
        headers=_headers(ctx.member.token, key=str(uuid4())),
    )
    assert complete.status_code == 200, complete.text

    owner_notifications = _notifications_for(ctx.owner.client, ctx.owner.token)
    assert any(
        n["resource_ref"] == ref and n["notification_type"] == "delegation.completed"
        for n in owner_notifications
    )


def test_delegation_expiry_duplicate_event_does_not_double_notify(
    notification_context: _NotificationContext,
) -> None:
    """The migration's own literal "duplicate underlying event does not
    double-notify" requirement -- forces the same delegation through the
    lazy-expiry transition twice (resetting it back to `proposed` with a
    past `due_at` between the two `GET /delegations` calls), the concrete
    shape a race between two concurrent requests both lazily expiring the
    same overdue delegation would produce. Only one notification per party
    should survive, via `member_notifications`'s own `ON CONFLICT (...) DO
    NOTHING` against `uq_member_notifications_dedup`.
    """
    ctx = notification_context
    incident = _create_incident(ctx.owner.client, ctx.owner.token)
    delegation = _propose_delegation(
        ctx.owner.client,
        ctx.owner.token,
        recipient_account_id=ctx.member.account_id,
        obligation_resource_id=incident["id"],
        due_at=datetime.now(UTC) + timedelta(seconds=1),
    )
    delegation_id = UUID(delegation["id"])
    past_due = datetime.now(UTC) - timedelta(hours=1)

    with engine.begin() as connection:
        connection.execute(
            text("UPDATE delegations SET due_at = :due_at WHERE id = :id"),
            {"due_at": past_due, "id": delegation_id},
        )

    first = ctx.owner.client.get("/api/v1/delegations", headers=_headers(ctx.owner.token))
    assert first.status_code == 200, first.text
    expired = next(d for d in first.json()["delegations"] if d["id"] == delegation["id"])
    assert expired["status"] == "expired"

    # Force the exact same underlying event to recur.
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE delegations SET status = 'proposed', due_at = :due_at WHERE id = :id"),
            {"due_at": past_due, "id": delegation_id},
        )

    second = ctx.owner.client.get("/api/v1/delegations", headers=_headers(ctx.owner.token))
    assert second.status_code == 200, second.text
    expired_again = next(d for d in second.json()["delegations"] if d["id"] == delegation["id"])
    assert expired_again["status"] == "expired"

    ref = f"delegations:{delegation['id']}"
    with engine.begin() as connection:
        for account_id in (ctx.owner.account_id, ctx.member.account_id):
            count = connection.execute(
                text(
                    "SELECT count(*) FROM member_notifications "
                    "WHERE account_id = :account_id AND notification_type = 'delegation.expired' "
                    "AND resource_ref = :ref"
                ),
                {"account_id": account_id, "ref": ref},
            ).scalar_one()
            assert count == 1


def test_list_notifications_unread_only_filter(
    notification_context: _NotificationContext,
) -> None:
    ctx = notification_context
    incident = _create_incident(ctx.owner.client, ctx.owner.token)
    grant_response = ctx.owner.client.post(
        "/api/v1/sharing/grants",
        json={
            "resource_type": "incidents",
            "resource_id": incident["id"],
            "grantee_account_id": str(ctx.member.account_id),
            "actions": ["read"],
            "narrow_visibility": True,
        },
        headers=_headers(ctx.owner.token, key=str(uuid4())),
    )
    assert grant_response.status_code == 201, grant_response.text
    notification_id = _notifications_for(ctx.member.client, ctx.member.token)[0]["id"]

    unread_before = _notifications_for(ctx.member.client, ctx.member.token, unread_only="true")
    assert any(n["id"] == notification_id for n in unread_before)

    mark_read = ctx.member.client.post(
        f"/api/v1/notifications/{notification_id}/read",
        headers=_headers(ctx.member.token),
    )
    assert mark_read.status_code == 200, mark_read.text
    assert mark_read.json()["read_at"] is not None

    unread_after = _notifications_for(ctx.member.client, ctx.member.token, unread_only="true")
    assert all(n["id"] != notification_id for n in unread_after)


def test_mark_notification_read_is_idempotent(
    notification_context: _NotificationContext,
) -> None:
    ctx = notification_context
    incident = _create_incident(ctx.owner.client, ctx.owner.token)
    grant_response = ctx.owner.client.post(
        "/api/v1/sharing/grants",
        json={
            "resource_type": "incidents",
            "resource_id": incident["id"],
            "grantee_account_id": str(ctx.member.account_id),
            "actions": ["read"],
            "narrow_visibility": True,
        },
        headers=_headers(ctx.owner.token, key=str(uuid4())),
    )
    assert grant_response.status_code == 201, grant_response.text
    notification_id = _notifications_for(ctx.member.client, ctx.member.token)[0]["id"]

    first = ctx.member.client.post(
        f"/api/v1/notifications/{notification_id}/read", headers=_headers(ctx.member.token)
    )
    assert first.status_code == 200, first.text
    second = ctx.member.client.post(
        f"/api/v1/notifications/{notification_id}/read", headers=_headers(ctx.member.token)
    )
    assert second.status_code == 200, second.text
    assert first.json()["read_at"] == second.json()["read_at"]


def test_mark_notification_read_404s_for_a_different_account(
    notification_context: _NotificationContext,
) -> None:
    ctx = notification_context
    incident = _create_incident(ctx.owner.client, ctx.owner.token)
    grant_response = ctx.owner.client.post(
        "/api/v1/sharing/grants",
        json={
            "resource_type": "incidents",
            "resource_id": incident["id"],
            "grantee_account_id": str(ctx.member.account_id),
            "actions": ["read"],
            "narrow_visibility": True,
        },
        headers=_headers(ctx.owner.token, key=str(uuid4())),
    )
    assert grant_response.status_code == 201, grant_response.text
    notification_id = _notifications_for(ctx.member.client, ctx.member.token)[0]["id"]

    denied = ctx.other_member.client.post(
        f"/api/v1/notifications/{notification_id}/read", headers=_headers(ctx.other_member.token)
    )
    assert denied.status_code == 404, denied.text
    assert denied.json()["detail"] == "NOTIFICATION_NOT_FOUND"


# ---------------------------------------------------------------------------
# GET /shared/activity
# ---------------------------------------------------------------------------


def test_shared_activity_shows_workspace_visible_resource_to_any_member(
    notification_context: _NotificationContext,
) -> None:
    ctx = notification_context
    incident = _create_incident(ctx.owner.client, ctx.owner.token)

    feed = ctx.other_member.client.get(
        "/api/v1/shared/activity", headers=_headers(ctx.other_member.token)
    )
    assert feed.status_code == 200, feed.text
    matches = [
        item
        for item in feed.json()["items"]
        if item["aggregate_type"] == "incident" and item["aggregate_id"] == incident["id"]
    ]
    assert len(matches) == 1
    assert matches[0]["event_type"] == "incident.created"


def test_shared_activity_redacts_private_resource_from_other_members(
    notification_context: _NotificationContext,
) -> None:
    ctx = notification_context
    domain_response = ctx.owner.client.post(
        "/api/v1/personal/domains",
        json={"domain_key": "habits"},
        headers=_headers(ctx.owner.token, key=str(uuid4())),
    )
    assert domain_response.status_code == 201, domain_response.text
    domain_id = domain_response.json()["id"]

    # The owner's own feed includes it.
    owner_feed = ctx.owner.client.get("/api/v1/shared/activity", headers=_headers(ctx.owner.token))
    assert owner_feed.status_code == 200, owner_feed.text
    assert any(item["aggregate_id"] == domain_id for item in owner_feed.json()["items"])

    # No other member ever sees it, in any form -- it never appears at all,
    # not merely with fields stripped.
    other_feed = ctx.other_member.client.get(
        "/api/v1/shared/activity", headers=_headers(ctx.other_member.token)
    )
    assert other_feed.status_code == 200, other_feed.text
    assert all(item["aggregate_id"] != domain_id for item in other_feed.json()["items"])


def test_shared_activity_excludes_unmapped_aggregate_types(
    notification_context: _NotificationContext,
) -> None:
    """`invitation` audit events exist (`ecc.domains.identity.invitations`)
    but are not in `_AGGREGATE_TYPE_TO_RESOURCE_TYPE` -- this disclosed
    scope boundary means they are excluded from the feed entirely (fails
    closed), not shown unfiltered.
    """
    ctx = notification_context
    invite_response = ctx.owner.client.post(
        f"/api/v1/identity/workspaces/{ctx.workspace_id}/invitations",
        json={"email": "invitee@example.test", "role": "member"},
        headers=_headers(ctx.owner.token, key=str(uuid4())),
    )
    assert invite_response.status_code == 201, invite_response.text
    invitation_id = invite_response.json()["id"]

    feed = ctx.owner.client.get("/api/v1/shared/activity", headers=_headers(ctx.owner.token))
    assert feed.status_code == 200, feed.text
    assert all(item["aggregate_id"] != invitation_id for item in feed.json()["items"])
