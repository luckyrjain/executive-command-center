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
        # `repositories`/`engineering_work_items` are deleted before
        # `pkos_nodes` even though their `team_entity_id` FK is nullable --
        # tidiness, not a hard ordering requirement (a `NULL`-valued FK row
        # is never a blocker), but keeping every referencing row gone
        # before its referenced `pkos_nodes` row avoids depending on that
        # nullability by coincidence.
        for table in (
            "engineering_work_items",
            "repositories",
            "datadog_monitors",
            "datadog_service_definitions",
            "datadog_dashboards",
            "changes",
            "reviews",
            "connector_accounts",
            "pkos_nodes",
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


def _insert_team_entity(
    workspace_id: UUID, *, name: str = "Platform", status: str = "active"
) -> UUID:
    entity_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO pkos_nodes (id, workspace_id, node_type, canonical_name, "
                "status, confidence, version, created_at, updated_at) "
                "VALUES (:id, :workspace_id, 'team', :name, :status, 1.0, 1, :now, :now)"
            ),
            {
                "id": entity_id,
                "workspace_id": workspace_id,
                "name": name,
                "status": status,
                "now": now,
            },
        )
    return entity_id


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
    team_entity_id: UUID | None = None,
    suggested_team_name: str | None = None,
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
                    observed_at, created_at, updated_at, team_entity_id, suggested_team_name
                ) VALUES (
                    :id, :ws, :acct, 'github', :ext, :name, 'https://x', 'main',
                    :permission_state, :freshness_state, :now, :now, :now,
                    :team_entity_id, :suggested_team_name
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
                "team_entity_id": team_entity_id,
                "suggested_team_name": suggested_team_name,
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
    team_entity_id: UUID | None = None,
    suggested_team_name: str | None = None,
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
                    observed_at, created_at, updated_at, team_entity_id, suggested_team_name
                ) VALUES (
                    :id, :ws, :acct, 'jira', :ext, :title, 'https://x', 'Bug', :status,
                    :reporter, :assignee, 'active', 'fresh', :now, :now, :now,
                    :team_entity_id, :suggested_team_name
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
                "team_entity_id": team_entity_id,
                "suggested_team_name": suggested_team_name,
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


def _insert_change(workspace_id: UUID, connector_account_id: UUID, repository_id: UUID) -> UUID:
    change_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO changes (
                    id, workspace_id, connector_account_id, repository_id, provider,
                    external_id, title, source_url, permission_state, freshness_state,
                    observed_at, created_at, updated_at
                ) VALUES (
                    :id, :ws, :acct, :repo, 'github', :ext, 'A change', 'https://x',
                    'active', 'fresh', :now, :now, :now
                )
                """
            ),
            {
                "id": change_id,
                "ws": workspace_id,
                "acct": connector_account_id,
                "repo": repository_id,
                "ext": f"change-{change_id}",
                "now": now,
            },
        )
    return change_id


def _insert_review(workspace_id: UUID, connector_account_id: UUID, change_id: UUID) -> UUID:
    review_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO reviews (
                    id, workspace_id, connector_account_id, change_id, provider,
                    external_id, source_url, permission_state, freshness_state,
                    observed_at, created_at, updated_at
                ) VALUES (
                    :id, :ws, :acct, :change, 'github', :ext, 'https://x',
                    'active', 'fresh', :now, :now, :now
                )
                """
            ),
            {
                "id": review_id,
                "ws": workspace_id,
                "acct": connector_account_id,
                "change": change_id,
                "ext": f"review-{review_id}",
                "now": now,
            },
        )
    return review_id


def test_disable_marks_changes_and_reviews_disconnected(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    """A round-2 correctness re-review of the disable cascade found the
    first fix pass only covered `repositories`/`engineering_work_items`,
    leaving `changes`/`reviews` (migration `0048`, the identical
    `workspace_id`/`connector_account_id`/`freshness_state` shape)
    permanently `fresh` after disable. Neither has a `GET` endpoint yet
    (a disclosed gap -- see `API-SCHEMAS.md`), so this checks the real
    column directly rather than through a query endpoint.
    """
    client, workspace_id, user_id = query_endpoints_test_context
    token = client.cookies.get("ecc_session")
    assert token is not None
    account_id = _insert_connector_account(workspace_id, user_id)
    repository_id = _insert_repository(workspace_id, account_id, name="repo-a")
    change_id = _insert_change(workspace_id, account_id, repository_id)
    _insert_review(workspace_id, account_id, change_id)

    response = client.post(
        f"/api/v1/engineering/connectors/{account_id}/disable",
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 200, response.text

    with engine.begin() as connection:
        change_state = connection.execute(
            text("SELECT freshness_state FROM changes WHERE id = :id"), {"id": change_id}
        ).scalar_one()
        review_state = connection.execute(
            text("SELECT freshness_state FROM reviews WHERE connector_account_id = :acct"),
            {"acct": account_id},
        ).scalar_one()
    assert change_state == "disconnected"
    assert review_state == "disconnected"


# --- Datadog connector (migration `0051_phase6_datadog_connector.py`) ------


def _insert_datadog_monitor(
    workspace_id: UUID, connector_account_id: UUID, *, name: str = "acceptance monitor"
) -> UUID:
    monitor_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO datadog_monitors (
                    id, workspace_id, connector_account_id, provider, external_id,
                    source_url, permission_state, freshness_state, observed_at,
                    created_at, updated_at, name
                ) VALUES (
                    :id, :ws, :acct, 'datadog', :ext, 'https://x', 'active', 'fresh',
                    :now, :now, :now, :name
                )
                """
            ),
            {
                "id": monitor_id,
                "ws": workspace_id,
                "acct": connector_account_id,
                "ext": f"monitor-{monitor_id}",
                "name": name,
                "now": now,
            },
        )
    return monitor_id


def _insert_datadog_service_definition(
    workspace_id: UUID, connector_account_id: UUID, *, name: str = "acceptance-service"
) -> UUID:
    definition_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO datadog_service_definitions (
                    id, workspace_id, connector_account_id, provider, external_id,
                    source_url, permission_state, freshness_state, observed_at,
                    created_at, updated_at, name
                ) VALUES (
                    :id, :ws, :acct, 'datadog', :ext, 'https://x', 'active', 'fresh',
                    :now, :now, :now, :name
                )
                """
            ),
            {
                "id": definition_id,
                "ws": workspace_id,
                "acct": connector_account_id,
                "ext": f"service-{definition_id}",
                "name": name,
                "now": now,
            },
        )
    return definition_id


def _insert_datadog_dashboard(
    workspace_id: UUID, connector_account_id: UUID, *, title: str = "Acceptance dashboard"
) -> UUID:
    dashboard_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO datadog_dashboards (
                    id, workspace_id, connector_account_id, provider, external_id,
                    source_url, permission_state, freshness_state, observed_at,
                    created_at, updated_at, title
                ) VALUES (
                    :id, :ws, :acct, 'datadog', :ext, 'https://x', 'active', 'fresh',
                    :now, :now, :now, :title
                )
                """
            ),
            {
                "id": dashboard_id,
                "ws": workspace_id,
                "acct": connector_account_id,
                "ext": f"dashboard-{dashboard_id}",
                "title": title,
                "now": now,
            },
        )
    return dashboard_id


def test_disable_marks_datadog_projections_disconnected(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    """PR #85's own security/correctness review found the disable cascade
    (`test_disable_marks_changes_and_reviews_disconnected`'s own docstring
    above has the full history of this exact gap recurring twice already)
    had not been extended to the three Datadog tables added by migration
    `0051_phase6_datadog_connector.py`. Unlike `changes`/`reviews`, these
    three do have their own `GET` endpoints, so this checks through them.
    """
    client, workspace_id, user_id = query_endpoints_test_context
    token = client.cookies.get("ecc_session")
    assert token is not None
    account_id = _insert_connector_account(
        workspace_id, user_id, provider="datadog", credential="dd"
    )
    _insert_datadog_monitor(workspace_id, account_id)
    _insert_datadog_service_definition(workspace_id, account_id)
    _insert_datadog_dashboard(workspace_id, account_id)

    response = client.post(
        f"/api/v1/engineering/connectors/{account_id}/disable",
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 200, response.text

    monitors = client.get("/api/v1/engineering/monitors").json()["monitors"]
    definitions = client.get("/api/v1/engineering/service-definitions").json()[
        "service_definitions"
    ]
    dashboards = client.get("/api/v1/engineering/dashboards").json()["dashboards"]
    assert [m["freshness_state"] for m in monitors] == ["disconnected"]
    assert [d["freshness_state"] for d in definitions] == ["disconnected"]
    assert [d["freshness_state"] for d in dashboards] == ["disconnected"]


def test_list_monitors_cross_workspace_isolation_and_team_entity_filter(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    client, workspace_id, user_id = query_endpoints_test_context
    account_id = _insert_connector_account(
        workspace_id, user_id, provider="datadog", credential="dd"
    )
    team_id = _insert_team_entity(workspace_id)
    mine = _insert_datadog_monitor(workspace_id, account_id, name="mine")
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE datadog_monitors SET team_entity_id = :team WHERE id = :id"),
            {"team": team_id, "id": mine},
        )
    _insert_datadog_monitor(workspace_id, account_id, name="unassigned")

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
                "VALUES (:id, :workspace_id, :email, 'test-password-hash', :now)"
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
            other_workspace_id, other_user_id, provider="datadog", credential="dd-other"
        )
        _insert_datadog_monitor(other_workspace_id, other_account_id, name="theirs")

        response = client.get("/api/v1/engineering/monitors")
        assert response.status_code == 200, response.text
        names = {m["name"] for m in response.json()["monitors"]}
        assert names == {"mine", "unassigned"}

        filtered = client.get(
            "/api/v1/engineering/monitors", params={"team_entity_id": str(team_id)}
        )
        assert filtered.status_code == 200, filtered.text
        assert [m["name"] for m in filtered.json()["monitors"]] == ["mine"]
    finally:
        _cleanup_workspace(other_workspace_id)


def test_list_service_definitions_and_dashboards_cross_workspace_isolation(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    client, workspace_id, user_id = query_endpoints_test_context
    account_id = _insert_connector_account(
        workspace_id, user_id, provider="datadog", credential="dd"
    )
    _insert_datadog_service_definition(workspace_id, account_id, name="mine-service")
    _insert_datadog_dashboard(workspace_id, account_id, title="Mine dashboard")

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
                "VALUES (:id, :workspace_id, :email, 'test-password-hash', :now)"
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
            other_workspace_id, other_user_id, provider="datadog", credential="dd-other"
        )
        _insert_datadog_service_definition(
            other_workspace_id, other_account_id, name="their-service"
        )
        _insert_datadog_dashboard(other_workspace_id, other_account_id, title="Their dashboard")

        definitions = client.get("/api/v1/engineering/service-definitions").json()[
            "service_definitions"
        ]
        assert [d["name"] for d in definitions] == ["mine-service"]

        dashboards = client.get("/api/v1/engineering/dashboards").json()["dashboards"]
        assert [d["title"] for d in dashboards] == ["Mine dashboard"]
    finally:
        _cleanup_workspace(other_workspace_id)


def test_list_service_definitions_and_dashboards_filter_by_team_entity_id(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    """`list_monitors_endpoint`'s `team_entity_id` filter has its own
    dedicated test (`test_list_monitors_cross_workspace_isolation_and_
    team_entity_filter`); review found its two siblings on service
    definitions and dashboards apply the identical clause but had no
    equivalent test proving it actually narrows results rather than
    silently being a no-op or erroring.
    """
    client, workspace_id, user_id = query_endpoints_test_context
    account_id = _insert_connector_account(
        workspace_id, user_id, provider="datadog", credential="dd"
    )
    team_id = _insert_team_entity(workspace_id)
    mine_definition = _insert_datadog_service_definition(
        workspace_id, account_id, name="mine-service"
    )
    _insert_datadog_service_definition(workspace_id, account_id, name="unassigned-service")
    mine_dashboard = _insert_datadog_dashboard(workspace_id, account_id, title="Mine dashboard")
    _insert_datadog_dashboard(workspace_id, account_id, title="Unassigned dashboard")
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE datadog_service_definitions SET team_entity_id = :team WHERE id = :id"),
            {"team": team_id, "id": mine_definition},
        )
        connection.execute(
            text("UPDATE datadog_dashboards SET team_entity_id = :team WHERE id = :id"),
            {"team": team_id, "id": mine_dashboard},
        )

    filtered_definitions = client.get(
        "/api/v1/engineering/service-definitions", params={"team_entity_id": str(team_id)}
    )
    assert filtered_definitions.status_code == 200, filtered_definitions.text
    assert [d["name"] for d in filtered_definitions.json()["service_definitions"]] == [
        "mine-service"
    ]

    filtered_dashboards = client.get(
        "/api/v1/engineering/dashboards", params={"team_entity_id": str(team_id)}
    )
    assert filtered_dashboards.status_code == 200, filtered_dashboards.text
    assert [d["title"] for d in filtered_dashboards.json()["dashboards"]] == ["Mine dashboard"]


# --- team linkage (migration `0050_phase6_team_linkage.py`) ---------------
#
# "hybrid: auto-suggest, human confirms": `suggested_team_name` is exercised
# by the adapter-level tests (`test_engineering_github_sync_postgres.py`
# et al) since only a sync call populates it; this section covers the
# "human confirms" half -- `POST .../repositories/{id}/team` and `POST
# .../work-items/{id}/team` -- plus both list endpoints' new
# `team_entity_id` filter.


def test_list_repositories_exposes_team_fields(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    client, workspace_id, user_id = query_endpoints_test_context
    account_id = _insert_connector_account(workspace_id, user_id)
    team_id = _insert_team_entity(workspace_id)
    _insert_repository(
        workspace_id,
        account_id,
        name="repo-a",
        team_entity_id=team_id,
        suggested_team_name="acme",
    )

    response = client.get("/api/v1/engineering/repositories")
    assert response.status_code == 200, response.text
    repo = response.json()["repositories"][0]
    assert repo["team_entity_id"] == str(team_id)
    assert repo["suggested_team_name"] == "acme"


def test_list_repositories_filters_by_team_entity_id(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    client, workspace_id, user_id = query_endpoints_test_context
    account_id = _insert_connector_account(workspace_id, user_id)
    team_id = _insert_team_entity(workspace_id)
    _insert_repository(workspace_id, account_id, name="assigned", team_entity_id=team_id)
    _insert_repository(workspace_id, account_id, name="unassigned")

    response = client.get(
        "/api/v1/engineering/repositories", params={"team_entity_id": str(team_id)}
    )
    assert response.status_code == 200, response.text
    names = [repo["name"] for repo in response.json()["repositories"]]
    assert names == ["assigned"]


def test_list_work_items_filters_by_team_entity_id(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    client, workspace_id, user_id = query_endpoints_test_context
    account_id = _insert_connector_account(workspace_id, user_id, provider="jira", credential="tok")
    team_id = _insert_team_entity(workspace_id)
    _insert_work_item(workspace_id, account_id, title="assigned", team_entity_id=team_id)
    _insert_work_item(workspace_id, account_id, title="unassigned")

    response = client.get("/api/v1/engineering/work-items", params={"team_entity_id": str(team_id)})
    assert response.status_code == 200, response.text
    titles = [item["title"] for item in response.json()["work_items"]]
    assert titles == ["assigned"]


def test_assign_repository_team_sets_confirmed_link(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    client, workspace_id, user_id = query_endpoints_test_context
    token = client.cookies.get("ecc_session")
    assert token is not None
    account_id = _insert_connector_account(workspace_id, user_id)
    repository_id = _insert_repository(workspace_id, account_id, name="repo-a")
    team_id = _insert_team_entity(workspace_id)

    response = client.post(
        f"/api/v1/engineering/repositories/{repository_id}/team",
        json={"expected_version": 1, "team_entity_id": str(team_id)},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["team_entity_id"] == str(team_id)
    assert body["team_assignment_version"] == 2
    assert body["team_assignment_updated_by"] == str(user_id)

    listed = client.get("/api/v1/engineering/repositories")
    assert listed.json()["repositories"][0]["team_entity_id"] == str(team_id)

    with engine.begin() as connection:
        audit_type = connection.execute(
            text(
                "SELECT event_type FROM audit_events "
                "WHERE workspace_id = :workspace_id AND aggregate_id = :repository_id"
            ),
            {"workspace_id": workspace_id, "repository_id": repository_id},
        ).scalar_one()
    assert audit_type == "repository.team_assigned"


def test_assign_repository_team_clears_with_null(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    client, workspace_id, user_id = query_endpoints_test_context
    token = client.cookies.get("ecc_session")
    assert token is not None
    account_id = _insert_connector_account(workspace_id, user_id)
    team_id = _insert_team_entity(workspace_id)
    repository_id = _insert_repository(
        workspace_id, account_id, name="repo-a", team_entity_id=team_id
    )

    response = client.post(
        f"/api/v1/engineering/repositories/{repository_id}/team",
        json={"expected_version": 1, "team_entity_id": None},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 200, response.text
    assert response.json()["team_entity_id"] is None


def test_assign_repository_team_reassign_overwrites_prior_team(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    """Distinct from assign-once and assign-then-clear (below): a second
    assignment to a DIFFERENT team must overwrite the first, not merge,
    append, or silently no-op -- the only way to prove this is checking a
    genuine A-then-B transition, not just "a value got set."
    """
    client, workspace_id, user_id = query_endpoints_test_context
    token = client.cookies.get("ecc_session")
    assert token is not None
    account_id = _insert_connector_account(workspace_id, user_id)
    repository_id = _insert_repository(workspace_id, account_id, name="repo-a")
    team_a = _insert_team_entity(workspace_id, name="Team A")
    team_b = _insert_team_entity(workspace_id, name="Team B")

    first = client.post(
        f"/api/v1/engineering/repositories/{repository_id}/team",
        json={"expected_version": 1, "team_entity_id": str(team_a)},
        headers=_headers(token, key=str(uuid4())),
    )
    assert first.status_code == 200, first.text
    assert first.json()["team_entity_id"] == str(team_a)
    assert first.json()["team_assignment_version"] == 2

    second = client.post(
        f"/api/v1/engineering/repositories/{repository_id}/team",
        json={"expected_version": 2, "team_entity_id": str(team_b)},
        headers=_headers(token, key=str(uuid4())),
    )
    assert second.status_code == 200, second.text
    assert second.json()["team_entity_id"] == str(team_b)
    assert second.json()["team_assignment_version"] == 3

    listed = client.get("/api/v1/engineering/repositories")
    assert listed.json()["repositories"][0]["team_entity_id"] == str(team_b)


def test_assign_repository_team_409_on_stale_expected_version(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    """`team_assignment_version` (migration `0050_phase6_team_linkage.py`)
    -- a second caller who read the row before the first caller's
    assignment landed must be told their view is stale, not have their
    write silently applied on top and lose the conflict signal.
    """
    client, workspace_id, user_id = query_endpoints_test_context
    token = client.cookies.get("ecc_session")
    assert token is not None
    account_id = _insert_connector_account(workspace_id, user_id)
    repository_id = _insert_repository(workspace_id, account_id, name="repo-a")
    team_id = _insert_team_entity(workspace_id)

    stale = client.post(
        f"/api/v1/engineering/repositories/{repository_id}/team",
        json={"expected_version": 99, "team_entity_id": str(team_id)},
        headers=_headers(token, key=str(uuid4())),
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"

    unchanged = client.get("/api/v1/engineering/repositories")
    assert unchanged.json()["repositories"][0]["team_entity_id"] is None


def test_assign_repository_team_replays_idempotency_key(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    """Mirrors `update_entity`'s own `Idempotency-Key` contract: a same-key
    retry of an identical request replays the first response rather than
    hitting the now-stale `expected_version` a second, real attempt would.
    """
    client, workspace_id, user_id = query_endpoints_test_context
    token = client.cookies.get("ecc_session")
    assert token is not None
    account_id = _insert_connector_account(workspace_id, user_id)
    repository_id = _insert_repository(workspace_id, account_id, name="repo-a")
    team_id = _insert_team_entity(workspace_id)
    key = str(uuid4())

    first = client.post(
        f"/api/v1/engineering/repositories/{repository_id}/team",
        json={"expected_version": 1, "team_entity_id": str(team_id)},
        headers=_headers(token, key=key),
    )
    assert first.status_code == 200, first.text
    assert first.json()["team_assignment_version"] == 2

    replay = client.post(
        f"/api/v1/engineering/repositories/{repository_id}/team",
        json={"expected_version": 1, "team_entity_id": str(team_id)},
        headers=_headers(token, key=key),
    )
    assert replay.status_code == 200, replay.text
    # `request_id` is per-HTTP-call envelope metadata, not part of the
    # cached resource state -- comparing it would fail even on a genuine
    # replay, matching this codebase's own established idempotency-replay
    # assertion shape elsewhere (e.g. `test_create_connector_idempotency_
    # replay_and_conflict`'s `replay.json()["id"] == first.json()["id"]`).
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["team_entity_id"] == first.json()["team_entity_id"]
    assert replay.json()["team_assignment_version"] == first.json()["team_assignment_version"]


def test_assign_repository_team_404_for_archived_team_entity(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    """`_validate_team_entity`'s `WHERE ... AND status = 'active'` clause --
    every other "not found" test uses a UUID that never existed at all,
    which would pass even without this predicate. An archived team must
    be rejected the identical way a nonexistent one is, or a merged/
    archived team could still be silently assigned.
    """
    client, workspace_id, user_id = query_endpoints_test_context
    token = client.cookies.get("ecc_session")
    assert token is not None
    account_id = _insert_connector_account(workspace_id, user_id)
    repository_id = _insert_repository(workspace_id, account_id, name="repo-a")
    archived_team = _insert_team_entity(workspace_id, name="Retired Team", status="archived")

    response = client.post(
        f"/api/v1/engineering/repositories/{repository_id}/team",
        json={"expected_version": 1, "team_entity_id": str(archived_team)},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TEAM_ENTITY_NOT_FOUND"


def test_assign_repository_team_404_for_unknown_repository(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    client, workspace_id, _user_id = query_endpoints_test_context
    token = client.cookies.get("ecc_session")
    assert token is not None
    team_id = _insert_team_entity(workspace_id)

    response = client.post(
        f"/api/v1/engineering/repositories/{uuid4()}/team",
        json={"expected_version": 1, "team_entity_id": str(team_id)},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "REPOSITORY_NOT_FOUND"


def test_assign_repository_team_404_for_unknown_team_entity(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    client, workspace_id, user_id = query_endpoints_test_context
    token = client.cookies.get("ecc_session")
    assert token is not None
    account_id = _insert_connector_account(workspace_id, user_id)
    repository_id = _insert_repository(workspace_id, account_id, name="repo-a")

    response = client.post(
        f"/api/v1/engineering/repositories/{repository_id}/team",
        json={"expected_version": 1, "team_entity_id": str(uuid4())},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TEAM_ENTITY_NOT_FOUND"


def test_assign_repository_team_422_for_wrong_entity_kind(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    """The confirmed link must reference a real `kind="team"` entity, not
    any `pkos_nodes` row -- e.g. confirming a same-named `organization`
    entity by mistake (the exact real-world name-collision risk
    `docs/phases/phase-002/DATA-MODEL.md`'s own disclosed gap already
    names for `team` vs. `organization`/`project`).
    """
    client, workspace_id, user_id = query_endpoints_test_context
    token = client.cookies.get("ecc_session")
    assert token is not None
    account_id = _insert_connector_account(workspace_id, user_id)
    repository_id = _insert_repository(workspace_id, account_id, name="repo-a")
    wrong_kind_entity = _insert_team_entity(workspace_id, name="Acme")
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE pkos_nodes SET node_type = 'organization' WHERE id = :id"),
            {"id": wrong_kind_entity},
        )

    response = client.post(
        f"/api/v1/engineering/repositories/{repository_id}/team",
        json={"expected_version": 1, "team_entity_id": str(wrong_kind_entity)},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "TEAM_ENTITY_KIND_MISMATCH"


def test_assign_repository_team_rejects_cross_workspace_entity(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    client, workspace_id, user_id = query_endpoints_test_context
    token = client.cookies.get("ecc_session")
    assert token is not None
    account_id = _insert_connector_account(workspace_id, user_id)
    repository_id = _insert_repository(workspace_id, account_id, name="repo-a")

    other_workspace_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Other', 'UTC', :now)"
            ),
            {"id": other_workspace_id, "now": now},
        )
    try:
        other_team_id = _insert_team_entity(other_workspace_id)
        response = client.post(
            f"/api/v1/engineering/repositories/{repository_id}/team",
            json={"expected_version": 1, "team_entity_id": str(other_team_id)},
            headers=_headers(token, key=str(uuid4())),
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "TEAM_ENTITY_NOT_FOUND"
    finally:
        _cleanup_workspace(other_workspace_id)


def test_assign_work_item_team_sets_confirmed_link(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    client, workspace_id, user_id = query_endpoints_test_context
    token = client.cookies.get("ecc_session")
    assert token is not None
    account_id = _insert_connector_account(workspace_id, user_id, provider="jira", credential="tok")
    work_item_id = _insert_work_item(workspace_id, account_id, title="item-a")
    team_id = _insert_team_entity(workspace_id)

    response = client.post(
        f"/api/v1/engineering/work-items/{work_item_id}/team",
        json={"expected_version": 1, "team_entity_id": str(team_id)},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["team_entity_id"] == str(team_id)
    assert body["team_assignment_version"] == 2
    assert body["team_assignment_updated_by"] == str(user_id)

    with engine.begin() as connection:
        audit_type = connection.execute(
            text(
                "SELECT event_type FROM audit_events "
                "WHERE workspace_id = :workspace_id AND aggregate_id = :work_item_id"
            ),
            {"workspace_id": workspace_id, "work_item_id": work_item_id},
        ).scalar_one()
    assert audit_type == "engineering_work_item.team_assigned"


def test_assign_work_item_team_404_for_unknown_work_item(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    client, workspace_id, _user_id = query_endpoints_test_context
    token = client.cookies.get("ecc_session")
    assert token is not None
    team_id = _insert_team_entity(workspace_id)

    response = client.post(
        f"/api/v1/engineering/work-items/{uuid4()}/team",
        json={"expected_version": 1, "team_entity_id": str(team_id)},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "WORK_ITEM_NOT_FOUND"


def test_assign_work_item_team_clears_with_null(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    """Mirrors `test_assign_repository_team_clears_with_null` -- proves the
    work-item side's own `UPDATE engineering_work_items SET team_entity_id
    = NULL ...` (table-specific SQL, not shared with the repository
    endpoint) actually clears rather than a copy/paste error leaving it
    untouched or clearing the wrong column.
    """
    client, workspace_id, user_id = query_endpoints_test_context
    token = client.cookies.get("ecc_session")
    assert token is not None
    account_id = _insert_connector_account(workspace_id, user_id, provider="jira", credential="tok")
    team_id = _insert_team_entity(workspace_id)
    work_item_id = _insert_work_item(
        workspace_id, account_id, title="item-a", team_entity_id=team_id
    )

    response = client.post(
        f"/api/v1/engineering/work-items/{work_item_id}/team",
        json={"expected_version": 1, "team_entity_id": None},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 200, response.text
    assert response.json()["team_entity_id"] is None


def test_assign_work_item_team_409_on_stale_expected_version(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    client, workspace_id, user_id = query_endpoints_test_context
    token = client.cookies.get("ecc_session")
    assert token is not None
    account_id = _insert_connector_account(workspace_id, user_id, provider="jira", credential="tok")
    work_item_id = _insert_work_item(workspace_id, account_id, title="item-a")
    team_id = _insert_team_entity(workspace_id)

    response = client.post(
        f"/api/v1/engineering/work-items/{work_item_id}/team",
        json={"expected_version": 99, "team_entity_id": str(team_id)},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "VERSION_CONFLICT"


def test_assign_work_item_team_404_for_unknown_team_entity(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    client, workspace_id, user_id = query_endpoints_test_context
    token = client.cookies.get("ecc_session")
    assert token is not None
    account_id = _insert_connector_account(workspace_id, user_id, provider="jira", credential="tok")
    work_item_id = _insert_work_item(workspace_id, account_id, title="item-a")

    response = client.post(
        f"/api/v1/engineering/work-items/{work_item_id}/team",
        json={"expected_version": 1, "team_entity_id": str(uuid4())},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "TEAM_ENTITY_NOT_FOUND"


def test_assign_work_item_team_422_for_wrong_entity_kind(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    client, workspace_id, user_id = query_endpoints_test_context
    token = client.cookies.get("ecc_session")
    assert token is not None
    account_id = _insert_connector_account(workspace_id, user_id, provider="jira", credential="tok")
    work_item_id = _insert_work_item(workspace_id, account_id, title="item-a")
    wrong_kind_entity = _insert_team_entity(workspace_id, name="Acme")
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE pkos_nodes SET node_type = 'organization' WHERE id = :id"),
            {"id": wrong_kind_entity},
        )

    response = client.post(
        f"/api/v1/engineering/work-items/{work_item_id}/team",
        json={"expected_version": 1, "team_entity_id": str(wrong_kind_entity)},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "TEAM_ENTITY_KIND_MISMATCH"


def test_assign_work_item_team_rejects_cross_workspace_entity(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    client, workspace_id, user_id = query_endpoints_test_context
    token = client.cookies.get("ecc_session")
    assert token is not None
    account_id = _insert_connector_account(workspace_id, user_id, provider="jira", credential="tok")
    work_item_id = _insert_work_item(workspace_id, account_id, title="item-a")

    other_workspace_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Other', 'UTC', :now)"
            ),
            {"id": other_workspace_id, "now": now},
        )
    try:
        other_team_id = _insert_team_entity(other_workspace_id)
        response = client.post(
            f"/api/v1/engineering/work-items/{work_item_id}/team",
            json={"expected_version": 1, "team_entity_id": str(other_team_id)},
            headers=_headers(token, key=str(uuid4())),
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "TEAM_ENTITY_NOT_FOUND"
    finally:
        _cleanup_workspace(other_workspace_id)


def test_assign_work_item_team_rejects_unknown_fields(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    client, workspace_id, user_id = query_endpoints_test_context
    token = client.cookies.get("ecc_session")
    assert token is not None
    account_id = _insert_connector_account(workspace_id, user_id, provider="jira", credential="tok")
    work_item_id = _insert_work_item(workspace_id, account_id, title="item-a")

    response = client.post(
        f"/api/v1/engineering/work-items/{work_item_id}/team",
        json={"expected_version": 1, "team_entity_id": None, "unexpected": "field"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 422


def test_assign_repository_team_rejects_unknown_fields(
    query_endpoints_test_context: tuple[TestClient, UUID, UUID],
) -> None:
    """`TeamAssignmentRequest`'s `model_config = ConfigDict(extra="forbid")`
    -- matches every other write-body model in this codebase.
    """
    client, workspace_id, user_id = query_endpoints_test_context
    token = client.cookies.get("ecc_session")
    assert token is not None
    account_id = _insert_connector_account(workspace_id, user_id)
    repository_id = _insert_repository(workspace_id, account_id, name="repo-a")

    response = client.post(
        f"/api/v1/engineering/repositories/{repository_id}/team",
        json={"expected_version": 1, "team_entity_id": None, "unexpected": "field"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 422
