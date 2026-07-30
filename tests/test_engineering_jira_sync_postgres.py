"""Phase 6 Engineering Workspace Task 4: Jira work-item sync
(`docs/superpowers/plans/2026-07-27-phase-6-engineering-workspace.md`,
`backend/ecc/domains/engineering/jira_adapter.py`).

Covers, per this task's own scope ("Jira work-item sync -- work items
only, Jira is not a source-control provider"):

1. `JiraAdapter.authorize`: success (parses `accountId`/`displayName`
   from `GET /rest/api/3/myself`, always returns an empty `granted_
   scopes` -- see `jira_adapter.py`'s own module docstring for why a
   personal API token has no scope grant to verify at all, not even
   GitHub's degraded fine-grained-PAT case), a malformed credential
   (not `site|email|api_token`) rejection, 401 rejection, non-200
   rejection, network-error rejection.
2. `JiraAdapter._sync_work_items` (`httpx.MockTransport` for the Jira API
   call; a real seeded workspace/connector_account row is still required,
   since `_upsert_work_item` genuinely writes to `engineering_work_
   items`): single page, offset-based pagination (`startAt`/`maxResults`/
   `total`, not a `Link` header), the incremental-cursor stop-early
   condition, rate-limit handling succeeding after a bounded wait, giving
   up beyond it (`partial`), giving up the same way when the one retry is
   itself still rate-limited, and the `_MAX_PAGES_PER_CALL` bound
   reporting `partial` rather than a silent `succeeded`.
3. `refresh_permissions`/`disconnect`(documented no-op)/`handle_webhook`
   contract coverage.
4. End-to-end through the real `/sync` endpoint (monkeypatched registry
   substituting a mock-transport `JiraAdapter` for `"jira"`): backfill
   writes `engineering_work_items` rows correctly, an incremental sync
   afterward only upserts genuinely newer rows, and a rate-limited sync
   reports `partial` without moving the connector account to `error`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from json import dumps
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import engine
from ecc.domains.engineering import connector_accounts as connector_accounts_module
from ecc.domains.engineering.connectors import (
    AdapterAuthorizationError,
    ConnectorAccountContext,
    ConnectorRegistry,
)
from ecc.domains.engineering.crypto import encrypt_credential
from ecc.domains.engineering.jira_adapter import JiraAdapter
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_SITE = "acme.atlassian.net"
_CREDENTIAL = f"{_SITE}|jane@acme.test|jira-api-token"


_UNSET: dict[str, Any] = {}


def _issue(
    issue_id: int,
    *,
    key: str,
    summary: str,
    updated: str,
    status: str = "In Progress",
    assignee: dict[str, Any] | None = _UNSET,
    reporter: dict[str, Any] | None = _UNSET,
    project: dict[str, Any] | None = None,
) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "summary": summary,
        "issuetype": {"name": "Bug"},
        "status": {"name": status},
        "reporter": {"accountId": "reporter-1"} if reporter is _UNSET else reporter,
        "assignee": {"accountId": "assignee-1"} if assignee is _UNSET else assignee,
        "updated": updated,
    }
    if project is not None:
        fields["project"] = project
    return {"id": issue_id, "key": key, "fields": fields}


def _search_response(
    issues: list[dict[str, Any]], *, start_at: int = 0, total: int | None = None
) -> dict[str, Any]:
    return {
        "issues": issues,
        "startAt": start_at,
        "maxResults": 100,
        "total": total if total is not None else start_at + len(issues),
    }


def _json_response(
    body: Any, *, status_code: int = 200, headers: dict[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        headers={"content-type": "application/json", **(headers or {})},
        content=dumps(body),
    )


# --- unit-level: JiraAdapter.authorize --------------------------------------


def test_jira_adapter_authorize_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == _SITE
        assert request.url.path == "/rest/api/3/myself"
        assert request.headers["Authorization"].startswith("Basic ")
        return _json_response({"accountId": "acc-1", "displayName": "Jane Doe"})

    adapter = JiraAdapter(transport=httpx.MockTransport(handler))
    authorization = adapter.authorize(_CREDENTIAL)
    assert authorization.external_account_id == "acc-1"
    assert authorization.display_name == "Jane Doe"
    # No scope grant exists for a personal API token to report -- always
    # honestly empty, never a fabricated match against some required set.
    assert authorization.granted_scopes == frozenset()


def test_jira_adapter_authorize_rejects_malformed_credential() -> None:
    adapter = JiraAdapter(transport=httpx.MockTransport(lambda r: _json_response({})))
    with pytest.raises(AdapterAuthorizationError, match="site\\|email\\|api_token"):
        adapter.authorize("not-the-right-shape")


def test_jira_adapter_authorize_rejects_empty_credential_segment() -> None:
    adapter = JiraAdapter(transport=httpx.MockTransport(lambda r: _json_response({})))
    with pytest.raises(AdapterAuthorizationError, match="site\\|email\\|api_token"):
        adapter.authorize("|jane@acme.test|jira-api-token")


def test_jira_adapter_authorize_rejects_non_atlassian_site() -> None:
    """`site` becomes the host of every request URL this adapter builds --
    unlike GitHub/GitLab's hardcoded base URLs, it flows straight from
    operator-supplied credential input. A `site` that isn't a genuine
    `{subdomain}.atlassian.net` Jira Cloud host must be rejected outright
    rather than letting this adapter issue an outbound request to it (an
    SSRF risk an internal/arbitrary host would otherwise open up).
    """
    adapter = JiraAdapter(transport=httpx.MockTransport(lambda r: _json_response({})))
    with pytest.raises(AdapterAuthorizationError, match="atlassian.net"):
        adapter.authorize("internal.example.test|jane@acme.test|jira-api-token")


def test_jira_adapter_authorize_rejects_subdomain_confusion_site() -> None:
    """A `site` crafted to end in the real `atlassian.net` suffix as a
    substring (rather than being a genuine `atlassian.net` subdomain) must
    still be rejected -- the pattern's anchoring is the entire SSRF
    protection, so this specifically exercises the anchoring rather than
    just "some non-atlassian host is rejected."
    """
    adapter = JiraAdapter(transport=httpx.MockTransport(lambda r: _json_response({})))
    with pytest.raises(AdapterAuthorizationError, match="atlassian.net"):
        adapter.authorize("acme.atlassian.net.evil.test|jane@acme.test|jira-api-token")


def test_backfill_rejects_non_atlassian_site_too() -> None:
    """The SSRF guard lives in `_parse_credential`, called from every
    URL-building path, not just `authorize` -- proves `backfill` (via
    `_sync_work_items`) also refuses to issue a request to a malicious
    `site` rather than the check only guarding the authorize call.
    """
    adapter = JiraAdapter(transport=httpx.MockTransport(lambda r: _json_response({})))
    context = ConnectorAccountContext(
        workspace_id=uuid4(),
        connector_account_id=uuid4(),
        external_account_id="acc-1",
        credential="internal.example.test|jane@acme.test|jira-api-token",
    )
    with pytest.raises(RuntimeError, match="atlassian.net"):
        adapter.backfill(context, "work_item")


def test_jira_adapter_authorize_rejects_401() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"message": "Unauthorized"}, status_code=401)

    adapter = JiraAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(AdapterAuthorizationError):
        adapter.authorize(_CREDENTIAL)


def test_jira_adapter_authorize_rejects_non_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"message": "Server error"}, status_code=500)

    adapter = JiraAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(AdapterAuthorizationError):
        adapter.authorize(_CREDENTIAL)


def test_jira_adapter_authorize_rejects_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    adapter = JiraAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(AdapterAuthorizationError):
        adapter.authorize(_CREDENTIAL)


# --- unit-level: work-item sync (no database) -------------------------------


def _account_context() -> ConnectorAccountContext:
    """A context backed by no real database rows -- only safe to use with
    an adapter call that never reaches `_upsert_work_item` (an
    unregistered/non-work-item resource type, an authorize/refresh_
    permissions/disconnect call, or a rate-limited call that gives up
    before ever writing a row).
    """
    return ConnectorAccountContext(
        workspace_id=uuid4(),
        connector_account_id=uuid4(),
        external_account_id="acc-1",
        credential=_CREDENTIAL,
    )


@pytest.fixture
def seeded_account_context() -> Iterator[ConnectorAccountContext]:
    """A context backed by a real `workspaces`/`users`/`connector_accounts`
    row set -- required for any adapter call that reaches `_upsert_work_
    item`, which genuinely writes to `engineering_work_items` (FK'd to
    `connector_accounts`/`workspaces`) rather than being itself mocked.
    """
    workspace_id = uuid4()
    user_id = uuid4()
    account_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Jira Adapter Unit Test', 'UTC', :now)"
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
                """
                INSERT INTO connector_accounts (
                    id, workspace_id, provider, external_account_id, display_name,
                    granted_scopes, encrypted_credentials, status, version,
                    created_by, updated_by, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, 'jira', 'jira-unit-test', 'Jira unit test',
                    ARRAY[]::text[], :encrypted, 'active', 1,
                    :actor_id, :actor_id, :now, :now
                )
                """
            ),
            {
                "id": account_id,
                "workspace_id": workspace_id,
                "encrypted": encrypt_credential(_CREDENTIAL),
                "actor_id": user_id,
                "now": now,
            },
        )
    try:
        yield ConnectorAccountContext(
            workspace_id=workspace_id,
            connector_account_id=account_id,
            external_account_id="jira-unit-test",
            credential=_CREDENTIAL,
        )
    finally:
        with engine.begin() as connection:
            for table in ("engineering_work_items", "connector_accounts", "users"):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                    {"workspace_id": workspace_id},
                )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": workspace_id},
            )


def test_backfill_single_page(seeded_account_context: ConnectorAccountContext) -> None:
    issues = [
        _issue(1, key="ACME-1", summary="First bug", updated="2024-01-03T00:00:00.000+0000"),
        _issue(2, key="ACME-2", summary="Second bug", updated="2024-01-02T00:00:00.000+0000"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rest/api/3/search"
        return _json_response(_search_response(issues))

    adapter = JiraAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(seeded_account_context, "work_item")
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 2
    assert outcome.next_cursor == "2024-01-03T00:00:00.000+0000"


def test_backfill_populates_suggested_team_name_from_project_name(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    """Migration `0050_phase6_team_linkage.py`'s "hybrid: auto-suggest,
    human confirms" design -- Jira has no native "team" construct at all,
    so `_upsert_work_item` uses the issue's own `fields.project.name` as
    the closest available heuristic (an explicit, disclosed heuristic --
    see that migration's own docstring and `CONNECTOR-CONTRACT.md`).
    `_SEARCH_FIELDS` now requests `project` too, so this also proves that
    field reaches the JQL search request.
    """
    issues = [
        _issue(
            1,
            key="ACME-1",
            summary="First bug",
            updated="2024-01-03T00:00:00.000+0000",
            project={"key": "ACME", "name": "Acme Platform"},
        )
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/rest/api/3/search":
            assert "project" in request.url.params.get("fields", "")
        return _json_response(_search_response(issues))

    adapter = JiraAdapter(transport=httpx.MockTransport(handler))
    adapter.backfill(seeded_account_context, "work_item")

    with engine.begin() as connection:
        suggested = connection.execute(
            text(
                "SELECT suggested_team_name FROM engineering_work_items "
                "WHERE workspace_id = :workspace_id"
            ),
            {"workspace_id": seeded_account_context.workspace_id},
        ).scalar_one()
    assert suggested == "Acme Platform"


def test_backfill_suggested_team_name_is_none_without_project_field(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    """A real Jira issue payload missing `fields.project` entirely (rather
    than an empty object) must not raise -- `_upsert_work_item` degrades to
    `suggested_team_name IS NULL` instead, mirroring the analogous
    without-owner/without-namespace tests GitHub/GitLab already have for
    this same fallback chain.
    """
    issues = [_issue(1, key="ACME-1", summary="First bug", updated="2024-01-03T00:00:00.000+0000")]

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(_search_response(issues))

    adapter = JiraAdapter(transport=httpx.MockTransport(handler))
    adapter.backfill(seeded_account_context, "work_item")

    with engine.begin() as connection:
        suggested = connection.execute(
            text(
                "SELECT suggested_team_name FROM engineering_work_items "
                "WHERE workspace_id = :workspace_id"
            ),
            {"workspace_id": seeded_account_context.workspace_id},
        ).scalar_one()
    assert suggested is None


def test_incremental_resync_refreshes_suggestion_without_touching_confirmed_team(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    """A confirmed `team_entity_id` (set only through `POST .../work-items/
    {id}/team`, never by a sync) must survive indefinitely across re-syncs,
    even as `suggested_team_name` keeps refreshing from the provider's own
    latest payload -- the same regression `github_adapter`'s own test file
    proves, exercised here since `_upsert_work_item`'s `ON CONFLICT ... DO
    UPDATE` clause is authored per-adapter, not shared.
    """
    issues = [
        _issue(
            1,
            key="ACME-1",
            summary="First bug",
            updated="2024-01-03T00:00:00.000+0000",
            project={"key": "ACME", "name": "Acme Platform"},
        )
    ]
    adapter = JiraAdapter(
        transport=httpx.MockTransport(lambda r: _json_response(_search_response(issues)))
    )
    adapter.backfill(seeded_account_context, "work_item")

    confirmed_team_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO pkos_nodes (id, workspace_id, node_type, canonical_name, "
                "status, confidence, version, created_at, updated_at) "
                "VALUES (:id, :workspace_id, 'team', 'Platform', 'active', 1.0, 1, :now, :now)"
            ),
            {
                "id": confirmed_team_id,
                "workspace_id": seeded_account_context.workspace_id,
                "now": datetime.now(UTC),
            },
        )
        connection.execute(
            text(
                "UPDATE engineering_work_items SET team_entity_id = :team_id "
                "WHERE workspace_id = :workspace_id"
            ),
            {"team_id": confirmed_team_id, "workspace_id": seeded_account_context.workspace_id},
        )

    issues_renamed = [
        _issue(
            1,
            key="ACME-1",
            summary="First bug",
            updated="2024-01-04T00:00:00.000+0000",
            project={"key": "ACME", "name": "Acme Platform Renamed"},
        )
    ]
    adapter2 = JiraAdapter(
        transport=httpx.MockTransport(lambda r: _json_response(_search_response(issues_renamed)))
    )
    adapter2.incremental_sync(seeded_account_context, "work_item", "2024-01-03T00:00:00.000+0000")

    try:
        with engine.begin() as connection:
            row = connection.execute(
                text(
                    "SELECT suggested_team_name, team_entity_id FROM engineering_work_items "
                    "WHERE workspace_id = :workspace_id"
                ),
                {"workspace_id": seeded_account_context.workspace_id},
            ).one()
        assert row.suggested_team_name == "Acme Platform Renamed"
        assert row.team_entity_id == confirmed_team_id
    finally:
        # `seeded_account_context`'s own teardown doesn't know about the
        # `pkos_nodes` row this test inserted directly -- clearing
        # `engineering_work_items.team_entity_id` first, then the node
        # itself, avoids leaving the fixture's own `DELETE FROM workspaces`
        # blocked by the FK this test's own `UPDATE` created.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE engineering_work_items SET team_entity_id = NULL "
                    "WHERE workspace_id = :workspace_id"
                ),
                {"workspace_id": seeded_account_context.workspace_id},
            )
            connection.execute(
                text("DELETE FROM pkos_nodes WHERE id = :id"), {"id": confirmed_team_id}
            )


def test_backfill_paginates_via_offset(seeded_account_context: ConnectorAccountContext) -> None:
    page1 = [_issue(1, key="ACME-1", summary="First", updated="2024-01-05T00:00:00.000+0000")]
    page2 = [_issue(2, key="ACME-2", summary="Second", updated="2024-01-04T00:00:00.000+0000")]

    def handler(request: httpx.Request) -> httpx.Response:
        start_at = int(request.url.params["startAt"])
        if start_at == 0:
            return _json_response(_search_response(page1, start_at=0, total=2))
        if start_at == 1:
            return _json_response(_search_response(page2, start_at=1, total=2))
        raise AssertionError(f"unexpected startAt {start_at}")

    adapter = JiraAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(seeded_account_context, "work_item")
    assert outcome.items_processed == 2
    assert outcome.next_cursor == "2024-01-05T00:00:00.000+0000"


def test_backfill_writes_null_assignee_and_reporter_for_unassigned_issue(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    """Jira issues are routinely unassigned/unreported -- `fields.assignee`/
    `fields.reporter` come back as JSON `null`, not merely absent, on a real
    Jira response. `_upsert_work_item`'s defensive `fields.get(...) or {}`
    handling must actually be exercised by a real null value, not just
    assumed correct because every other test's fixture always supplies one.
    """
    issue = _issue(
        1,
        key="ACME-1",
        summary="Unassigned bug",
        updated="2024-01-01T00:00:00.000+0000",
        assignee=None,
        reporter=None,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(_search_response([issue]))

    adapter = JiraAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(seeded_account_context, "work_item")
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1

    with engine.begin() as connection:
        row = connection.execute(
            text(
                "SELECT reporter_external_id, assignee_external_id "
                "FROM engineering_work_items WHERE workspace_id = :workspace_id"
            ),
            {"workspace_id": seeded_account_context.workspace_id},
        ).one()
    assert row.reporter_external_id is None
    assert row.assignee_external_id is None


def test_backfill_then_resync_with_changed_title_updates_row_and_content_hash(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    """Proves `_upsert_work_item`'s `ON CONFLICT` clause actually updates an
    existing row (rather than silently no-op'ing) and that `_content_hash`
    genuinely changes when a real content field (title) changes -- not just
    that a first-time insert works.
    """
    first = _issue(
        1, key="ACME-1", summary="Original title", updated="2024-01-01T00:00:00.000+0000"
    )

    def first_handler(request: httpx.Request) -> httpx.Response:
        return _json_response(_search_response([first]))

    adapter = JiraAdapter(transport=httpx.MockTransport(first_handler))
    adapter.backfill(seeded_account_context, "work_item")

    with engine.begin() as connection:
        before = connection.execute(
            text(
                "SELECT title, content_hash FROM engineering_work_items "
                "WHERE workspace_id = :workspace_id"
            ),
            {"workspace_id": seeded_account_context.workspace_id},
        ).one()

    renamed = _issue(
        1, key="ACME-1", summary="Renamed title", updated="2024-01-02T00:00:00.000+0000"
    )

    def second_handler(request: httpx.Request) -> httpx.Response:
        return _json_response(_search_response([renamed]))

    adapter2 = JiraAdapter(transport=httpx.MockTransport(second_handler))
    adapter2.incremental_sync(
        seeded_account_context, "work_item", cursor="2024-01-01T00:00:00.000+0000"
    )

    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT title, content_hash FROM engineering_work_items "
                "WHERE workspace_id = :workspace_id"
            ),
            {"workspace_id": seeded_account_context.workspace_id},
        ).all()
    assert len(rows) == 1
    after = rows[0]
    assert after.title == "Renamed title"
    assert before.title == "Original title"
    assert after.content_hash != before.content_hash


def test_incremental_sync_stops_at_prior_cursor(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    issues = [
        _issue(1, key="ACME-1", summary="First", updated="2024-01-05T00:00:00.000+0000"),
        _issue(2, key="ACME-2", summary="Second", updated="2024-01-01T00:00:00.000+0000"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(_search_response(issues, total=100))

    adapter = JiraAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.incremental_sync(
        seeded_account_context, "work_item", cursor="2024-01-02T00:00:00.000+0000"
    )
    assert outcome.items_processed == 1
    assert outcome.next_cursor == "2024-01-05T00:00:00.000+0000"


def test_incremental_sync_cursor_comparison_is_dst_safe(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    """A regression lock for the fix comparing `fields.updated` as parsed,
    timezone-aware datetimes rather than raw strings. A Jira site observing
    US Eastern's real 2024-11-03 fall-back DST transition can serialize a
    genuinely *later* instant with a *smaller* offset (`-0500` after the
    transition vs `-0400` before it) such that the two timestamp strings
    sort in the wrong order lexicographically: "01:15...-0500" (06:15 UTC)
    is a lexicographically smaller string than "01:30...-0400" (05:30 UTC)
    even though it is chronologically 45 minutes later. Under the old raw-
    string comparison this issue would have been incorrectly treated as
    not-newer-than-cursor and silently dropped; the fix must still process
    it and must still report it as the new cursor.
    """
    cursor = "2024-11-03T01:30:00.000-0400"
    later_issue_smaller_string = _issue(
        1, key="ACME-1", summary="Genuinely later", updated="2024-11-03T01:15:00.000-0500"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(_search_response([later_issue_smaller_string], total=1))

    adapter = JiraAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.incremental_sync(seeded_account_context, "work_item", cursor=cursor)
    assert outcome.items_processed == 1
    assert outcome.next_cursor == "2024-11-03T01:15:00.000-0500"


def test_incremental_sync_with_no_cursor_behaves_like_backfill(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    issues = [_issue(1, key="ACME-1", summary="First", updated="2024-01-01T00:00:00.000+0000")]

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(_search_response(issues))

    adapter = JiraAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.incremental_sync(seeded_account_context, "work_item", cursor=None)
    assert outcome.items_processed == 1


def test_non_work_item_resource_type_is_a_zero_item_no_op() -> None:
    adapter = JiraAdapter(transport=httpx.MockTransport(lambda r: _json_response({})))
    outcome = adapter.backfill(_account_context(), "repository")
    assert outcome.items_processed == 0
    assert outcome.status == "succeeded"


def test_rate_limit_retry_succeeds_after_bounded_wait(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    calls = {"count": 0}
    sleeps: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return _json_response(
                {"message": "rate limited"}, status_code=429, headers={"Retry-After": "1"}
            )
        return _json_response(
            _search_response(
                [_issue(1, key="ACME-1", summary="First", updated="2024-01-01T00:00:00.000+0000")]
            )
        )

    adapter = JiraAdapter(
        transport=httpx.MockTransport(handler), sleep=lambda seconds: sleeps.append(seconds)
    )
    outcome = adapter.backfill(seeded_account_context, "work_item")
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1
    assert sleeps == [1.0]


def test_rate_limit_gives_up_beyond_bounded_wait() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {"message": "rate limited"}, status_code=429, headers={"Retry-After": "3600"}
        )

    adapter = JiraAdapter(transport=httpx.MockTransport(handler), sleep=lambda seconds: None)
    outcome = adapter.backfill(_account_context(), "work_item")
    assert outcome.status == "partial"
    assert outcome.items_processed == 0
    assert outcome.error_summary is not None
    assert "rate limit" in outcome.error_summary.lower()


def test_rate_limit_still_limited_after_retry_reports_partial_not_failure() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return _json_response(
            {"message": "rate limited"}, status_code=429, headers={"Retry-After": "0"}
        )

    adapter = JiraAdapter(transport=httpx.MockTransport(handler), sleep=lambda seconds: None)
    outcome = adapter.backfill(_account_context(), "work_item")
    assert outcome.status == "partial"
    assert outcome.items_processed == 0
    assert outcome.next_cursor is None
    assert outcome.error_summary is not None
    assert "rate limit" in outcome.error_summary.lower()
    assert calls["count"] == 2


def test_rate_limit_gives_up_immediately_when_retry_after_header_absent() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"message": "rate limited"}, status_code=429)

    adapter = JiraAdapter(transport=httpx.MockTransport(handler), sleep=lambda seconds: None)
    outcome = adapter.backfill(_account_context(), "work_item")
    assert outcome.status == "partial"
    assert outcome.items_processed == 0


def test_rate_limit_gives_up_immediately_when_retry_after_malformed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {"message": "rate limited"}, status_code=429, headers={"Retry-After": "soon"}
        )

    adapter = JiraAdapter(transport=httpx.MockTransport(handler), sleep=lambda seconds: None)
    outcome = adapter.backfill(_account_context(), "work_item")
    assert outcome.status == "partial"
    assert outcome.items_processed == 0


def test_sync_work_items_raises_on_generic_failure_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"message": "Server error"}, status_code=500)

    adapter = JiraAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="500"):
        adapter.backfill(_account_context(), "work_item")


def test_page_cap_reports_partial_with_more_pages_remaining(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    from ecc.domains.engineering import jira_adapter as jira_adapter_module

    def handler(request: httpx.Request) -> httpx.Response:
        start_at = int(request.url.params["startAt"])
        return _json_response(
            _search_response(
                [
                    _issue(
                        start_at + 1,
                        key=f"ACME-{start_at + 1}",
                        summary="Issue",
                        updated=f"2024-01-{(start_at % 28) + 1:02d}T00:00:00.000+0000",
                    )
                ],
                start_at=start_at,
                total=100_000,
            )
        )

    adapter = JiraAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(seeded_account_context, "work_item")
    assert outcome.status == "partial"
    assert outcome.items_processed == jira_adapter_module._MAX_PAGES_PER_CALL
    assert outcome.next_cursor is not None
    assert outcome.error_summary is not None
    assert "page" in outcome.error_summary.lower()


def test_refresh_permissions() -> None:
    def unauthorized(request: httpx.Request) -> httpx.Response:
        return _json_response({"message": "Unauthorized"}, status_code=401)

    def authorized(request: httpx.Request) -> httpx.Response:
        return _json_response({"accountId": "acc-1", "displayName": "Jane"})

    lost_adapter = JiraAdapter(transport=httpx.MockTransport(unauthorized))
    assert lost_adapter.refresh_permissions(_account_context()) == "permission_lost"

    active_adapter = JiraAdapter(transport=httpx.MockTransport(authorized))
    assert active_adapter.refresh_permissions(_account_context()) == "active"


def test_refresh_permissions_fails_open_on_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    adapter = JiraAdapter(transport=httpx.MockTransport(handler))
    assert adapter.refresh_permissions(_account_context()) == "active"


def test_refresh_permissions_returns_permission_lost_for_malformed_credential() -> None:
    """`_parse_credential` is called before any network request; review
    found this earlier, non-network branch of `refresh_permissions` (a
    corrupted stored credential, e.g. from a botched encryption-key
    rotation) had no test at all, unlike the already-covered
    network-error and 401 branches.
    """
    context = ConnectorAccountContext(
        workspace_id=uuid4(),
        connector_account_id=uuid4(),
        external_account_id="acc-1",
        credential="not-a-valid-jira-credential",
    )
    adapter = JiraAdapter()
    assert adapter.refresh_permissions(context) == "permission_lost"


def test_disconnect_is_a_no_op() -> None:
    adapter = JiraAdapter()
    assert adapter.disconnect(_account_context()) is None


def test_handle_webhook_ignores_wrong_event_type() -> None:
    adapter = JiraAdapter()
    payload = dumps({"webhookEvent": "jira:issue_deleted", "issue": {"id": 1}}).encode()
    outcome = adapter.handle_webhook(_account_context(), payload, {})
    assert outcome.items_processed == 0


def test_handle_webhook_ignores_empty_payload() -> None:
    adapter = JiraAdapter()
    outcome = adapter.handle_webhook(_account_context(), b"", {})
    assert outcome.items_processed == 0


def test_handle_webhook_ignores_matching_event_with_no_issue_key() -> None:
    adapter = JiraAdapter()
    payload = dumps({"webhookEvent": "jira:issue_updated"}).encode()
    outcome = adapter.handle_webhook(_account_context(), payload, {})
    assert outcome.items_processed == 0
    assert outcome.status == "succeeded"


def test_handle_webhook_upserts_on_real_issue_updated_payload(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    adapter = JiraAdapter()
    issue = _issue(1, key="ACME-1", summary="First", updated="2024-01-01T00:00:00.000+0000")
    payload = dumps({"webhookEvent": "jira:issue_updated", "issue": issue}).encode()

    outcome = adapter.handle_webhook(seeded_account_context, payload, {})
    assert outcome.items_processed == 1
    assert outcome.status == "succeeded"

    with engine.begin() as connection:
        title = connection.execute(
            text("SELECT title FROM engineering_work_items WHERE workspace_id = :workspace_id"),
            {"workspace_id": seeded_account_context.workspace_id},
        ).scalar_one()
    assert title == "First"


# --- integration: real /sync endpoint, mocked Jira transport ---------------


@pytest.fixture
def engineering_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Jira Sync Test', 'UTC', :now)"
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
        yield client, workspace_id, user_id, token
    finally:
        client.close()
        _cleanup_workspace(workspace_id)


def _cleanup_workspace(workspace_id: UUID) -> None:
    with engine.begin() as connection:
        for table in (
            "engineering_work_items",
            "sync_runs",
            "sync_cursors",
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


def _headers(token: str, key: str | None = None) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    headers = {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _insert_jira_connector_account(workspace_id: UUID, user_id: UUID) -> UUID:
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
                    :id, :workspace_id, 'jira', 'jira-fixture-account', 'Jira fixture',
                    ARRAY[]::text[], :encrypted, 'active', 1,
                    :actor_id, :actor_id, :now, :now
                )
                """
            ),
            {
                "id": account_id,
                "workspace_id": workspace_id,
                "encrypted": encrypt_credential(_CREDENTIAL),
                "actor_id": user_id,
                "now": now,
            },
        )
    return account_id


def test_sync_backfill_writes_work_items_then_incremental_only_writes_newer(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, workspace_id, user_id, token = engineering_test_context
    account_id = _insert_jira_connector_account(workspace_id, user_id)

    backfill_issues = [
        _issue(1, key="ACME-1", summary="First", updated="2024-01-02T00:00:00.000+0000"),
        _issue(2, key="ACME-2", summary="Second", updated="2024-01-01T00:00:00.000+0000"),
    ]
    state = {"call": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["call"] += 1
        if state["call"] == 1:
            return _json_response(_search_response(backfill_issues))
        return _json_response(
            _search_response(
                [
                    _issue(
                        3, key="ACME-3", summary="Third", updated="2024-01-03T00:00:00.000+0000"
                    ),
                    _issue(
                        1, key="ACME-1", summary="First", updated="2024-01-02T00:00:00.000+0000"
                    ),
                ]
            )
        )

    registry = ConnectorRegistry()
    registry.register(JiraAdapter(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(connector_accounts_module, "connector_registry", registry)

    backfill_response = client.post(
        f"/api/v1/engineering/connectors/{account_id}/sync",
        json={"run_type": "backfill", "resource_type": "work_item"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert backfill_response.status_code == 201, backfill_response.text
    assert backfill_response.json()["items_processed"] == 2

    with engine.begin() as connection:
        titles = {
            row[0]
            for row in connection.execute(
                text("SELECT title FROM engineering_work_items WHERE workspace_id = :workspace_id"),
                {"workspace_id": workspace_id},
            )
        }
    assert titles == {"First", "Second"}

    incremental_response = client.post(
        f"/api/v1/engineering/connectors/{account_id}/sync",
        json={"run_type": "incremental", "resource_type": "work_item"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert incremental_response.status_code == 201, incremental_response.text
    assert incremental_response.json()["items_processed"] == 1

    with engine.begin() as connection:
        titles = {
            row[0]
            for row in connection.execute(
                text("SELECT title FROM engineering_work_items WHERE workspace_id = :workspace_id"),
                {"workspace_id": workspace_id},
            )
        }
    assert titles == {"First", "Second", "Third"}


def test_sync_reports_partial_on_rate_limit_and_records_it(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, workspace_id, user_id, token = engineering_test_context
    account_id = _insert_jira_connector_account(workspace_id, user_id)

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {"message": "rate limited"}, status_code=429, headers={"Retry-After": "3600"}
        )

    registry = ConnectorRegistry()
    registry.register(
        JiraAdapter(transport=httpx.MockTransport(handler), sleep=lambda seconds: None)
    )
    monkeypatch.setattr(connector_accounts_module, "connector_registry", registry)

    response = client.post(
        f"/api/v1/engineering/connectors/{account_id}/sync",
        json={"run_type": "backfill", "resource_type": "work_item"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["status"] == "partial"
    assert body["error_summary"] is not None

    with engine.begin() as connection:
        account_status = connection.execute(
            text("SELECT status FROM connector_accounts WHERE id = :id"), {"id": account_id}
        ).scalar_one()
    assert account_status == "active"
