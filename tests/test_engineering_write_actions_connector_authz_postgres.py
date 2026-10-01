"""Automation write actions authorize the acting user against the chosen
connector (`backend/ecc/domains/engineering/write_actions.py`
`_load_credential`).

A workflow's graph-authored `input_mapping` can name any
`connector_account_id` in the workspace, any write-role member can start a
run, and approval requests are workspace-visible -- so before this check a
member could post a comment through a connector they cannot even see (one
another member made `private`, or shared explicitly with someone else, or
had ownership transferred away from them). These tests pin the per-actor
`authz.authorize(..., resource_type="connector_accounts", action="read"/
"write")` gate: unit-level for all three adapters, grant/role edges, and
end-to-end through the real worker + approval path (approved by the very
member who started the run), asserting the provider is never called.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from identity_fixtures import create_identity
from lock_race_support import holder_backend_pid, wait_for_lock_waiter
from pydantic import BaseModel
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.domains.automation import approvals as automation_approvals
from ecc.domains.automation import policy as automation_policy
from ecc.domains.automation import worker as automation_worker
from ecc.domains.automation import workflows as automation_workflows
from ecc.domains.automation.adapters import AdapterRegistry
from ecc.domains.engineering.crypto import encrypt_credential
from ecc.domains.engineering.write_actions import (
    GitHubAddIssueCommentAdapter,
    GitHubAddIssueCommentInput,
    GitLabAddNoteAdapter,
    GitLabAddNoteInput,
    JiraAddCommentAdapter,
    JiraAddCommentInput,
    WriteActionRejected,
)

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


@dataclass(frozen=True)
class _Workspace:
    workspace_id: UUID
    owner_user_id: UUID
    member_user_id: UUID
    member_account_id: UUID


@pytest.fixture
def two_member_workspace() -> Iterator[_Workspace]:
    workspace_id = uuid4()
    owner_user_id = uuid4()
    member_user_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Write Action Authz', 'UTC', :now)"
            ),
            {"id": workspace_id, "now": now},
        )
        create_identity(connection, workspace_id=workspace_id, user_id=owner_user_id, now=now)
        member_account_id = create_identity(
            connection,
            workspace_id=workspace_id,
            user_id=member_user_id,
            now=now,
            role="member",
        )
    try:
        yield _Workspace(workspace_id, owner_user_id, member_user_id, member_account_id)
    finally:
        with engine.begin() as connection:
            for table in (
                "resource_grants",
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
                    text(f"DELETE FROM {table} WHERE workspace_id = :ws"),  # noqa: S608
                    {"ws": workspace_id},
                )
            connection.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": workspace_id})


def _insert_connector(
    ws: _Workspace, *, provider: str, owner_id: UUID, visibility: str, credential: str
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
                    created_by, updated_by, created_at, updated_at, owner_id, visibility
                ) VALUES (
                    :id, :ws, :provider, :ext, 'Fixture account', ARRAY[]::text[], :cred,
                    'active', 1, :owner, :owner, :now, :now, :owner, :visibility
                )
                """
            ),
            {
                "id": account_id,
                "ws": ws.workspace_id,
                "provider": provider,
                "ext": f"{provider}-{account_id}",
                "cred": encrypt_credential(credential),
                "owner": owner_id,
                "visibility": visibility,
                "now": now,
            },
        )
    return account_id


def _insert_repository(ws: _Workspace, connector_account_id: UUID) -> UUID:
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
                    :id, :ws, :acct, 'github', :ext, 'acme/widgets', 'https://x', 'main',
                    'active', 'fresh', :now, :now, :now
                )
                """
            ),
            {
                "id": repository_id,
                "ws": ws.workspace_id,
                "acct": connector_account_id,
                "ext": f"repo-{repository_id}",
                "now": now,
            },
        )
    return repository_id


def _insert_work_item(ws: _Workspace, connector_account_id: UUID) -> UUID:
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
                    :id, :ws, :acct, 'jira', :ext, 'A work item',
                    'https://acme.atlassian.net/browse/PROJ-5', 'Bug', 'To Do',
                    'active', 'fresh', :now, :now, :now
                )
                """
            ),
            {
                "id": work_item_id,
                "ws": ws.workspace_id,
                "acct": connector_account_id,
                "ext": str(uuid4().int)[:12],
                "now": now,
            },
        )
    return work_item_id


def _grant(ws: _Workspace, connector_account_id: UUID, actions: list[str]) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO resource_grants (
                    id, workspace_id, grantee_account_id, resource_type, resource_id,
                    actions, granted_by, expires_at, created_at
                ) VALUES (
                    :id, :ws, :grantee, 'connector_accounts', :rid, :actions, :by, NULL, :now
                )
                """
            ),
            {
                "id": uuid4(),
                "ws": ws.workspace_id,
                "grantee": ws.member_account_id,
                "rid": connector_account_id,
                "actions": actions,
                "by": ws.owner_user_id,
                "now": datetime.now(UTC),
            },
        )


class _RecordingTransport(httpx.MockTransport):
    def __init__(self) -> None:
        self.calls = 0
        super().__init__(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if request.url.host == "acme.atlassian.net":
            return httpx.Response(201, json={"id": "9"})
        return httpx.Response(201, json={"id": 9, "html_url": "https://example.test/c/9"})


# An adapter factory plus an input builder, per provider -- so the denial is
# proven for every `_load_credential` call site, not just GitHub's.
_Case = tuple[
    str, Callable[[httpx.BaseTransport], Any], Callable[[_Workspace, UUID, UUID], BaseModel]
]


def _github_input(ws: _Workspace, actor_id: UUID, account_id: UUID) -> BaseModel:
    return GitHubAddIssueCommentInput(
        workspace_id=ws.workspace_id,
        actor_id=actor_id,
        connector_account_id=account_id,
        repository_id=_insert_repository(ws, account_id),
        issue_number=1,
        body="hello",
    )


def _gitlab_input(ws: _Workspace, actor_id: UUID, account_id: UUID) -> BaseModel:
    return GitLabAddNoteInput(
        workspace_id=ws.workspace_id,
        actor_id=actor_id,
        connector_account_id=account_id,
        project_path="acme/widgets",
        issue_iid=1,
        body="hello",
    )


def _jira_input(ws: _Workspace, actor_id: UUID, account_id: UUID) -> BaseModel:
    return JiraAddCommentInput(
        workspace_id=ws.workspace_id,
        actor_id=actor_id,
        connector_account_id=account_id,
        work_item_id=_insert_work_item(ws, account_id),
        body="hello",
    )


_CASES: list[_Case] = [
    ("github", lambda t: GitHubAddIssueCommentAdapter(transport=t), _github_input),
    (
        "gitlab",
        lambda t: GitLabAddNoteAdapter(transport=t, resolve_host=lambda _h: ["93.184.216.34"]),
        _gitlab_input,
    ),
    ("jira", lambda t: JiraAddCommentAdapter(transport=t), _jira_input),
]

_CREDENTIALS = {
    "github": "ghp_owner_secret",
    "gitlab": '{"host": "https://gitlab.example.test", "token": "glpat-owner-secret"}',
    "jira": "acme.atlassian.net|o@example.test|owner-secret",
}


@pytest.mark.parametrize(
    ("provider", "make_adapter", "make_input"), _CASES, ids=["github", "gitlab", "jira"]
)
@pytest.mark.parametrize("visibility", ["private", "shared_explicitly"])
def test_member_cannot_use_another_members_restricted_connector(
    two_member_workspace: _Workspace,
    provider: str,
    make_adapter: Callable[[httpx.BaseTransport], Any],
    make_input: Callable[[_Workspace, UUID, UUID], BaseModel],
    visibility: str,
) -> None:
    ws = two_member_workspace
    account_id = _insert_connector(
        ws,
        provider=provider,
        owner_id=ws.owner_user_id,
        visibility=visibility,
        credential=_CREDENTIALS[provider],
    )
    transport = _RecordingTransport()
    adapter = make_adapter(transport)

    with pytest.raises(WriteActionRejected, match="not usable by actor"):
        adapter.execute(make_input(ws, ws.member_user_id, account_id))
    assert transport.calls == 0

    # Positive control: the connector's owner still can.
    adapter.execute(make_input(ws, ws.owner_user_id, account_id))
    assert transport.calls == 1


def test_connector_transferred_away_is_no_longer_usable_by_its_creator(
    two_member_workspace: _Workspace,
) -> None:
    """The member created the connector, then ownership moved to the
    workspace owner with private visibility: `created_by` is not authority."""
    ws = two_member_workspace
    account_id = _insert_connector(
        ws,
        provider="github",
        owner_id=ws.member_user_id,
        visibility="workspace",
        credential=_CREDENTIALS["github"],
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE connector_accounts SET owner_id = :owner, visibility = 'private' "
                "WHERE id = :id"
            ),
            {"owner": ws.owner_user_id, "id": account_id},
        )
    transport = _RecordingTransport()
    adapter = GitHubAddIssueCommentAdapter(transport=transport)
    with pytest.raises(WriteActionRejected):
        adapter.execute(_github_input(ws, ws.member_user_id, account_id))
    assert transport.calls == 0


@pytest.mark.parametrize(
    ("granted_actions", "allowed"),
    [(["read"], False), (["read", "write"], True)],
)
def test_explicit_grant_must_cover_write(
    two_member_workspace: _Workspace, granted_actions: list[str], allowed: bool
) -> None:
    ws = two_member_workspace
    account_id = _insert_connector(
        ws,
        provider="github",
        owner_id=ws.owner_user_id,
        visibility="shared_explicitly",
        credential=_CREDENTIALS["github"],
    )
    _grant(ws, account_id, granted_actions)
    transport = _RecordingTransport()
    adapter = GitHubAddIssueCommentAdapter(transport=transport)
    action_input = _github_input(ws, ws.member_user_id, account_id)
    if allowed:
        adapter.execute(action_input)
        assert transport.calls == 1
    else:
        with pytest.raises(WriteActionRejected):
            adapter.execute(action_input)
        assert transport.calls == 0


@pytest.mark.parametrize(("role", "status"), [("viewer", "active"), ("member", "removed")])
def test_actor_without_active_write_role_is_denied_on_workspace_connector(
    two_member_workspace: _Workspace, role: str, status: str
) -> None:
    ws = two_member_workspace
    account_id = _insert_connector(
        ws,
        provider="github",
        owner_id=ws.owner_user_id,
        visibility="workspace",
        credential=_CREDENTIALS["github"],
    )
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE workspace_memberships SET role = :role, status = :status "
                "WHERE workspace_id = :ws AND users_id = :uid"
            ),
            {"role": role, "status": status, "ws": ws.workspace_id, "uid": ws.member_user_id},
        )
    transport = _RecordingTransport()
    adapter = GitHubAddIssueCommentAdapter(transport=transport)
    with pytest.raises(WriteActionRejected):
        adapter.execute(_github_input(ws, ws.member_user_id, account_id))
    assert transport.calls == 0


def _publish_workflow(ws: _Workspace, workflow_id: str, graph: dict[str, Any]) -> None:
    author = ws.member_user_id
    with SessionFactory() as session, session.begin():
        automation_workflows.create_workflow_draft(
            session,
            ws.workspace_id,
            author,
            workflow_id=workflow_id,
            graph=graph,
            trigger_refs=[],
            policy_ref=None,
        )
    with SessionFactory() as session, session.begin():
        policy_row = automation_policy.create_policy(
            session,
            ws.workspace_id,
            author,
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
            ws.workspace_id,
            author,
            workflow_id=workflow_id,
            graph=graph,
            trigger_refs=[],
            policy_ref=policy_row.id,
        )
        activated = automation_workflows.activate_workflow_version(
            session, ws.workspace_id, draft.id
        )
    assert isinstance(activated, automation_workflows.WorkflowVersion)


def test_member_run_naming_another_members_private_connector_fails_after_self_approval(
    two_member_workspace: _Workspace,
) -> None:
    """End to end: the member authors and starts a run naming the owner's
    private GitHub connector, approves it themselves (approval requests are
    workspace-visible), and the worker still never sends the request."""
    ws = two_member_workspace
    account_id = _insert_connector(
        ws,
        provider="github",
        owner_id=ws.owner_user_id,
        visibility="private",
        credential=_CREDENTIALS["github"],
    )
    repository_id = _insert_repository(ws, account_id)
    transport = _RecordingTransport()
    registry = AdapterRegistry()
    registry.register(GitHubAddIssueCommentAdapter(transport=transport))

    workflow_id = f"test.write-action-authz.{uuid4().hex}"
    graph = {
        "steps": [
            {
                "step_id": "s1",
                "step_type": "action",
                "action_ref": "github.add_issue_comment",
                "input_mapping": {
                    "workspace_id": str(ws.workspace_id),
                    "actor_id": str(ws.member_user_id),
                    "connector_account_id": str(account_id),
                    "repository_id": str(repository_id),
                    "issue_number": 9,
                    "body": "Posted under someone else's identity.",
                },
                "on_success": "succeeded",
                "on_failure": "failed",
            }
        ]
    }
    _publish_workflow(ws, workflow_id, graph)

    with SessionFactory() as session, session.begin():
        queued = automation_worker.enqueue_run(
            session, ws.workspace_id, ws.member_user_id, workflow_id=workflow_id
        )
    assert isinstance(queued, automation_worker.WorkflowRun)

    with SessionFactory() as session:
        claimed = automation_worker.claim_next_run(session, "worker-a")
        assert claimed is not None and claimed.id == queued.id
        paused = automation_worker.process_claimed_run(session, claimed, registry, "worker-a")
    assert paused.status == "waiting_approval"

    with SessionFactory() as session, session.begin():
        pending = automation_approvals.get_pending_approval(session, ws.workspace_id, queued.id, 0)
        assert pending is not None
        decided = automation_approvals.decide_approval(
            session,
            ws.workspace_id,
            ws.member_user_id,
            pending.id,
            "approved",
            current_action_digest=pending.action_digest,
        )
    assert isinstance(decided, automation_approvals.ApprovalRequest)

    with SessionFactory() as session:
        reclaimed = automation_worker.claim_next_run(session, "worker-b")
        assert reclaimed is not None and reclaimed.id == queued.id
        finished = automation_worker.process_claimed_run(session, reclaimed, registry, "worker-b")
    assert finished.status == "failed"
    assert transport.calls == 0


def test_transfer_committing_during_the_lock_wait_is_seen_by_the_authorization(
    two_member_workspace: _Workspace,
) -> None:
    """Lock before authorizing: a concurrent ownership transfer holds the
    connector row; the write action waits on it, and once the transfer
    commits the authorization reads the post-transfer row -- the member
    who was allowed before the transfer is denied after it."""
    ws = two_member_workspace
    account_id = _insert_connector(
        ws,
        provider="github",
        owner_id=ws.member_user_id,
        visibility="workspace",
        credential=_CREDENTIALS["github"],
    )
    action_input = _github_input(ws, ws.member_user_id, account_id)
    transport = _RecordingTransport()
    adapter = GitHubAddIssueCommentAdapter(transport=transport)
    outcome: dict[str, BaseException | None] = {}

    def run_write() -> None:
        try:
            adapter.execute(action_input)
            outcome["error"] = None
        except BaseException as exc:  # noqa: BLE001 -- surfaced to the main thread
            outcome["error"] = exc

    with engine.connect() as transfer:
        transaction = transfer.begin()
        holder_pid = holder_backend_pid(transfer)
        transfer.execute(
            text(
                "UPDATE connector_accounts SET owner_id = :owner, visibility = 'private' "
                "WHERE id = :id"
            ),
            {"owner": ws.owner_user_id, "id": account_id},
        )
        worker = threading.Thread(target=run_write)
        worker.start()
        wait_for_lock_waiter("connector_accounts", holder_pid=holder_pid)
        transaction.commit()
    worker.join(timeout=10)
    assert not worker.is_alive()
    assert isinstance(outcome["error"], WriteActionRejected)
    assert transport.calls == 0
