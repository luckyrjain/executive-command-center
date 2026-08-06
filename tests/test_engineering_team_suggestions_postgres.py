"""Team suggestions review page (`docs/superpowers/specs/2026-08-06-team-
suggestions-review-page-design.md`) -- `GET /api/v1/engineering/team-
suggestions` aggregation, `POST .../confirm`, `POST .../dismiss`.
"""

from __future__ import annotations

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


@pytest.fixture
def suggestions_context():
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Team Suggestions Test', 'UTC', :now)"
            ),
            {"id": workspace_id, "now": now},
        )
        create_identity(
            connection, workspace_id=workspace_id, user_id=user_id,
            email=f"{user_id}@example.test", now=now,
        )
        connection.execute(
            text(
                "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                "expires_at, last_seen_at) "
                "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :now)"
            ),
            {
                "id": uuid4(), "workspace_id": workspace_id, "user_id": user_id,
                "token_hash": sha256(token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1), "now": now,
            },
        )

    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    try:
        yield client, workspace_id, user_id, token
    finally:
        client.close()
        with engine.begin() as connection:
            for table in (
                "repositories", "engineering_work_items", "connector_accounts",
                "event_outbox", "audit_events", "idempotency_records",
                "pkos_nodes", "sessions", "users",
            ):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                    {"workspace_id": workspace_id},
                )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": workspace_id},
            )


def _headers(token: str, key: str | None = None) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    headers = {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _insert_connector_account(workspace_id: UUID, user_id: UUID, *, provider: str = "github") -> UUID:
    account_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO connector_accounts (id, workspace_id, provider, "
                "external_account_id, display_name, granted_scopes, encrypted_credentials, "
                "status, version, created_at, updated_at, created_by, updated_by, owner_id, visibility) "
                "VALUES (:id, :workspace_id, :provider, 'ext-1', 'Acct', '{}', "
                "'encrypted', 'active', 1, :now, :now, :user_id, :user_id, :user_id, 'workspace')"
            ),
            {"id": account_id, "workspace_id": workspace_id, "provider": provider, "now": now, "user_id": user_id},
        )
    return account_id


def _insert_repository(
    workspace_id: UUID, connector_account_id: UUID, owner_id: UUID, *,
    name: str, suggested_team_name: str | None,
    team_entity_id: UUID | None = None, dismissed: bool = False,
) -> UUID:
    repo_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO repositories (id, workspace_id, connector_account_id, "
                "provider, external_id, name, source_url, permission_state, "
                "freshness_state, observed_at, created_at, updated_at, "
                "suggested_team_name, team_entity_id, team_suggestion_dismissed_at, "
                "owner_id, visibility) "
                "VALUES (:id, :workspace_id, :connector_account_id, 'github', :ext_id, "
                ":name, :source_url, 'active', 'fresh', :now, :now, :now, "
                ":suggested_team_name, :team_entity_id, :dismissed_at, :owner_id, 'workspace')"
            ),
            {
                "id": repo_id, "workspace_id": workspace_id,
                "connector_account_id": connector_account_id,
                "ext_id": str(repo_id), "name": name,
                "source_url": f"https://github.com/{name}", "now": now,
                "suggested_team_name": suggested_team_name, "team_entity_id": team_entity_id,
                "dismissed_at": now if dismissed else None, "owner_id": owner_id,
            },
        )
    return repo_id


def _insert_work_item(
    workspace_id: UUID, connector_account_id: UUID, owner_id: UUID, *,
    title: str, suggested_team_name: str | None,
    team_entity_id: UUID | None = None, dismissed: bool = False,
) -> UUID:
    item_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO engineering_work_items (id, workspace_id, connector_account_id, "
                "provider, external_id, title, source_url, permission_state, "
                "freshness_state, observed_at, created_at, updated_at, "
                "suggested_team_name, team_entity_id, team_suggestion_dismissed_at, "
                "owner_id, visibility) "
                "VALUES (:id, :workspace_id, :connector_account_id, 'jira', :ext_id, "
                ":title, :source_url, 'active', 'fresh', :now, :now, :now, "
                ":suggested_team_name, :team_entity_id, :dismissed_at, :owner_id, 'workspace')"
            ),
            {
                "id": item_id, "workspace_id": workspace_id,
                "connector_account_id": connector_account_id,
                "ext_id": str(item_id), "title": title,
                "source_url": f"https://example.test/{item_id}", "now": now,
                "suggested_team_name": suggested_team_name, "team_entity_id": team_entity_id,
                "dismissed_at": now if dismissed else None, "owner_id": owner_id,
            },
        )
    return item_id


def _insert_pkos_team(workspace_id: UUID, *, name: str = "Platform") -> UUID:
    team_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO pkos_nodes (id, workspace_id, node_type, canonical_name, "
                "status, confidence, version, created_at, updated_at) "
                "VALUES (:id, :workspace_id, 'team', :name, 'active', 1.0, 1, :now, :now)"
            ),
            {"id": team_id, "workspace_id": workspace_id, "name": name, "now": now},
        )
    return team_id


def test_team_suggestions_groups_by_suggested_name_across_resource_types(suggestions_context) -> None:
    client, workspace_id, user_id, token = suggestions_context
    account_id = _insert_connector_account(workspace_id, user_id)
    _insert_repository(workspace_id, account_id, user_id, name="acme/a", suggested_team_name="Platform")
    _insert_repository(workspace_id, account_id, user_id, name="acme/b", suggested_team_name="Platform")
    _insert_repository(workspace_id, account_id, user_id, name="acme/c", suggested_team_name="Growth")

    response = client.get("/api/v1/engineering/team-suggestions", headers=_headers(token))
    assert response.status_code == 200
    items = {item["suggested_team_name"]: item for item in response.json()["items"]}
    assert items["Platform"]["repository_count"] == 2
    assert items["Platform"]["work_item_count"] == 0
    assert items["Growth"]["repository_count"] == 1
    assert len(items["Platform"]["sample_items"]) == 2


def test_team_suggestions_excludes_confirmed_dismissed_and_null_names(suggestions_context) -> None:
    client, workspace_id, user_id, token = suggestions_context
    account_id = _insert_connector_account(workspace_id, user_id)
    team_id = _insert_pkos_team(workspace_id)
    _insert_repository(workspace_id, account_id, user_id, name="acme/confirmed", suggested_team_name="Platform", team_entity_id=team_id)
    _insert_repository(workspace_id, account_id, user_id, name="acme/dismissed", suggested_team_name="Growth", dismissed=True)
    _insert_repository(workspace_id, account_id, user_id, name="acme/no-suggestion", suggested_team_name=None)
    _insert_repository(workspace_id, account_id, user_id, name="acme/pending", suggested_team_name="Infra")

    response = client.get("/api/v1/engineering/team-suggestions", headers=_headers(token))
    names = {item["suggested_team_name"] for item in response.json()["items"]}
    assert names == {"Infra"}


def test_team_suggestions_sample_items_cap_is_shared_across_resource_types(suggestions_context) -> None:
    """`sample_items` must be capped at 5 *combined* across `repository`
    and `work_item` rows, not 5 per resource type. 3 repositories + 3 work
    items sharing a `suggested_team_name` produce 6 matching rows total;
    the cap must trim that to 5, and -- since repository rows are
    processed first -- the 5 kept samples must include both resource
    types (3 repositories + 2 work items), proving the cap is shared
    rather than reset per loop.
    """
    client, workspace_id, user_id, token = suggestions_context
    account_id = _insert_connector_account(workspace_id, user_id)
    for i in range(3):
        _insert_repository(
            workspace_id, account_id, user_id, name=f"acme/repo-{i}",
            suggested_team_name="Platform",
        )
    for i in range(3):
        _insert_work_item(
            workspace_id, account_id, user_id, title=f"Work item {i}",
            suggested_team_name="Platform",
        )

    response = client.get("/api/v1/engineering/team-suggestions", headers=_headers(token))
    assert response.status_code == 200
    items = {item["suggested_team_name"]: item for item in response.json()["items"]}
    platform = items["Platform"]
    assert platform["repository_count"] == 3
    assert platform["work_item_count"] == 3
    assert len(platform["sample_items"]) == 5

    resource_types = {sample["resource_type"] for sample in platform["sample_items"]}
    assert resource_types == {"repository", "work_item"}


def test_confirm_team_suggestion_bulk_assigns_across_resource_types(suggestions_context) -> None:
    client, workspace_id, user_id, token = suggestions_context
    account_id = _insert_connector_account(workspace_id, user_id)
    team_id = _insert_pkos_team(workspace_id, name="Platform")
    repo_a = _insert_repository(workspace_id, account_id, user_id, name="acme/a", suggested_team_name="Platform")
    repo_b = _insert_repository(workspace_id, account_id, user_id, name="acme/b", suggested_team_name="Platform")
    _insert_repository(workspace_id, account_id, user_id, name="acme/other", suggested_team_name="Growth")

    response = client.post(
        "/api/v1/engineering/team-suggestions/confirm",
        json={"suggested_team_name": "Platform", "team_entity_id": str(team_id)},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body["updated"]) == {str(repo_a), str(repo_b)}
    assert body["skipped_unauthorized"] == []

    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT team_entity_id, team_assignment_version FROM repositories "
                "WHERE workspace_id = :workspace_id AND suggested_team_name = 'Platform'"
            ),
            {"workspace_id": workspace_id},
        ).all()
    assert all(row.team_entity_id == team_id for row in rows)
    assert all(row.team_assignment_version == 2 for row in rows)


def test_confirm_team_suggestion_is_idempotent_on_replay(suggestions_context) -> None:
    client, workspace_id, user_id, token = suggestions_context
    account_id = _insert_connector_account(workspace_id, user_id)
    team_id = _insert_pkos_team(workspace_id)
    _insert_repository(workspace_id, account_id, user_id, name="acme/a", suggested_team_name="Platform")
    key = str(uuid4())
    payload = {"suggested_team_name": "Platform", "team_entity_id": str(team_id)}

    first = client.post(
        "/api/v1/engineering/team-suggestions/confirm", json=payload, headers=_headers(token, key=key)
    )
    second = client.post(
        "/api/v1/engineering/team-suggestions/confirm", json=payload, headers=_headers(token, key=key)
    )
    # Compare only business logic fields, not request_id/correlation_id which are added per-request
    first_body = first.json()
    second_body = second.json()
    assert first_body["updated"] == second_body["updated"]
    assert first_body["skipped_unauthorized"] == second_body["skipped_unauthorized"]

    with engine.begin() as connection:
        version = connection.execute(
            text(
                "SELECT team_assignment_version FROM repositories WHERE workspace_id = :workspace_id"
            ),
            {"workspace_id": workspace_id},
        ).scalar_one()
    assert version == 2  # not bumped twice


def test_confirm_team_suggestion_rejects_non_team_entity(suggestions_context) -> None:
    client, workspace_id, user_id, token = suggestions_context
    account_id = _insert_connector_account(workspace_id, user_id)
    _insert_repository(workspace_id, account_id, user_id, name="acme/a", suggested_team_name="Platform")
    not_a_team_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO pkos_nodes (id, workspace_id, node_type, canonical_name, "
                "status, confidence, version, created_at, updated_at) "
                "VALUES (:id, :workspace_id, 'person', 'Not A Team', 'active', 1.0, 1, :now, :now)"
            ),
            {"id": not_a_team_id, "workspace_id": workspace_id, "now": now},
        )

    response = client.post(
        "/api/v1/engineering/team-suggestions/confirm",
        json={"suggested_team_name": "Platform", "team_entity_id": str(not_a_team_id)},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "TEAM_ENTITY_KIND_MISMATCH"
