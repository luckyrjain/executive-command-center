"""Phase 6 Engineering Workspace Task 7 -- "Approved write actions":
GitHub/GitLab/Jira write actions registered as `ecc.domains.automation.
adapters.ActionAdapter`s, dispatched through the existing Phase 5
`worker.py` gate under an `automation_policies` row. This module invents
no second authority mechanism -- everything about *whether* one of these
adapters is allowed to dispatch (policy usability, approval-mode,
digest-bound human approval, kill switches) is Phase 5's own, unchanged
machinery; see `ecc.domains.automation.worker`/`approvals`/`policy` for
that machinery itself.

**Scope: one action concept, three provider flavours -- add a comment to
an existing GitHub issue/PR, GitLab issue, or Jira issue.** Neither the
Task 7 plan-doc line ("GitHub/GitLab/Jira write actions ... no second
authority mechanism") nor the design doc names a concrete action
taxonomy -- scopes (`repo`/`api`/`write:jira-work`, `CONNECTOR-
CONTRACT.md`'s own table) are named, actions are not. "Add a comment" is
chosen deliberately over every other candidate (create issue/PR/MR, merge,
transition, label, close) for reasons worth stating plainly rather than
leaving implicit:

- **It is the only write uniformly, cleanly reversible across all three
  providers** (a comment can be deleted: GitHub `DELETE /repos/{o}/{r}/
  issues/comments/{id}`, GitLab `DELETE /projects/{id}/issues/{iid}/
  notes/{note_id}`, Jira `DELETE /rest/api/3/issue/{key}/comment/{id}`).
  Contrast "create issue" -- GitHub's REST API has no endpoint to delete
  an issue at all (only GraphQL `deleteIssue`, requiring repo-admin), so
  that action could not honestly declare `reversible=True`.
- **It creates no new external business identity** -- a comment attaches
  to something that already exists, unlike an issue/PR/MR, which would
  need its own synced-projection lifecycle (`DATA-MODEL.md` has no
  "comments" table, and adding one is out of this task's own scope).
- **It maps onto the closed high-impact taxonomy (`automation/adapters.
  py`'s `HIGH_IMPACT_CATEGORIES`) with no judgment call**: `public`
  ("creates content visible outside the workspace owner's private
  systems") is exactly, literally what posting a comment is.

**Every adapter here declares `high_impact_categories = frozenset({
"public"})`, unconditionally.** This is not merely the honest taxonomy
answer -- it is the actual, load-bearing access control. `policy.py`'s
own module docstring discloses that `automation_policies.action_types`/
`data_classes`/`value_limit` are stored but **never enforced** against a
dispatching adapter: a policy scoped to `action_types=["note.create"]`
does not stop a workflow from naming `github.add_issue_comment` once it
is registered in the shared registry. `evaluate_approval_requirement`
(`automation/approvals.py`) checks `high_impact_categories` *first*,
before any policy-mode branch, and returns `True` unconditionally the
moment it is non-empty -- so declaring `{"public"}` here is what
guarantees every single dispatch of one of these adapters requires a
fresh, digest-bound, human-decided approval, regardless of the
authorizing policy's own `approval_mode`. Omitting this category (leaving
`high_impact_categories=frozenset()`, technically defensible on
"reversible, low blast radius" grounds alone) would let a `bounded_
recurring` policy dispatch these writes with no per-run approval at all,
relying entirely on the three unenforced policy fields above -- a real
gap this module deliberately does not open.

**Containment is asymmetric across providers, disclosed rather than
silently uneven.** GitHub's `repository_id` and Jira's `work_item_id`
inputs are validated against this workspace's own already-synced
`repositories`/`engineering_work_items` rows (Task 2/Task 4's own read
sync) -- a workflow can only name a repository or issue this connection
has actually observed, not an arbitrary `owner/repo` string typed into a
workflow's `input_mapping`. GitLab has **no equivalent containment**:
Task 5's own module docstring (`gitlab_adapter.py`) discloses that
GitLab's `changes`/`reviews` sync is deferred to a Task 5 follow-up that
has not landed, so no synced GitLab issue/MR projection exists yet to
validate `project_path`/`issue_iid` against at all -- `gitlab.add_note`'s
only containment is "this `connector_account_id` belongs to this
workspace and is a GitLab connection," identical in kind to (but weaker
than) the other two. Closing this gap is the natural follow-up once
GitLab's own change/review sync exists, not this task's to invent early.

**Retry-safety is classified by failure *timing*, not failure *type*
alone -- getting this wrong is the single most likely way this module
could double-post.** A connection failure *before* the request reaches
the wire (`httpx.ConnectError`/`httpx.ConnectTimeout`) is genuinely safe
to retry -- nothing was sent -- and raises `TransientAdapterError`
(`ecc.domains.automation.adapters`), the one exception class `worker.
run_step` treats as retry-safe. A **read timeout on a POST is NOT
retry-safe** -- the provider may have already created the comment before
the response was lost -- so it raises a plain exception, landing the
step at `'failed'` for a human to review rather than a bounded automatic
retry that could post the same comment twice. An HTTP 429 (the request
was received and explicitly rejected before any write occurred) is also
classified `TransientAdapterError` for the same "provably never reached
the write path" reason.

**No scope pre-check against `connector_accounts.granted_scopes`,
disclosed.** `CONNECTOR-CONTRACT.md`'s write-scope table names `repo`/
`api`/`write:jira-work`, but none of the three providers' `authorize()`
implementations make this checkable in practice: GitHub's classic-token
`repo` scope is already read+write (no separate write grant to detect),
and a fine-grained PAT reports no scopes at all; Jira's `JiraAdapter.
required_scopes` is unconditionally `frozenset()` since a personal API
token has no scope grant to report; only GitLab's `granted_scopes` is
genuinely reliable. Rather than implement a scope check that only works
for one of three providers (implying a guarantee this module cannot
actually make), these adapters rely on the provider's own 403/401 at
write time as the real signal, surfaced as a plain (non-retried) failure.

**No `compensate()` is implemented, disclosed rather than silently
absent.** `ActionAdapter.compensate(action_input) -> BaseModel` (Decision
9) receives the *original* input, never the prior `execute()` call's
output -- so a real "delete the comment we just created" compensation
would need the provider-assigned comment id `execute()` alone learns,
which the protocol does not thread back in. Inventing a side-channel
(e.g. persisting created-comment ids somewhere `compensate()` could read)
is exactly the kind of new mechanism this task's own scope ("no second
authority mechanism") argues against adding casually; a dedicated
`github.delete_issue_comment`-style adapter, registered separately and
named explicitly in a workflow's own compensation step, is the
documented follow-up if a real caller needs this.
"""

from __future__ import annotations

from base64 import b64encode
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote
from uuid import UUID

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

# `ecc.domains.engineering.connectors` must be imported before any of
# `github_adapter`/`gitlab_adapter`/`jira_adapter` -- each of those three
# modules imports from `.connectors` at its own top, and `connectors.py`
# itself imports all three back at its own bottom (`ConnectorRegistry`'s
# own module docstring). That circular pair only resolves safely when
# `connectors.py` is the first of the four to begin loading (the ordering
# every existing caller, e.g. `connector_accounts.py`, already happens to
# follow); importing one of the three adapter modules directly first, as
# this module's own need for `GITHUB_API_BASE_URL`/`_parse_credential`
# would otherwise do, breaks that invariant and raises `ImportError:
# cannot import name '...' from partially initialized module` (confirmed
# directly against a real interpreter while writing this module). This
# import exists solely to force the safe ordering; nothing in this module
# uses the `connectors` module itself.
import ecc.domains.engineering.connectors  # noqa: F401
from ecc.database import SessionFactory
from ecc.domains.engineering.crypto import decrypt_credential
from ecc.domains.engineering.github_adapter import GITHUB_API_BASE_URL, _safe_repo_path_segment
from ecc.domains.engineering.gitlab_adapter import _parse_credential as _parse_gitlab_credential
from ecc.domains.engineering.jira_adapter import _parse_credential as _parse_jira_credential

# `TransientAdapterError` is deliberately imported inside `_classify_and_
# raise`/`_raise_for_write_response` below, not here at module top-level.
# A top-level `from ecc.domains.automation.adapters import
# TransientAdapterError` would recreate a genuine two-way circular import
# with that exact module (`adapters.py` registers this module's three
# adapters at its own bottom) -- and, unlike the `connectors`-ordering
# fix immediately above, no import ordering resolves it: reproduced
# directly against a real interpreter, `import ecc.domains.engineering.
# write_actions` as the very first touch of either module raises
# `ImportError: cannot import name 'GitHubAddIssueCommentAdapter' from
# partially initialized module`, regardless of whether `connectors` was
# already loaded. Deferring the import into the two functions that
# actually use the exception class (called only at runtime, long after
# both modules have finished loading) removes this module's import-time
# dependency on `automation.adapters` entirely, closing the cycle
# unconditionally rather than relying on which module happens to import
# first.


class WriteActionRejected(ValueError):
    """The requested target (`connector_account_id`/`repository_id`/
    `work_item_id`) does not belong to this workspace, is the wrong
    provider, or is not a connection this workspace has actually synced
    -- a data/authorization problem, never a transient one, so `worker.
    run_step` must not retry it (a plain exception, not
    `TransientAdapterError`, lands the step at `'failed'`).
    """


def _load_credential(
    session: Session, *, workspace_id: UUID, connector_account_id: UUID, expected_provider: str
) -> str:
    row = (
        session.execute(
            text(
                "SELECT provider, status, encrypted_credentials FROM connector_accounts "
                "WHERE workspace_id = :workspace_id AND id = :id"
            ),
            {"workspace_id": workspace_id, "id": connector_account_id},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        raise WriteActionRejected(
            f"connector_account_id {connector_account_id} does not belong to workspace "
            f"{workspace_id}"
        )
    if row["provider"] != expected_provider:
        raise WriteActionRejected(
            f"connector_account_id {connector_account_id} is a {row['provider']!r} connection, "
            f"not {expected_provider!r}"
        )
    if row["status"] != "active":
        raise WriteActionRejected(
            f"connector_account_id {connector_account_id} is not active (status={row['status']!r})"
        )
    return decrypt_credential(bytes(row["encrypted_credentials"]))


def _classify_and_raise(provider: str, exc: httpx.HTTPError) -> None:
    # Imported here, not at module top -- see this module's own docstring
    # ("no top-level dependency on `ecc.domains.automation.adapters`") for
    # why a lazy import is the actual fix for the write_actions<->adapters
    # circular import, not merely an import-ordering workaround.
    from ecc.domains.automation.adapters import TransientAdapterError

    if isinstance(exc, httpx.ConnectError | httpx.ConnectTimeout):
        raise TransientAdapterError(
            f"{provider} write request failed before it was sent: {exc}"
        ) from exc
    raise RuntimeError(
        f"{provider} write request's outcome is uncertain (not safe to retry): {exc}"
    ) from exc


def _raise_for_write_response(provider: str, response: httpx.Response) -> None:
    from ecc.domains.automation.adapters import TransientAdapterError

    if response.status_code == 429:
        raise TransientAdapterError(
            f"{provider} write request rejected (rate limited, never reached the write path): "
            f"HTTP 429"
        )
    if response.status_code >= 400:
        raise RuntimeError(f"{provider} write request rejected: HTTP {response.status_code}")


# ---------------------------------------------------------------------------
# github.add_issue_comment
# ---------------------------------------------------------------------------


class GitHubAddIssueCommentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    actor_id: UUID
    connector_account_id: UUID
    repository_id: UUID
    issue_number: int = Field(gt=0)
    body: str = Field(min_length=1, max_length=65536)


class GitHubAddIssueCommentOutput(BaseModel):
    """Deliberately excludes `body` -- `worker._redact_payload` only
    redacts by dict key name, never by scanning a string value, so a
    comment body that happened to contain leaked secret material would
    pass through unredacted into `workflow_run_steps.output` verbatim.
    """

    workspace_id: UUID
    comment_external_id: str
    source_url: str
    created_at: datetime


class GitHubAddIssueCommentAdapter:
    adapter_id = "github.add_issue_comment"
    # Explicitly widened to `type[BaseModel]`, not the inferred narrower
    # type -- `type[X]` is invariant under mypy's structural Protocol
    # matching (`local_adapters.py`'s identical comment on every one of
    # its own three adapters).
    input_schema: type[BaseModel] = GitHubAddIssueCommentInput
    output_schema: type[BaseModel] = GitHubAddIssueCommentOutput
    reversible = True
    high_impact_categories: frozenset[str] = frozenset({"public"})

    def __init__(
        self, *, transport: httpx.BaseTransport | None = None, timeout_seconds: float = 10.0
    ) -> None:
        client_kwargs: dict[str, Any] = {
            "base_url": GITHUB_API_BASE_URL,
            "timeout": timeout_seconds,
        }
        if transport is not None:
            client_kwargs["transport"] = transport
        self._client = httpx.Client(**client_kwargs)

    def simulate(self, action_input: BaseModel) -> BaseModel:
        """Purely in-memory -- no network call, matching `local_adapters.
        LocalCreateNoteAdapter.simulate`'s own "must not touch the real
        system at all" precedent (safer than a read-only existence-check
        GET, which would still require real network access from inside
        `simulate()`).
        """
        assert isinstance(action_input, GitHubAddIssueCommentInput)
        return GitHubAddIssueCommentOutput(
            workspace_id=action_input.workspace_id,
            comment_external_id="preview",
            source_url=f"{GITHUB_API_BASE_URL}/repos/.../issues/{action_input.issue_number}",
            created_at=datetime.now(UTC),
        )

    def execute(self, action_input: BaseModel) -> BaseModel:
        assert isinstance(action_input, GitHubAddIssueCommentInput)
        with SessionFactory() as session:
            credential = _load_credential(
                session,
                workspace_id=action_input.workspace_id,
                connector_account_id=action_input.connector_account_id,
                expected_provider="github",
            )
            repo = (
                session.execute(
                    text(
                        "SELECT name FROM repositories WHERE workspace_id = :workspace_id "
                        "AND id = :id AND connector_account_id = :connector_account_id "
                        "AND provider = 'github'"
                    ),
                    {
                        "workspace_id": action_input.workspace_id,
                        "id": action_input.repository_id,
                        "connector_account_id": action_input.connector_account_id,
                    },
                )
                .mappings()
                .one_or_none()
            )
        if repo is None:
            raise WriteActionRejected(
                f"repository_id {action_input.repository_id} is not a repository this "
                f"connector account has synced in this workspace"
            )
        full_name = _safe_repo_path_segment(repo["name"])
        headers = {
            "Authorization": f"Bearer {credential}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        try:
            response = self._client.post(
                f"/repos/{full_name}/issues/{action_input.issue_number}/comments",
                headers=headers,
                json={"body": action_input.body},
            )
        except httpx.HTTPError as exc:
            _classify_and_raise("GitHub", exc)
            raise  # unreachable, satisfies mypy's control-flow analysis
        _raise_for_write_response("GitHub", response)
        payload = response.json()
        return GitHubAddIssueCommentOutput(
            workspace_id=action_input.workspace_id,
            comment_external_id=str(payload["id"]),
            source_url=payload["html_url"],
            created_at=datetime.now(UTC),
        )


# ---------------------------------------------------------------------------
# gitlab.add_note
# ---------------------------------------------------------------------------


class GitLabAddNoteInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    actor_id: UUID
    connector_account_id: UUID
    project_path: str = Field(min_length=1, max_length=500)
    issue_iid: int = Field(gt=0)
    body: str = Field(min_length=1, max_length=65536)

    @field_validator("project_path")
    @classmethod
    def _reject_dot_segments(cls, value: str) -> str:
        # `quote(value, safe="")` (this module's `GitLabAddNoteAdapter.
        # execute`) percent-encodes every `/` in `value`, but `.` is an
        # RFC 3986 *unreserved* character `quote` never touches -- so a
        # slash-free `value` of "." or ".." survives encoding as a literal
        # dot-segment, which httpx (and any RFC-3986-compliant client)
        # then resolves against the request path *before* sending it. A
        # bare ".." here turns `/projects/{value}/issues/{iid}/notes` into
        # `/issues/{iid}/notes` on the wire -- a different endpoint
        # entirely, outside the `/projects/*` subtree this adapter's own
        # containment story assumes every request stays inside. Rejecting
        # any "."/".." path segment closes this without rejecting a real
        # GitLab project path, which never contains one.
        if any(segment in ("", ".", "..") for segment in value.split("/")):
            raise ValueError("project_path must not contain an empty, '.', or '..' path segment")
        return value


class GitLabAddNoteOutput(BaseModel):
    workspace_id: UUID
    note_external_id: str
    source_url: str
    created_at: datetime


class GitLabAddNoteAdapter:
    adapter_id = "gitlab.add_note"
    input_schema: type[BaseModel] = GitLabAddNoteInput
    output_schema: type[BaseModel] = GitLabAddNoteOutput
    reversible = True
    high_impact_categories: frozenset[str] = frozenset({"public"})

    def __init__(
        self, *, transport: httpx.BaseTransport | None = None, timeout_seconds: float = 10.0
    ) -> None:
        client_kwargs: dict[str, Any] = {"timeout": timeout_seconds}
        if transport is not None:
            client_kwargs["transport"] = transport
        self._client = httpx.Client(**client_kwargs)

    def simulate(self, action_input: BaseModel) -> BaseModel:
        assert isinstance(action_input, GitLabAddNoteInput)
        with SessionFactory() as session:
            credential = _load_credential(
                session,
                workspace_id=action_input.workspace_id,
                connector_account_id=action_input.connector_account_id,
                expected_provider="gitlab",
            )
        host, _token = _parse_gitlab_credential(credential)
        return GitLabAddNoteOutput(
            workspace_id=action_input.workspace_id,
            note_external_id="preview",
            source_url=(
                f"https://{host}/{action_input.project_path}/-/issues/{action_input.issue_iid}"
            ),
            created_at=datetime.now(UTC),
        )

    def execute(self, action_input: BaseModel) -> BaseModel:
        assert isinstance(action_input, GitLabAddNoteInput)
        with SessionFactory() as session:
            credential = _load_credential(
                session,
                workspace_id=action_input.workspace_id,
                connector_account_id=action_input.connector_account_id,
                expected_provider="gitlab",
            )
        host, token = _parse_gitlab_credential(credential)
        headers = {"PRIVATE-TOKEN": token, "Accept": "application/json"}
        encoded_project = quote(action_input.project_path, safe="")
        try:
            response = self._client.post(
                f"https://{host}/api/v4/projects/{encoded_project}/issues/"
                f"{action_input.issue_iid}/notes",
                headers=headers,
                json={"body": action_input.body},
            )
        except httpx.HTTPError as exc:
            _classify_and_raise("GitLab", exc)
            raise
        _raise_for_write_response("GitLab", response)
        payload = response.json()
        return GitLabAddNoteOutput(
            workspace_id=action_input.workspace_id,
            note_external_id=str(payload["id"]),
            source_url=(
                f"https://{host}/{action_input.project_path}/-/issues/"
                f"{action_input.issue_iid}#note_{payload['id']}"
            ),
            created_at=datetime.now(UTC),
        )


# ---------------------------------------------------------------------------
# jira.add_comment
# ---------------------------------------------------------------------------


class JiraAddCommentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workspace_id: UUID
    actor_id: UUID
    connector_account_id: UUID
    work_item_id: UUID
    body: str = Field(min_length=1, max_length=65536)


class JiraAddCommentOutput(BaseModel):
    workspace_id: UUID
    comment_external_id: str
    source_url: str
    created_at: datetime


class JiraAddCommentAdapter:
    adapter_id = "jira.add_comment"
    input_schema: type[BaseModel] = JiraAddCommentInput
    output_schema: type[BaseModel] = JiraAddCommentOutput
    reversible = True
    high_impact_categories: frozenset[str] = frozenset({"public"})

    def __init__(
        self, *, transport: httpx.BaseTransport | None = None, timeout_seconds: float = 10.0
    ) -> None:
        client_kwargs: dict[str, Any] = {"timeout": timeout_seconds}
        if transport is not None:
            client_kwargs["transport"] = transport
        self._client = httpx.Client(**client_kwargs)

    def simulate(self, action_input: BaseModel) -> BaseModel:
        assert isinstance(action_input, JiraAddCommentInput)
        return JiraAddCommentOutput(
            workspace_id=action_input.workspace_id,
            comment_external_id="preview",
            source_url="",
            created_at=datetime.now(UTC),
        )

    def execute(self, action_input: BaseModel) -> BaseModel:
        assert isinstance(action_input, JiraAddCommentInput)
        with SessionFactory() as session:
            credential = _load_credential(
                session,
                workspace_id=action_input.workspace_id,
                connector_account_id=action_input.connector_account_id,
                expected_provider="jira",
            )
            work_item = (
                session.execute(
                    text(
                        "SELECT external_id, source_url FROM engineering_work_items "
                        "WHERE workspace_id = :workspace_id AND id = :id "
                        "AND connector_account_id = :connector_account_id AND provider = 'jira'"
                    ),
                    {
                        "workspace_id": action_input.workspace_id,
                        "id": action_input.work_item_id,
                        "connector_account_id": action_input.connector_account_id,
                    },
                )
                .mappings()
                .one_or_none()
            )
        if work_item is None:
            raise WriteActionRejected(
                f"work_item_id {action_input.work_item_id} is not a work item this "
                f"connector account has synced in this workspace"
            )
        site, email, api_token = _parse_jira_credential(credential)
        basic = b64encode(f"{email}:{api_token}".encode()).decode()
        headers = {"Authorization": f"Basic {basic}", "Accept": "application/json"}
        # `external_id` is Jira's internal numeric issue id (`jira_adapter.
        # _upsert_work_item` stores `str(issue["id"])`), which the REST
        # comment endpoint accepts as `{issueIdOrKey}` -- but Jira's web
        # `/browse/{...}` route only resolves the human-facing issue *key*
        # (e.g. "PROJ-123"), not the numeric id. `engineering_work_items.
        # source_url` already holds the correct `https://{site}/browse/
        # {key}` link (`jira_adapter.py`'s own sync code builds it that
        # way); reused here for the output link rather than reconstructed
        # from `external_id`, which would silently produce a broken link.
        # Same dot-segment path-escape defense as `_safe_repo_path_segment`/
        # `GitLabAddNoteInput._reject_dot_segments` -- review found this was
        # the one provider write action of the three missing it: `external_id`
        # is ordinarily a purely numeric database key, but nothing enforces
        # that shape, so a compromised or misbehaving connected Jira instance
        # returning a crafted `id` field could otherwise escape the intended
        # `/rest/api/3/issue/{id}/comment` path.
        issue_id = _safe_repo_path_segment(work_item["external_id"])
        try:
            response = self._client.post(
                f"https://{site}/rest/api/3/issue/{issue_id}/comment",
                headers=headers,
                json={
                    "body": {
                        "type": "doc",
                        "version": 1,
                        "content": [
                            {
                                "type": "paragraph",
                                "content": [{"type": "text", "text": action_input.body}],
                            }
                        ],
                    }
                },
            )
        except httpx.HTTPError as exc:
            _classify_and_raise("Jira", exc)
            raise
        _raise_for_write_response("Jira", response)
        payload = response.json()
        return JiraAddCommentOutput(
            workspace_id=action_input.workspace_id,
            comment_external_id=str(payload["id"]),
            source_url=f"{work_item['source_url']}?focusedCommentId={payload['id']}",
            created_at=datetime.now(UTC),
        )
