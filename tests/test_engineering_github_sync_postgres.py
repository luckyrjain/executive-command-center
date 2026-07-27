"""Phase 6 Engineering Workspace Task 2: GitHub read sync
(`docs/superpowers/plans/2026-07-27-phase-6-engineering-workspace.md`,
`backend/ecc/domains/engineering/github_adapter.py`).

Covers, per this task's own scope ("GitHub read sync -- repositories
only, no work-items/changes/reviews/deployments yet"):

1. `GitHubAdapter.authorize`: success (parses `X-OAuth-Scopes`), 401
   rejection, non-200 rejection, network-error rejection, rejecting a
   classic token missing the required `repo` scope, and accepting a
   fine-grained PAT (no `X-OAuth-Scopes` header at all) with an honestly
   empty `granted_scopes` rather than a fabricated full grant.
2. `GitHubAdapter._sync_repositories` (`httpx.MockTransport` for the
   GitHub API call; a real seeded workspace/connector_account row is still
   required, since `_upsert_repository` -- mirroring `ecc.domains.
   automation.local_adapters.LocalCreateNoteAdapter`'s own "opens its own
   session" precedent -- genuinely writes to `repositories` rather than
   being itself mocked): single page, pagination via the `Link` header
   (including a `next` URL containing a comma, proving `response.links`
   parses correctly where a hand-rolled comma-split would not), the
   incremental-cursor stop-early condition, rate-limit handling succeeding
   after a bounded wait, giving up beyond it (`partial`, which never
   reaches `_upsert_repository` at all), and giving up the same way when
   the one retry is itself still rate-limited, and the `_MAX_PAGES_PER_CALL`
   bound reporting `partial` (with the cursor preserved) rather than a
   silent `succeeded` when more pages remain.
3. `refresh_permissions`/`disconnect`/`handle_webhook` contract coverage.
4. End-to-end through the real `/sync` endpoint (monkeypatched registry
   substituting a mock-transport `GitHubAdapter` for `"github"`):
   backfill writes `repositories` rows correctly, an incremental sync
   afterward only upserts genuinely newer rows, and the restructured
   three-phase `sync_connector_endpoint` (Task 1's disclosed pool-
   exhaustion fix) still produces the same externally-observable result
   as the single-transaction version it replaced.
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
from ecc.domains.engineering.github_adapter import GitHubAdapter
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


def _repo(repo_id: int, *, full_name: str, updated_at: str) -> dict[str, Any]:
    return {
        "id": repo_id,
        "full_name": full_name,
        "html_url": f"https://github.com/{full_name}",
        "default_branch": "main",
        "updated_at": updated_at,
    }


def _json_response(
    body: Any, *, status_code: int = 200, headers: dict[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        headers={"content-type": "application/json", **(headers or {})},
        content=dumps(body),
    )


# --- unit-level: GitHubAdapter.authorize -----------------------------------


def test_github_adapter_authorize_success_parses_scopes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/user"
        assert request.headers["Authorization"] == "Bearer ghp_test"
        return _json_response(
            {"id": 555, "login": "octocat"},
            headers={"X-OAuth-Scopes": "repo, read:user"},
        )

    adapter = GitHubAdapter(transport=httpx.MockTransport(handler))
    authorization = adapter.authorize("ghp_test")
    assert authorization.external_account_id == "555"
    assert authorization.display_name == "octocat"
    assert authorization.granted_scopes == frozenset({"repo", "read:user"})


def test_github_adapter_authorize_rejects_missing_required_scope() -> None:
    """A classic OAuth token/PAT (`X-OAuth-Scopes` present) missing the
    required `repo` scope is rejected -- review found the previous
    fallback (`granted or _REQUIRED_SCOPES`) fabricated a full grant
    whenever the header was empty/missing instead of ever checking it.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {"id": 1, "login": "x"}, headers={"X-OAuth-Scopes": "read:user, gist"}
        )

    adapter = GitHubAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(AdapterAuthorizationError, match="repo"):
        adapter.authorize("classic-token-missing-repo-scope")


def test_github_adapter_authorize_rejects_classic_token_with_empty_scopes_header() -> None:
    """Distinct from the fine-grained-PAT case below: `X-OAuth-Scopes`
    *present but empty* (a classic OAuth token authorized with zero
    scopes) is a real, checkable signal -- unlike an *absent* header, this
    must still fail the same subset check as any other under-scoped
    classic token, not be treated as unverifiable.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"id": 1, "login": "x"}, headers={"X-OAuth-Scopes": ""})

    adapter = GitHubAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(AdapterAuthorizationError, match="repo"):
        adapter.authorize("classic-token-zero-scopes")


def test_github_adapter_authorize_fine_grained_pat_has_no_scopes_header() -> None:
    """GitHub never emits `X-OAuth-Scopes` for a fine-grained PAT -- this
    is accepted (not rejected: refusing every fine-grained PAT outright
    would make GitHub's own recommended modern token type unusable
    through this connector) with an honestly-empty `granted_scopes`
    rather than a fabricated full grant. See `CONNECTOR-CONTRACT.md`'s
    matching "Accepted limitation" section.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"id": 1, "login": "x"})  # no X-OAuth-Scopes header

    adapter = GitHubAdapter(transport=httpx.MockTransport(handler))
    authorization = adapter.authorize("fine-grained-pat")
    assert authorization.granted_scopes == frozenset()


def test_github_adapter_authorize_rejects_401() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"message": "Bad credentials"}, status_code=401)

    adapter = GitHubAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(AdapterAuthorizationError):
        adapter.authorize("bad-token")


def test_github_adapter_authorize_rejects_non_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"message": "Server error"}, status_code=500)

    adapter = GitHubAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(AdapterAuthorizationError):
        adapter.authorize("token")


def test_github_adapter_authorize_rejects_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    adapter = GitHubAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(AdapterAuthorizationError):
        adapter.authorize("token")


# --- unit-level: repository sync (no database) -----------------------------


def _account_context() -> ConnectorAccountContext:
    """A context backed by no real database rows -- only safe to use with
    an adapter call that never reaches `_upsert_repository` (an
    unregistered/non-repository resource type, an authorize/refresh_
    permissions/disconnect call, or a rate-limited call that gives up
    before ever writing a repository row).
    """
    return ConnectorAccountContext(
        workspace_id=uuid4(),
        connector_account_id=uuid4(),
        external_account_id="555",
        credential="ghp_test",
    )


@pytest.fixture
def seeded_account_context() -> Iterator[ConnectorAccountContext]:
    """A context backed by a real `workspaces`/`users`/`connector_accounts`
    row set -- required for any adapter call that reaches `_upsert_
    repository`, which genuinely writes to `repositories` (FK'd to
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
                "VALUES (:id, 'GitHub Adapter Unit Test', 'UTC', :now)"
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
                    :id, :workspace_id, 'github', 'gh-unit-test', 'GitHub unit test',
                    ARRAY['contents:read'], :encrypted, 'active', 1,
                    :actor_id, :actor_id, :now, :now
                )
                """
            ),
            {
                "id": account_id,
                "workspace_id": workspace_id,
                "encrypted": encrypt_credential("ghp_test"),
                "actor_id": user_id,
                "now": now,
            },
        )
    try:
        yield ConnectorAccountContext(
            workspace_id=workspace_id,
            connector_account_id=account_id,
            external_account_id="gh-unit-test",
            credential="ghp_test",
        )
    finally:
        with engine.begin() as connection:
            for table in ("repositories", "connector_accounts", "users"):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                    {"workspace_id": workspace_id},
                )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": workspace_id},
            )


def test_backfill_single_page(seeded_account_context: ConnectorAccountContext) -> None:
    repos = [
        _repo(1, full_name="acme/a", updated_at="2024-01-03T00:00:00Z"),
        _repo(2, full_name="acme/b", updated_at="2024-01-02T00:00:00Z"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/user/repos"
        return _json_response(repos)

    adapter = GitHubAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(seeded_account_context, "repository")
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 2
    assert outcome.next_cursor == "2024-01-03T00:00:00Z"


def test_backfill_paginates_via_link_header(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    page1 = [_repo(1, full_name="acme/a", updated_at="2024-01-05T00:00:00Z")]
    page2 = [_repo(2, full_name="acme/b", updated_at="2024-01-04T00:00:00Z")]

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        if page == "1":
            return _json_response(
                page1,
                headers={"Link": '<https://api.github.com/user/repos?page=2>; rel="next"'},
            )
        if page == "2":
            return _json_response(page2)
        raise AssertionError(f"unexpected page {page}")

    adapter = GitHubAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(seeded_account_context, "repository")
    assert outcome.items_processed == 2
    assert outcome.next_cursor == "2024-01-05T00:00:00Z"


def test_incremental_sync_stops_at_prior_cursor(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    repos = [
        _repo(1, full_name="acme/a", updated_at="2024-01-05T00:00:00Z"),
        _repo(2, full_name="acme/b", updated_at="2024-01-01T00:00:00Z"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(repos)

    adapter = GitHubAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.incremental_sync(
        seeded_account_context, "repository", cursor="2024-01-02T00:00:00Z"
    )
    # Only the first repo (updated after the cursor) is processed; the
    # second (updated_at <= cursor) triggers the stop-early condition.
    assert outcome.items_processed == 1
    assert outcome.next_cursor == "2024-01-05T00:00:00Z"


def test_incremental_sync_with_no_cursor_behaves_like_backfill(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    repos = [_repo(1, full_name="acme/a", updated_at="2024-01-05T00:00:00Z")]

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(repos)

    adapter = GitHubAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.incremental_sync(seeded_account_context, "repository", cursor=None)
    assert outcome.items_processed == 1


def test_non_repository_resource_type_is_a_zero_item_no_op() -> None:
    adapter = GitHubAdapter(transport=httpx.MockTransport(lambda r: _json_response([])))
    outcome = adapter.backfill(_account_context(), "work_item")
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
                {"message": "rate limited"},
                status_code=403,
                headers={"X-RateLimit-Remaining": "0", "Retry-After": "1"},
            )
        return _json_response([_repo(1, full_name="acme/a", updated_at="2024-01-01T00:00:00Z")])

    adapter = GitHubAdapter(
        transport=httpx.MockTransport(handler), sleep=lambda seconds: sleeps.append(seconds)
    )
    outcome = adapter.backfill(seeded_account_context, "repository")
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1
    assert sleeps == [1.0]


def test_rate_limit_gives_up_beyond_bounded_wait() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {"message": "rate limited"},
            status_code=403,
            headers={"X-RateLimit-Remaining": "0", "Retry-After": "3600"},
        )

    adapter = GitHubAdapter(transport=httpx.MockTransport(handler), sleep=lambda seconds: None)
    outcome = adapter.backfill(_account_context(), "repository")
    assert outcome.status == "partial"
    assert outcome.items_processed == 0
    assert outcome.error_summary is not None
    assert "rate limit" in outcome.error_summary.lower()


def test_rate_limit_still_limited_after_retry_reports_partial_not_failure() -> None:
    """A second consecutive rate-limit hit (the retry response is itself
    still rate-limited) previously fell through to the `status_code !=
    200` branch, raising an opaque `RuntimeError` -- the whole sync run
    reported `failed` instead of the same clean `partial` + preserved-
    cursor outcome a first-hit rate limit gets. One retry is the bound;
    review found a still-limited retry must give up the same way.
    """
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return _json_response(
            {"message": "rate limited"},
            status_code=429,
            headers={"Retry-After": "0"},
        )

    adapter = GitHubAdapter(transport=httpx.MockTransport(handler), sleep=lambda seconds: None)
    outcome = adapter.backfill(_account_context(), "repository")
    assert outcome.status == "partial"
    assert outcome.items_processed == 0
    assert outcome.next_cursor is None
    assert outcome.error_summary is not None
    assert "rate limit" in outcome.error_summary.lower()
    assert calls["count"] == 2


def test_page_cap_reports_partial_with_more_pages_remaining(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    """Hitting `_MAX_PAGES_PER_CALL` while the last-fetched page still had
    a `next` link previously fell through to the same `succeeded` return
    every genuinely-complete sync gets -- silently capping a large
    account's backfill with no signal that more repositories remained.
    """
    from ecc.domains.engineering import github_adapter as github_adapter_module

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return _json_response(
            [_repo(page, full_name=f"acme/r{page}", updated_at=f"2024-01-{page:02d}T00:00:00Z")],
            headers={"Link": '<https://api.github.com/user/repos?page=999>; rel="next"'},
        )

    adapter = GitHubAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(seeded_account_context, "repository")
    assert outcome.status == "partial"
    assert outcome.items_processed == github_adapter_module._MAX_PAGES_PER_CALL
    assert outcome.next_cursor is not None
    assert outcome.error_summary is not None
    assert "page" in outcome.error_summary.lower()


def test_link_header_next_url_containing_comma_still_paginates(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    """`response.links` (httpx's own RFC 8288 parser) replaces a hand-
    rolled comma-split that could mis-parse a `Link` header whose URL
    itself contains a comma (a legal, if unusual, query-string value),
    silently truncating pagination early.
    """
    page1 = [_repo(1, full_name="acme/a", updated_at="2024-01-05T00:00:00Z")]
    page2 = [_repo(2, full_name="acme/b", updated_at="2024-01-04T00:00:00Z")]

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        if page == "1":
            return _json_response(
                page1,
                headers={
                    "Link": (
                        '<https://api.github.com/user/repos?page=2&q=a,b>; rel="next", '
                        '<https://api.github.com/user/repos?page=9>; rel="last"'
                    )
                },
            )
        if page == "2":
            return _json_response(page2)
        raise AssertionError(f"unexpected page {page}")

    adapter = GitHubAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(seeded_account_context, "repository")
    assert outcome.items_processed == 2
    assert outcome.next_cursor == "2024-01-05T00:00:00Z"


def test_refresh_permissions() -> None:
    def unauthorized(request: httpx.Request) -> httpx.Response:
        return _json_response({"message": "Bad credentials"}, status_code=401)

    def authorized(request: httpx.Request) -> httpx.Response:
        return _json_response({"id": 1, "login": "x"})

    lost_adapter = GitHubAdapter(transport=httpx.MockTransport(unauthorized))
    assert lost_adapter.refresh_permissions(_account_context()) == "permission_lost"

    active_adapter = GitHubAdapter(transport=httpx.MockTransport(authorized))
    assert active_adapter.refresh_permissions(_account_context()) == "active"


def test_disconnect_is_a_no_op() -> None:
    adapter = GitHubAdapter()
    assert adapter.disconnect(_account_context()) is None


def test_handle_webhook_ignores_non_repository_events() -> None:
    adapter = GitHubAdapter()
    outcome = adapter.handle_webhook(
        _account_context(), b'{"action": "opened"}', {"X-GitHub-Event": "issues"}
    )
    assert outcome.items_processed == 0


def test_handle_webhook_ignores_empty_payload() -> None:
    adapter = GitHubAdapter()
    outcome = adapter.handle_webhook(_account_context(), b"", {"X-GitHub-Event": "repository"})
    assert outcome.items_processed == 0


# --- integration: real /sync endpoint, mocked GitHub transport -------------


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
                "VALUES (:id, 'GitHub Sync Test', 'UTC', :now)"
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
            "repositories",
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


def _insert_github_connector_account(
    workspace_id: UUID, user_id: UUID, *, credential: str = "ghp_fixture"
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
                    :id, :workspace_id, 'github', 'gh-fixture-account', 'GitHub fixture',
                    ARRAY['contents:read'], :encrypted, 'active', 1,
                    :actor_id, :actor_id, :now, :now
                )
                """
            ),
            {
                "id": account_id,
                "workspace_id": workspace_id,
                "encrypted": encrypt_credential(credential),
                "actor_id": user_id,
                "now": now,
            },
        )
    return account_id


def test_sync_backfill_writes_repositories_then_incremental_only_writes_newer(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, workspace_id, user_id, token = engineering_test_context
    account_id = _insert_github_connector_account(workspace_id, user_id)

    backfill_repos = [
        _repo(1, full_name="acme/a", updated_at="2024-01-02T00:00:00Z"),
        _repo(2, full_name="acme/b", updated_at="2024-01-01T00:00:00Z"),
    ]
    state = {"call": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["call"] += 1
        if state["call"] == 1:
            return _json_response(backfill_repos)
        # Incremental call: one genuinely-newer repo, one already-seen repo
        # at the prior cursor -- only the newer one should be upserted.
        return _json_response(
            [
                _repo(3, full_name="acme/c", updated_at="2024-01-03T00:00:00Z"),
                _repo(1, full_name="acme/a", updated_at="2024-01-02T00:00:00Z"),
            ]
        )

    registry = ConnectorRegistry()
    registry.register(GitHubAdapter(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(connector_accounts_module, "connector_registry", registry)

    backfill_response = client.post(
        f"/api/v1/engineering/connectors/{account_id}/sync",
        json={"run_type": "backfill", "resource_type": "repository"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert backfill_response.status_code == 201, backfill_response.text
    assert backfill_response.json()["items_processed"] == 2

    with engine.begin() as connection:
        names = {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM repositories WHERE workspace_id = :workspace_id"),
                {"workspace_id": workspace_id},
            )
        }
    assert names == {"acme/a", "acme/b"}

    incremental_response = client.post(
        f"/api/v1/engineering/connectors/{account_id}/sync",
        json={"run_type": "incremental", "resource_type": "repository"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert incremental_response.status_code == 201, incremental_response.text
    assert incremental_response.json()["items_processed"] == 1

    with engine.begin() as connection:
        names = {
            row[0]
            for row in connection.execute(
                text("SELECT name FROM repositories WHERE workspace_id = :workspace_id"),
                {"workspace_id": workspace_id},
            )
        }
    assert names == {"acme/a", "acme/b", "acme/c"}


def test_sync_reports_partial_on_rate_limit_and_records_it(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, workspace_id, user_id, token = engineering_test_context
    account_id = _insert_github_connector_account(workspace_id, user_id)

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {"message": "rate limited"},
            status_code=403,
            headers={"X-RateLimit-Remaining": "0", "Retry-After": "3600"},
        )

    registry = ConnectorRegistry()
    registry.register(
        GitHubAdapter(transport=httpx.MockTransport(handler), sleep=lambda seconds: None)
    )
    monkeypatch.setattr(connector_accounts_module, "connector_registry", registry)

    response = client.post(
        f"/api/v1/engineering/connectors/{account_id}/sync",
        json={"run_type": "backfill", "resource_type": "repository"},
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
    # 'partial' is a successful (non-exception) SyncOutcome status -- the
    # connector account is not moved to 'error' the way an adapter
    # exception would; only sync_runs.status/error_summary reflect it.
    assert account_status == "active"
