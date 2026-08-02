"""Phase 8 Task 2: invitations (`ecc.domains.identity.invitations`, and the
`POST /identity/accounts` changes in `ecc.domains.identity.accounts` that
wire real token validation into account creation).

Covers: invitation create (role-gated, rejects an already-active member,
rejects a second concurrently-pending invite for the same recipient),
list (role-gated), the brand-new-recipient path (`POST /accounts?token=...`
registers + auto-accepts + logs in, in one call), the existing-account path
(`POST /invitations/{id}/accept` from a session established in a different
workspace), reject, revoke, expired/invalid/mismatched-email token
rejections at every checkpoint, a concurrent-accept race (only one of two
simultaneous accepts may succeed), and cross-workspace isolation.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from identity_fixtures import create_identity
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import engine
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


def _cleanup_workspace(workspace_id: UUID) -> None:
    with engine.begin() as connection:
        # `invitations.invited_by` is ON DELETE RESTRICT against `users` --
        # must be deleted before `users`, not after.
        for table in (
            "workspace_memberships",
            "sessions",
            "event_outbox",
            "audit_events",
            "invitations",
            "users",
        ):
            connection.execute(
                text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                {"workspace_id": workspace_id},
            )
        connection.execute(
            text("DELETE FROM workspaces WHERE id = :workspace_id"), {"workspace_id": workspace_id}
        )


def _cleanup_account(account_id: UUID) -> None:
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM accounts WHERE id = :id"), {"id": account_id})


def _cleanup_new_member(account_id: UUID) -> None:
    """Delete the workspace-scoped `sessions`/`audit_events`/`workspace_
    memberships`/`users` rows a `POST /accounts?token=...` auto-join
    created for `account_id`, in FK-safe order, before deleting the
    account itself. Narrower than `_cleanup_workspace` (which the
    surrounding `owner_context` fixture already runs for the *whole*
    workspace, later, in its own `finally`) -- calling `_cleanup_account`
    directly without this first would violate `fk_users_account`'s
    RESTRICT, since the new member's own `users` row is still alive at
    that point.
    """
    with engine.begin() as connection:
        connection.execute(
            text(
                "DELETE FROM sessions WHERE user_id IN "
                "(SELECT id FROM users WHERE account_id = :id)"
            ),
            {"id": account_id},
        )
        connection.execute(
            text(
                "DELETE FROM audit_events WHERE actor_id IN "
                "(SELECT id FROM users WHERE account_id = :id)"
            ),
            {"id": account_id},
        )
        connection.execute(
            text("DELETE FROM workspace_memberships WHERE account_id = :id"), {"id": account_id}
        )
        connection.execute(text("DELETE FROM users WHERE account_id = :id"), {"id": account_id})
    _cleanup_account(account_id)


def _headers(token: str) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    return {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}


def _new_session(workspace_id: UUID, users_id: UUID, now: datetime) -> str:
    token = f"session-{uuid4()}"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                "expires_at, last_seen_at) "
                "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :now)"
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "user_id": users_id,
                "token_hash": sha256(token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "now": now,
            },
        )
    return token


def _make_workspace(name: str, now: datetime) -> UUID:
    workspace_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, created_at, timezone) "
                "VALUES (:id, :name, :now, 'UTC')"
            ),
            {"id": workspace_id, "name": name, "now": now},
        )
    return workspace_id


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


@pytest.fixture
def owner_context() -> Iterator[tuple[TestClient, UUID, UUID, UUID, str]]:
    """An owner-role identity in a fresh workspace, session already set on
    the returned client -- the inviter for most tests below.
    """
    now = datetime.now(UTC)
    workspace_id = _make_workspace("Inviter WS", now)
    users_id = uuid4()
    with engine.begin() as connection:
        account_id = create_identity(
            connection, workspace_id=workspace_id, user_id=users_id, now=now, role="owner"
        )
    token = _new_session(workspace_id, users_id, now)
    with TestClient(app) as c:
        c.cookies.set("ecc_session", token)
        try:
            yield c, workspace_id, users_id, account_id, token
        finally:
            _cleanup_workspace(workspace_id)
            _cleanup_account(account_id)


def test_create_invitation_requires_owner_or_admin(
    owner_context: tuple[TestClient, UUID, UUID, UUID, str],
) -> None:
    client, workspace_id, _users_id, _account_id, owner_token = owner_context
    now = datetime.now(UTC)
    viewer_users_id = uuid4()
    # A genuinely separate identity, not a second membership for the
    # owner's own account -- one account can only hold one `users` row per
    # workspace (`uq_users_workspace_account`), so `add_membership` (which
    # attaches an *existing* account to a *different* workspace) is the
    # wrong helper for "a second person in this same workspace."
    with engine.begin() as connection:
        create_identity(
            connection, workspace_id=workspace_id, user_id=viewer_users_id, now=now, role="viewer"
        )
    viewer_token = _new_session(workspace_id, viewer_users_id, now)
    with TestClient(app) as viewer_client:
        viewer_client.cookies.set("ecc_session", viewer_token)
        resp = viewer_client.post(
            f"/api/v1/identity/workspaces/{workspace_id}/invitations",
            json={"email": f"{uuid4()}@example.test", "role": "member"},
            headers=_headers(viewer_token),
        )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "INSUFFICIENT_ROLE"


def test_create_invitation_rejects_already_active_member(
    owner_context: tuple[TestClient, UUID, UUID, UUID, str],
) -> None:
    client, workspace_id, _users_id, account_id, owner_token = owner_context
    resp = client.get("/api/v1/identity/workspaces", headers=_headers(owner_token))
    assert resp.status_code == 200, resp.text
    # Inviting the owner's own email (already an active member of this workspace).
    with engine.begin() as connection:
        row = (
            connection.execute(
                text("SELECT email FROM accounts WHERE id = :id"), {"id": account_id}
            )
            .mappings()
            .one()
        )
    invite = client.post(
        f"/api/v1/identity/workspaces/{workspace_id}/invitations",
        json={"email": row["email"], "role": "member"},
        headers=_headers(owner_token),
    )
    assert invite.status_code == 409, invite.text
    assert invite.json()["error"]["code"] == "ALREADY_MEMBER"


def test_create_invitation_rejects_second_concurrently_pending(
    owner_context: tuple[TestClient, UUID, UUID, UUID, str],
) -> None:
    client, workspace_id, _users_id, _account_id, owner_token = owner_context
    email = f"{uuid4()}@example.test"
    first = client.post(
        f"/api/v1/identity/workspaces/{workspace_id}/invitations",
        json={"email": email, "role": "member"},
        headers=_headers(owner_token),
    )
    assert first.status_code == 201, first.text
    second = client.post(
        f"/api/v1/identity/workspaces/{workspace_id}/invitations",
        json={"email": email, "role": "viewer"},
        headers=_headers(owner_token),
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "INVITATION_ALREADY_PENDING"


def test_list_invitations_requires_owner_or_admin(
    owner_context: tuple[TestClient, UUID, UUID, UUID, str],
) -> None:
    client, workspace_id, _users_id, _account_id, owner_token = owner_context
    now = datetime.now(UTC)
    member_users_id = uuid4()
    # See test_create_invitation_requires_owner_or_admin's own comment --
    # a separate identity, not a second membership for the owner's account.
    with engine.begin() as connection:
        create_identity(
            connection, workspace_id=workspace_id, user_id=member_users_id, now=now, role="member"
        )
    member_token = _new_session(workspace_id, member_users_id, now)
    with TestClient(app) as member_client:
        member_client.cookies.set("ecc_session", member_token)
        resp = member_client.get(
            f"/api/v1/identity/workspaces/{workspace_id}/invitations",
            headers=_headers(member_token),
        )
    assert resp.status_code == 403, resp.text


def test_new_recipient_registers_and_auto_joins_via_token(
    owner_context: tuple[TestClient, UUID, UUID, UUID, str],
) -> None:
    client, workspace_id, _users_id, _account_id, owner_token = owner_context
    email = f"{uuid4()}@example.test"
    invite = client.post(
        f"/api/v1/identity/workspaces/{workspace_id}/invitations",
        json={"email": email, "role": "member"},
        headers=_headers(owner_token),
    )
    assert invite.status_code == 201, invite.text
    token = invite.json()["token"]

    with TestClient(app) as new_client:
        resp = new_client.post(
            f"/api/v1/identity/accounts?token={token}",
            json={"email": email, "password": "a brand new password", "display_name": "New Hire"},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["workspace_id"] == str(workspace_id)
        assert body["role"] == "member"
        assert "ecc_session" in new_client.cookies

        workspaces = new_client.get(
            "/api/v1/identity/workspaces", headers=_headers(new_client.cookies["ecc_session"])
        )
        assert workspaces.status_code == 200, workspaces.text
        assert str(workspace_id) in {w["id"] for w in workspaces.json()["workspaces"]}

    _cleanup_new_member(UUID(body["id"]))


def test_account_creation_rejects_invalid_token(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/identity/accounts?token=not-a-real-token",
        json={
            "email": f"{uuid4()}@example.test",
            "password": "a real password!",
            "display_name": "X",
        },
    )
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "INVITATION_TOKEN_INVALID"


def test_account_creation_rejects_expired_invitation(
    owner_context: tuple[TestClient, UUID, UUID, UUID, str],
) -> None:
    client, workspace_id, _users_id, _account_id, owner_token = owner_context
    email = f"{uuid4()}@example.test"
    invite = client.post(
        f"/api/v1/identity/workspaces/{workspace_id}/invitations",
        json={"email": email, "role": "member"},
        headers=_headers(owner_token),
    )
    token = invite.json()["token"]
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE invitations SET expires_at = :past WHERE id = :id"),
            {"past": datetime.now(UTC) - timedelta(seconds=1), "id": invite.json()["id"]},
        )
    resp = client.post(
        f"/api/v1/identity/accounts?token={token}",
        json={"email": email, "password": "a real password!", "display_name": "X"},
    )
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "INVITATION_EXPIRED"


def test_account_creation_rejects_email_mismatch(
    owner_context: tuple[TestClient, UUID, UUID, UUID, str],
) -> None:
    client, workspace_id, _users_id, _account_id, owner_token = owner_context
    invite = client.post(
        f"/api/v1/identity/workspaces/{workspace_id}/invitations",
        json={"email": f"{uuid4()}@example.test", "role": "member"},
        headers=_headers(owner_token),
    )
    token = invite.json()["token"]
    resp = client.post(
        f"/api/v1/identity/accounts?token={token}",
        json={
            "email": f"{uuid4()}@example.test",
            "password": "a real password!",
            "display_name": "X",
        },
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "INVITATION_EMAIL_MISMATCH"


def test_existing_account_accepts_invitation_to_a_second_workspace(
    owner_context: tuple[TestClient, UUID, UUID, UUID, str],
) -> None:
    client, workspace_id, _users_id, _account_id, owner_token = owner_context
    # An existing account with a session in some *other* workspace.
    now = datetime.now(UTC)
    other_workspace_id = _make_workspace("Existing Account's WS", now)
    other_users_id = uuid4()
    with engine.begin() as connection:
        existing_account_id = create_identity(
            connection, workspace_id=other_workspace_id, user_id=other_users_id, now=now
        )
        existing_email = (
            connection.execute(
                text("SELECT email FROM accounts WHERE id = :id"), {"id": existing_account_id}
            )
            .mappings()
            .one()["email"]
        )
    existing_token = _new_session(other_workspace_id, other_users_id, now)

    invite = client.post(
        f"/api/v1/identity/workspaces/{workspace_id}/invitations",
        json={"email": existing_email, "role": "admin"},
        headers=_headers(owner_token),
    )
    assert invite.status_code == 201, invite.text
    invite_token = invite.json()["token"]
    invite_id = invite.json()["id"]

    try:
        with TestClient(app) as existing_client:
            existing_client.cookies.set("ecc_session", existing_token)
            accept = existing_client.post(
                f"/api/v1/identity/invitations/{invite_id}/accept",
                json={"token": invite_token},
                headers=_headers(existing_token),
            )
        assert accept.status_code == 200, accept.text
        assert accept.json()["status"] == "accepted"
        assert accept.json()["workspace_id"] == str(workspace_id)
        assert accept.json()["role"] == "admin"
    finally:
        _cleanup_workspace(other_workspace_id)
        _cleanup_account(existing_account_id)


def test_accept_invitation_rejects_wrong_token(
    owner_context: tuple[TestClient, UUID, UUID, UUID, str],
) -> None:
    client, workspace_id, _users_id, _account_id, owner_token = owner_context
    invite = client.post(
        f"/api/v1/identity/workspaces/{workspace_id}/invitations",
        json={"email": f"{uuid4()}@example.test", "role": "member"},
        headers=_headers(owner_token),
    )
    invite_id = invite.json()["id"]
    resp = client.post(
        f"/api/v1/identity/invitations/{invite_id}/accept",
        json={"token": "definitely-the-wrong-token"},
        headers=_headers(owner_token),
    )
    assert resp.status_code == 401, resp.text
    assert resp.json()["error"]["code"] == "INVITATION_TOKEN_INVALID"


def test_reject_invitation_then_accept_fails(
    owner_context: tuple[TestClient, UUID, UUID, UUID, str],
) -> None:
    client, workspace_id, _users_id, _account_id, owner_token = owner_context
    now = datetime.now(UTC)
    other_workspace_id = _make_workspace("Rejector WS", now)
    rejector_users_id = uuid4()
    with engine.begin() as connection:
        rejector_account_id = create_identity(
            connection, workspace_id=other_workspace_id, user_id=rejector_users_id, now=now
        )
        rejector_email = (
            connection.execute(
                text("SELECT email FROM accounts WHERE id = :id"), {"id": rejector_account_id}
            )
            .mappings()
            .one()["email"]
        )
    rejector_token = _new_session(other_workspace_id, rejector_users_id, now)

    invite = client.post(
        f"/api/v1/identity/workspaces/{workspace_id}/invitations",
        json={"email": rejector_email, "role": "member"},
        headers=_headers(owner_token),
    )
    invite_id = invite.json()["id"]
    invite_token = invite.json()["token"]

    try:
        with TestClient(app) as rejector_client:
            rejector_client.cookies.set("ecc_session", rejector_token)
            reject = rejector_client.post(
                f"/api/v1/identity/invitations/{invite_id}/reject",
                json={"token": invite_token},
                headers=_headers(rejector_token),
            )
            assert reject.status_code == 200, reject.text
            assert reject.json()["status"] == "rejected"

            retry = rejector_client.post(
                f"/api/v1/identity/invitations/{invite_id}/accept",
                json={"token": invite_token},
                headers=_headers(rejector_token),
            )
        assert retry.status_code == 409, retry.text
        assert retry.json()["error"]["code"] == "INVITATION_ALREADY_RESOLVED"
    finally:
        _cleanup_workspace(other_workspace_id)
        _cleanup_account(rejector_account_id)


def test_revoke_invitation_by_admin_prevents_later_acceptance(
    owner_context: tuple[TestClient, UUID, UUID, UUID, str],
) -> None:
    client, workspace_id, _users_id, _account_id, owner_token = owner_context
    invite = client.post(
        f"/api/v1/identity/workspaces/{workspace_id}/invitations",
        json={"email": f"{uuid4()}@example.test", "role": "member"},
        headers=_headers(owner_token),
    )
    invite_id = invite.json()["id"]

    revoke = client.post(
        f"/api/v1/identity/invitations/{invite_id}/revoke", headers=_headers(owner_token)
    )
    assert revoke.status_code == 200, revoke.text
    assert revoke.json()["status"] == "revoked"

    resp = client.post(
        f"/api/v1/identity/accounts?token={invite.json()['token']}",
        json={
            "email": invite.json()["email"],
            "password": "a real password!",
            "display_name": "X",
        },
    )
    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "INVITATION_ALREADY_RESOLVED"


def test_revoke_invitation_requires_owner_or_admin(
    owner_context: tuple[TestClient, UUID, UUID, UUID, str],
) -> None:
    client, workspace_id, _users_id, _account_id, owner_token = owner_context
    invite = client.post(
        f"/api/v1/identity/workspaces/{workspace_id}/invitations",
        json={"email": f"{uuid4()}@example.test", "role": "member"},
        headers=_headers(owner_token),
    )
    invite_id = invite.json()["id"]

    now = datetime.now(UTC)
    viewer_users_id = uuid4()
    # See test_create_invitation_requires_owner_or_admin's own comment --
    # a separate identity, not a second membership for the owner's account.
    with engine.begin() as connection:
        create_identity(
            connection, workspace_id=workspace_id, user_id=viewer_users_id, now=now, role="viewer"
        )
    viewer_token = _new_session(workspace_id, viewer_users_id, now)
    with TestClient(app) as viewer_client:
        viewer_client.cookies.set("ecc_session", viewer_token)
        resp = viewer_client.post(
            f"/api/v1/identity/invitations/{invite_id}/revoke", headers=_headers(viewer_token)
        )
    assert resp.status_code == 403, resp.text


def test_revoke_invitation_cross_workspace_isolation(
    owner_context: tuple[TestClient, UUID, UUID, UUID, str],
) -> None:
    """Caller in workspace A cannot revoke an invitation belonging to
    workspace B, even by guessing a real invitation id.
    """
    client, workspace_id, _users_id, _account_id, owner_token = owner_context
    now = datetime.now(UTC)
    foreign_workspace_id = _make_workspace("Foreign WS", now)
    foreign_users_id = uuid4()
    with engine.begin() as connection:
        foreign_account_id = create_identity(
            connection, workspace_id=foreign_workspace_id, user_id=foreign_users_id, now=now
        )
    foreign_token = _new_session(foreign_workspace_id, foreign_users_id, now)

    invite = client.post(
        f"/api/v1/identity/workspaces/{workspace_id}/invitations",
        json={"email": f"{uuid4()}@example.test", "role": "member"},
        headers=_headers(owner_token),
    )
    invite_id = invite.json()["id"]

    try:
        with TestClient(app) as foreign_client:
            foreign_client.cookies.set("ecc_session", foreign_token)
            resp = foreign_client.post(
                f"/api/v1/identity/invitations/{invite_id}/revoke", headers=_headers(foreign_token)
            )
        assert resp.status_code == 404, resp.text
        assert resp.json()["error"]["code"] == "INVITATION_NOT_FOUND"
    finally:
        _cleanup_workspace(foreign_workspace_id)
        _cleanup_account(foreign_account_id)


def test_concurrent_accept_of_same_token_only_one_succeeds(
    owner_context: tuple[TestClient, UUID, UUID, UUID, str],
) -> None:
    """Both `accept` calls race for the same row-locked invitation; the
    second must see it already resolved, never double-accept.
    """
    client, workspace_id, _users_id, _account_id, owner_token = owner_context
    now = datetime.now(UTC)
    other_workspace_id = _make_workspace("Racer WS", now)
    racer_users_id = uuid4()
    with engine.begin() as connection:
        racer_account_id = create_identity(
            connection, workspace_id=other_workspace_id, user_id=racer_users_id, now=now
        )
        racer_email = (
            connection.execute(
                text("SELECT email FROM accounts WHERE id = :id"), {"id": racer_account_id}
            )
            .mappings()
            .one()["email"]
        )
    racer_token = _new_session(other_workspace_id, racer_users_id, now)

    invite = client.post(
        f"/api/v1/identity/workspaces/{workspace_id}/invitations",
        json={"email": racer_email, "role": "member"},
        headers=_headers(owner_token),
    )
    invite_id = invite.json()["id"]
    invite_token = invite.json()["token"]

    try:
        results = []
        with TestClient(app) as racer_client:
            racer_client.cookies.set("ecc_session", racer_token)
            for _ in range(2):
                results.append(
                    racer_client.post(
                        f"/api/v1/identity/invitations/{invite_id}/accept",
                        json={"token": invite_token},
                        headers=_headers(racer_token),
                    )
                )
        statuses = sorted(r.status_code for r in results)
        assert statuses == [200, 409], [r.text for r in results]
    finally:
        _cleanup_workspace(other_workspace_id)
        _cleanup_account(racer_account_id)
