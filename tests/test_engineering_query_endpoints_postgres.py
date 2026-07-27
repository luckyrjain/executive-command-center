"""Phase 6 Engineering Workspace Task 8 ("Executive UX and browser
acceptance"): `GET /api/v1/engineering/repositories` and `GET /api/v1/
engineering/work-items`.

Neither endpoint was named by any of Tasks 1-7's own scope even though
`repositories` has been synced since Task 2 (migration `0045_phase6_
repositories.py`) and `engineering_work_items` since Task 4 (migration
`0047_phase6_work_items.py`) -- see `connector_accounts.py`'s own comment
above `RepositoryResponse` for the full "disclosed addition" reasoning.
This file covers, per that disclosed scope:

1. Workspace-scoped listing for both endpoints, ordered as documented
   (`name`/`title` ascending).
2. The optional `connector_account_id` filter on both endpoints.
3. The optional `status` filter on work items (a plain string match, not a
   closed enum -- `engineering_work_items.status` carries no CHECK
   constraint).
4. Cross-workspace isolation on both endpoints.
5. An empty list (not an error) when nothing has synced yet -- the "first
   sync" UX state's own backend precondition.
6. `reporter_external_id`/`assignee_external_id` round-trip on work items
   exactly as stored -- these are raw, never-resolved provider identifiers
   (no resolution-candidate mechanism exists for engineering identities),
   so the frontend's "conflicting identities" disclosure state depends on
   this endpoint returning them completely unmodified, not silently
   dropping or renaming them.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import engine
from ecc.domains.engineering.crypto import encrypt_credential
from ecc.main import app

settings = get_settings()


def _headers(token: str, key: str | None = None) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    headers = {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


@pytest.fixture
def query_endpoints_test_context() -> Iterator[tuple[TestClient, UUID, UUID]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Query Endpoints Test', 'UTC', :now)"
            ),
            {"id": workspace_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO users (id, workspace_id, email, password_hash, created_at) "
                "VALUES (:id, :workspace_id, :email, 'test-password-hash', :now)"
            ),
            {
                "id": user_id,
                "workspace_id": workspace_id,
                "email": f"{user_id}@example.test",
                "now": now,
            },
        )
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

    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    try:
        yield client, workspace_id, user_id
    finally:
        client.close()
        _cleanup_workspace(workspace_id)


def _cleanup_workspace(workspace_id: UUID) -> None:
    with engine.begin() as connection:
        for table in (
            "engineering_work_items",
            "repositories",
            "connector_accounts",
            "event_outbox",
            "audit_events",
            "idempotency_records",
            "sessions",
            "users",
        ):
            connection.execute(
                text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                {"workspace_id": workspace_id},
            )
        connection.execute(
            text("DELETE FROM workspaces WHERE id = :workspace_id"), {"workspace_id": workspace_id}
        )


def _insert_connector_account(
    workspace_id: UUID, user_id: UUID, *, provider: str = "github", credential: str = "ghp_x"
) -> UUID:
    account_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO connector_accounts (
                    id, workspace_id, provider, external_account_id, display_name,
                    granted_scopes, encrypted_credentials, status, version,
                    created_by, updated_by, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, :provider, :ext, 'Fixture account',
                    ARRAY[]::text[], :cred, 'active', 1, :actor, :actor, :now, :now
                )
                """
            ),
            {
                "id": account_id,
                "workspace_id": workspace_id,
                "provider": provider,
                "ext": f"{provider}-acct-{account_id}",
                "cred": encrypt_credential(credential),
                "actor": user_id,
                "now": now,
            },
        )
    return account_id


def _insert_repository(
    workspace_id: UUID,
    connector_account_id: UUID,
    *,
    name: str,
    permission_state: str = "active",
    freshness_state: str = "fresh",
) -> UUID:
    repository_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO repositories (
                    id, workspace_id, connector_account_id, provider, external_id,
                    name, source_url, default_branch, permission_state, freshness_state,
                    observed_at, created_at, updated_at
                ) VALUES (
                    :id, :ws, :acct, 'github', :ext, :name, 'https://x', 'main',
                    :permission_state, :freshness_state, :now, :now, :now
                )
                """
            ),
            {
                "id": repository_id,
                "ws": workspace_id,
                "acct": connector_account_id,
                "ext": f"repo-{repository_id}",
                "name": name,
                "permission_state": permission_state,
                "freshness_state": freshness_state,
                "now": now,
            },
        )
    return repository_id


def _insert_work_item(
    workspace_id: UUID,
    connector_account_id: UUID,
    *,
    title: str,
    status: str | None = "To Do",
    reporter_external_id: str | None = "reporter-raw-123",
    assignee_external_id: str | None = "assignee-raw-456",
) -> UUID:
    work_item_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO engineering_work_items (
                    id, workspace_id, connector_account_id, provider, external_id,
                    title, source_url, item_type, status, reporter_external_id,
                    assignee_external_id, permission_state, freshness_state,
                    observed_at, created_at, updated_at
                ) VALUES (
                    :id, :ws, :acct, 'jira', :ext, :title, 'https://x', 'Bug', :status,
                    :reporter, :assignee, 'active', 'fresh', :now, :now, :now
                )
                """
            ),
            {
                "id": work_item_id,
                "ws": workspace_id,
                "acct": connector_account_id,
                "ext": f"item-{work_item_id}",
                "title": title,
                "status": status,
                "reporter": reporter_external_id,
                "assignee": assignee_external_id,
                "now": now,
            },
        )
    return work_item_id


# --- repositories -------------------------------------------------------


def test_list_repositories_returns_workspace_scoped_rows_ordered_by_name(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    client, workspace_id, user_id = query_endpoints_test_context
    account_id = _insert_connector_account(workspace_id, user_id)
    _insert_repository(workspace_id, account_id, name="zeta")
    _insert_repository(workspace_id, account_id, name="alpha")

    response = client.get("/api/v1/engineering/repositories")
    assert response.status_code == 200, response.text
    names = [repo["name"] for repo in response.json()["repositories"]]
    assert names == ["alpha", "zeta"]


def test_list_repositories_returns_empty_list_when_none_synced(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    client, _workspace_id, _user_id = query_endpoints_test_context
    response = client.get("/api/v1/engineering/repositories")
    assert response.status_code == 200, response.text
    assert response.json()["repositories"] == []


def test_list_repositories_filters_by_connector_account_id(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    client, workspace_id, user_id = query_endpoints_test_context
    account_a = _insert_connector_account(workspace_id, user_id, credential="ghp_a")
    account_b = _insert_connector_account(workspace_id, user_id, credential="ghp_b")
    _insert_repository(workspace_id, account_a, name="from-a")
    _insert_repository(workspace_id, account_b, name="from-b")

    response = client.get(
        "/api/v1/engineering/repositories",
        params={"connector_account_id": str(account_a)},
    )
    assert response.status_code == 200, response.text
    names = [repo["name"] for repo in response.json()["repositories"]]
    assert names == ["from-a"]


def test_list_repositories_exposes_permission_and_freshness_state(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    client, workspace_id, user_id = query_endpoints_test_context
    account_id = _insert_connector_account(workspace_id, user_id)
    _insert_repository(
        workspace_id,
        account_id,
        name="degraded",
        permission_state="permission_lost",
        freshness_state="stale",
    )

    response = client.get("/api/v1/engineering/repositories")
    assert response.status_code == 200, response.text
    repo = response.json()["repositories"][0]
    assert repo["permission_state"] == "permission_lost"
    assert repo["freshness_state"] == "stale"


def test_list_repositories_cross_workspace_isolation(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    client, workspace_id, user_id = query_endpoints_test_context
    account_id = _insert_connector_account(workspace_id, user_id)
    _insert_repository(workspace_id, account_id, name="mine")

    other_workspace_id = uuid4()
    other_user_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Other', 'UTC', :now)"
            ),
            {"id": other_workspace_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO users (id, workspace_id, email, password_hash, created_at) "
                "VALUES (:id, :workspace_id, :email, 'x', :now)"
            ),
            {
                "id": other_user_id,
                "workspace_id": other_workspace_id,
                "email": f"{other_user_id}@example.test",
                "now": now,
            },
        )
    try:
        other_account_id = _insert_connector_account(
            other_workspace_id, other_user_id, credential="ghp_other"
        )
        _insert_repository(other_workspace_id, other_account_id, name="theirs")

        response = client.get("/api/v1/engineering/repositories")
        assert response.status_code == 200, response.text
        names = [repo["name"] for repo in response.json()["repositories"]]
        assert names == ["mine"]
    finally:
        _cleanup_workspace(other_workspace_id)


# --- work items -----------------------------------------------------------


def test_list_work_items_returns_workspace_scoped_rows_ordered_by_title(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    client, workspace_id, user_id = query_endpoints_test_context
    account_id = _insert_connector_account(workspace_id, user_id, provider="jira", credential="tok")
    _insert_work_item(workspace_id, account_id, title="zeta issue")
    _insert_work_item(workspace_id, account_id, title="alpha issue")

    response = client.get("/api/v1/engineering/work-items")
    assert response.status_code == 200, response.text
    titles = [item["title"] for item in response.json()["work_items"]]
    assert titles == ["alpha issue", "zeta issue"]


def test_list_work_items_returns_empty_list_when_none_synced(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    client, _workspace_id, _user_id = query_endpoints_test_context
    response = client.get("/api/v1/engineering/work-items")
    assert response.status_code == 200, response.text
    assert response.json()["work_items"] == []


def test_list_work_items_filters_by_status(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    client, workspace_id, user_id = query_endpoints_test_context
    account_id = _insert_connector_account(workspace_id, user_id, provider="jira", credential="tok")
    _insert_work_item(workspace_id, account_id, title="open one", status="To Do")
    _insert_work_item(workspace_id, account_id, title="done one", status="Done")

    response = client.get("/api/v1/engineering/work-items", params={"status": "Done"})
    assert response.status_code == 200, response.text
    titles = [item["title"] for item in response.json()["work_items"]]
    assert titles == ["done one"]


def test_list_work_items_filters_by_connector_account_id(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    client, workspace_id, user_id = query_endpoints_test_context
    account_a = _insert_connector_account(
        workspace_id, user_id, provider="jira", credential="tok-a"
    )
    account_b = _insert_connector_account(
        workspace_id, user_id, provider="jira", credential="tok-b"
    )
    _insert_work_item(workspace_id, account_a, title="from-a")
    _insert_work_item(workspace_id, account_b, title="from-b")

    response = client.get(
        "/api/v1/engineering/work-items",
        params={"connector_account_id": str(account_a)},
    )
    assert response.status_code == 200, response.text
    titles = [item["title"] for item in response.json()["work_items"]]
    assert titles == ["from-a"]


def test_list_work_items_exposes_unresolved_external_ids_unmodified(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    """The frontend's "conflicting identities" disclosure state (`UX-
    STATES.md`) depends entirely on this endpoint returning `reporter_
    external_id`/`assignee_external_id` exactly as stored -- there is no
    resolution-candidate mechanism for engineering identities (unlike
    Phase 2's `resolution_candidates`), so a raw provider identifier is
    the only thing the frontend has to disclose. A silently dropped or
    renamed field here would make that disclosure impossible.
    """
    client, workspace_id, user_id = query_endpoints_test_context
    account_id = _insert_connector_account(workspace_id, user_id, provider="jira", credential="tok")
    _insert_work_item(
        workspace_id,
        account_id,
        title="unresolved identities",
        reporter_external_id="jira-user-9001",
        assignee_external_id=None,
    )

    response = client.get("/api/v1/engineering/work-items")
    assert response.status_code == 200, response.text
    item = response.json()["work_items"][0]
    assert item["reporter_external_id"] == "jira-user-9001"
    assert item["assignee_external_id"] is None


def test_list_work_items_cross_workspace_isolation(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    client, workspace_id, user_id = query_endpoints_test_context
    account_id = _insert_connector_account(workspace_id, user_id, provider="jira", credential="tok")
    _insert_work_item(workspace_id, account_id, title="mine")

    other_workspace_id = uuid4()
    other_user_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Other', 'UTC', :now)"
            ),
            {"id": other_workspace_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO users (id, workspace_id, email, password_hash, created_at) "
                "VALUES (:id, :workspace_id, :email, 'x', :now)"
            ),
            {
                "id": other_user_id,
                "workspace_id": other_workspace_id,
                "email": f"{other_user_id}@example.test",
                "now": now,
            },
        )
    try:
        other_account_id = _insert_connector_account(
            other_workspace_id, other_user_id, provider="jira", credential="tok-other"
        )
        _insert_work_item(other_workspace_id, other_account_id, title="theirs")

        response = client.get("/api/v1/engineering/work-items")
        assert response.status_code == 200, response.text
        titles = [item["title"] for item in response.json()["work_items"]]
        assert titles == ["mine"]
    finally:
        _cleanup_workspace(other_workspace_id)


# --- disable cascades freshness_state (final Phase 6 review finding) ----
#
# `CONNECTOR-CONTRACT.md`: disabling a connector retains its previously-
# synced projections, visible not deleted, with a `disconnected` freshness
# state -- `disable_connector_endpoint` never implemented that cascade, so
# these rows kept reporting `freshness_state='fresh'` forever after
# disable. These two tests prove the cascade landed and is workspace- and
# connector-account-scoped, not a blanket update.


def test_disable_marks_repositories_disconnected(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    client, workspace_id, user_id = query_endpoints_test_context
    token = client.cookies.get("ecc_session")
    assert token is not None
    account_id = _insert_connector_account(workspace_id, user_id)
    _insert_repository(workspace_id, account_id, name="repo-a")

    response = client.post(
        f"/api/v1/engineering/connectors/{account_id}/disable",
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 200, response.text

    listed = client.get("/api/v1/engineering/repositories")
    assert listed.status_code == 200, listed.text
    assert [repo["freshness_state"] for repo in listed.json()["repositories"]] == ["disconnected"]


def test_disable_marks_work_items_disconnected(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    client, workspace_id, user_id = query_endpoints_test_context
    token = client.cookies.get("ecc_session")
    assert token is not None
    account_id = _insert_connector_account(workspace_id, user_id, provider="jira", credential="tok")
    _insert_work_item(workspace_id, account_id, title="item-a")

    response = client.post(
        f"/api/v1/engineering/connectors/{account_id}/disable",
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 200, response.text

    listed = client.get("/api/v1/engineering/work-items")
    assert listed.status_code == 200, listed.text
    assert [item["freshness_state"] for item in listed.json()["work_items"]] == ["disconnected"]


def test_disable_does_not_touch_a_different_connector_accounts_rows(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    client, workspace_id, user_id = query_endpoints_test_context
    token = client.cookies.get("ecc_session")
    assert token is not None
    disabled_account = _insert_connector_account(workspace_id, user_id, credential="ghp_a")
    other_account = _insert_connector_account(workspace_id, user_id, credential="ghp_b")
    _insert_repository(workspace_id, disabled_account, name="from-disabled")
    _insert_repository(workspace_id, other_account, name="from-other")

    response = client.post(
        f"/api/v1/engineering/connectors/{disabled_account}/disable",
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 200, response.text

    listed = client.get("/api/v1/engineering/repositories").json()["repositories"]
    states = {repo["name"]: repo["freshness_state"] for repo in listed}
    assert states == {"from-disabled": "disconnected", "from-other": "fresh"}
