"""Phase 6 Engineering Workspace Task 3: GitLab read sync
(`docs/superpowers/plans/2026-07-27-phase-6-engineering-workspace.md`,
`backend/ecc/domains/engineering/gitlab_adapter.py`).

Covers, per this task's own scope ("GitLab read sync -- repositories
only, no work-items/changes/reviews/deployments yet", identical to
Task 2's own GitHub scope):

1. `GitLabAdapter.authorize`: success (parses `scopes` from the `GET
   /personal_access_tokens/self` JSON body, then `GET /user` for
   identity), rejecting a token missing a required scope, rejecting a
   revoked token, rejecting an inactive token, 401 rejection, non-200
   rejection, network-error rejection. Unlike GitHub, GitLab always
   returns real scopes for every token -- there is no "header absent,
   accept anyway" case to cover here (see `gitlab_adapter.py`'s own
   module docstring).
2. `GitLabAdapter._sync_repositories` (`httpx.MockTransport` for the
   GitLab API call; a real seeded workspace/connector_account row is
   still required, since `_upsert_repository` genuinely writes to
   `repositories`): single page, pagination via the `Link` header
   (including a `next` URL containing a comma), the incremental-cursor
   stop-early condition, rate-limit handling succeeding after a bounded
   wait, giving up beyond it (`partial`), giving up the same way when the
   one retry is itself still rate-limited, and the `_MAX_PAGES_PER_CALL`
   bound reporting `partial` rather than a silent `succeeded`.
3. `refresh_permissions`/`disconnect`/`handle_webhook` contract coverage,
   including `disconnect`'s real (not no-op) revocation attempt -- both
   the success (204) and the expected-in-practice-failure (403, this
   connector's own read-only scopes) paths.
4. End-to-end through the real `/sync` endpoint (monkeypatched registry
   substituting a mock-transport `GitLabAdapter` for `"gitlab"`):
   backfill writes `repositories` rows correctly, an incremental sync
   afterward only upserts genuinely newer rows, and a rate-limited sync
   reports `partial` without moving the connector account to `error`.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
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
from ecc.domains.engineering.gitlab_adapter import (
    GitLabAdapter,
    _InvalidCredentialError,
    _is_private_address,
    _parse_credential,
)
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


def _project(
    project_id: int, *, path: str, updated_at: str, namespace: Any = None
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": project_id,
        "path_with_namespace": path,
        "web_url": f"https://gitlab.com/{path}",
        "default_branch": "main",
        "last_activity_at": updated_at,
    }
    if namespace is not None:
        result["namespace"] = namespace
    return result


def _json_response(
    body: Any, *, status_code: int = 200, headers: dict[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        headers={"content-type": "application/json", **(headers or {})},
        content=dumps(body),
    )


def _token_self_response(
    *, scopes: list[str], active: bool = True, revoked: bool = False
) -> dict[str, Any]:
    return {"id": 1, "scopes": scopes, "active": active, "revoked": revoked, "user_id": 555}


# --- unit-level: credential parsing and SSRF host guard --------------------


def test_parse_credential_splits_host_and_token() -> None:
    assert _parse_credential("gitlab.com|glpat_test") == ("gitlab.com", "glpat_test")
    assert _parse_credential("gitlab-ee.mpokket.org|glpat-xyz") == (
        "gitlab-ee.mpokket.org",
        "glpat-xyz",
    )


def test_parse_credential_reads_a_bare_token_as_a_legacy_gitlab_com_credential() -> None:
    """Backward compatibility, not laxity: every `connector_accounts` row
    for provider `gitlab` written before self-managed support shipped holds
    a bare personal access token, and there is no endpoint to rewrite a
    stored credential in place. Rejecting those rows would break `/sync`,
    `refresh_permissions`, `disconnect`, and `gitlab.add_note` for every
    existing gitlab.com connection with no remediation short of
    disconnect + reconnect (which mints a new `external_account_id`). A
    bare token could only ever have been issued by gitlab.com, since that
    was the sole reachable host before the `host|token` format existed.
    """
    assert _parse_credential("glpat-legacy-bare-token") == ("gitlab.com", "glpat-legacy-bare-token")


def test_parse_credential_rejects_empty_credential() -> None:
    with pytest.raises(_InvalidCredentialError):
        _parse_credential("")


def test_parse_credential_rejects_empty_host_or_token() -> None:
    with pytest.raises(_InvalidCredentialError):
        _parse_credential("|glpat_test")
    with pytest.raises(_InvalidCredentialError):
        _parse_credential("gitlab.com|")


def test_parse_credential_rejects_scheme_in_host() -> None:
    with pytest.raises(_InvalidCredentialError):
        _parse_credential("https://gitlab.com|glpat_test")


def test_parse_credential_rejects_path_in_host() -> None:
    with pytest.raises(_InvalidCredentialError):
        _parse_credential("gitlab.com/api|glpat_test")


def test_parse_credential_rejects_whitespace_in_host() -> None:
    with pytest.raises(_InvalidCredentialError):
        _parse_credential("gitlab.com |glpat_test")


def test_is_private_address_flags_loopback_link_local_and_rfc1918() -> None:
    assert _is_private_address("127.0.0.1") is True
    assert _is_private_address("169.254.169.254") is True
    assert _is_private_address("10.0.0.5") is True
    assert _is_private_address("172.16.0.1") is True
    assert _is_private_address("192.168.1.1") is True
    assert _is_private_address("::1") is True


def test_is_private_address_allows_public_addresses() -> None:
    assert _is_private_address("8.8.8.8") is False
    assert _is_private_address("140.82.112.3") is False


def test_reject_private_host_raises_for_resolved_private_address() -> None:
    adapter = GitLabAdapter(resolve_host=lambda host: ["169.254.169.254"])
    with pytest.raises(AdapterAuthorizationError, match="private/internal"):
        adapter._reject_private_host("gitlab-internal.example.com")


def test_reject_private_host_allows_public_address() -> None:
    adapter = GitLabAdapter(resolve_host=lambda host: ["140.82.112.3"])
    adapter._reject_private_host("gitlab.com")  # must not raise


def test_reject_private_host_raises_for_unresolvable_host() -> None:
    def _fail(host: str) -> list[str]:
        raise AdapterAuthorizationError("GitLab host could not be resolved: nxdomain")

    adapter = GitLabAdapter(resolve_host=_fail)
    with pytest.raises(AdapterAuthorizationError, match="could not be resolved"):
        adapter._reject_private_host("does-not-exist.invalid")


# --- unit-level: GitLabAdapter.authorize ------------------------------------


def test_gitlab_adapter_authorize_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["PRIVATE-TOKEN"] == "glpat_test"
        if request.url.path == "/api/v4/personal_access_tokens/self":
            return _json_response(_token_self_response(scopes=["read_api", "read_repository"]))
        assert request.url.path == "/api/v4/user"
        return _json_response({"id": 555, "username": "octocat"})

    adapter = GitLabAdapter(transport=httpx.MockTransport(handler))
    authorization = adapter.authorize("gitlab.com|glpat_test")
    assert authorization.external_account_id == "gitlab.com:555"
    assert authorization.display_name == "octocat"
    assert authorization.granted_scopes == frozenset({"read_api", "read_repository"})


def test_gitlab_adapter_authorize_rejects_missing_required_scope() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(_token_self_response(scopes=["read_user"]))

    adapter = GitLabAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(AdapterAuthorizationError, match="read_api"):
        adapter.authorize("gitlab.com|token-missing-scopes")


def test_gitlab_adapter_authorize_rejects_revoked_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            _token_self_response(scopes=["read_api", "read_repository"], revoked=True)
        )

    adapter = GitLabAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(AdapterAuthorizationError, match="revoked"):
        adapter.authorize("gitlab.com|revoked-token")


def test_gitlab_adapter_authorize_rejects_inactive_token() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            _token_self_response(scopes=["read_api", "read_repository"], active=False)
        )

    adapter = GitLabAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(AdapterAuthorizationError, match="active"):
        adapter.authorize("gitlab.com|inactive-token")


def test_gitlab_adapter_authorize_rejects_401() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"message": "401 Unauthorized"}, status_code=401)

    adapter = GitLabAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(AdapterAuthorizationError):
        adapter.authorize("gitlab.com|bad-token")


def test_gitlab_adapter_authorize_rejects_non_200() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"message": "Server error"}, status_code=500)

    adapter = GitLabAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(AdapterAuthorizationError):
        adapter.authorize("gitlab.com|token")


def test_gitlab_adapter_authorize_rejects_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    adapter = GitLabAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(AdapterAuthorizationError):
        adapter.authorize("gitlab.com|token")


def test_gitlab_adapter_authorize_rejects_second_call_non_200() -> None:
    """`authorize`'s two-call sequence (`personal_access_tokens/self` then
    `/user`) has a failure mode GitHub's single-call `authorize` has no
    analogue for -- the first call succeeding (valid, in-scope token) but
    the second (`GET /user`, for identity) failing. Untested by review.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/personal_access_tokens/self"):
            return _json_response(_token_self_response(scopes=["read_api", "read_repository"]))
        return _json_response({"message": "Server error"}, status_code=500)

    adapter = GitLabAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(AdapterAuthorizationError):
        adapter.authorize("gitlab.com|token")


def test_gitlab_adapter_authorize_rejects_second_call_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/personal_access_tokens/self"):
            return _json_response(_token_self_response(scopes=["read_api", "read_repository"]))
        raise httpx.ConnectError("connection refused")

    adapter = GitLabAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(AdapterAuthorizationError):
        adapter.authorize("gitlab.com|token")


def test_gitlab_adapter_authorize_success_self_managed_host() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "gitlab-ee.mpokket.org"
        if request.url.path == "/api/v4/personal_access_tokens/self":
            return _json_response(_token_self_response(scopes=["read_api", "read_repository"]))
        assert request.url.path == "/api/v4/user"
        return _json_response({"id": 7, "username": "priya"})

    adapter = GitLabAdapter(
        transport=httpx.MockTransport(handler),
        resolve_host=lambda host: ["3.3.3.3"],
    )
    authorization = adapter.authorize("gitlab-ee.mpokket.org|glpat_private")
    assert authorization.external_account_id == "gitlab-ee.mpokket.org:7"
    assert authorization.display_name == "priya"


def test_gitlab_adapter_authorize_accepts_a_legacy_bare_token_credential() -> None:
    """A `connector_accounts` row written before self-managed support
    shipped holds a bare token with no `|`. `authorize()` must read it as a
    gitlab.com credential rather than raising -- and still emit the new
    `host:user_id` `external_account_id`, since `authorize()` only ever
    runs at connect time (an already-connected legacy row's stored bare
    `external_account_id` is never re-derived from a fresh call, so there
    is nothing to stay bit-compatible with here).
    """

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "gitlab.com"
        assert request.headers["PRIVATE-TOKEN"] == "glpat-legacy-bare"
        if request.url.path == "/api/v4/personal_access_tokens/self":
            return _json_response(_token_self_response(scopes=["read_api", "read_repository"]))
        return _json_response({"id": 555, "username": "legacy-user"})

    adapter = GitLabAdapter(
        transport=httpx.MockTransport(handler), resolve_host=lambda host: ["140.82.112.3"]
    )
    authorization = adapter.authorize("glpat-legacy-bare")
    assert authorization.external_account_id == "gitlab.com:555"
    assert authorization.display_name == "legacy-user"


def test_gitlab_adapter_authorize_rejects_private_host_end_to_end() -> None:
    from ecc.domains.engineering.connectors import AdapterAuthorizationError

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not make an HTTP call once the host is rejected")

    adapter = GitLabAdapter(
        transport=httpx.MockTransport(handler),
        resolve_host=lambda host: ["169.254.169.254"],
    )
    with pytest.raises(AdapterAuthorizationError, match="private/internal"):
        adapter.authorize("gitlab-internal.example.com|glpat_test")


def test_gitlab_adapter_two_hosts_same_numeric_user_id_do_not_collide() -> None:
    def handler_for(user_id: int) -> Callable[[httpx.Request], httpx.Response]:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v4/personal_access_tokens/self":
                return _json_response(_token_self_response(scopes=["read_api", "read_repository"]))
            return _json_response({"id": user_id, "username": f"user{user_id}"})

        return handler

    cloud_adapter = GitLabAdapter(transport=httpx.MockTransport(handler_for(42)))
    self_managed_adapter = GitLabAdapter(
        transport=httpx.MockTransport(handler_for(42)), resolve_host=lambda host: ["3.3.3.3"]
    )
    cloud_auth = cloud_adapter.authorize("gitlab.com|token-a")
    self_managed_auth = self_managed_adapter.authorize("gitlab-ee.mpokket.org|token-b")
    assert cloud_auth.external_account_id == "gitlab.com:42"
    assert self_managed_auth.external_account_id == "gitlab-ee.mpokket.org:42"
    assert cloud_auth.external_account_id != self_managed_auth.external_account_id


# --- unit-level: repository sync (no database) ------------------------------


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
        external_account_id="gitlab.com:555",
        credential="gitlab.com|glpat_test",
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
                "VALUES (:id, 'GitLab Adapter Unit Test', 'UTC', :now)"
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
                    :id, :workspace_id, 'gitlab', 'gl-unit-test', 'GitLab unit test',
                    ARRAY['read_api', 'read_repository'], :encrypted, 'active', 1,
                    :actor_id, :actor_id, :now, :now
                )
                """
            ),
            {
                "id": account_id,
                "workspace_id": workspace_id,
                "encrypted": encrypt_credential("gitlab.com|glpat_test"),
                "actor_id": user_id,
                "now": now,
            },
        )
    try:
        yield ConnectorAccountContext(
            workspace_id=workspace_id,
            connector_account_id=account_id,
            external_account_id="gl-unit-test",
            credential="gitlab.com|glpat_test",
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
    projects = [
        _project(1, path="acme/a", updated_at="2024-01-03T00:00:00Z"),
        _project(2, path="acme/b", updated_at="2024-01-02T00:00:00Z"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v4/projects"
        return _json_response(projects)

    adapter = GitLabAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(seeded_account_context, "repository")
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 2
    assert outcome.next_cursor == "2024-01-03T00:00:00Z"


def test_backfill_populates_suggested_team_name_from_namespace_object(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    """Migration `0050_phase6_team_linkage.py`'s "hybrid: auto-suggest,
    human confirms" design -- `_upsert_repository` now writes GitLab's own
    `namespace.name` (the group or user this project belongs to) into
    `repositories.suggested_team_name`. The full `GET /projects` REST
    representation nests `namespace` as an object -- see the next test for
    the webhook payload's differently-shaped bare-string `namespace`.
    """
    projects = [
        _project(
            1,
            path="acme/a",
            updated_at="2024-01-03T00:00:00Z",
            namespace={"id": 9, "name": "Acme Group", "path": "acme", "kind": "group"},
        )
    ]
    adapter = GitLabAdapter(transport=httpx.MockTransport(lambda r: _json_response(projects)))
    adapter.backfill(seeded_account_context, "repository")

    with engine.begin() as connection:
        suggested = connection.execute(
            text("SELECT suggested_team_name FROM repositories WHERE workspace_id = :workspace_id"),
            {"workspace_id": seeded_account_context.workspace_id},
        ).scalar_one()
    assert suggested == "Acme Group"


def test_backfill_suggested_team_name_handles_bare_string_namespace(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    """A real GitLab `Push Hook` webhook payload's embedded `project.
    namespace` is a bare display-name string, not the REST API's nested
    object -- the identical REST-vs-webhook schema mismatch `_with_push_
    event_activity_timestamp`'s own docstring documents for
    `last_activity_at`. `_suggested_team_name` must handle both shapes
    without raising.
    """
    projects = [
        _project(1, path="acme/a", updated_at="2024-01-03T00:00:00Z", namespace="Acme Group")
    ]
    adapter = GitLabAdapter(transport=httpx.MockTransport(lambda r: _json_response(projects)))
    adapter.backfill(seeded_account_context, "repository")

    with engine.begin() as connection:
        suggested = connection.execute(
            text("SELECT suggested_team_name FROM repositories WHERE workspace_id = :workspace_id"),
            {"workspace_id": seeded_account_context.workspace_id},
        ).scalar_one()
    assert suggested == "Acme Group"


def test_incremental_resync_refreshes_suggestion_without_touching_confirmed_team(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    """A confirmed `team_entity_id` (set only through `POST .../repositories/
    {id}/team`, never by a sync) must survive indefinitely across re-syncs,
    even as `suggested_team_name` keeps refreshing from the provider's own
    latest payload -- the same regression `github_adapter`'s own test file
    proves, exercised here since `_upsert_repository`'s `ON CONFLICT ... DO
    UPDATE` clause is authored per-adapter, not shared.
    """
    projects = [_project(1, path="acme/a", updated_at="2024-01-03T00:00:00Z", namespace="Acme")]
    adapter = GitLabAdapter(transport=httpx.MockTransport(lambda r: _json_response(projects)))
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

    projects_renamed = [
        _project(1, path="acme/a", updated_at="2024-01-04T00:00:00Z", namespace="Acme Renamed")
    ]
    adapter2 = GitLabAdapter(
        transport=httpx.MockTransport(lambda r: _json_response(projects_renamed))
    )
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
        assert row.suggested_team_name == "Acme Renamed"
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


def test_backfill_paginates_via_link_header(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    page1 = [_project(1, path="acme/a", updated_at="2024-01-05T00:00:00Z")]
    page2 = [_project(2, path="acme/b", updated_at="2024-01-04T00:00:00Z")]

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page")
        if page == "1":
            return _json_response(
                page1,
                headers={
                    "Link": (
                        '<https://gitlab.com/api/v4/projects?page=2&q=a,b>; rel="next", '
                        '<https://gitlab.com/api/v4/projects?page=9>; rel="last"'
                    )
                },
            )
        if page == "2":
            return _json_response(page2)
        raise AssertionError(f"unexpected page {page}")

    adapter = GitLabAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(seeded_account_context, "repository")
    assert outcome.items_processed == 2
    assert outcome.next_cursor == "2024-01-05T00:00:00Z"


def test_incremental_sync_stops_at_prior_cursor(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    projects = [
        _project(1, path="acme/a", updated_at="2024-01-05T00:00:00Z"),
        _project(2, path="acme/b", updated_at="2024-01-01T00:00:00Z"),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(projects)

    adapter = GitLabAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.incremental_sync(
        seeded_account_context, "repository", cursor="2024-01-02T00:00:00Z"
    )
    assert outcome.items_processed == 1
    assert outcome.next_cursor == "2024-01-05T00:00:00Z"


def test_incremental_sync_with_no_cursor_behaves_like_backfill(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    projects = [_project(1, path="acme/a", updated_at="2024-01-01T00:00:00Z")]

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(projects)

    adapter = GitLabAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.incremental_sync(seeded_account_context, "repository", cursor=None)
    assert outcome.items_processed == 1


def test_non_repository_resource_type_is_a_zero_item_no_op() -> None:
    adapter = GitLabAdapter(transport=httpx.MockTransport(lambda r: _json_response([])))
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
                {"message": "rate limited"}, status_code=429, headers={"Retry-After": "1"}
            )
        return _json_response([_project(1, path="acme/a", updated_at="2024-01-01T00:00:00Z")])

    adapter = GitLabAdapter(
        transport=httpx.MockTransport(handler), sleep=lambda seconds: sleeps.append(seconds)
    )
    outcome = adapter.backfill(seeded_account_context, "repository")
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1
    assert sleeps == [1.0]


def test_rate_limit_gives_up_beyond_bounded_wait() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {"message": "rate limited"}, status_code=429, headers={"Retry-After": "3600"}
        )

    adapter = GitLabAdapter(transport=httpx.MockTransport(handler), sleep=lambda seconds: None)
    outcome = adapter.backfill(_account_context(), "repository")
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

    adapter = GitLabAdapter(transport=httpx.MockTransport(handler), sleep=lambda seconds: None)
    outcome = adapter.backfill(_account_context(), "repository")
    assert outcome.status == "partial"
    assert outcome.items_processed == 0
    assert outcome.next_cursor is None
    assert outcome.error_summary is not None
    assert "rate limit" in outcome.error_summary.lower()
    assert calls["count"] == 2


def test_rate_limit_gives_up_immediately_when_retry_after_header_absent() -> None:
    """`_rate_limit_wait_seconds` returns `None` when `Retry-After` is
    absent entirely -- a distinct code path from "present but too large,"
    untested by review.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"message": "rate limited"}, status_code=429)

    adapter = GitLabAdapter(transport=httpx.MockTransport(handler), sleep=lambda seconds: None)
    outcome = adapter.backfill(_account_context(), "repository")
    assert outcome.status == "partial"
    assert outcome.items_processed == 0


def test_rate_limit_gives_up_immediately_when_retry_after_malformed() -> None:
    """`_rate_limit_wait_seconds`'s `except ValueError: return None` branch
    for a non-numeric `Retry-After` value -- untested by review.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {"message": "rate limited"}, status_code=429, headers={"Retry-After": "soon"}
        )

    adapter = GitLabAdapter(transport=httpx.MockTransport(handler), sleep=lambda seconds: None)
    outcome = adapter.backfill(_account_context(), "repository")
    assert outcome.status == "partial"
    assert outcome.items_processed == 0


def test_sync_repositories_raises_on_generic_failure_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"message": "Server error"}, status_code=500)

    adapter = GitLabAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="500"):
        adapter.backfill(_account_context(), "repository")


def test_page_cap_reports_partial_with_more_pages_remaining(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    from ecc.domains.engineering import gitlab_adapter as gitlab_adapter_module

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return _json_response(
            [_project(page, path=f"acme/r{page}", updated_at=f"2024-01-{page:02d}T00:00:00Z")],
            headers={"Link": '<https://gitlab.com/api/v4/projects?page=999>; rel="next"'},
        )

    adapter = GitLabAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(seeded_account_context, "repository")
    assert outcome.status == "partial"
    assert outcome.items_processed == gitlab_adapter_module._MAX_PAGES_PER_CALL
    assert outcome.next_cursor is not None
    assert outcome.error_summary is not None
    assert "page" in outcome.error_summary.lower()


def test_refresh_permissions() -> None:
    def unauthorized(request: httpx.Request) -> httpx.Response:
        return _json_response({"message": "401 Unauthorized"}, status_code=401)

    def revoked(request: httpx.Request) -> httpx.Response:
        return _json_response(_token_self_response(scopes=["read_api"], revoked=True))

    def active(request: httpx.Request) -> httpx.Response:
        return _json_response(_token_self_response(scopes=["read_api", "read_repository"]))

    lost_adapter = GitLabAdapter(transport=httpx.MockTransport(unauthorized))
    assert lost_adapter.refresh_permissions(_account_context()) == "permission_lost"

    revoked_adapter = GitLabAdapter(transport=httpx.MockTransport(revoked))
    assert revoked_adapter.refresh_permissions(_account_context()) == "permission_lost"

    active_adapter = GitLabAdapter(transport=httpx.MockTransport(active))
    assert active_adapter.refresh_permissions(_account_context()) == "active"


def test_refresh_permissions_fails_open_on_network_error() -> None:
    """Deliberate fail-open (matches `GitHubAdapter.refresh_permissions`'s
    identical precedent): a transient network failure must not be
    mistaken for a real permission-loss signal.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    adapter = GitLabAdapter(transport=httpx.MockTransport(handler))
    assert adapter.refresh_permissions(_account_context()) == "active"


def test_disconnect_succeeds_on_204() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "DELETE"
        assert request.url.path == "/api/v4/personal_access_tokens/self"
        return httpx.Response(status_code=204)

    adapter = GitLabAdapter(transport=httpx.MockTransport(handler))
    assert adapter.disconnect(_account_context()) is None


def test_disconnect_raises_on_expected_insufficient_scope_failure() -> None:
    """Unlike GitHub's hard no-op, GitLab's self-revocation endpoint is
    real -- and, per this connector's own read-only default scopes
    (`CONNECTOR-CONTRACT.md`), realistically expected to reject the
    attempt (403) rather than silently succeed. This proves the attempt is
    genuinely made (not silently skipped) and that a real failure
    surfaces as an exception -- `disable_connector_endpoint`'s own
    best-effort try/except is what actually absorbs it in production, not
    this adapter method itself.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"message": "403 Forbidden"}, status_code=403)

    adapter = GitLabAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="403"):
        adapter.disconnect(_account_context())


def test_disconnect_succeeds_on_404_already_revoked() -> None:
    """`404` (token already deleted/revoked out-of-band, e.g. through
    GitLab's own settings UI) is treated as success alongside GitLab's
    documented `204` -- disconnecting must not fail just because the
    provider-side token no longer exists to revoke.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"message": "404 Not Found"}, status_code=404)

    adapter = GitLabAdapter(transport=httpx.MockTransport(handler))
    assert adapter.disconnect(_account_context()) is None


def test_disconnect_raises_on_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    adapter = GitLabAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="revocation request failed"):
        adapter.disconnect(_account_context())


def test_handle_webhook_ignores_non_push_hook_events() -> None:
    adapter = GitLabAdapter()
    outcome = adapter.handle_webhook(
        _account_context(), b'{"object_kind": "issue"}', {"X-Gitlab-Event": "Issue Hook"}
    )
    assert outcome.items_processed == 0


def test_handle_webhook_ignores_empty_payload() -> None:
    adapter = GitLabAdapter()
    outcome = adapter.handle_webhook(_account_context(), b"", {"X-Gitlab-Event": "Push Hook"})
    assert outcome.items_processed == 0


def test_handle_webhook_upserts_on_real_push_hook_payload(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    """Review found `handle_webhook` previously gated on `X-Gitlab-Event:
    Project Hook` -- not a real GitLab webhook event value (GitLab has no
    such event; `Push Hook` is the real project-webhook event whose
    payload nests a `project` object). This proves the parsing/upsert
    logic itself actually works against a real `Push Hook` payload shape
    -- deliberately *not* reusing `_project()` (that helper's shape
    matches the full `GET /projects` REST representation, which is a
    genuinely different, larger schema than what GitLab's webhook events
    actually carry -- a real Push Hook's embedded `project` object has no
    `last_activity_at` field at all, only `commits`). Also proves the
    synthesized-timestamp fallback: `provider_updated_at` ends up as the
    most recent commit's own `timestamp`, not `NULL`.
    """
    adapter = GitLabAdapter()
    payload = dumps(
        {
            "object_kind": "push",
            "project": {
                "id": 1,
                "path_with_namespace": "acme/a",
                "web_url": "https://gitlab.com/acme/a",
                "default_branch": "main",
                # No `last_activity_at` -- real Push Hook payloads never
                # carry this field on the embedded `project` object.
            },
            "commits": [
                {"id": "abc123", "timestamp": "2024-01-01T00:00:00Z"},
                {"id": "def456", "timestamp": "2024-01-02T00:00:00Z"},
            ],
        }
    ).encode()

    outcome = adapter.handle_webhook(
        seeded_account_context, payload, {"X-Gitlab-Event": "Push Hook"}
    )
    assert outcome.items_processed == 1
    assert outcome.status == "succeeded"

    with engine.begin() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT name, provider_updated_at FROM repositories "
                    "WHERE workspace_id = :workspace_id"
                ),
                {"workspace_id": seeded_account_context.workspace_id},
            )
            .mappings()
            .one()
        )
    assert row["name"] == "acme/a"
    # The *last* commit in the array (GitLab orders oldest-first) is the
    # one whose timestamp becomes provider_updated_at -- not NULL, and not
    # the first commit's earlier timestamp.
    assert row["provider_updated_at"] == datetime(2024, 1, 2, tzinfo=UTC)


def test_handle_webhook_synthesizes_receipt_time_when_push_has_no_commits(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    """A tag-only push or branch deletion carries an empty `commits` array
    -- `provider_updated_at` must still end up as a real timestamp (the
    time of receipt), never `NULL`.
    """
    adapter = GitLabAdapter()
    payload = dumps(
        {
            "object_kind": "push",
            "project": {
                "id": 2,
                "path_with_namespace": "acme/b",
                "web_url": "https://gitlab.com/acme/b",
                "default_branch": "main",
            },
            "commits": [],
        }
    ).encode()

    outcome = adapter.handle_webhook(
        seeded_account_context, payload, {"X-Gitlab-Event": "Push Hook"}
    )
    assert outcome.items_processed == 1

    with engine.begin() as connection:
        provider_updated_at = connection.execute(
            text(
                "SELECT provider_updated_at FROM repositories "
                "WHERE workspace_id = :workspace_id AND external_id = '2'"
            ),
            {"workspace_id": seeded_account_context.workspace_id},
        ).scalar_one()
    assert provider_updated_at is not None


# --- integration: real /sync endpoint, mocked GitLab transport -------------


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
                "VALUES (:id, 'GitLab Sync Test', 'UTC', :now)"
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


def _insert_gitlab_connector_account(
    workspace_id: UUID, user_id: UUID, *, credential: str = "gitlab.com|glpat_fixture"
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
                    :id, :workspace_id, 'gitlab', 'gl-fixture-account', 'GitLab fixture',
                    ARRAY['read_api', 'read_repository'], :encrypted, 'active', 1,
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
    account_id = _insert_gitlab_connector_account(workspace_id, user_id)

    backfill_projects = [
        _project(1, path="acme/a", updated_at="2024-01-02T00:00:00Z"),
        _project(2, path="acme/b", updated_at="2024-01-01T00:00:00Z"),
    ]
    state = {"call": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["call"] += 1
        if state["call"] == 1:
            return _json_response(backfill_projects)
        return _json_response(
            [
                _project(3, path="acme/c", updated_at="2024-01-03T00:00:00Z"),
                _project(1, path="acme/a", updated_at="2024-01-02T00:00:00Z"),
            ]
        )

    registry = ConnectorRegistry()
    registry.register(GitLabAdapter(transport=httpx.MockTransport(handler)))
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
    account_id = _insert_gitlab_connector_account(workspace_id, user_id)

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {"message": "rate limited"}, status_code=429, headers={"Retry-After": "3600"}
        )

    registry = ConnectorRegistry()
    registry.register(
        GitLabAdapter(transport=httpx.MockTransport(handler), sleep=lambda seconds: None)
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
    # exception would (matches github_adapter.py's identical test).
    assert account_status == "active"
