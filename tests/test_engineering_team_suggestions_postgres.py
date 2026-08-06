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
            connection,
            workspace_id=workspace_id,
            user_id=user_id,
            email=f"{user_id}@example.test",
            now=now,
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
        yield client, workspace_id, user_id, token
    finally:
        client.close()
        with engine.begin() as connection:
            for table in (
                "repositories",
                "engineering_work_items",
                "connector_accounts",
                "event_outbox",
                "audit_events",
                "idempotency_records",
                "pkos_nodes",
                "sessions",
                "users",
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


def _insert_connector_account(
    workspace_id: UUID, user_id: UUID, *, provider: str = "github"
) -> UUID:
    account_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO connector_accounts (id, workspace_id, provider, "
                "external_account_id, display_name, granted_scopes, encrypted_credentials, "
                "status, version, created_at, updated_at, created_by, updated_by, "
                "owner_id, visibility) "
                "VALUES (:id, :workspace_id, :provider, 'ext-1', 'Acct', '{}', "
                "'encrypted', 'active', 1, :now, :now, :user_id, :user_id, :user_id, 'workspace')"
            ),
            {
                "id": account_id,
                "workspace_id": workspace_id,
                "provider": provider,
                "now": now,
                "user_id": user_id,
            },
        )
    return account_id


def _insert_repository(
    workspace_id: UUID,
    connector_account_id: UUID,
    owner_id: UUID,
    *,
    name: str,
    suggested_team_name: str | None,
    team_entity_id: UUID | None = None,
    dismissed: bool = False,
    visibility: str = "workspace",
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
                ":suggested_team_name, :team_entity_id, :dismissed_at, :owner_id, :visibility)"
            ),
            {
                "id": repo_id,
                "workspace_id": workspace_id,
                "connector_account_id": connector_account_id,
                "ext_id": str(repo_id),
                "name": name,
                "source_url": f"https://github.com/{name}",
                "now": now,
                "suggested_team_name": suggested_team_name,
                "team_entity_id": team_entity_id,
                "dismissed_at": now if dismissed else None,
                "owner_id": owner_id,
                "visibility": visibility,
            },
        )
    return repo_id


def _insert_work_item(
    workspace_id: UUID,
    connector_account_id: UUID,
    owner_id: UUID,
    *,
    title: str,
    suggested_team_name: str | None,
    team_entity_id: UUID | None = None,
    dismissed: bool = False,
    visibility: str = "workspace",
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
                ":suggested_team_name, :team_entity_id, :dismissed_at, :owner_id, :visibility)"
            ),
            {
                "id": item_id,
                "workspace_id": workspace_id,
                "connector_account_id": connector_account_id,
                "ext_id": str(item_id),
                "title": title,
                "source_url": f"https://example.test/{item_id}",
                "now": now,
                "suggested_team_name": suggested_team_name,
                "team_entity_id": team_entity_id,
                "dismissed_at": now if dismissed else None,
                "owner_id": owner_id,
                "visibility": visibility,
            },
        )
    return item_id


def _cleanup_workspace(workspace_id: UUID) -> None:
    """Mirrors `test_engineering_query_endpoints_postgres.py`'s own helper
    of the same name -- tears down a second, ad-hoc workspace created
    inline by a cross-workspace-isolation test, since `suggestions_
    context`'s own teardown only ever scopes to the one workspace it
    created.
    """
    with engine.begin() as connection:
        for table in (
            "engineering_work_items",
            "repositories",
            "connector_accounts",
            "pkos_nodes",
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


def test_team_suggestions_groups_by_suggested_name_across_resource_types(
    suggestions_context,
) -> None:
    client, workspace_id, user_id, token = suggestions_context
    account_id = _insert_connector_account(workspace_id, user_id)
    _insert_repository(
        workspace_id, account_id, user_id, name="acme/a", suggested_team_name="Platform"
    )
    _insert_repository(
        workspace_id, account_id, user_id, name="acme/b", suggested_team_name="Platform"
    )
    _insert_repository(
        workspace_id, account_id, user_id, name="acme/c", suggested_team_name="Growth"
    )

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
    _insert_repository(
        workspace_id,
        account_id,
        user_id,
        name="acme/confirmed",
        suggested_team_name="Platform",
        team_entity_id=team_id,
    )
    _insert_repository(
        workspace_id,
        account_id,
        user_id,
        name="acme/dismissed",
        suggested_team_name="Growth",
        dismissed=True,
    )
    _insert_repository(
        workspace_id, account_id, user_id, name="acme/no-suggestion", suggested_team_name=None
    )
    _insert_repository(
        workspace_id, account_id, user_id, name="acme/pending", suggested_team_name="Infra"
    )

    response = client.get("/api/v1/engineering/team-suggestions", headers=_headers(token))
    names = {item["suggested_team_name"] for item in response.json()["items"]}
    assert names == {"Infra"}


def test_team_suggestions_sample_items_cap_is_shared_across_resource_types(
    suggestions_context,
) -> None:
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
            workspace_id,
            account_id,
            user_id,
            name=f"acme/repo-{i}",
            suggested_team_name="Platform",
        )
    for i in range(3):
        _insert_work_item(
            workspace_id,
            account_id,
            user_id,
            title=f"Work item {i}",
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
    """Despite its name, this test previously only ever inserted
    `repositories` rows -- it now also seeds an `engineering_work_items`
    row sharing the same `suggested_team_name`, asserting both tables get
    `team_entity_id`/`team_assignment_version` updated and that the work
    item's `engineering_work_item.team_assigned` audit event is written,
    the same way `assign_repository_team_endpoint`'s own single-item test
    checks `repository.team_assigned`.
    """
    client, workspace_id, user_id, token = suggestions_context
    account_id = _insert_connector_account(workspace_id, user_id)
    team_id = _insert_pkos_team(workspace_id, name="Platform")
    repo_a = _insert_repository(
        workspace_id, account_id, user_id, name="acme/a", suggested_team_name="Platform"
    )
    repo_b = _insert_repository(
        workspace_id, account_id, user_id, name="acme/b", suggested_team_name="Platform"
    )
    work_item_a = _insert_work_item(
        workspace_id, account_id, user_id, title="Item A", suggested_team_name="Platform"
    )
    _insert_repository(
        workspace_id, account_id, user_id, name="acme/other", suggested_team_name="Growth"
    )

    response = client.post(
        "/api/v1/engineering/team-suggestions/confirm",
        json={"suggested_team_name": "Platform", "team_entity_id": str(team_id)},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body["updated"]) == {str(repo_a), str(repo_b), str(work_item_a)}
    assert body["skipped_unauthorized"] == []

    with engine.begin() as connection:
        repo_rows = connection.execute(
            text(
                "SELECT team_entity_id, team_assignment_version FROM repositories "
                "WHERE workspace_id = :workspace_id AND suggested_team_name = 'Platform'"
            ),
            {"workspace_id": workspace_id},
        ).all()
        work_item_rows = connection.execute(
            text(
                "SELECT team_entity_id, team_assignment_version FROM engineering_work_items "
                "WHERE workspace_id = :workspace_id AND suggested_team_name = 'Platform'"
            ),
            {"workspace_id": workspace_id},
        ).all()
        work_item_audit_types = (
            connection.execute(
                text(
                    "SELECT event_type FROM audit_events "
                    "WHERE workspace_id = :workspace_id AND aggregate_id = :aggregate_id"
                ),
                {"workspace_id": workspace_id, "aggregate_id": work_item_a},
            )
            .scalars()
            .all()
        )
    assert all(row.team_entity_id == team_id for row in repo_rows)
    assert all(row.team_assignment_version == 2 for row in repo_rows)
    assert all(row.team_entity_id == team_id for row in work_item_rows)
    assert all(row.team_assignment_version == 2 for row in work_item_rows)
    assert work_item_audit_types == ["engineering_work_item.team_assigned"]


def test_confirm_team_suggestion_is_idempotent_on_replay(suggestions_context) -> None:
    client, workspace_id, user_id, token = suggestions_context
    account_id = _insert_connector_account(workspace_id, user_id)
    team_id = _insert_pkos_team(workspace_id)
    _insert_repository(
        workspace_id, account_id, user_id, name="acme/a", suggested_team_name="Platform"
    )
    key = str(uuid4())
    payload = {"suggested_team_name": "Platform", "team_entity_id": str(team_id)}

    first = client.post(
        "/api/v1/engineering/team-suggestions/confirm",
        json=payload,
        headers=_headers(token, key=key),
    )
    second = client.post(
        "/api/v1/engineering/team-suggestions/confirm",
        json=payload,
        headers=_headers(token, key=key),
    )
    # Compare only business logic fields, not request_id/correlation_id which are added per-request
    first_body = first.json()
    second_body = second.json()
    assert first_body["updated"] == second_body["updated"]
    assert first_body["skipped_unauthorized"] == second_body["skipped_unauthorized"]

    with engine.begin() as connection:
        version = connection.execute(
            text(
                "SELECT team_assignment_version FROM repositories "
                "WHERE workspace_id = :workspace_id"
            ),
            {"workspace_id": workspace_id},
        ).scalar_one()
    assert version == 2  # not bumped twice


def test_confirm_team_suggestion_rejects_non_team_entity(suggestions_context) -> None:
    client, workspace_id, user_id, token = suggestions_context
    account_id = _insert_connector_account(workspace_id, user_id)
    _insert_repository(
        workspace_id, account_id, user_id, name="acme/a", suggested_team_name="Platform"
    )
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


def test_dismiss_team_suggestion_hides_it_without_assigning_team(suggestions_context) -> None:
    client, workspace_id, user_id, token = suggestions_context
    account_id = _insert_connector_account(workspace_id, user_id)
    _insert_repository(
        workspace_id, account_id, user_id, name="acme/a", suggested_team_name="Personal Namespace"
    )

    response = client.post(
        "/api/v1/engineering/team-suggestions/dismiss",
        json={"suggested_team_name": "Personal Namespace"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 200
    assert len(response.json()["updated"]) == 1

    with engine.begin() as connection:
        row = connection.execute(
            text(
                "SELECT team_entity_id, team_suggestion_dismissed_at FROM repositories "
                "WHERE workspace_id = :workspace_id"
            ),
            {"workspace_id": workspace_id},
        ).one()
    assert row.team_entity_id is None
    assert row.team_suggestion_dismissed_at is not None

    list_response = client.get("/api/v1/engineering/team-suggestions", headers=_headers(token))
    assert list_response.json()["items"] == []


def test_dismiss_team_suggestion_is_idempotent_on_replay(suggestions_context) -> None:
    client, workspace_id, user_id, token = suggestions_context
    account_id = _insert_connector_account(workspace_id, user_id)
    _insert_repository(
        workspace_id, account_id, user_id, name="acme/a", suggested_team_name="Personal Namespace"
    )
    key = str(uuid4())
    payload = {"suggested_team_name": "Personal Namespace"}

    first = client.post(
        "/api/v1/engineering/team-suggestions/dismiss",
        json=payload,
        headers=_headers(token, key=key),
    )
    second = client.post(
        "/api/v1/engineering/team-suggestions/dismiss",
        json=payload,
        headers=_headers(token, key=key),
    )
    # Compare only business logic fields, not request_id/correlation_id which are added per-request
    first_body = first.json()
    second_body = second.json()
    assert first_body["updated"] == second_body["updated"]
    assert first_body["skipped_unauthorized"] == second_body["skipped_unauthorized"]


# --- I1 fix: bulk endpoints' main security surface -------------------------


def test_confirm_team_suggestion_never_reveals_private_row_of_another_user(
    suggestions_context,
) -> None:
    """The I1 fix: `_lock_and_authorize_suggestion_candidates`'s candidate
    `SELECT` is now visibility-filtered (`authz.visible_resource_filter_
    sql`, `action="read"`) before it ever locks/authorizes a row -- see
    `authz.authorize`'s docstring (`backend/ecc/platform/authz.py` around
    L798-812) for why calling `action="write"` alone on an invisible row
    and reporting it in `skipped_unauthorized` would leak that the row
    exists. Seeds one repository the acting user owns (visible + writable)
    and one owned by a different workspace member with `visibility=
    'private'` (invisible to the acting user, per `authorize()`'s step 3,
    and no grant exists). The private row must not appear in `updated` NOR
    `skipped_unauthorized` -- it must be indistinguishable from a row that
    doesn't exist at all -- and must remain completely untouched.
    """
    client, workspace_id, user_id, token = suggestions_context
    other_user_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        create_identity(
            connection,
            workspace_id=workspace_id,
            user_id=other_user_id,
            email=f"{other_user_id}@example.test",
            now=now,
        )
    account_id = _insert_connector_account(workspace_id, user_id)
    team_id = _insert_pkos_team(workspace_id, name="Platform")
    accessible = _insert_repository(
        workspace_id, account_id, user_id, name="acme/mine", suggested_team_name="Platform"
    )
    invisible = _insert_repository(
        workspace_id,
        account_id,
        other_user_id,
        name="acme/theirs",
        suggested_team_name="Platform",
        visibility="private",
    )

    response = client.post(
        "/api/v1/engineering/team-suggestions/confirm",
        json={"suggested_team_name": "Platform", "team_entity_id": str(team_id)},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["updated"] == [str(accessible)]
    assert body["skipped_unauthorized"] == []
    assert str(invisible) not in body["updated"]
    assert str(invisible) not in body["skipped_unauthorized"]

    with engine.begin() as connection:
        rows = {
            row.id: row.team_entity_id
            for row in connection.execute(
                text(
                    "SELECT id, team_entity_id FROM repositories WHERE workspace_id = :workspace_id"
                ),
                {"workspace_id": workspace_id},
            ).all()
        }
    assert rows[accessible] == team_id
    assert rows[invisible] is None


def test_dismiss_team_suggestion_never_reveals_private_row_of_another_user(
    suggestions_context,
) -> None:
    """Dismiss sibling of `test_confirm_team_suggestion_never_reveals_
    private_row_of_another_user` -- same visibility setup, exercising
    `/dismiss` instead of `/confirm` since both endpoints share `_lock_
    and_authorize_suggestion_candidates`.
    """
    client, workspace_id, user_id, token = suggestions_context
    other_user_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        create_identity(
            connection,
            workspace_id=workspace_id,
            user_id=other_user_id,
            email=f"{other_user_id}@example.test",
            now=now,
        )
    account_id = _insert_connector_account(workspace_id, user_id)
    accessible = _insert_repository(
        workspace_id,
        account_id,
        user_id,
        name="acme/mine",
        suggested_team_name="Personal Namespace",
    )
    invisible = _insert_repository(
        workspace_id,
        account_id,
        other_user_id,
        name="acme/theirs",
        suggested_team_name="Personal Namespace",
        visibility="private",
    )

    response = client.post(
        "/api/v1/engineering/team-suggestions/dismiss",
        json={"suggested_team_name": "Personal Namespace"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["updated"] == [str(accessible)]
    assert body["skipped_unauthorized"] == []
    assert str(invisible) not in body["updated"]
    assert str(invisible) not in body["skipped_unauthorized"]

    with engine.begin() as connection:
        rows = {
            row.id: row.team_suggestion_dismissed_at
            for row in connection.execute(
                text(
                    "SELECT id, team_suggestion_dismissed_at FROM repositories "
                    "WHERE workspace_id = :workspace_id"
                ),
                {"workspace_id": workspace_id},
            ).all()
        }
    assert rows[accessible] is not None
    assert rows[invisible] is None


def _viewer_actor_context(*, workspace_name: str) -> tuple[TestClient, UUID, UUID, UUID, str]:
    """Sets up a workspace with two identities -- the acting user holding
    `viewer` role (read-only per `_ROLE_PERMISSIONS`, see `authz.py` line
    ~123) and a second, unrelated user -- plus a session for the viewer.
    Returns `(client, workspace_id, viewer_user_id, other_user_id, token)`.
    Caller must call `_cleanup_workspace(workspace_id)` and `client.
    close()` when done.
    """
    workspace_id = uuid4()
    viewer_user_id = uuid4()
    other_user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, :name, 'UTC', :now)"
            ),
            {"id": workspace_id, "name": workspace_name, "now": now},
        )
        create_identity(
            connection,
            workspace_id=workspace_id,
            user_id=viewer_user_id,
            email=f"{viewer_user_id}@example.test",
            now=now,
            role="viewer",
        )
        create_identity(
            connection,
            workspace_id=workspace_id,
            user_id=other_user_id,
            email=f"{other_user_id}@example.test",
            now=now,
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
                "user_id": viewer_user_id,
                "token_hash": sha256(token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "now": now,
            },
        )
    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    return client, workspace_id, viewer_user_id, other_user_id, token


def test_confirm_team_suggestion_skips_readable_but_not_writable_row() -> None:
    """The one case a row can validly appear in `skipped_unauthorized`
    post-I1-fix (unlike an outright invisible row -- see `test_confirm_
    team_suggestion_never_reveals_private_row_of_another_user` -- which
    the fixed candidate `SELECT`'s visibility filter now excludes before
    it is ever locked): the acting user holds `viewer` role in this
    workspace (read-only per `_ROLE_PERMISSIONS`), and seeds one default-
    visibility (`'workspace'`) repository they own themselves (writable
    via `authorize()`'s ownership override, step 2, regardless of role)
    and one `'workspace'`-visibility repository owned by a different user
    (readable -- `visibility='workspace'` plus the viewer's own `read`
    role permission make it visible -- but not writable, since `viewer`
    lacks the `write` role permission and isn't the owner). Confirming
    must update only the owned row and report exactly the other row's id
    in `skipped_unauthorized`.
    """
    client, workspace_id, viewer_user_id, other_user_id, token = _viewer_actor_context(
        workspace_name="Viewer Partial Auth Confirm Test"
    )
    try:
        account_id = _insert_connector_account(workspace_id, viewer_user_id)
        team_id = _insert_pkos_team(workspace_id, name="Platform")
        accessible = _insert_repository(
            workspace_id,
            account_id,
            viewer_user_id,
            name="acme/mine",
            suggested_team_name="Platform",
        )
        readable_only = _insert_repository(
            workspace_id,
            account_id,
            other_user_id,
            name="acme/theirs",
            suggested_team_name="Platform",
        )

        response = client.post(
            "/api/v1/engineering/team-suggestions/confirm",
            json={"suggested_team_name": "Platform", "team_entity_id": str(team_id)},
            headers=_headers(token, key=str(uuid4())),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["updated"] == [str(accessible)]
        assert body["skipped_unauthorized"] == [str(readable_only)]

        with engine.begin() as connection:
            rows = {
                row.id: row.team_entity_id
                for row in connection.execute(
                    text(
                        "SELECT id, team_entity_id FROM repositories "
                        "WHERE workspace_id = :workspace_id"
                    ),
                    {"workspace_id": workspace_id},
                ).all()
            }
        assert rows[accessible] == team_id
        assert rows[readable_only] is None
    finally:
        client.close()
        _cleanup_workspace(workspace_id)


def test_dismiss_team_suggestion_skips_readable_but_not_writable_row() -> None:
    """Dismiss sibling of `test_confirm_team_suggestion_skips_readable_
    but_not_writable_row`.
    """
    client, workspace_id, viewer_user_id, other_user_id, token = _viewer_actor_context(
        workspace_name="Viewer Partial Auth Dismiss Test"
    )
    try:
        account_id = _insert_connector_account(workspace_id, viewer_user_id)
        accessible = _insert_repository(
            workspace_id,
            account_id,
            viewer_user_id,
            name="acme/mine",
            suggested_team_name="Personal Namespace",
        )
        readable_only = _insert_repository(
            workspace_id,
            account_id,
            other_user_id,
            name="acme/theirs",
            suggested_team_name="Personal Namespace",
        )

        response = client.post(
            "/api/v1/engineering/team-suggestions/dismiss",
            json={"suggested_team_name": "Personal Namespace"},
            headers=_headers(token, key=str(uuid4())),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["updated"] == [str(accessible)]
        assert body["skipped_unauthorized"] == [str(readable_only)]

        with engine.begin() as connection:
            rows = {
                row.id: row.team_suggestion_dismissed_at
                for row in connection.execute(
                    text(
                        "SELECT id, team_suggestion_dismissed_at FROM repositories "
                        "WHERE workspace_id = :workspace_id"
                    ),
                    {"workspace_id": workspace_id},
                ).all()
            }
        assert rows[accessible] is not None
        assert rows[readable_only] is None
    finally:
        client.close()
        _cleanup_workspace(workspace_id)


def test_dismiss_team_suggestion_hides_work_items_too(suggestions_context) -> None:
    """`test_dismiss_team_suggestion_hides_it_without_assigning_team` only
    covers `repositories`; this covers `engineering_work_items` sharing
    the same `suggested_team_name`, confirming `team_suggestion_dismissed_
    at` gets set there too.
    """
    client, workspace_id, user_id, token = suggestions_context
    account_id = _insert_connector_account(workspace_id, user_id)
    repo_id = _insert_repository(
        workspace_id,
        account_id,
        user_id,
        name="acme/a",
        suggested_team_name="Personal Namespace",
    )
    work_item_id = _insert_work_item(
        workspace_id,
        account_id,
        user_id,
        title="Item A",
        suggested_team_name="Personal Namespace",
    )

    response = client.post(
        "/api/v1/engineering/team-suggestions/dismiss",
        json={"suggested_team_name": "Personal Namespace"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 200, response.text
    assert set(response.json()["updated"]) == {str(repo_id), str(work_item_id)}

    with engine.begin() as connection:
        repo_row = connection.execute(
            text(
                "SELECT team_entity_id, team_suggestion_dismissed_at FROM repositories "
                "WHERE id = :id"
            ),
            {"id": repo_id},
        ).one()
        work_item_row = connection.execute(
            text(
                "SELECT team_entity_id, team_suggestion_dismissed_at "
                "FROM engineering_work_items WHERE id = :id"
            ),
            {"id": work_item_id},
        ).one()
    assert repo_row.team_entity_id is None
    assert repo_row.team_suggestion_dismissed_at is not None
    assert work_item_row.team_entity_id is None
    assert work_item_row.team_suggestion_dismissed_at is not None


def _seed_other_workspace() -> tuple[UUID, UUID]:
    """Creates a second, fully independent workspace + identity for a
    cross-workspace-isolation test -- caller is responsible for calling
    `_cleanup_workspace` on the returned workspace id afterward.
    """
    other_workspace_id = uuid4()
    other_user_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Other Team Suggestions Workspace', 'UTC', :now)"
            ),
            {"id": other_workspace_id, "now": now},
        )
        create_identity(
            connection,
            workspace_id=other_workspace_id,
            user_id=other_user_id,
            email=f"{other_user_id}@example.test",
            now=now,
        )
    return other_workspace_id, other_user_id


def test_confirm_team_suggestion_does_not_leak_across_workspaces(suggestions_context) -> None:
    """Seeds a matching `suggested_team_name` in workspace A (the acting
    session's own workspace) and workspace B, confirms with workspace A's
    session, and asserts workspace B's row is untouched -- both bulk
    endpoints' candidate `SELECT`/`UPDATE` already scope by `workspace_id`
    (never request-derived), but this had no end-to-end regression test.
    """
    client, workspace_id, user_id, token = suggestions_context
    account_id = _insert_connector_account(workspace_id, user_id)
    team_id = _insert_pkos_team(workspace_id, name="Platform")
    repo_mine = _insert_repository(
        workspace_id, account_id, user_id, name="acme/mine", suggested_team_name="Platform"
    )

    other_workspace_id, other_user_id = _seed_other_workspace()
    try:
        other_account_id = _insert_connector_account(other_workspace_id, other_user_id)
        other_repo = _insert_repository(
            other_workspace_id,
            other_account_id,
            other_user_id,
            name="acme/theirs",
            suggested_team_name="Platform",
        )

        response = client.post(
            "/api/v1/engineering/team-suggestions/confirm",
            json={"suggested_team_name": "Platform", "team_entity_id": str(team_id)},
            headers=_headers(token, key=str(uuid4())),
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["updated"] == [str(repo_mine)]
        assert body["skipped_unauthorized"] == []

        with engine.begin() as connection:
            other_row = connection.execute(
                text("SELECT team_entity_id FROM repositories WHERE id = :id"),
                {"id": other_repo},
            ).one()
        assert other_row.team_entity_id is None
    finally:
        _cleanup_workspace(other_workspace_id)


def test_dismiss_team_suggestion_does_not_leak_across_workspaces(suggestions_context) -> None:
    """Dismiss sibling of `test_confirm_team_suggestion_does_not_leak_
    across_workspaces`.
    """
    client, workspace_id, user_id, token = suggestions_context
    account_id = _insert_connector_account(workspace_id, user_id)
    repo_mine = _insert_repository(
        workspace_id,
        account_id,
        user_id,
        name="acme/mine",
        suggested_team_name="Personal Namespace",
    )

    other_workspace_id, other_user_id = _seed_other_workspace()
    try:
        other_account_id = _insert_connector_account(other_workspace_id, other_user_id)
        other_repo = _insert_repository(
            other_workspace_id,
            other_account_id,
            other_user_id,
            name="acme/theirs",
            suggested_team_name="Personal Namespace",
        )

        response = client.post(
            "/api/v1/engineering/team-suggestions/dismiss",
            json={"suggested_team_name": "Personal Namespace"},
            headers=_headers(token, key=str(uuid4())),
        )
        assert response.status_code == 200, response.text
        assert response.json()["updated"] == [str(repo_mine)]

        with engine.begin() as connection:
            other_row = connection.execute(
                text("SELECT team_suggestion_dismissed_at FROM repositories WHERE id = :id"),
                {"id": other_repo},
            ).one()
        assert other_row.team_suggestion_dismissed_at is None
    finally:
        _cleanup_workspace(other_workspace_id)
