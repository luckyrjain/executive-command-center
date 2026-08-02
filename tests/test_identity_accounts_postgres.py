"""Phase 8 Task 1: account/membership/session framework
(`ecc.domains.identity.accounts`, `docs/phases/phase-008/API-SCHEMAS.md`).

Covers: `POST /identity/accounts` (create + duplicate-email rejection),
`POST /identity/auth/login` (single-membership auto-login, multi-membership
`pending_login_token` branch, wrong-password/disabled-account/no-active-
membership rejections), `POST /identity/auth/select-workspace` (both the
post-login pending-token path and the already-authenticated switch path,
plus cross-account isolation -- an account can never select a workspace it
has no active membership in, even knowing the real `workspace_id`),
`GET /identity/workspaces`, `POST /identity/workspaces`,
`PATCH /identity/workspaces/{id}` (role-gated).

No `invitations` table exists yet (Task 2) -- `POST /identity/accounts`
is genuine self-registration in this task's own scope, and every
membership below is seeded directly via `identity_fixtures.create_identity`/
`add_membership`, mirroring how every other Phase 7/8 test file seeds its
fixture identity without going through a real signup flow.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from identity_fixtures import add_membership, create_identity
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
        for table in ("workspace_memberships", "sessions", "event_outbox", "audit_events", "users"):
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


def _headers(token: str, key: str | None = None) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    headers = {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


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


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as c:
        yield c


def test_create_account_and_reject_duplicate_email(client: TestClient) -> None:
    email = f"{uuid4()}@example.test"
    resp = client.post(
        "/api/v1/identity/accounts",
        json={"email": email, "password": "correct horse battery", "display_name": "Alex"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["email"] == email
    assert body["display_name"] == "Alex"

    try:
        dup = client.post(
            "/api/v1/identity/accounts",
            json={
                "email": email.upper(),
                "password": "a different password",
                "display_name": "Alex 2",
            },
        )
        assert dup.status_code == 409, dup.text
        assert dup.json()["error"]["code"] == "EMAIL_ALREADY_REGISTERED"
    finally:
        _cleanup_account(UUID(body["id"]))


def test_create_account_rejects_short_password(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/identity/accounts",
        json={"email": f"{uuid4()}@example.test", "password": "short", "display_name": "Alex"},
    )
    assert resp.status_code == 422, resp.text


def test_login_with_single_active_membership_auto_authenticates(client: TestClient) -> None:
    workspace_id, users_id, account_id = uuid4(), uuid4(), None
    now = datetime.now(UTC)
    password = "a genuinely long password"
    email = f"{uuid4()}@example.test"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, created_at, timezone) "
                "VALUES (:id, 'W', :now, 'UTC')"
            ),
            {"id": workspace_id, "now": now},
        )
    resp = client.post(
        "/api/v1/identity/accounts",
        json={"email": email, "password": password, "display_name": "Login Test"},
    )
    assert resp.status_code == 201, resp.text
    account_id = UUID(resp.json()["id"])
    with engine.begin() as connection:
        add_membership(
            connection,
            workspace_id=workspace_id,
            account_id=account_id,
            users_id=users_id,
            role="owner",
            now=now,
        )

    try:
        login = client.post(
            "/api/v1/identity/auth/login", json={"email": email, "password": password}
        )
        assert login.status_code == 200, login.text
        body = login.json()
        assert body["status"] == "authenticated"
        assert body["workspace_id"] == str(workspace_id)
        assert "ecc_session" in client.cookies
    finally:
        _cleanup_workspace(workspace_id)
        _cleanup_account(account_id)


def test_login_wrong_password_is_rejected(client: TestClient) -> None:
    email = f"{uuid4()}@example.test"
    resp = client.post(
        "/api/v1/identity/accounts",
        json={"email": email, "password": "the real password", "display_name": "X"},
    )
    account_id = UUID(resp.json()["id"])
    try:
        login = client.post(
            "/api/v1/identity/auth/login", json={"email": email, "password": "wrong password"}
        )
        assert login.status_code == 401, login.text
        assert login.json()["error"]["code"] == "INVALID_CREDENTIALS"
    finally:
        _cleanup_account(account_id)


def test_login_with_no_active_membership_is_rejected(client: TestClient) -> None:
    email = f"{uuid4()}@example.test"
    password = "another real password"
    resp = client.post(
        "/api/v1/identity/accounts",
        json={"email": email, "password": password, "display_name": "X"},
    )
    account_id = UUID(resp.json()["id"])
    try:
        login = client.post(
            "/api/v1/identity/auth/login", json={"email": email, "password": password}
        )
        assert login.status_code == 403, login.text
        assert login.json()["error"]["code"] == "NO_ACTIVE_MEMBERSHIP"
    finally:
        _cleanup_account(account_id)


def test_login_disabled_account_is_rejected(client: TestClient) -> None:
    email = f"{uuid4()}@example.test"
    password = "yet another real password"
    resp = client.post(
        "/api/v1/identity/accounts",
        json={"email": email, "password": password, "display_name": "X"},
    )
    account_id = UUID(resp.json()["id"])
    try:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE accounts SET disabled_at = :now WHERE id = :id"),
                {"now": datetime.now(UTC), "id": account_id},
            )
        login = client.post(
            "/api/v1/identity/auth/login", json={"email": email, "password": password}
        )
        assert login.status_code == 401, login.text
    finally:
        _cleanup_account(account_id)


def test_login_with_two_active_memberships_returns_select_workspace(client: TestClient) -> None:
    now = datetime.now(UTC)
    workspace_a, workspace_b = uuid4(), uuid4()
    users_a, users_b = uuid4(), uuid4()
    email = f"{uuid4()}@example.test"
    password = "a shared login password"

    with engine.begin() as connection:
        for wid, name in ((workspace_a, "A"), (workspace_b, "B")):
            connection.execute(
                text(
                    "INSERT INTO workspaces (id, name, created_at, timezone) "
                    "VALUES (:id, :name, :now, 'UTC')"
                ),
                {"id": wid, "name": name, "now": now},
            )
    resp = client.post(
        "/api/v1/identity/accounts",
        json={"email": email, "password": password, "display_name": "Multi"},
    )
    account_id = UUID(resp.json()["id"])
    with engine.begin() as connection:
        add_membership(
            connection,
            workspace_id=workspace_a,
            account_id=account_id,
            users_id=users_a,
            role="owner",
            now=now,
        )
        add_membership(
            connection,
            workspace_id=workspace_b,
            account_id=account_id,
            users_id=users_b,
            role="member",
            now=now,
        )

    try:
        login = client.post(
            "/api/v1/identity/auth/login", json={"email": email, "password": password}
        )
        assert login.status_code == 200, login.text
        body = login.json()
        assert body["status"] == "select_workspace"
        assert "pending_login_token" in body
        returned_ids = {w["workspace_id"] for w in body["workspaces"]}
        assert returned_ids == {str(workspace_a), str(workspace_b)}
        assert "ecc_session" not in client.cookies

        select = client.post(
            "/api/v1/identity/auth/select-workspace",
            json={
                "workspace_id": str(workspace_b),
                "pending_login_token": body["pending_login_token"],
            },
        )
        assert select.status_code == 200, select.text
        assert select.json()["workspace_id"] == str(workspace_b)
        assert "ecc_session" in client.cookies
    finally:
        _cleanup_workspace(workspace_a)
        _cleanup_workspace(workspace_b)
        _cleanup_account(account_id)


def test_select_workspace_rejects_expired_or_forged_pending_token(client: TestClient) -> None:
    resp = client.post(
        "/api/v1/identity/auth/select-workspace",
        json={
            "workspace_id": str(uuid4()),
            "pending_login_token": f"{uuid4()}:9999999999:deadbeef",
        },
    )
    assert resp.status_code == 401, resp.text


def test_select_workspace_rejects_a_workspace_the_account_has_no_membership_in(
    client: TestClient,
) -> None:
    """Cross-account isolation: knowing a real `workspace_id` is not enough."""
    now = datetime.now(UTC)
    owned_workspace = uuid4()
    foreign_workspace = uuid4()
    users_id = uuid4()
    email = f"{uuid4()}@example.test"
    password = "isolation test password"

    with engine.begin() as connection:
        for wid in (owned_workspace, foreign_workspace):
            connection.execute(
                text(
                    "INSERT INTO workspaces (id, name, created_at, timezone) "
                    "VALUES (:id, 'W', :now, 'UTC')"
                ),
                {"id": wid, "now": now},
            )
    resp = client.post(
        "/api/v1/identity/accounts",
        json={"email": email, "password": password, "display_name": "X"},
    )
    account_id = UUID(resp.json()["id"])
    with engine.begin() as connection:
        add_membership(
            connection,
            workspace_id=owned_workspace,
            account_id=account_id,
            users_id=users_id,
            role="owner",
            now=now,
        )

    try:
        login = client.post(
            "/api/v1/identity/auth/login", json={"email": email, "password": password}
        )
        assert login.json()["status"] == "authenticated"
        token = client.cookies["ecc_session"]

        switch = client.post(
            "/api/v1/identity/auth/select-workspace",
            json={"workspace_id": str(foreign_workspace)},
            headers=_headers(token),
        )
        assert switch.status_code == 404, switch.text
        assert switch.json()["error"]["code"] == "WORKSPACE_NOT_FOUND"
    finally:
        _cleanup_workspace(owned_workspace)
        _cleanup_workspace(foreign_workspace)
        _cleanup_account(account_id)


def test_authenticated_switch_requires_csrf(client: TestClient) -> None:
    now = datetime.now(UTC)
    workspace_id = uuid4()
    users_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, created_at, timezone) "
                "VALUES (:id, 'W', :now, 'UTC')"
            ),
            {"id": workspace_id, "now": now},
        )
        account_id = create_identity(
            connection, workspace_id=workspace_id, user_id=users_id, now=now
        )
    token = _new_session(workspace_id, users_id, now)

    try:
        client.cookies.set("ecc_session", token)
        resp = client.post(
            "/api/v1/identity/auth/select-workspace",
            json={"workspace_id": str(workspace_id)},
        )
        assert resp.status_code == 403, resp.text
    finally:
        _cleanup_workspace(workspace_id)
        _cleanup_account(account_id)


@pytest.fixture
def workspace_test_context() -> Iterator[tuple[TestClient, UUID, UUID, UUID, str]]:
    workspace_id = uuid4()
    users_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, created_at, timezone) "
                "VALUES (:id, 'Owner WS', :now, 'UTC')"
            ),
            {"id": workspace_id, "now": now},
        )
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


def test_list_workspaces_returns_only_active_memberships(
    workspace_test_context: tuple[TestClient, UUID, UUID, UUID, str],
) -> None:
    client, workspace_id, _users_id, account_id, token = workspace_test_context
    other_workspace = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, created_at, timezone) "
                "VALUES (:id, 'Suspended WS', :now, 'UTC')"
            ),
            {"id": other_workspace, "now": now},
        )
        add_membership(
            connection,
            workspace_id=other_workspace,
            account_id=account_id,
            users_id=uuid4(),
            role="member",
            status="suspended",
            now=now,
        )

    try:
        resp = client.get("/api/v1/identity/workspaces", headers=_headers(token))
        assert resp.status_code == 200, resp.text
        ids = {w["id"] for w in resp.json()["workspaces"]}
        assert str(workspace_id) in ids
        assert str(other_workspace) not in ids
    finally:
        _cleanup_workspace(other_workspace)


def test_create_workspace_makes_caller_owner(
    workspace_test_context: tuple[TestClient, UUID, UUID, UUID, str],
) -> None:
    client, _workspace_id, _users_id, _account_id, token = workspace_test_context
    resp = client.post(
        "/api/v1/identity/workspaces",
        json={"name": "New Co", "timezone": "America/New_York"},
        headers=_headers(token),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "New Co"
    assert body["role"] == "owner"
    _cleanup_workspace(UUID(body["id"]))


def test_create_workspace_requires_csrf(
    workspace_test_context: tuple[TestClient, UUID, UUID, UUID, str],
) -> None:
    client, *_rest = workspace_test_context
    resp = client.post(
        "/api/v1/identity/workspaces",
        json={"name": "No CSRF Co", "timezone": "UTC"},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "CSRF_TOKEN_REQUIRED"


def test_patch_workspace_requires_csrf(
    workspace_test_context: tuple[TestClient, UUID, UUID, UUID, str],
) -> None:
    client, workspace_id, *_rest = workspace_test_context
    resp = client.patch(
        f"/api/v1/identity/workspaces/{workspace_id}",
        json={"name": "Should not apply"},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "CSRF_TOKEN_REQUIRED"


def test_patch_workspace_requires_owner_or_admin_role(
    workspace_test_context: tuple[TestClient, UUID, UUID, UUID, str],
) -> None:
    client, workspace_id, _users_id, account_id, token = workspace_test_context
    now = datetime.now(UTC)
    viewer_workspace = uuid4()
    viewer_users_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, created_at, timezone) "
                "VALUES (:id, 'Viewer WS', :now, 'UTC')"
            ),
            {"id": viewer_workspace, "now": now},
        )
        add_membership(
            connection,
            workspace_id=viewer_workspace,
            account_id=account_id,
            users_id=viewer_users_id,
            role="viewer",
            now=now,
        )
    viewer_token = _new_session(viewer_workspace, viewer_users_id, now)

    try:
        owner_patch = client.patch(
            f"/api/v1/identity/workspaces/{workspace_id}",
            json={"name": "Renamed"},
            headers=_headers(token),
        )
        assert owner_patch.status_code == 200, owner_patch.text
        assert owner_patch.json()["name"] == "Renamed"

        with TestClient(app) as viewer_client:
            viewer_client.cookies.set("ecc_session", viewer_token)
            viewer_patch = viewer_client.patch(
                f"/api/v1/identity/workspaces/{viewer_workspace}",
                json={"name": "Should not work"},
                headers=_headers(viewer_token),
            )
        assert viewer_patch.status_code == 403, viewer_patch.text
        assert viewer_patch.json()["error"]["code"] == "INSUFFICIENT_ROLE"
    finally:
        _cleanup_workspace(viewer_workspace)


def test_get_workspace_not_found_for_a_different_workspace_id(
    workspace_test_context: tuple[TestClient, UUID, UUID, UUID, str],
) -> None:
    client, _workspace_id, _users_id, _account_id, token = workspace_test_context
    resp = client.get(f"/api/v1/identity/workspaces/{uuid4()}", headers=_headers(token))
    assert resp.status_code == 404, resp.text


def test_disabling_account_revokes_an_already_issued_session_immediately(
    workspace_test_context: tuple[TestClient, UUID, UUID, UUID, str],
) -> None:
    """`require_auth_context` (`ecc.auth`) joins through to `accounts` so a
    disabled account's live sessions stop working without needing a separate
    revocation step -- the same "no cache to invalidate" propagation
    `docs/superpowers/specs/2026-08-01-phase-8-multi-user-design.md`
    Decision 4 promises for every other revocation path in this phase.
    """
    client, _workspace_id, _users_id, account_id, token = workspace_test_context

    before = client.get("/api/v1/identity/workspaces", headers=_headers(token))
    assert before.status_code == 200, before.text

    with engine.begin() as connection:
        connection.execute(
            text("UPDATE accounts SET disabled_at = :now WHERE id = :id"),
            {"now": datetime.now(UTC), "id": account_id},
        )

    after = client.get("/api/v1/identity/workspaces", headers=_headers(token))
    assert after.status_code == 401, after.text
