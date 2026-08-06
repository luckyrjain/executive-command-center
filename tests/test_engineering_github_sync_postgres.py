"""Phase 6 Engineering Workspace Task 2/5: GitHub read sync
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

**Task 5 adds** (`github_adapter.py`'s own "Changes"/"Reviews" module-
docstring sections have the full design): `_sync_changes`'s per-
repository fan-out over already-synced `repositories` rows (single repo,
multiple repos sharing one call's page budget, the JSON per-repository
cursor persisting real progress, the `_MAX_PAGES_PER_CALL` bound), a
merged-PR filter (a closed-but-not-merged PR is never upserted),
`_sync_reviews`'s reviews-plus-timeline two-call-per-change shape
(`requested_at` from the earliest `review_requested` timeline event,
applied to every review synced for that change; a change whose timeline
call fails is not advanced past, so its `requested_at` is retried rather
than stuck `NULL`), and end-to-end coverage through the real `/sync`
endpoint for both new resource types.
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
from identity_fixtures import create_identity
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


def _repo(
    repo_id: int, *, full_name: str, updated_at: str, owner_login: str | None = None
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": repo_id,
        "full_name": full_name,
        "html_url": f"https://github.com/{full_name}",
        "default_branch": "main",
        "updated_at": updated_at,
    }
    if owner_login is not None:
        result["owner"] = {"login": owner_login}
    return result


def _json_response(
    body: Any, *, status_code: int = 200, headers: dict[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        headers={"content-type": "application/json", **(headers or {})},
        content=dumps(body),
    )


def _pr(
    pr_id: int, *, number: int, title: str, merged_at: str | None, updated_at: str
) -> dict[str, Any]:
    return {
        "id": pr_id,
        "number": number,
        "title": title,
        "html_url": f"https://github.com/acme/repo/pull/{number}",
        "state": "closed",
        "user": {"id": 900 + pr_id},
        "created_at": "2024-01-01T00:00:00Z",
        "merged_at": merged_at,
        "updated_at": updated_at,
    }


def _review(review_id: int, *, state: str, submitted_at: str | None) -> dict[str, Any]:
    return {
        "id": review_id,
        "user": {"id": 700 + review_id},
        "state": state,
        "submitted_at": submitted_at,
        "html_url": f"https://github.com/acme/repo/pull/1#review-{review_id}",
    }


def _timeline_event(event: str, *, created_at: str) -> dict[str, Any]:
    return {"event": event, "created_at": created_at}


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


# --- unit-level: _safe_repo_path_segment (final Phase 6 review) ------------


@pytest.mark.parametrize("full_name", ["..", ".", "acme/..", "../widgets", "acme/./widgets", ""])
def test_safe_repo_path_segment_rejects_dot_and_empty_segments(full_name: str) -> None:
    """Mirrors `GitLabAddNoteInput._reject_dot_segments`'s own parametrized
    test -- `full_name` is never directly user input (it comes from
    `repositories.name`, itself populated only from GitHub's own repo-
    listing response), but this helper is the sole defense-in-depth
    backstop against the same dot-segment path-escape bug class if that
    assumption were ever wrong, and it had zero test coverage, positive or
    negative, before this test (final Phase 6 review found this gap)."""
    from ecc.domains.engineering.github_adapter import _safe_repo_path_segment

    with pytest.raises(RuntimeError, match="invalid path segment"):
        _safe_repo_path_segment(full_name)


@pytest.mark.parametrize("full_name", ["acme/widgets", "acme/repo.name", "acme-org/repo-name"])
def test_safe_repo_path_segment_passes_through_a_real_repo_name(full_name: str) -> None:
    """A literal `.` *inside* a segment (e.g. `repo.name`) is a real,
    legitimate GitHub repo name and must not be rejected -- only a
    segment that is *exactly* `.`/`..`/empty is a dot-segment."""
    from ecc.domains.engineering.github_adapter import _safe_repo_path_segment

    assert _safe_repo_path_segment(full_name) == full_name


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
        create_identity(
            connection,
            workspace_id=workspace_id,
            user_id=user_id,
            email=f"{user_id}@example.test",
            now=now,
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
            # `reviews`/`changes` are FK'd `ON DELETE CASCADE` from
            # `repositories`/`connector_accounts`, so deleting those two
            # would clean them up regardless -- listed explicitly anyway,
            # matching this fixture's existing style of naming every
            # table rather than relying on cascade alone.
            for table in ("reviews", "changes", "repositories", "connector_accounts", "users"):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                    {"workspace_id": workspace_id},
                )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": workspace_id},
            )


def _seed_repository(
    *, workspace_id: UUID, connector_account_id: UUID, external_id: str, name: str
) -> UUID:
    """Directly inserts a `repositories` row -- `_sync_changes` fans out
    over whatever this same connector account has already synced, so
    change/review tests need at least one such row to exist before
    exercising it, without needing a full repository-sync call first.
    """
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
                    :id, :workspace_id, :connector_account_id, 'github', :external_id,
                    :name, :source_url, 'main', 'active', 'fresh', :now, :now, :now
                )
                """
            ),
            {
                "id": repository_id,
                "workspace_id": workspace_id,
                "connector_account_id": connector_account_id,
                "external_id": external_id,
                "name": name,
                "source_url": f"https://github.com/{name}",
                "now": now,
            },
        )
    return repository_id


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


def test_backfill_populates_suggested_team_name_from_owner_login(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    """Migration `0050_phase6_team_linkage.py`'s "hybrid: auto-suggest,
    human confirms" design -- `_upsert_repository` now writes GitHub's own
    `owner.login` (the org or user this repository belongs to) into
    `repositories.suggested_team_name`, a free-text hint only, never a
    link (see that migration's own docstring for why).
    """
    repos = [_repo(1, full_name="acme/a", updated_at="2024-01-03T00:00:00Z", owner_login="acme")]
    adapter = GitHubAdapter(transport=httpx.MockTransport(lambda r: _json_response(repos)))
    adapter.backfill(seeded_account_context, "repository")

    with engine.begin() as connection:
        suggested = connection.execute(
            text(
                "SELECT suggested_team_name, team_entity_id FROM repositories "
                "WHERE workspace_id = :workspace_id"
            ),
            {"workspace_id": seeded_account_context.workspace_id},
        ).one()
    assert suggested.suggested_team_name == "acme"
    assert suggested.team_entity_id is None


def test_backfill_suggested_team_name_is_none_without_owner(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    repos = [_repo(1, full_name="acme/a", updated_at="2024-01-03T00:00:00Z")]
    adapter = GitHubAdapter(transport=httpx.MockTransport(lambda r: _json_response(repos)))
    adapter.backfill(seeded_account_context, "repository")

    with engine.begin() as connection:
        suggested = connection.execute(
            text("SELECT suggested_team_name FROM repositories WHERE workspace_id = :workspace_id"),
            {"workspace_id": seeded_account_context.workspace_id},
        ).scalar_one()
    assert suggested is None


def test_incremental_resync_refreshes_suggestion_without_touching_confirmed_team(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    """A confirmed `team_entity_id` (set only through `POST .../repositories/
    {id}/team`, never by a sync) must survive indefinitely across re-syncs,
    even as `suggested_team_name` keeps refreshing from the provider's own
    latest payload -- the whole point of separating the two columns.
    """
    repos = [_repo(1, full_name="acme/a", updated_at="2024-01-03T00:00:00Z", owner_login="acme")]
    adapter = GitHubAdapter(transport=httpx.MockTransport(lambda r: _json_response(repos)))
    adapter.backfill(seeded_account_context, "repository")

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
                "UPDATE repositories SET team_entity_id = :team_id "
                "WHERE workspace_id = :workspace_id"
            ),
            {"team_id": confirmed_team_id, "workspace_id": seeded_account_context.workspace_id},
        )

    repos_renamed = [
        _repo(1, full_name="acme/a", updated_at="2024-01-04T00:00:00Z", owner_login="acme-renamed")
    ]
    adapter2 = GitHubAdapter(transport=httpx.MockTransport(lambda r: _json_response(repos_renamed)))
    adapter2.incremental_sync(seeded_account_context, "repository", "2024-01-03T00:00:00Z")

    try:
        with engine.begin() as connection:
            row = connection.execute(
                text(
                    "SELECT suggested_team_name, team_entity_id FROM repositories "
                    "WHERE workspace_id = :workspace_id"
                ),
                {"workspace_id": seeded_account_context.workspace_id},
            ).one()
        assert row.suggested_team_name == "acme-renamed"
        assert row.team_entity_id == confirmed_team_id
    finally:
        # `seeded_account_context`'s own teardown doesn't know about the
        # `pkos_nodes` row this test inserted directly -- clearing
        # `repositories.team_entity_id` first, then the node itself,
        # avoids leaving the fixture's own `DELETE FROM workspaces` blocked
        # by the FK this test's own `UPDATE` created.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE repositories SET team_entity_id = NULL "
                    "WHERE workspace_id = :workspace_id"
                ),
                {"workspace_id": seeded_account_context.workspace_id},
            )
            connection.execute(
                text("DELETE FROM pkos_nodes WHERE id = :id"), {"id": confirmed_team_id}
            )


def test_incremental_resync_clears_dismissed_suggestion_when_name_changes(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    """A human dismissal (`team_suggestion_dismissed_at`) is a judgment
    about one specific `suggested_team_name` -- if the repo's owner
    changes, the next sync must clear the old dismissal so the new
    suggestion isn't silently suppressed by a stale one.
    """
    repos = [_repo(1, full_name="acme/a", updated_at="2024-01-03T00:00:00Z", owner_login="acme")]
    adapter = GitHubAdapter(transport=httpx.MockTransport(lambda r: _json_response(repos)))
    adapter.backfill(seeded_account_context, "repository")

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE repositories SET team_suggestion_dismissed_at = :now "
                "WHERE workspace_id = :workspace_id"
            ),
            {"now": datetime.now(UTC), "workspace_id": seeded_account_context.workspace_id},
        )

    # Same owner, later sync -- dismissal must survive.
    repos_same_owner = [
        _repo(1, full_name="acme/a", updated_at="2024-01-04T00:00:00Z", owner_login="acme")
    ]
    adapter2 = GitHubAdapter(transport=httpx.MockTransport(lambda r: _json_response(repos_same_owner)))
    adapter2.incremental_sync(seeded_account_context, "repository", "2024-01-03T00:00:00Z")

    with engine.begin() as connection:
        still_dismissed = connection.execute(
            text(
                "SELECT team_suggestion_dismissed_at FROM repositories "
                "WHERE workspace_id = :workspace_id"
            ),
            {"workspace_id": seeded_account_context.workspace_id},
        ).scalar_one()
    assert still_dismissed is not None

    # Owner changes -- dismissal must clear, new suggestion must show.
    repos_new_owner = [
        _repo(1, full_name="acme/a", updated_at="2024-01-05T00:00:00Z", owner_login="acme-new")
    ]
    adapter3 = GitHubAdapter(transport=httpx.MockTransport(lambda r: _json_response(repos_new_owner)))
    adapter3.incremental_sync(seeded_account_context, "repository", "2024-01-04T00:00:00Z")

    with engine.begin() as connection:
        cleared = connection.execute(
            text(
                "SELECT team_suggestion_dismissed_at, suggested_team_name FROM repositories "
                "WHERE workspace_id = :workspace_id"
            ),
            {"workspace_id": seeded_account_context.workspace_id},
        ).one()
    assert cleared.team_suggestion_dismissed_at is None
    assert cleared.suggested_team_name == "acme-new"


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


# --- unit-level: change sync (Task 5) ---------------------------------------


def test_changes_backfill_with_no_synced_repositories_is_a_zero_item_no_op(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    """No repository has been synced yet for this connector account --
    `_list_synced_repositories` returns an empty list, so the fan-out
    loop never executes at all. Must still report `succeeded`/0 items
    rather than crashing or reporting `partial`.
    """
    adapter = GitHubAdapter(transport=httpx.MockTransport(lambda r: _json_response([])))
    outcome = adapter.backfill(seeded_account_context, "change")
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 0
    assert outcome.next_cursor == '{"repos":{}}'


def test_changes_backfill_repo_with_no_prs_at_all_reports_succeeded(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    """Distinct from `test_changes_backfill_ignores_closed_but_not_merged_prs`
    -- this repo's own PR list response is entirely empty (`[]`), not
    merely lacking merged ones.
    """
    _seed_repository(
        workspace_id=seeded_account_context.workspace_id,
        connector_account_id=seeded_account_context.connector_account_id,
        external_id="101",
        name="acme/repo1",
    )
    adapter = GitHubAdapter(transport=httpx.MockTransport(lambda r: _json_response([])))
    outcome = adapter.backfill(seeded_account_context, "change")
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 0


def test_changes_incremental_sync_with_malformed_cursor_degrades_to_fresh_walk(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    """A cursor that isn't valid JSON (e.g. a flat-timestamp-style cursor
    left over from a schema change, or another resource type's cursor
    read against the wrong one) must degrade to "no per-repository
    progress recorded yet" rather than raising `json.JSONDecodeError`
    uncaught -- review found the previous version turned this into an
    opaque `failed` sync run instead of a graceful, resumable one.
    """
    repo_id = _seed_repository(
        workspace_id=seeded_account_context.workspace_id,
        connector_account_id=seeded_account_context.connector_account_id,
        external_id="101",
        name="acme/repo1",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            [
                _pr(
                    1,
                    number=1,
                    title="Fix",
                    merged_at="2024-01-02T00:00:00Z",
                    updated_at="2024-01-02T00:00:00Z",
                )
            ]
        )

    adapter = GitHubAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.incremental_sync(seeded_account_context, "change", "not-valid-json{{{")
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1

    with engine.begin() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM changes WHERE workspace_id = :workspace_id"),
            {"workspace_id": seeded_account_context.workspace_id},
        ).scalar_one()
    assert count == 1
    assert repo_id  # the seeded repo was actually reached


def test_changes_incremental_sync_with_wrong_shaped_json_cursor_degrades_to_fresh_walk(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    """Valid JSON, but not this method's own `{"repos": {...}}` shape
    (e.g. a plain JSON string or number) -- must also degrade gracefully,
    not just malformed-JSON specifically.
    """
    _seed_repository(
        workspace_id=seeded_account_context.workspace_id,
        connector_account_id=seeded_account_context.connector_account_id,
        external_id="101",
        name="acme/repo1",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            [
                _pr(
                    1,
                    number=1,
                    title="Fix",
                    merged_at="2024-01-02T00:00:00Z",
                    updated_at="2024-01-02T00:00:00Z",
                )
            ]
        )

    adapter = GitHubAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.incremental_sync(seeded_account_context, "change", '"2024-01-01T00:00:00Z"')
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1


def test_changes_backfill_fans_out_over_two_repos(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    repo1 = _seed_repository(
        workspace_id=seeded_account_context.workspace_id,
        connector_account_id=seeded_account_context.connector_account_id,
        external_id="101",
        name="acme/repo1",
    )
    repo2 = _seed_repository(
        workspace_id=seeded_account_context.workspace_id,
        connector_account_id=seeded_account_context.connector_account_id,
        external_id="102",
        name="acme/repo2",
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/repos/acme/repo1/pulls":
            return _json_response(
                [
                    _pr(
                        1,
                        number=1,
                        title="Fix 1",
                        merged_at="2024-01-02T00:00:00Z",
                        updated_at="2024-01-02T00:00:00Z",
                    )
                ]
            )
        if request.url.path == "/repos/acme/repo2/pulls":
            return _json_response(
                [
                    _pr(
                        2,
                        number=5,
                        title="Fix 2",
                        merged_at="2024-01-03T00:00:00Z",
                        updated_at="2024-01-03T00:00:00Z",
                    )
                ]
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    adapter = GitHubAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(seeded_account_context, "change")
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 2
    assert set(calls) == {"/repos/acme/repo1/pulls", "/repos/acme/repo2/pulls"}

    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT external_id, repository_id FROM changes "
                "WHERE workspace_id = :workspace_id ORDER BY external_id"
            ),
            {"workspace_id": seeded_account_context.workspace_id},
        ).all()
    assert [(row.external_id, row.repository_id) for row in rows] == [
        ("1", repo1),
        ("2", repo2),
    ]


def test_changes_backfill_ignores_closed_but_not_merged_prs(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    _seed_repository(
        workspace_id=seeded_account_context.workspace_id,
        connector_account_id=seeded_account_context.connector_account_id,
        external_id="101",
        name="acme/repo1",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            [
                _pr(
                    1,
                    number=1,
                    title="Closed, not merged",
                    merged_at=None,
                    updated_at="2024-01-02T00:00:00Z",
                )
            ]
        )

    adapter = GitHubAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(seeded_account_context, "change")
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 0

    with engine.begin() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM changes WHERE workspace_id = :workspace_id"),
            {"workspace_id": seeded_account_context.workspace_id},
        ).scalar_one()
    assert count == 0


def test_changes_incremental_sync_resumes_per_repository_cursor(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    """Proves the JSON per-repository cursor shape: repo1's own cursor is
    already caught up (its page-1 response is entirely at-or-before its
    own stored cursor), while repo2 has a genuinely newer PR -- only
    repo2's PR should be (re-)upserted, and the returned cursor must
    still carry repo1's own unchanged value forward, not drop it.
    """
    from json import dumps as json_dumps

    repo1 = _seed_repository(
        workspace_id=seeded_account_context.workspace_id,
        connector_account_id=seeded_account_context.connector_account_id,
        external_id="101",
        name="acme/repo1",
    )
    repo2 = _seed_repository(
        workspace_id=seeded_account_context.workspace_id,
        connector_account_id=seeded_account_context.connector_account_id,
        external_id="102",
        name="acme/repo2",
    )
    since_cursor = json_dumps(
        {"repos": {"101": "2024-01-05T00:00:00Z", "102": "2024-01-01T00:00:00Z"}}
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/repo1/pulls":
            return _json_response(
                [
                    _pr(
                        1,
                        number=1,
                        title="Old",
                        merged_at="2024-01-04T00:00:00Z",
                        updated_at="2024-01-04T00:00:00Z",
                    )
                ]
            )
        if request.url.path == "/repos/acme/repo2/pulls":
            return _json_response(
                [
                    _pr(
                        2,
                        number=5,
                        title="New",
                        merged_at="2024-01-06T00:00:00Z",
                        updated_at="2024-01-06T00:00:00Z",
                    )
                ]
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    adapter = GitHubAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.incremental_sync(seeded_account_context, "change", since_cursor)
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1
    assert outcome.next_cursor is not None
    from json import loads as json_loads

    cursor = json_loads(outcome.next_cursor)
    assert cursor["repos"]["101"] == "2024-01-05T00:00:00Z"
    assert cursor["repos"]["102"] == "2024-01-06T00:00:00Z"

    with engine.begin() as connection:
        external_ids = {
            row[0]
            for row in connection.execute(
                text("SELECT external_id FROM changes WHERE workspace_id = :workspace_id"),
                {"workspace_id": seeded_account_context.workspace_id},
            )
        }
    assert external_ids == {"2"}
    assert repo1 and repo2  # both repos exist; only repo2's PR was new


def test_changes_page_cap_reports_partial_with_progress_preserved(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    from ecc.domains.engineering import github_adapter as github_adapter_module

    _seed_repository(
        workspace_id=seeded_account_context.workspace_id,
        connector_account_id=seeded_account_context.connector_account_id,
        external_id="101",
        name="acme/repo1",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return _json_response(
            [
                _pr(
                    page,
                    number=page,
                    title=f"PR {page}",
                    merged_at=f"2024-01-{page:02d}T00:00:00Z",
                    updated_at=f"2024-01-{page:02d}T00:00:00Z",
                )
            ],
            headers={
                "Link": '<https://api.github.com/repos/acme/repo1/pulls?page=999>; rel="next"'
            },
        )

    adapter = GitHubAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(seeded_account_context, "change")
    assert outcome.status == "partial"
    assert outcome.items_processed == github_adapter_module._MAX_PAGES_PER_CALL
    assert outcome.next_cursor is not None
    assert outcome.error_summary is not None


def test_changes_rate_limit_gives_up_reports_partial(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    _seed_repository(
        workspace_id=seeded_account_context.workspace_id,
        connector_account_id=seeded_account_context.connector_account_id,
        external_id="101",
        name="acme/repo1",
    )
    adapter = GitHubAdapter(
        transport=httpx.MockTransport(
            lambda r: _json_response(
                {"message": "rate limited"},
                status_code=403,
                headers={"X-RateLimit-Remaining": "0", "Retry-After": "3600"},
            )
        ),
        sleep=lambda seconds: None,
    )
    outcome = adapter.backfill(seeded_account_context, "change")
    assert outcome.status == "partial"
    assert outcome.items_processed == 0


def test_non_change_resource_type_still_a_zero_item_no_op_for_unregistered_types() -> None:
    adapter = GitHubAdapter(transport=httpx.MockTransport(lambda r: _json_response([])))
    outcome = adapter.backfill(_account_context(), "deployment")
    assert outcome.items_processed == 0
    assert outcome.status == "succeeded"


# --- unit-level: review sync (Task 5) ---------------------------------------


def _seed_change(
    *,
    workspace_id: UUID,
    connector_account_id: UUID,
    repository_id: UUID,
    external_id: str,
    provider_number: str,
    merged_at: datetime,
) -> UUID:
    change_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO changes (
                    id, workspace_id, connector_account_id, repository_id, provider,
                    external_id, provider_number, title, source_url, status, merged_at,
                    permission_state, freshness_state, observed_at, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, :connector_account_id, :repository_id, 'github',
                    :external_id, :provider_number, 'A change', 'https://x', 'closed', :merged_at,
                    'active', 'fresh', :now, :now, :now
                )
                """
            ),
            {
                "id": change_id,
                "workspace_id": workspace_id,
                "connector_account_id": connector_account_id,
                "repository_id": repository_id,
                "external_id": external_id,
                "provider_number": provider_number,
                "merged_at": merged_at,
                "now": now,
            },
        )
    return change_id


def test_reviews_backfill_captures_requested_at_from_timeline(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    repo = _seed_repository(
        workspace_id=seeded_account_context.workspace_id,
        connector_account_id=seeded_account_context.connector_account_id,
        external_id="101",
        name="acme/repo1",
    )
    _seed_change(
        workspace_id=seeded_account_context.workspace_id,
        connector_account_id=seeded_account_context.connector_account_id,
        repository_id=repo,
        external_id="9001",
        provider_number="42",
        merged_at=datetime(2024, 1, 2, tzinfo=UTC),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/repo1/pulls/42/reviews":
            return _json_response(
                [_review(1, state="APPROVED", submitted_at="2024-01-01T12:00:00Z")]
            )
        if request.url.path == "/repos/acme/repo1/issues/42/timeline":
            return _json_response(
                [
                    _timeline_event("review_requested", created_at="2024-01-01T11:00:00Z"),
                    _timeline_event("review_requested", created_at="2024-01-01T10:00:00Z"),
                    _timeline_event("commented", created_at="2024-01-01T10:30:00Z"),
                ]
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    adapter = GitHubAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(seeded_account_context, "review")
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1

    with engine.begin() as connection:
        row = connection.execute(
            text(
                "SELECT review_state, requested_at, submitted_at FROM reviews "
                "WHERE workspace_id = :workspace_id"
            ),
            {"workspace_id": seeded_account_context.workspace_id},
        ).one()
    assert row.review_state == "approved"
    assert row.requested_at == datetime(2024, 1, 1, 10, 0, tzinfo=UTC)
    assert row.submitted_at == datetime(2024, 1, 1, 12, 0, tzinfo=UTC)


def test_reviews_skips_pending_reviews_with_no_submitted_at(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    repo = _seed_repository(
        workspace_id=seeded_account_context.workspace_id,
        connector_account_id=seeded_account_context.connector_account_id,
        external_id="101",
        name="acme/repo1",
    )
    _seed_change(
        workspace_id=seeded_account_context.workspace_id,
        connector_account_id=seeded_account_context.connector_account_id,
        repository_id=repo,
        external_id="9001",
        provider_number="42",
        merged_at=datetime(2024, 1, 2, tzinfo=UTC),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/repo1/pulls/42/reviews":
            return _json_response([_review(1, state="PENDING", submitted_at=None)])
        if request.url.path == "/repos/acme/repo1/issues/42/timeline":
            return _json_response([])
        raise AssertionError(f"unexpected path {request.url.path}")

    adapter = GitHubAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(seeded_account_context, "review")
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 0


def test_reviews_backfill_with_zero_reviews_skips_the_timeline_call(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    """Distinct from `test_reviews_skips_pending_reviews_with_no_submitted_at`
    above (which returns a non-empty, PENDING-only list, still entering
    the `if reviews:` branch) -- this change has a genuinely empty
    reviews list, so the timeline call's own second-request budget slot
    must never be spent at all. A request to the timeline path would
    raise `AssertionError` via `handler`, so a passing test proves the
    call never happened.
    """
    repo = _seed_repository(
        workspace_id=seeded_account_context.workspace_id,
        connector_account_id=seeded_account_context.connector_account_id,
        external_id="101",
        name="acme/repo1",
    )
    _seed_change(
        workspace_id=seeded_account_context.workspace_id,
        connector_account_id=seeded_account_context.connector_account_id,
        repository_id=repo,
        external_id="9001",
        provider_number="42",
        merged_at=datetime(2024, 1, 2, tzinfo=UTC),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/repo1/pulls/42/reviews":
            return _json_response([])
        raise AssertionError(f"unexpected path {request.url.path} -- timeline should not be called")

    adapter = GitHubAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(seeded_account_context, "review")
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 0


def test_reviews_incremental_sync_stops_at_prior_merged_at_cursor(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    repo = _seed_repository(
        workspace_id=seeded_account_context.workspace_id,
        connector_account_id=seeded_account_context.connector_account_id,
        external_id="101",
        name="acme/repo1",
    )
    _seed_change(
        workspace_id=seeded_account_context.workspace_id,
        connector_account_id=seeded_account_context.connector_account_id,
        repository_id=repo,
        external_id="9001",
        provider_number="42",
        merged_at=datetime(2024, 1, 1, tzinfo=UTC),
    )

    adapter = GitHubAdapter(transport=httpx.MockTransport(lambda r: _json_response([])))
    outcome = adapter.incremental_sync(
        seeded_account_context, "review", datetime(2024, 1, 2, tzinfo=UTC).isoformat()
    )
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 0


def test_reviews_rate_limit_on_timeline_call_gives_up_without_advancing_cursor(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    """The reviews call itself succeeds, but the follow-up timeline call
    is rate-limited beyond the bound -- the change must not be advanced
    past (its `requested_at` would otherwise be stuck unset forever,
    since the cursor-driven query never looks backward).
    """
    repo = _seed_repository(
        workspace_id=seeded_account_context.workspace_id,
        connector_account_id=seeded_account_context.connector_account_id,
        external_id="101",
        name="acme/repo1",
    )
    _seed_change(
        workspace_id=seeded_account_context.workspace_id,
        connector_account_id=seeded_account_context.connector_account_id,
        repository_id=repo,
        external_id="9001",
        provider_number="42",
        merged_at=datetime(2024, 1, 2, tzinfo=UTC),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/acme/repo1/pulls/42/reviews":
            return _json_response(
                [_review(1, state="APPROVED", submitted_at="2024-01-01T12:00:00Z")]
            )
        if request.url.path == "/repos/acme/repo1/issues/42/timeline":
            return _json_response(
                {"message": "rate limited"},
                status_code=403,
                headers={"X-RateLimit-Remaining": "0", "Retry-After": "3600"},
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    adapter = GitHubAdapter(transport=httpx.MockTransport(handler), sleep=lambda seconds: None)
    outcome = adapter.backfill(seeded_account_context, "review")
    assert outcome.status == "partial"
    assert outcome.items_processed == 0
    assert outcome.next_cursor is None

    with engine.begin() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM reviews WHERE workspace_id = :workspace_id"),
            {"workspace_id": seeded_account_context.workspace_id},
        ).scalar_one()
    assert count == 0


def test_refresh_permissions() -> None:
    def unauthorized(request: httpx.Request) -> httpx.Response:
        return _json_response({"message": "Bad credentials"}, status_code=401)

    def authorized(request: httpx.Request) -> httpx.Response:
        return _json_response({"id": 1, "login": "x"})

    lost_adapter = GitHubAdapter(transport=httpx.MockTransport(unauthorized))
    assert lost_adapter.refresh_permissions(_account_context()) == "permission_lost"

    active_adapter = GitHubAdapter(transport=httpx.MockTransport(authorized))
    assert active_adapter.refresh_permissions(_account_context()) == "active"


def test_refresh_permissions_fails_open_on_network_error() -> None:
    """Deliberate fail-open: a transient network failure must not be
    mistaken for a real permission-loss signal. GitLab/Jira/Datadog's own
    identical fail-open behavior (each explicitly documented as mirroring
    this adapter's own precedent) all have this regression test; this
    adapter -- the one the other three are copying -- did not.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    adapter = GitHubAdapter(transport=httpx.MockTransport(handler))
    assert adapter.refresh_permissions(_account_context()) == "active"


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
        _cleanup_workspace(workspace_id)


def _cleanup_workspace(workspace_id: UUID) -> None:
    with engine.begin() as connection:
        for table in (
            "reviews",
            "changes",
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


def test_sync_change_resource_type_through_real_endpoint(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end proof that `resource_type: "change"` reaches
    `_sync_changes` through the real `/sync` endpoint (not just the
    adapter called directly), fanning out over a repository already
    synced via a prior repository backfill.
    """
    client, workspace_id, user_id, token = engineering_test_context
    account_id = _insert_github_connector_account(workspace_id, user_id)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/user/repos":
            return _json_response(
                [_repo(1, full_name="acme/repo1", updated_at="2024-01-01T00:00:00Z")]
            )
        if request.url.path == "/repos/acme/repo1/pulls":
            return _json_response(
                [
                    _pr(
                        9,
                        number=9,
                        title="Fix",
                        merged_at="2024-01-02T00:00:00Z",
                        updated_at="2024-01-02T00:00:00Z",
                    )
                ]
            )
        raise AssertionError(f"unexpected path {request.url.path}")

    registry = ConnectorRegistry()
    registry.register(GitHubAdapter(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(connector_accounts_module, "connector_registry", registry)

    repo_response = client.post(
        f"/api/v1/engineering/connectors/{account_id}/sync",
        json={"run_type": "backfill", "resource_type": "repository"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert repo_response.status_code == 201, repo_response.text

    change_response = client.post(
        f"/api/v1/engineering/connectors/{account_id}/sync",
        json={"run_type": "backfill", "resource_type": "change"},
        headers=_headers(token, key=str(uuid4())),
    )
    assert change_response.status_code == 201, change_response.text
    assert change_response.json()["items_processed"] == 1

    with engine.begin() as connection:
        title = connection.execute(
            text("SELECT title FROM changes WHERE workspace_id = :workspace_id"),
            {"workspace_id": workspace_id},
        ).scalar_one()
    assert title == "Fix"
