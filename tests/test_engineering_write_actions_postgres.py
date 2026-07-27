"""Phase 6 Engineering Workspace Task 7: "Approved write actions"
(`backend/ecc/domains/engineering/write_actions.py`).

Covers, per that module's own disclosed scope (see its module docstring
for the full "why add-a-comment, why this containment, why this
retry-safety classification" reasoning):

1. The shared production `ecc.domains.automation.adapters.registry`
   resolves all three new adapters (`github.add_issue_comment`,
   `gitlab.add_note`, `jira.add_comment`), each declaring
   `high_impact_categories == {"public"}` and `reversible is True`.
2. Each adapter's `execute()` against a real Postgres-backed
   `connector_accounts`/`repositories`/`engineering_work_items` fixture,
   with a mocked HTTP transport (no real network call): success (a
   comment/note is "created," output never carries `body`), the
   connector-account-not-found/wrong-workspace/wrong-provider/
   inactive-connector rejections, and (GitHub/Jira only) the
   repository-not-synced/work-item-not-synced containment rejection.
3. Retry-safety classification: a pre-send connection failure raises
   `TransientAdapterError`; an HTTP 429 raises `TransientAdapterError`;
   a read timeout and a 4xx/5xx response both raise a plain (non-
   retried) exception.
4. `simulate()` never makes a network call (asserted via a mock
   transport that raises if invoked at all) and returns a preview output.
5. End-to-end through the real `ecc.domains.automation.worker` dispatch
   path for `github.add_issue_comment`: `high_impact_categories={
   "public"}` pauses the run on `waiting_approval` before any HTTP call
   is made, and only dispatches (making the mocked HTTP call exactly
   once) after a real `decide_approval("approved")` call, mirroring
   `tests/test_automation_adapters_postgres.py`'s own `fake.
   external_action`/`local.send_test_notification` precedent -- proving
   this is dispatched through the real Phase 5 gate, not merely through
   a private call to `execute()`.
"""

from __future__ import annotations

import json
from base64 import b64encode
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.automation import adapters as automation_adapters
from ecc.domains.automation import approvals as automation_approvals
from ecc.domains.automation import policy as automation_policy
from ecc.domains.automation import worker as automation_worker
from ecc.domains.automation import workflows as automation_workflows
from ecc.domains.automation.adapters import AdapterRegistry, TransientAdapterError
from ecc.domains.engineering.crypto import encrypt_credential
from ecc.domains.engineering.write_actions import (
    GitHubAddIssueCommentAdapter,
    GitHubAddIssueCommentInput,
    GitHubAddIssueCommentOutput,
    GitLabAddNoteAdapter,
    GitLabAddNoteInput,
    GitLabAddNoteOutput,
    JiraAddCommentAdapter,
    JiraAddCommentInput,
    JiraAddCommentOutput,
    WriteActionRejected,
)

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


# --- fixtures ----------------------------------------------------------------


@pytest.fixture
def write_actions_test_context() -> Iterator[tuple[UUID, UUID]]:
    workspace_id = uuid4()
    user_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Write Actions Test', 'UTC', :now)"
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
    try:
        yield workspace_id, user_id
    finally:
        _cleanup_workspace(workspace_id)


def _cleanup_workspace(workspace_id: UUID) -> None:
    with engine.begin() as connection:
        for table in (
            "approval_requests",
            "workflow_run_steps",
            "workflow_runs",
            "automation_policies",
            "workflow_versions",
            "workflow_definitions",
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
    workspace_id: UUID,
    user_id: UUID,
    *,
    provider: str,
    credential: str,
    status: str = "active",
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
                    ARRAY[]::text[], :cred, :status, 1, :actor, :actor, :now, :now
                )
                """
            ),
            {
                "id": account_id,
                "workspace_id": workspace_id,
                "provider": provider,
                "ext": f"{provider}-acct-{account_id}",
                "cred": encrypt_credential(credential),
                "status": status,
                "actor": user_id,
                "now": now,
            },
        )
    return account_id


def _insert_repository(
    workspace_id: UUID, connector_account_id: UUID, *, name: str = "acme/widgets"
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
                    'active', 'fresh', :now, :now, :now
                )
                """
            ),
            {
                "id": repository_id,
                "ws": workspace_id,
                "acct": connector_account_id,
                "ext": f"repo-{repository_id}",
                "name": name,
                "now": now,
            },
        )
    return repository_id


def _insert_work_item(
    workspace_id: UUID,
    connector_account_id: UUID,
    *,
    external_id: str = "10001",
    source_url: str = "https://acme.atlassian.net/browse/PROJ-5",
) -> UUID:
    """`external_id` (Jira's internal numeric issue id) and `source_url`
    (built from the human-facing issue *key*, e.g. "PROJ-5") are
    deliberately different values by default -- matching real
    `jira_adapter.py` sync data exactly, and proving `jira.add_comment`'s
    output `source_url` is derived from the stored `source_url` column,
    not reconstructed from `external_id` (which would silently produce a
    broken `/browse/{numeric-id}` link).
    """
    work_item_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO engineering_work_items (
                    id, workspace_id, connector_account_id, provider, external_id,
                    title, source_url, item_type, status, permission_state, freshness_state,
                    observed_at, created_at, updated_at
                ) VALUES (
                    :id, :ws, :acct, 'jira', :ext, 'A work item', :source_url, 'Bug', 'To Do',
                    'active', 'fresh', :now, :now, :now
                )
                """
            ),
            {
                "id": work_item_id,
                "ws": workspace_id,
                "acct": connector_account_id,
                "ext": external_id,
                "source_url": source_url,
                "now": now,
            },
        )
    return work_item_id


def _json_response(status_code: int, payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


# ---------------------------------------------------------------------------
# 1. Shared registry resolution
# ---------------------------------------------------------------------------


def test_shared_registry_resolves_all_three_write_adapters() -> None:
    for adapter_id in ("github.add_issue_comment", "gitlab.add_note", "jira.add_comment"):
        adapter = automation_adapters.registry.get(adapter_id)
        assert adapter is not None, f"{adapter_id} not registered in the shared registry"
        assert adapter.adapter_id == adapter_id
        assert adapter.high_impact_categories == frozenset({"public"})
        assert adapter.reversible is True


# ---------------------------------------------------------------------------
# 2. github.add_issue_comment
# ---------------------------------------------------------------------------


def test_github_add_issue_comment_success_excludes_body_from_output(
    write_actions_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = write_actions_test_context
    account_id = _insert_connector_account(
        workspace_id, user_id, provider="github", credential="ghp_x"
    )
    repository_id = _insert_repository(workspace_id, account_id, name="acme/widgets")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/acme/widgets/issues/42/comments"
        assert request.headers["Authorization"] == "Bearer ghp_x"
        return _json_response(
            201, {"id": 555, "html_url": "https://github.com/acme/widgets/issues/42#comment-555"}
        )

    adapter = GitHubAddIssueCommentAdapter(transport=httpx.MockTransport(handler))
    action_input = GitHubAddIssueCommentInput(
        workspace_id=workspace_id,
        actor_id=user_id,
        connector_account_id=account_id,
        repository_id=repository_id,
        issue_number=42,
        body="A secret-looking token: ghp_shouldnotleak",
    )
    output = adapter.execute(action_input)
    assert isinstance(output, GitHubAddIssueCommentOutput)
    assert output.comment_external_id == "555"
    assert output.source_url == "https://github.com/acme/widgets/issues/42#comment-555"
    assert not hasattr(output, "body")
    assert "shouldnotleak" not in output.model_dump_json()


def test_github_add_issue_comment_rejects_repository_not_synced_by_this_connector(
    write_actions_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = write_actions_test_context
    account_id = _insert_connector_account(
        workspace_id, user_id, provider="github", credential="ghp_x"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not make a network call when containment check fails")

    adapter = GitHubAddIssueCommentAdapter(transport=httpx.MockTransport(handler))
    action_input = GitHubAddIssueCommentInput(
        workspace_id=workspace_id,
        actor_id=user_id,
        connector_account_id=account_id,
        repository_id=uuid4(),
        issue_number=1,
        body="x",
    )
    with pytest.raises(WriteActionRejected):
        adapter.execute(action_input)


def test_github_add_issue_comment_rejects_connector_account_in_different_workspace(
    write_actions_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = write_actions_test_context
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
            other_workspace_id, other_user_id, provider="github", credential="ghp_other"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("must not make a network call for a cross-workspace account")

        adapter = GitHubAddIssueCommentAdapter(transport=httpx.MockTransport(handler))
        action_input = GitHubAddIssueCommentInput(
            workspace_id=workspace_id,
            actor_id=user_id,
            connector_account_id=other_account_id,
            repository_id=uuid4(),
            issue_number=1,
            body="x",
        )
        with pytest.raises(WriteActionRejected):
            adapter.execute(action_input)
    finally:
        _cleanup_workspace(other_workspace_id)


def test_github_add_issue_comment_rejects_wrong_provider_connector_account(
    write_actions_test_context: tuple[UUID, UUID],
) -> None:
    """A real, synced repository row must exist for `repository_id`
    (contrary to `_insert_repository`'s own hardcoded `provider = 'github'`
    literal, unrelated to the connector account's own provider column) --
    otherwise the containment check alone (not the provider check this
    test means to isolate) would already reject the call, and the test
    would pass even if `_load_credential`'s provider check were deleted.
    """
    workspace_id, user_id = write_actions_test_context
    jira_account_id = _insert_connector_account(
        workspace_id, user_id, provider="jira", credential="site.atlassian.net|a@b.com|tok"
    )
    repository_id = _insert_repository(workspace_id, jira_account_id)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not make a network call for a wrong-provider account")

    adapter = GitHubAddIssueCommentAdapter(transport=httpx.MockTransport(handler))
    action_input = GitHubAddIssueCommentInput(
        workspace_id=workspace_id,
        actor_id=user_id,
        connector_account_id=jira_account_id,
        repository_id=repository_id,
        issue_number=1,
        body="x",
    )
    with pytest.raises(WriteActionRejected):
        adapter.execute(action_input)


def test_github_add_issue_comment_rejects_disconnected_connector_account(
    write_actions_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = write_actions_test_context
    account_id = _insert_connector_account(
        workspace_id, user_id, provider="github", credential="ghp_x", status="disconnected"
    )
    repository_id = _insert_repository(workspace_id, account_id)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not make a network call for a disconnected account")

    adapter = GitHubAddIssueCommentAdapter(transport=httpx.MockTransport(handler))
    action_input = GitHubAddIssueCommentInput(
        workspace_id=workspace_id,
        actor_id=user_id,
        connector_account_id=account_id,
        repository_id=repository_id,
        issue_number=1,
        body="x",
    )
    with pytest.raises(WriteActionRejected):
        adapter.execute(action_input)


def test_github_add_issue_comment_connection_failure_is_transient(
    write_actions_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = write_actions_test_context
    account_id = _insert_connector_account(
        workspace_id, user_id, provider="github", credential="ghp_x"
    )
    repository_id = _insert_repository(workspace_id, account_id)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    adapter = GitHubAddIssueCommentAdapter(transport=httpx.MockTransport(handler))
    action_input = GitHubAddIssueCommentInput(
        workspace_id=workspace_id,
        actor_id=user_id,
        connector_account_id=account_id,
        repository_id=repository_id,
        issue_number=1,
        body="x",
    )
    with pytest.raises(TransientAdapterError):
        adapter.execute(action_input)


def test_github_add_issue_comment_read_timeout_is_not_transient(
    write_actions_test_context: tuple[UUID, UUID],
) -> None:
    """A timeout on a POST is genuinely ambiguous -- the comment may have
    already been created before the response was lost -- so this must
    raise a plain exception (never retried), not `TransientAdapterError`.
    """
    workspace_id, user_id = write_actions_test_context
    account_id = _insert_connector_account(
        workspace_id, user_id, provider="github", credential="ghp_x"
    )
    repository_id = _insert_repository(workspace_id, account_id)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    adapter = GitHubAddIssueCommentAdapter(transport=httpx.MockTransport(handler))
    action_input = GitHubAddIssueCommentInput(
        workspace_id=workspace_id,
        actor_id=user_id,
        connector_account_id=account_id,
        repository_id=repository_id,
        issue_number=1,
        body="x",
    )
    with pytest.raises(RuntimeError):
        adapter.execute(action_input)


def test_github_add_issue_comment_rate_limited_is_transient(
    write_actions_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = write_actions_test_context
    account_id = _insert_connector_account(
        workspace_id, user_id, provider="github", credential="ghp_x"
    )
    repository_id = _insert_repository(workspace_id, account_id)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "1"})

    adapter = GitHubAddIssueCommentAdapter(transport=httpx.MockTransport(handler))
    action_input = GitHubAddIssueCommentInput(
        workspace_id=workspace_id,
        actor_id=user_id,
        connector_account_id=account_id,
        repository_id=repository_id,
        issue_number=1,
        body="x",
    )
    with pytest.raises(TransientAdapterError):
        adapter.execute(action_input)


def test_github_add_issue_comment_4xx_is_not_transient(
    write_actions_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = write_actions_test_context
    account_id = _insert_connector_account(
        workspace_id, user_id, provider="github", credential="ghp_x"
    )
    repository_id = _insert_repository(workspace_id, account_id)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "Not Found"})

    adapter = GitHubAddIssueCommentAdapter(transport=httpx.MockTransport(handler))
    action_input = GitHubAddIssueCommentInput(
        workspace_id=workspace_id,
        actor_id=user_id,
        connector_account_id=account_id,
        repository_id=repository_id,
        issue_number=1,
        body="x",
    )
    with pytest.raises(RuntimeError):
        adapter.execute(action_input)


def test_github_add_issue_comment_simulate_makes_no_network_call(
    write_actions_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = write_actions_test_context

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("simulate() must never make a network call")

    adapter = GitHubAddIssueCommentAdapter(transport=httpx.MockTransport(handler))
    action_input = GitHubAddIssueCommentInput(
        workspace_id=workspace_id,
        actor_id=user_id,
        connector_account_id=uuid4(),
        repository_id=uuid4(),
        issue_number=1,
        body="x",
    )
    output = adapter.simulate(action_input)
    assert isinstance(output, GitHubAddIssueCommentOutput)
    assert output.workspace_id == workspace_id


# ---------------------------------------------------------------------------
# 3. gitlab.add_note (no synced-projection containment, disclosed)
# ---------------------------------------------------------------------------


def test_gitlab_add_note_success_excludes_body_from_output(
    write_actions_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = write_actions_test_context
    account_id = _insert_connector_account(
        workspace_id, user_id, provider="gitlab", credential="glpat-x"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        # `.path` decodes `%2F` back to `/` for display -- assert against
        # `.raw_path` (the literal bytes sent on the wire) to actually
        # prove the project path was percent-encoded, not merely that a
        # slash appears somewhere in a human-readable rendering.
        assert request.url.raw_path == b"/api/v4/projects/acme%2Fwidgets/issues/7/notes"
        assert request.headers["PRIVATE-TOKEN"] == "glpat-x"
        return _json_response(201, {"id": 99})

    adapter = GitLabAddNoteAdapter(transport=httpx.MockTransport(handler))
    action_input = GitLabAddNoteInput(
        workspace_id=workspace_id,
        actor_id=user_id,
        connector_account_id=account_id,
        project_path="acme/widgets",
        issue_iid=7,
        body="a secret-looking value: glpat-shouldnotleak",
    )
    output = adapter.execute(action_input)
    assert isinstance(output, GitLabAddNoteOutput)
    assert output.note_external_id == "99"
    assert "note_99" in output.source_url
    assert "shouldnotleak" not in output.model_dump_json()


def test_gitlab_add_note_rejects_nonexistent_connector_account(
    write_actions_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = write_actions_test_context

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not make a network call for a nonexistent account")

    adapter = GitLabAddNoteAdapter(transport=httpx.MockTransport(handler))
    action_input = GitLabAddNoteInput(
        workspace_id=workspace_id,
        actor_id=user_id,
        connector_account_id=uuid4(),
        project_path="acme/widgets",
        issue_iid=1,
        body="x",
    )
    with pytest.raises(WriteActionRejected):
        adapter.execute(action_input)


def test_gitlab_add_note_rejects_connector_account_in_different_workspace(
    write_actions_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = write_actions_test_context
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
            other_workspace_id, other_user_id, provider="gitlab", credential="glpat-other"
        )

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("must not make a network call for a cross-workspace account")

        adapter = GitLabAddNoteAdapter(transport=httpx.MockTransport(handler))
        action_input = GitLabAddNoteInput(
            workspace_id=workspace_id,
            actor_id=user_id,
            connector_account_id=other_account_id,
            project_path="acme/widgets",
            issue_iid=1,
            body="x",
        )
        with pytest.raises(WriteActionRejected):
            adapter.execute(action_input)
    finally:
        _cleanup_workspace(other_workspace_id)


def test_gitlab_add_note_rejects_wrong_provider_connector_account(
    write_actions_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = write_actions_test_context
    github_account_id = _insert_connector_account(
        workspace_id, user_id, provider="github", credential="ghp_x"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not make a network call for a wrong-provider account")

    adapter = GitLabAddNoteAdapter(transport=httpx.MockTransport(handler))
    action_input = GitLabAddNoteInput(
        workspace_id=workspace_id,
        actor_id=user_id,
        connector_account_id=github_account_id,
        project_path="acme/widgets",
        issue_iid=1,
        body="x",
    )
    with pytest.raises(WriteActionRejected):
        adapter.execute(action_input)


def test_gitlab_add_note_rejects_disconnected_connector_account(
    write_actions_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = write_actions_test_context
    account_id = _insert_connector_account(
        workspace_id, user_id, provider="gitlab", credential="glpat-x", status="disconnected"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not make a network call for a disconnected account")

    adapter = GitLabAddNoteAdapter(transport=httpx.MockTransport(handler))
    action_input = GitLabAddNoteInput(
        workspace_id=workspace_id,
        actor_id=user_id,
        connector_account_id=account_id,
        project_path="acme/widgets",
        issue_iid=1,
        body="x",
    )
    with pytest.raises(WriteActionRejected):
        adapter.execute(action_input)


def test_gitlab_add_note_connection_failure_is_transient(
    write_actions_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = write_actions_test_context
    account_id = _insert_connector_account(
        workspace_id, user_id, provider="gitlab", credential="glpat-x"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    adapter = GitLabAddNoteAdapter(transport=httpx.MockTransport(handler))
    action_input = GitLabAddNoteInput(
        workspace_id=workspace_id,
        actor_id=user_id,
        connector_account_id=account_id,
        project_path="acme/widgets",
        issue_iid=1,
        body="x",
    )
    with pytest.raises(TransientAdapterError):
        adapter.execute(action_input)


def test_gitlab_add_note_read_timeout_is_not_transient(
    write_actions_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = write_actions_test_context
    account_id = _insert_connector_account(
        workspace_id, user_id, provider="gitlab", credential="glpat-x"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    adapter = GitLabAddNoteAdapter(transport=httpx.MockTransport(handler))
    action_input = GitLabAddNoteInput(
        workspace_id=workspace_id,
        actor_id=user_id,
        connector_account_id=account_id,
        project_path="acme/widgets",
        issue_iid=1,
        body="x",
    )
    with pytest.raises(RuntimeError):
        adapter.execute(action_input)


def test_gitlab_add_note_rate_limited_is_transient(
    write_actions_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = write_actions_test_context
    account_id = _insert_connector_account(
        workspace_id, user_id, provider="gitlab", credential="glpat-x"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "1"})

    adapter = GitLabAddNoteAdapter(transport=httpx.MockTransport(handler))
    action_input = GitLabAddNoteInput(
        workspace_id=workspace_id,
        actor_id=user_id,
        connector_account_id=account_id,
        project_path="acme/widgets",
        issue_iid=1,
        body="x",
    )
    with pytest.raises(TransientAdapterError):
        adapter.execute(action_input)


def test_gitlab_add_note_4xx_is_not_transient(
    write_actions_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = write_actions_test_context
    account_id = _insert_connector_account(
        workspace_id, user_id, provider="gitlab", credential="glpat-x"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "404 Not found"})

    adapter = GitLabAddNoteAdapter(transport=httpx.MockTransport(handler))
    action_input = GitLabAddNoteInput(
        workspace_id=workspace_id,
        actor_id=user_id,
        connector_account_id=account_id,
        project_path="acme/widgets",
        issue_iid=1,
        body="x",
    )
    with pytest.raises(RuntimeError):
        adapter.execute(action_input)


def test_gitlab_add_note_simulate_makes_no_network_call(
    write_actions_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = write_actions_test_context

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("simulate() must never make a network call")

    adapter = GitLabAddNoteAdapter(transport=httpx.MockTransport(handler))
    action_input = GitLabAddNoteInput(
        workspace_id=workspace_id,
        actor_id=user_id,
        connector_account_id=uuid4(),
        project_path="acme/widgets",
        issue_iid=1,
        body="x",
    )
    output = adapter.simulate(action_input)
    assert isinstance(output, GitLabAddNoteOutput)
    assert output.workspace_id == workspace_id


def test_gitlab_add_note_no_containment_check_by_design(
    write_actions_test_context: tuple[UUID, UUID],
) -> None:
    """Disclosed asymmetry, made explicit (not merely incidental): unlike
    `github.add_issue_comment`/`jira.add_comment`, `gitlab.add_note` has
    no synced-projection lookup at all -- an arbitrary `project_path`/
    `issue_iid` this workspace's connector never actually observed still
    succeeds, because GitLab's own change/review sync (the table this
    would validate against) does not exist yet. See this module's own
    docstring for the full reasoning.
    """
    workspace_id, user_id = write_actions_test_context
    account_id = _insert_connector_account(
        workspace_id, user_id, provider="gitlab", credential="glpat-x"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(201, {"id": 42})

    adapter = GitLabAddNoteAdapter(transport=httpx.MockTransport(handler))
    action_input = GitLabAddNoteInput(
        workspace_id=workspace_id,
        actor_id=user_id,
        connector_account_id=account_id,
        project_path="nonexistent/never-synced",
        issue_iid=999,
        body="x",
    )
    output = adapter.execute(action_input)
    assert isinstance(output, GitLabAddNoteOutput)
    assert output.note_external_id == "42"


# ---------------------------------------------------------------------------
# 4. jira.add_comment
# ---------------------------------------------------------------------------


def test_jira_add_comment_success_excludes_body_from_output(
    write_actions_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = write_actions_test_context
    account_id = _insert_connector_account(
        workspace_id, user_id, provider="jira", credential="acme.atlassian.net|a@b.com|tok"
    )
    work_item_id = _insert_work_item(
        workspace_id,
        account_id,
        external_id="10001",
        source_url="https://acme.atlassian.net/browse/PROJ-5",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "acme.atlassian.net"
        assert request.url.path == "/rest/api/3/issue/10001/comment"
        assert request.headers["Authorization"] == ("Basic " + b64encode(b"a@b.com:tok").decode())
        body = json.loads(request.content)
        assert body == {
            "body": {
                "type": "doc",
                "version": 1,
                "content": [
                    {
                        "type": "paragraph",
                        "content": [
                            {"type": "text", "text": "a secret-looking value: tok-shouldnotleak"}
                        ],
                    }
                ],
            }
        }
        return _json_response(201, {"id": "20002"})

    adapter = JiraAddCommentAdapter(transport=httpx.MockTransport(handler))
    action_input = JiraAddCommentInput(
        workspace_id=workspace_id,
        actor_id=user_id,
        connector_account_id=account_id,
        work_item_id=work_item_id,
        body="a secret-looking value: tok-shouldnotleak",
    )
    output = adapter.execute(action_input)
    assert isinstance(output, JiraAddCommentOutput)
    assert output.comment_external_id == "20002"
    # The 10001-vs-PROJ-5 distinction is the whole point: `external_id`
    # (Jira's internal numeric id, used for the API call above) must
    # never leak into the *link* -- only the real, browsable issue key
    # (from the work item's own stored `source_url`) may.
    assert output.source_url == "https://acme.atlassian.net/browse/PROJ-5?focusedCommentId=20002"
    assert "10001" not in output.source_url
    assert "shouldnotleak" not in output.model_dump_json()


def test_jira_add_comment_rejects_work_item_not_synced_by_this_connector(
    write_actions_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = write_actions_test_context
    account_id = _insert_connector_account(
        workspace_id, user_id, provider="jira", credential="acme.atlassian.net|a@b.com|tok"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not make a network call when containment check fails")

    adapter = JiraAddCommentAdapter(transport=httpx.MockTransport(handler))
    action_input = JiraAddCommentInput(
        workspace_id=workspace_id,
        actor_id=user_id,
        connector_account_id=account_id,
        work_item_id=uuid4(),
        body="x",
    )
    with pytest.raises(WriteActionRejected):
        adapter.execute(action_input)


def test_jira_add_comment_rejects_connector_account_in_different_workspace(
    write_actions_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = write_actions_test_context
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
            other_workspace_id,
            other_user_id,
            provider="jira",
            credential="other.atlassian.net|a@b.com|tok",
        )

        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("must not make a network call for a cross-workspace account")

        adapter = JiraAddCommentAdapter(transport=httpx.MockTransport(handler))
        action_input = JiraAddCommentInput(
            workspace_id=workspace_id,
            actor_id=user_id,
            connector_account_id=other_account_id,
            work_item_id=uuid4(),
            body="x",
        )
        with pytest.raises(WriteActionRejected):
            adapter.execute(action_input)
    finally:
        _cleanup_workspace(other_workspace_id)


def test_jira_add_comment_rejects_wrong_provider_connector_account(
    write_actions_test_context: tuple[UUID, UUID],
) -> None:
    """A real, synced work item row must exist for `work_item_id` (its
    containment query filters on `connector_account_id` and `provider =
    'jira'` literally, independent of the connector account's own
    provider column) -- otherwise the containment check alone (not the
    provider check this test means to isolate) would already reject the
    call, and the test would pass even if `_load_credential`'s provider
    check were deleted.
    """
    workspace_id, user_id = write_actions_test_context
    github_account_id = _insert_connector_account(
        workspace_id, user_id, provider="github", credential="ghp_x"
    )
    work_item_id = _insert_work_item(workspace_id, github_account_id)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not make a network call for a wrong-provider account")

    adapter = JiraAddCommentAdapter(transport=httpx.MockTransport(handler))
    action_input = JiraAddCommentInput(
        workspace_id=workspace_id,
        actor_id=user_id,
        connector_account_id=github_account_id,
        work_item_id=work_item_id,
        body="x",
    )
    with pytest.raises(WriteActionRejected):
        adapter.execute(action_input)


def test_jira_add_comment_rejects_disconnected_connector_account(
    write_actions_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = write_actions_test_context
    account_id = _insert_connector_account(
        workspace_id,
        user_id,
        provider="jira",
        credential="acme.atlassian.net|a@b.com|tok",
        status="disconnected",
    )
    work_item_id = _insert_work_item(workspace_id, account_id)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not make a network call for a disconnected account")

    adapter = JiraAddCommentAdapter(transport=httpx.MockTransport(handler))
    action_input = JiraAddCommentInput(
        workspace_id=workspace_id,
        actor_id=user_id,
        connector_account_id=account_id,
        work_item_id=work_item_id,
        body="x",
    )
    with pytest.raises(WriteActionRejected):
        adapter.execute(action_input)


def test_jira_add_comment_connection_failure_is_transient(
    write_actions_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = write_actions_test_context
    account_id = _insert_connector_account(
        workspace_id, user_id, provider="jira", credential="acme.atlassian.net|a@b.com|tok"
    )
    work_item_id = _insert_work_item(workspace_id, account_id)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    adapter = JiraAddCommentAdapter(transport=httpx.MockTransport(handler))
    action_input = JiraAddCommentInput(
        workspace_id=workspace_id,
        actor_id=user_id,
        connector_account_id=account_id,
        work_item_id=work_item_id,
        body="x",
    )
    with pytest.raises(TransientAdapterError):
        adapter.execute(action_input)


def test_jira_add_comment_read_timeout_is_not_transient(
    write_actions_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = write_actions_test_context
    account_id = _insert_connector_account(
        workspace_id, user_id, provider="jira", credential="acme.atlassian.net|a@b.com|tok"
    )
    work_item_id = _insert_work_item(workspace_id, account_id)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    adapter = JiraAddCommentAdapter(transport=httpx.MockTransport(handler))
    action_input = JiraAddCommentInput(
        workspace_id=workspace_id,
        actor_id=user_id,
        connector_account_id=account_id,
        work_item_id=work_item_id,
        body="x",
    )
    with pytest.raises(RuntimeError):
        adapter.execute(action_input)


def test_jira_add_comment_rate_limited_is_transient(
    write_actions_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = write_actions_test_context
    account_id = _insert_connector_account(
        workspace_id, user_id, provider="jira", credential="acme.atlassian.net|a@b.com|tok"
    )
    work_item_id = _insert_work_item(workspace_id, account_id)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "1"})

    adapter = JiraAddCommentAdapter(transport=httpx.MockTransport(handler))
    action_input = JiraAddCommentInput(
        workspace_id=workspace_id,
        actor_id=user_id,
        connector_account_id=account_id,
        work_item_id=work_item_id,
        body="x",
    )
    with pytest.raises(TransientAdapterError):
        adapter.execute(action_input)


def test_jira_add_comment_4xx_is_not_transient(
    write_actions_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = write_actions_test_context
    account_id = _insert_connector_account(
        workspace_id, user_id, provider="jira", credential="acme.atlassian.net|a@b.com|tok"
    )
    work_item_id = _insert_work_item(workspace_id, account_id)

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(404, {"errorMessages": ["Issue does not exist"]})

    adapter = JiraAddCommentAdapter(transport=httpx.MockTransport(handler))
    action_input = JiraAddCommentInput(
        workspace_id=workspace_id,
        actor_id=user_id,
        connector_account_id=account_id,
        work_item_id=work_item_id,
        body="x",
    )
    with pytest.raises(RuntimeError):
        adapter.execute(action_input)


def test_jira_add_comment_simulate_makes_no_network_call(
    write_actions_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = write_actions_test_context

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("simulate() must never make a network call")

    adapter = JiraAddCommentAdapter(transport=httpx.MockTransport(handler))
    action_input = JiraAddCommentInput(
        workspace_id=workspace_id,
        actor_id=user_id,
        connector_account_id=uuid4(),
        work_item_id=uuid4(),
        body="x",
    )
    output = adapter.simulate(action_input)
    assert isinstance(output, JiraAddCommentOutput)
    assert output.workspace_id == workspace_id


# ---------------------------------------------------------------------------
# 5. github.add_issue_comment end-to-end through the real worker dispatch path
# ---------------------------------------------------------------------------


def _action_step(
    step_id: str, action_ref: str, *, input_mapping: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "step_id": step_id,
        "step_type": "action",
        "action_ref": action_ref,
        "input_mapping": input_mapping or {},
        "on_success": "succeeded",
        "on_failure": "failed",
    }


def _publish_workflow(
    workspace_id: UUID, user_id: UUID, workflow_id: str, graph: dict[str, Any]
) -> automation_workflows.WorkflowVersion:
    with SessionFactory() as session, session.begin():
        automation_workflows.create_workflow_draft(
            session,
            workspace_id,
            user_id,
            workflow_id=workflow_id,
            graph=graph,
            trigger_refs=[],
            policy_ref=None,
        )
    with SessionFactory() as session, session.begin():
        policy_row = automation_policy.create_policy(
            session,
            workspace_id,
            user_id,
            workflow_id=workflow_id,
            action_types=[],
            data_classes=[],
            value_limit=Decimal("1000000"),
            count_limit=1000,
            rate_limit=None,
            schedule=None,
            approval_mode="bounded_recurring",
        )
    with SessionFactory() as session, session.begin():
        draft = automation_workflows.create_workflow_draft(
            session,
            workspace_id,
            user_id,
            workflow_id=workflow_id,
            graph=graph,
            trigger_refs=[],
            policy_ref=policy_row.id,
        )
        activated = automation_workflows.activate_workflow_version(session, workspace_id, draft.id)
    assert isinstance(activated, automation_workflows.WorkflowVersion)
    return activated


def test_github_add_issue_comment_dispatches_through_the_real_worker_gate(
    write_actions_test_context: tuple[UUID, UUID],
) -> None:
    """Proves this adapter is genuinely reached through Phase 5's own
    `worker.py` dispatch loop -- pausing on `waiting_approval` before any
    HTTP call, then dispatching (making the mocked HTTP call exactly
    once) only after a real `decide_approval("approved")` -- not merely
    through a private, direct call to `execute()`. Uses a throwaway
    `AdapterRegistry` holding a mock-transport-configured instance under
    the identical `adapter_id` the shared production registry uses, so
    the worker's own dispatch/gate/approval machinery is exercised
    unmodified while the HTTP call itself never reaches a real network.
    """
    workspace_id, user_id = write_actions_test_context
    account_id = _insert_connector_account(
        workspace_id, user_id, provider="github", credential="ghp_x"
    )
    repository_id = _insert_repository(workspace_id, account_id, name="acme/widgets")

    call_count = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        return _json_response(
            201, {"id": 777, "html_url": "https://github.com/acme/widgets/issues/9#comment-777"}
        )

    test_registry = AdapterRegistry()
    test_registry.register(GitHubAddIssueCommentAdapter(transport=httpx.MockTransport(handler)))

    workflow_id = f"test.write-actions.github-comment.{uuid4().hex}"
    graph = {
        "steps": [
            _action_step(
                "s1",
                "github.add_issue_comment",
                input_mapping={
                    "workspace_id": str(workspace_id),
                    "actor_id": str(user_id),
                    "connector_account_id": str(account_id),
                    "repository_id": str(repository_id),
                    "issue_number": 9,
                    "body": "Posted by an automation run.",
                },
            )
        ]
    }
    _publish_workflow(workspace_id, user_id, workflow_id, graph)

    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, workspace_id, user_id, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun)

    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
        assert claimed is not None
        paused = automation_worker.process_claimed_run(session, claimed, test_registry, "worker-a")
    assert paused.status == "waiting_approval"
    assert call_count["n"] == 0

    with SessionFactory() as session, session.begin():
        pending = automation_approvals.get_pending_approval(session, workspace_id, queued.id, 0)
        assert pending is not None
        assert pending.high_impact_categories == ("public",)
        decided = automation_approvals.decide_approval(
            session,
            workspace_id,
            user_id,
            pending.id,
            "approved",
            current_action_digest=pending.action_digest,
        )
    assert isinstance(decided, automation_approvals.ApprovalRequest)
    assert decided.status == "approved"

    with SessionFactory() as session:
        reclaimed = automation_worker.claim_next_run(session, "worker-b")
    assert reclaimed is not None
    with SessionFactory() as session:
        finished = automation_worker.process_claimed_run(
            session, reclaimed, test_registry, "worker-b"
        )
    assert finished.status == "succeeded"
    assert call_count["n"] == 1

    with SessionFactory() as session, session.begin():
        steps = automation_worker.list_run_steps(session, workspace_id, queued.id)
    assert len(steps) == 1
    assert steps[0].status == "succeeded"
    assert steps[0].output is not None
    assert steps[0].output["comment_external_id"] == "777"
    assert "body" not in steps[0].output
