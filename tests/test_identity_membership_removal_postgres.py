"""Phase 8 Task 8: `ecc.domains.identity.membership_removal` and
`ecc.platform.authz`'s `POST|GET /ownership/transfers`
(`docs/superpowers/plans/2026-08-01-phase-8-multi-user.md` Task 8). Per
this task's own stated test requirements:

1. Removal blocked while unresolved sole-ownership exists
   (`test_remove_member_blocked_by_owned_resources_then_succeeds_after_
   transfer`).
2. Removal proceeds after transfer/export (same test -- the transfer
   unblocks it, and the response's own `export` field is asserted).
3. Active delegations correctly resolved on removal
   (`test_remove_member_cancels_active_delegations_and_revokes_evidence_
   grants` -- both a still-`proposed` delegation naming the removed member
   as delegator and an `accepted` one naming them as recipient).
4. Historical `owner_id`/`created_by` attribution survives removal
   unchanged (same test -- the removed member's `users` row is asserted to
   still exist after removal, per Decision 1's "never delete the anchor").

Plus the two structural invariants this task's own module docstring
discloses (last-active-owner cannot be demoted/removed), member-listing
visibility, self-removal, session revocation (`PERMISSION-CONTRACT.md`'s
"second, independent propagation path"), and `POST|GET /ownership/
transfers`'s own authorization/validation surface.
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
        for table in (
            "member_notifications",
            "ownership_transfers",
            "delegations",
            "resource_grants",
            "incidents",
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
class _MembershipContext:
    workspace_id: UUID
    owner: _Actor
    admin: _Actor
    member_a: _Actor
    member_b: _Actor
    viewer: _Actor


@pytest.fixture
def membership_context() -> Iterator[_MembershipContext]:
    workspace_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Membership Removal Test', 'UTC', :now)"
            ),
            {"id": workspace_id, "now": now},
        )
        owner = _make_actor(connection, workspace_id=workspace_id, role="owner")
        admin = _make_actor(connection, workspace_id=workspace_id, role="admin")
        member_a = _make_actor(connection, workspace_id=workspace_id, role="member")
        member_b = _make_actor(connection, workspace_id=workspace_id, role="member")
        viewer = _make_actor(connection, workspace_id=workspace_id, role="viewer")

    context = _MembershipContext(
        workspace_id=workspace_id,
        owner=owner,
        admin=admin,
        member_a=member_a,
        member_b=member_b,
        viewer=viewer,
    )
    try:
        yield context
    finally:
        for actor in (owner, admin, member_a, member_b, viewer):
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
) -> dict[str, Any]:
    response = client.post(
        "/api/v1/delegations",
        json={
            "recipient_account_id": str(recipient_account_id),
            "obligation_type": "incidents",
            "obligation_resource_id": obligation_resource_id,
            "expected_outcome": "Resolve the incident",
            "due_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
            "evidence": [],
        },
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# GET|PATCH|DELETE /identity/workspaces/{id}/members(/{user_id})
# ---------------------------------------------------------------------------


def test_list_members_visible_to_any_active_role(membership_context: _MembershipContext) -> None:
    ctx = membership_context
    response = ctx.viewer.client.get(
        f"/api/v1/identity/workspaces/{ctx.workspace_id}/members",
        headers=_headers(ctx.viewer.token),
    )
    assert response.status_code == 200, response.text
    user_ids = {m["user_id"] for m in response.json()["members"]}
    assert user_ids == {
        str(ctx.owner.user_id),
        str(ctx.admin.user_id),
        str(ctx.member_a.user_id),
        str(ctx.member_b.user_id),
        str(ctx.viewer.user_id),
    }


def test_patch_member_role_updates_role(membership_context: _MembershipContext) -> None:
    ctx = membership_context
    response = ctx.owner.client.patch(
        f"/api/v1/identity/workspaces/{ctx.workspace_id}/members/{ctx.member_a.user_id}",
        json={"role": "admin"},
        headers=_headers(ctx.owner.token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "admin"

    with engine.begin() as connection:
        role = connection.execute(
            text("SELECT role FROM workspace_memberships WHERE users_id = :id"),
            {"id": ctx.member_a.user_id},
        ).scalar_one()
    assert role == "admin"


def test_patch_role_blocked_for_last_active_owner(membership_context: _MembershipContext) -> None:
    ctx = membership_context
    response = ctx.owner.client.patch(
        f"/api/v1/identity/workspaces/{ctx.workspace_id}/members/{ctx.owner.user_id}",
        json={"role": "admin"},
        headers=_headers(ctx.owner.token),
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "LAST_OWNER_CANNOT_BE_DEMOTED"


def test_admin_cannot_promote_a_member_to_owner(membership_context: _MembershipContext) -> None:
    """Found untested by the second whole-phase review, despite the guard
    itself being fixed during the first: an `admin` promoting someone to
    `owner` unilaterally is the exact privilege-escalation gap that round
    closed."""
    ctx = membership_context
    response = ctx.admin.client.patch(
        f"/api/v1/identity/workspaces/{ctx.workspace_id}/members/{ctx.member_a.user_id}",
        json={"role": "owner"},
        headers=_headers(ctx.admin.token),
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "INSUFFICIENT_ROLE"


def test_owner_can_promote_a_member_to_owner(membership_context: _MembershipContext) -> None:
    ctx = membership_context
    response = ctx.owner.client.patch(
        f"/api/v1/identity/workspaces/{ctx.workspace_id}/members/{ctx.member_a.user_id}",
        json={"role": "owner"},
        headers=_headers(ctx.owner.token),
    )
    assert response.status_code == 200, response.text
    assert response.json()["role"] == "owner"


def test_admin_cannot_demote_an_owner(membership_context: _MembershipContext) -> None:
    """Second whole-phase review: the promotion guard above only stopped an
    `admin` from *promoting* someone to `owner` -- nothing stopped them
    from *demoting* an existing owner instead, the same trust boundary
    applied asymmetrically. This must 403 before ever reaching the
    separate `LAST_OWNER_CANNOT_BE_DEMOTED` 409 check (`ctx.owner` is the
    workspace's only owner here, so that check would otherwise fire
    first -- this proves the role guard is checked first)."""
    ctx = membership_context
    response = ctx.admin.client.patch(
        f"/api/v1/identity/workspaces/{ctx.workspace_id}/members/{ctx.owner.user_id}",
        json={"role": "member"},
        headers=_headers(ctx.admin.token),
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "INSUFFICIENT_ROLE"


def test_remove_member_blocked_by_owned_resources_then_succeeds_after_transfer(
    membership_context: _MembershipContext,
) -> None:
    ctx = membership_context
    incident = _create_incident(ctx.member_a.client, ctx.member_a.token, title="Owned by A")

    blocked = ctx.owner.client.delete(
        f"/api/v1/identity/workspaces/{ctx.workspace_id}/members/{ctx.member_a.user_id}",
        headers=_headers(ctx.owner.token),
    )
    assert blocked.status_code == 409, blocked.text
    error = blocked.json()["error"]
    assert error["code"] == "OWNED_RESOURCES_BLOCK_REMOVAL"
    assert {"resource_type": "incidents", "count": 1} in error["details"]["owned_resources"]

    transfer = ctx.owner.client.post(
        "/api/v1/ownership/transfers",
        json={
            "resource_type": "incidents",
            "resource_id": incident["id"],
            "to_account_id": str(ctx.member_b.account_id),
        },
        headers=_headers(ctx.owner.token, key=str(uuid4())),
    )
    assert transfer.status_code == 201, transfer.text
    assert transfer.json()["from_account_id"] == str(ctx.member_a.account_id)
    assert transfer.json()["to_account_id"] == str(ctx.member_b.account_id)
    assert transfer.json()["status"] == "completed"

    removal = ctx.owner.client.delete(
        f"/api/v1/identity/workspaces/{ctx.workspace_id}/members/{ctx.member_a.user_id}",
        headers=_headers(ctx.owner.token),
    )
    assert removal.status_code == 200, removal.text
    export = removal.json()["export"]
    assert export["account_id"] == str(ctx.member_a.account_id)
    assert export["removed_at"] is not None

    with engine.begin() as connection:
        status_row = (
            connection.execute(
                text("SELECT status, removed_at FROM workspace_memberships WHERE users_id = :id"),
                {"id": ctx.member_a.user_id},
            )
            .mappings()
            .one()
        )
        assert status_row["status"] == "removed"
        assert status_row["removed_at"] is not None

        # Historical attribution survives -- the `users` FK anchor is never
        # deleted (Decision 1), and the transferred incident's new owner_id
        # correctly points at member_b's own users_id.
        still_a_user = connection.execute(
            text("SELECT 1 FROM users WHERE id = :id"), {"id": ctx.member_a.user_id}
        ).scalar_one_or_none()
        assert still_a_user == 1

        new_owner = connection.execute(
            text("SELECT owner_id FROM incidents WHERE id = :id"), {"id": UUID(incident["id"])}
        ).scalar_one()
    assert new_owner == ctx.member_b.user_id


def test_remove_member_blocked_for_last_active_owner(
    membership_context: _MembershipContext,
) -> None:
    ctx = membership_context
    response = ctx.owner.client.delete(
        f"/api/v1/identity/workspaces/{ctx.workspace_id}/members/{ctx.owner.user_id}",
        headers=_headers(ctx.owner.token),
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "LAST_OWNER_CANNOT_BE_REMOVED"


def test_admin_cannot_remove_an_owner(membership_context: _MembershipContext) -> None:
    """Symmetric with `test_admin_cannot_demote_an_owner` -- an admin
    removing an owner is just as much an unapproved strip of that owner's
    authority as demoting them. Must 403 before the separate
    `LAST_OWNER_CANNOT_BE_REMOVED` 409 check (same single-owner fixture)."""
    ctx = membership_context
    response = ctx.admin.client.delete(
        f"/api/v1/identity/workspaces/{ctx.workspace_id}/members/{ctx.owner.user_id}",
        headers=_headers(ctx.admin.token),
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "INSUFFICIENT_ROLE"


def test_remove_member_self_removal_allowed(membership_context: _MembershipContext) -> None:
    ctx = membership_context
    response = ctx.member_b.client.delete(
        f"/api/v1/identity/workspaces/{ctx.workspace_id}/members/{ctx.member_b.user_id}",
        headers=_headers(ctx.member_b.token),
    )
    assert response.status_code == 200, response.text


def test_remove_member_forbidden_for_non_owner_admin_non_self(
    membership_context: _MembershipContext,
) -> None:
    ctx = membership_context
    response = ctx.member_a.client.delete(
        f"/api/v1/identity/workspaces/{ctx.workspace_id}/members/{ctx.member_b.user_id}",
        headers=_headers(ctx.member_a.token),
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "INSUFFICIENT_ROLE"


def test_remove_member_cancels_active_delegations_and_revokes_evidence_grants(
    membership_context: _MembershipContext,
) -> None:
    ctx = membership_context
    incident_1 = _create_incident(ctx.owner.client, ctx.owner.token, title="Obligation 1")
    incident_2 = _create_incident(ctx.owner.client, ctx.owner.token, title="Obligation 2")

    # Still `proposed` -- member_a is the delegator.
    delegation_1 = _propose_delegation(
        ctx.member_a.client,
        ctx.member_a.token,
        recipient_account_id=ctx.member_b.account_id,
        obligation_resource_id=incident_1["id"],
    )

    # `accepted` -- member_a is the recipient, so accepting created a
    # resource_grants row naming member_a as grantee.
    delegation_2 = _propose_delegation(
        ctx.member_b.client,
        ctx.member_b.token,
        recipient_account_id=ctx.member_a.account_id,
        obligation_resource_id=incident_2["id"],
    )
    accept = ctx.member_a.client.post(
        f"/api/v1/delegations/{delegation_2['id']}/accept",
        headers=_headers(ctx.member_a.token, key=str(uuid4())),
    )
    assert accept.status_code == 200, accept.text

    removal = ctx.owner.client.delete(
        f"/api/v1/identity/workspaces/{ctx.workspace_id}/members/{ctx.member_a.user_id}",
        headers=_headers(ctx.owner.token),
    )
    assert removal.status_code == 200, removal.text

    with engine.begin() as connection:
        status_1 = connection.execute(
            text("SELECT status FROM delegations WHERE id = :id"),
            {"id": UUID(delegation_1["id"])},
        ).scalar_one()
        status_2 = connection.execute(
            text("SELECT status FROM delegations WHERE id = :id"),
            {"id": UUID(delegation_2["id"])},
        ).scalar_one()
        grant_revoked_at = connection.execute(
            text(
                "SELECT revoked_at FROM resource_grants "
                "WHERE grantee_account_id = :account_id AND resource_id = :resource_id"
            ),
            {"account_id": ctx.member_a.account_id, "resource_id": UUID(incident_2["id"])},
        ).scalar_one()
        event_types_1 = set(
            connection.execute(
                text(
                    "SELECT event_type FROM delegation_events "
                    "WHERE delegation_id = :id AND actor_account_id IS NULL"
                ),
                {"id": UUID(delegation_1["id"])},
            )
            .scalars()
            .all()
        )
    assert status_1 == "cancelled"
    assert status_2 == "cancelled"
    assert grant_revoked_at is not None
    assert "cancelled" in event_types_1


def test_remove_member_revokes_sessions(membership_context: _MembershipContext) -> None:
    ctx = membership_context
    removal = ctx.owner.client.delete(
        f"/api/v1/identity/workspaces/{ctx.workspace_id}/members/{ctx.member_a.user_id}",
        headers=_headers(ctx.owner.token),
    )
    assert removal.status_code == 200, removal.text

    with engine.begin() as connection:
        revoked_at = connection.execute(
            text(
                "SELECT revoked_at FROM sessions "
                "WHERE workspace_id = :workspace_id AND user_id = :user_id"
            ),
            {"workspace_id": ctx.workspace_id, "user_id": ctx.member_a.user_id},
        ).scalar_one()
    assert revoked_at is not None

    # The already-issued cookie no longer authenticates anything.
    denied = ctx.member_a.client.get(
        f"/api/v1/identity/workspaces/{ctx.workspace_id}/members",
        headers=_headers(ctx.member_a.token),
    )
    assert denied.status_code == 401, denied.text


# ---------------------------------------------------------------------------
# POST|GET /ownership/transfers
# ---------------------------------------------------------------------------


def test_ownership_transfer_requires_owner_admin_or_resource_owner(
    membership_context: _MembershipContext,
) -> None:
    ctx = membership_context
    incident = _create_incident(ctx.owner.client, ctx.owner.token)
    response = ctx.member_a.client.post(
        "/api/v1/ownership/transfers",
        json={
            "resource_type": "incidents",
            "resource_id": incident["id"],
            "to_account_id": str(ctx.member_b.account_id),
        },
        headers=_headers(ctx.member_a.token, key=str(uuid4())),
    )
    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "INSUFFICIENT_ROLE"


def test_ownership_transfer_rejects_ungrantable_resource_type(
    membership_context: _MembershipContext,
) -> None:
    ctx = membership_context
    response = ctx.owner.client.post(
        "/api/v1/ownership/transfers",
        json={
            "resource_type": "personal_domains",
            "resource_id": str(uuid4()),
            "to_account_id": str(ctx.member_b.account_id),
        },
        headers=_headers(ctx.owner.token, key=str(uuid4())),
    )
    assert response.status_code == 400, response.text
    assert response.json()["error"]["code"] == "RESOURCE_TYPE_NOT_GRANTABLE"


def test_ownership_transfer_requires_active_recipient_membership(
    membership_context: _MembershipContext,
) -> None:
    ctx = membership_context
    incident = _create_incident(ctx.owner.client, ctx.owner.token)
    response = ctx.owner.client.post(
        "/api/v1/ownership/transfers",
        json={
            "resource_type": "incidents",
            "resource_id": incident["id"],
            "to_account_id": str(uuid4()),
        },
        headers=_headers(ctx.owner.token, key=str(uuid4())),
    )
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "RECIPIENT_NOT_FOUND"


def test_list_ownership_transfers_role_scoped(membership_context: _MembershipContext) -> None:
    ctx = membership_context
    incident = _create_incident(ctx.owner.client, ctx.owner.token)
    transfer = ctx.owner.client.post(
        "/api/v1/ownership/transfers",
        json={
            "resource_type": "incidents",
            "resource_id": incident["id"],
            "to_account_id": str(ctx.member_b.account_id),
        },
        headers=_headers(ctx.owner.token, key=str(uuid4())),
    )
    assert transfer.status_code == 201, transfer.text
    transfer_id = transfer.json()["id"]

    owner_view = ctx.owner.client.get(
        "/api/v1/ownership/transfers", headers=_headers(ctx.owner.token)
    )
    assert transfer_id in {t["id"] for t in owner_view.json()["transfers"]}

    recipient_view = ctx.member_b.client.get(
        "/api/v1/ownership/transfers", headers=_headers(ctx.member_b.token)
    )
    assert transfer_id in {t["id"] for t in recipient_view.json()["transfers"]}

    uninvolved_view = ctx.member_a.client.get(
        "/api/v1/ownership/transfers", headers=_headers(ctx.member_a.token)
    )
    assert transfer_id not in {t["id"] for t in uninvolved_view.json()["transfers"]}
