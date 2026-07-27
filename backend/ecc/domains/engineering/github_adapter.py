"""Real GitHub REST API connector adapter (Phase 6 Task 2 -- the first
non-sandbox `ConnectorAdapter`, replacing `sandbox_adapter.
SandboxGithubAdapter` for the `github` provider slug in production use).

**HTTP client shape mirrors `ecc.domains.ai_runtime.ollama_client.
OllamaAdapter`'s established precedent** -- the only prior example in this
codebase of a real outbound call to an external HTTP service: an
injectable `transport: httpx.BaseTransport | None` constructor parameter
(used only by tests, via `httpx.MockTransport`; production code never
passes one), narrow exception handling that re-raises as this module's own
typed exceptions rather than leaking `httpx`'s, and no new dependency
(`httpx` is already RFC-005-approved, and `httpx.MockTransport` -- used by
every `ai_runtime` HTTP test -- is part of the already-pinned package, not
a new mocking library).

**Repository, change and review sync are implemented; work items and
deployments are not.** `engineering_work_items` is Jira-only scope
(`jira_adapter.py`, Task 4) and does not apply to GitHub in this
codebase's model; `deployments` remains deferred to a Task 5 follow-up
(see `docs/phases/phase-006/DELIVERY-INTELLIGENCE-CONTRACT.md`'s "Task 5
status" section for what that leaves `insufficient_coverage`).
`backfill`/`incremental_sync` return a zero-item success outcome for any
`resource_type` other than `"repository"`/`"change"`/`"review"` rather
than raising, since Task 1's `ConnectorAdapter` contract has no
per-adapter way to declare "this resource type isn't implemented yet"
and silently no-op-succeeding is less surprising to a caller than an
unexplained exception for a resource type the contract itself lists as
valid.

**Incremental cursor strategy (repositories).** GitHub's `GET /user/repos`
has no `since` parameter. `_sync_repositories` instead requests
`sort=updated&direction=desc` and walks pages until a row's own
`updated_at` falls at or before the prior cursor (the newest `updated_at`
observed in the previous sync call) -- the same "stop once you've caught
up" idiom a `since`-parameterized API would give for free, implemented
explicitly here because this endpoint doesn't offer one.

**Changes (merged pull requests): per-repository fan-out, not the Search
API.** GitHub has no single "every merged PR across every repo this
token can see" endpoint on the core REST API. Two real options exist:
`GET /search/issues?q=is:pr+is:merged...` (one call covers every repo,
but the Search API's own rate limit is a separate, much stricter 30
requests/minute bucket, and its `q=author:...` filtering has no clean
"any author, this repo" equivalent without listing every repo in the
query string anyway) versus `GET /repos/{owner}/{repo}/pulls?state=
closed` fanned out over the repositories this same connector account has
already synced (`_list_synced_repositories`, reading our own
`repositories` table -- no second GitHub call needed to rediscover the
repo list). Fan-out was chosen: it shares the core API's 5,000
requests/hour budget with `_sync_repositories` instead of introducing a
second, stricter rate-limit regime to manage, and per-repo pagination
reuses the identical "stop once caught up" idiom already established
here rather than a third cursor shape.

`_sync_changes`'s cursor is a JSON object, not a single timestamp --
`{"repos": {repository_external_id: newest_updated_at_seen, ...}}` --
because fan-out means "caught up" is a per-repository fact, not a global
one; a single flat timestamp cursor would force every repository back to
the front of its own history whenever any other repository sorts newer.
The shared `_MAX_PAGES_PER_CALL` budget is spent across every
repository's calls combined (not per-repository) in a stable,
`external_id`-ascending order; a repository already caught up costs only
one cheap page-1 call before moving to the next, so in the steady state a
modest budget still reaches every repository within one incremental sync
call. `status="partial"` (not `succeeded`) if any repository could not be
fully walked within budget this call, with the JSON cursor still
reflecting real, per-repository progress -- the next call resumes each
repository from where it left off, not from scratch.

**Reviews: sourced from already-synced `changes`, not a second API
discovery step.** `_sync_reviews` walks this connector account's own
`changes` rows (already fanned out by `_sync_changes`) ordered by
`merged_at` **ascending** (oldest still-uncovered change first --
deliberately, not descending: this method's cursor is a single
timestamp, not the changes cursor's per-repository JSON, so an early
per-call budget cutoff must leave off at the oldest unprocessed change,
never skip past it, or an older change would never be reachable again
once the cursor advances past newer ones), calling `GET /repos/{owner}/
{repo}/pulls/{provider_number}/reviews` once per still-uncovered change
(a merged PR's review count is realistically small enough that one
unpaginated call per change is sufficient for this task's scope; a PR
with over 100 reviews would be truncated -- an accepted, disclosed edge
case, not silently handled). The cursor is the single newest `merged_at`
already covered -- simpler than the changes cursor above, since the
source of "what needs syncing" is our own already-ordered `changes`
table, not a live paginated API response.

**Rate-limit handling.** GitHub signals exhaustion via `403`/`429` plus
`X-RateLimit-Remaining: 0` and `X-RateLimit-Reset` (unix epoch) or
`Retry-After`. `_request_with_rate_limit_retry` waits once, bounded to
`_RATE_LIMIT_MAX_WAIT_SECONDS` -- long enough to ride out a short reset
window inline, short enough to never turn one `POST /sync` HTTP request
into a multi-minute hang. Beyond that bound, the call gives up and the
sync reports `status="partial"` with the accumulated progress rather than
blocking the request indefinitely; a subsequent sync call resumes from
the cursor already recorded.

**Webhook ingestion has no receiving HTTP endpoint yet, and no signature
verification.** `handle_webhook` implements the parsing/upsert logic the
`ConnectorAdapter` contract asks for, but wiring a real `POST /engineering/
webhooks/github`-shaped route (and the webhook-secret storage a real
`X-Hub-Signature-256` check needs -- `connector_accounts` has no such
column yet) is genuinely new API surface `API-SCHEMAS.md` does not
currently name, and is disclosed here as deferred rather than silently
implemented as an unauthenticated write path. Do not wire this method to
a public route before that gap is closed.

**`disconnect()` is a no-op.** A fine-grained/classic personal access
token has no revocation API a connector can call on the user's behalf --
only the user can revoke it, from GitHub's own settings UI. This matches
`CONNECTOR-CONTRACT.md`'s "must not raise for provider does not support
revocation" expected outcome.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from hashlib import sha256
from json import dumps, loads
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import text

from ecc.database import SessionFactory

from .connectors import (
    AdapterAuthorizationError,
    ConnectorAccountContext,
    ConnectorAuthorization,
    PermissionState,
    SyncOutcome,
)

GITHUB_API_BASE_URL = "https://api.github.com"
# Classic OAuth scope name, not the fine-grained-PAT/GitHub-App permission
# vocabulary (`contents:read` etc.) `CONNECTOR-CONTRACT.md`'s scopes table
# originally listed. `X-OAuth-Scopes` -- the only header this endpoint
# exposes to check against -- is only ever populated for classic OAuth
# tokens/PATs, and only ever contains classic OAuth scope names; `repo`
# grants read (and write) access to both public and private repository
# contents, which is what backfill/incremental sync of repository metadata
# needs. See `authorize`'s own docstring for the fine-grained-PAT case,
# where this header is absent and there is nothing to check at all.
_REQUIRED_SCOPES: frozenset[str] = frozenset({"repo"})
_PAGE_SIZE = 100
# Bounded per-call page fetch -- a workspace with more pages than this
# resumes across multiple `/sync` calls via the cursor, rather than one
# request paginating unboundedly through an arbitrarily large account.
_MAX_PAGES_PER_CALL = 10
_RATE_LIMIT_MAX_WAIT_SECONDS = 5.0


def _safe_repo_path_segment(full_name: str) -> str:
    """Defense-in-depth against the same dot-segment path-escape bug class
    `write_actions.py`'s `GitLabAddNoteInput._reject_dot_segments` closed
    for GitLab's user-supplied `project_path` (final Phase 6 review
    finding). `full_name` here is never directly user input -- it always
    comes from `repositories.name`, itself populated only from GitHub's
    own repo-listing response (`_upsert_repository`, below), and GitHub's
    naming rules already disallow the `.`/`..`/empty path segments that
    make the underlying bug exploitable -- but validating at every call
    site that builds a request path from it, rather than trusting that
    upstream policy as the only backstop, keeps this adapter consistent
    with the GitLab adapter's own containment story.
    """
    if any(segment in ("", ".", "..") for segment in full_name.split("/")):
        raise RuntimeError(f"repository name {full_name!r} contains an invalid path segment")
    return full_name


def _content_hash(repo: Mapping[str, Any]) -> str:
    material = dumps(
        {
            "name": repo.get("full_name"),
            "default_branch": repo.get("default_branch"),
            "html_url": repo.get("html_url"),
            "updated_at": repo.get("updated_at"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(material.encode("utf-8")).hexdigest()


def _upsert_repository(
    *, workspace_id: Any, connector_account_id: Any, provider: str, repo: Mapping[str, Any]
) -> None:
    """Opens and commits its own session -- mirrors `ecc.domains.
    automation.local_adapters.LocalCreateNoteAdapter.execute`'s identical
    "no session threaded through the adapter protocol" precedent. Keeping
    this write on its own short-lived connection (rather than the
    dispatching request's own pooled session) is part of this task's
    pool-exhaustion fix: see `connector_accounts.py`'s `sync_connector_
    endpoint` for the other half (closing its own session before calling
    into this adapter at all).
    """
    now = datetime.now(UTC)
    provider_updated_at = repo.get("updated_at")
    with SessionFactory() as session:
        session.execute(
            text(
                """
                INSERT INTO repositories (
                    id, workspace_id, connector_account_id, provider, external_id,
                    name, source_url, default_branch, permission_state, freshness_state,
                    content_hash, provider_updated_at, observed_at, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, :connector_account_id, :provider, :external_id,
                    :name, :source_url, :default_branch, 'active', 'fresh',
                    :content_hash, :provider_updated_at, :now, :now, :now
                )
                ON CONFLICT (workspace_id, connector_account_id, external_id) DO UPDATE SET
                    name = EXCLUDED.name,
                    source_url = EXCLUDED.source_url,
                    default_branch = EXCLUDED.default_branch,
                    permission_state = 'active',
                    freshness_state = 'fresh',
                    content_hash = EXCLUDED.content_hash,
                    provider_updated_at = EXCLUDED.provider_updated_at,
                    observed_at = EXCLUDED.observed_at,
                    updated_at = EXCLUDED.updated_at
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "connector_account_id": connector_account_id,
                "provider": provider,
                "external_id": str(repo["id"]),
                "name": repo.get("full_name") or repo.get("name") or str(repo["id"]),
                "source_url": repo.get("html_url") or "",
                "default_branch": repo.get("default_branch"),
                "content_hash": _content_hash(repo),
                "provider_updated_at": provider_updated_at,
                "now": now,
            },
        )
        session.commit()


def _content_hash_change(pr: Mapping[str, Any]) -> str:
    material = dumps(
        {
            "title": pr.get("title"),
            "state": pr.get("state"),
            "merged_at": pr.get("merged_at"),
            "updated_at": pr.get("updated_at"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(material.encode("utf-8")).hexdigest()


def _content_hash_review(review: Mapping[str, Any], *, requested_at: str | None) -> str:
    material = dumps(
        {
            "state": review.get("state"),
            "submitted_at": review.get("submitted_at"),
            "requested_at": requested_at,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(material.encode("utf-8")).hexdigest()


def _list_synced_repositories(
    *, workspace_id: Any, connector_account_id: Any
) -> list[dict[str, Any]]:
    """Repositories this same connector account has already synced into
    `repositories` -- `_sync_changes` fans out over this list rather than
    making a second GitHub call to rediscover it (see this module's own
    docstring for why fan-out over the Search API was chosen). Ordered by
    `external_id` for a stable, deterministic iteration order across
    calls -- required for the shared page budget in `_sync_changes` to
    make real progress on every repository over successive calls rather
    than always restarting from an arbitrary order.
    """
    with SessionFactory() as session:
        rows = session.execute(
            text(
                """
                SELECT id, external_id, name
                FROM repositories
                WHERE workspace_id = :workspace_id
                  AND connector_account_id = :connector_account_id
                  AND provider = 'github'
                ORDER BY external_id
                """
            ),
            {"workspace_id": workspace_id, "connector_account_id": connector_account_id},
        )
        return [dict(row) for row in rows.mappings().all()]


def _upsert_change(
    *,
    workspace_id: Any,
    connector_account_id: Any,
    provider: str,
    repository_id: Any,
    pr: Mapping[str, Any],
) -> None:
    """Opens and commits its own session -- identical discipline to
    `_upsert_repository`'s own docstring.
    """
    now = datetime.now(UTC)
    with SessionFactory() as session:
        session.execute(
            text(
                """
                INSERT INTO changes (
                    id, workspace_id, connector_account_id, repository_id, provider,
                    external_id, provider_number, title, source_url, status,
                    author_external_id, provider_created_at, merged_at,
                    permission_state, freshness_state, content_hash,
                    provider_updated_at, observed_at, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, :connector_account_id, :repository_id, :provider,
                    :external_id, :provider_number, :title, :source_url, :status,
                    :author_external_id, :provider_created_at, :merged_at,
                    'active', 'fresh', :content_hash,
                    :provider_updated_at, :now, :now, :now
                )
                ON CONFLICT (workspace_id, connector_account_id, external_id) DO UPDATE SET
                    title = EXCLUDED.title,
                    source_url = EXCLUDED.source_url,
                    status = EXCLUDED.status,
                    merged_at = EXCLUDED.merged_at,
                    permission_state = 'active',
                    freshness_state = 'fresh',
                    content_hash = EXCLUDED.content_hash,
                    provider_updated_at = EXCLUDED.provider_updated_at,
                    observed_at = EXCLUDED.observed_at,
                    updated_at = EXCLUDED.updated_at
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "connector_account_id": connector_account_id,
                "repository_id": repository_id,
                "provider": provider,
                "external_id": str(pr["id"]),
                "provider_number": str(pr.get("number")) if pr.get("number") is not None else None,
                "title": pr.get("title") or f"PR #{pr.get('number', pr['id'])}",
                "source_url": pr.get("html_url") or "",
                "status": pr.get("state"),
                "author_external_id": str((pr.get("user") or {}).get("id"))
                if (pr.get("user") or {}).get("id") is not None
                else None,
                "provider_created_at": pr.get("created_at"),
                "merged_at": pr.get("merged_at"),
                "content_hash": _content_hash_change(pr),
                "provider_updated_at": pr.get("updated_at"),
                "now": now,
            },
        )
        session.commit()


def _earliest_review_requested_at(timeline_events: list[Mapping[str, Any]]) -> str | None:
    """The `review_requested` timeline event has no analogue on `GET
    .../pulls/{number}/reviews` itself -- only `GET .../issues/{number}/
    timeline` emits it. A PR can have more than one (re-requesting a
    reviewer, requesting an additional one); the earliest is "when review
    was first asked for," the correct start point for review-latency's
    own "review-requested-at to first-review-at" definition. Every
    timeline `created_at` GitHub emits is UTC (`Z`-suffixed), so plain
    string `min()` is a safe chronological comparison here -- unlike
    `jira_adapter.py`'s own timezone-offset-variable timestamps, this is
    not the same DST-unsafe comparison that module's fix addresses.
    """
    requested_at_values: list[str] = [
        str(event.get("created_at"))
        for event in timeline_events
        if event.get("event") == "review_requested" and event.get("created_at")
    ]
    return min(requested_at_values) if requested_at_values else None


def _list_changes_needing_reviews(
    *, workspace_id: Any, connector_account_id: Any, since_merged_at: datetime | None
) -> list[dict[str, Any]]:
    """Changes already synced by `_sync_changes`, **oldest-merged first**
    (ascending -- the module docstring's own "Reviews" section has the
    full reasoning: a single flat `merged_at` cursor means an early
    per-call budget cutoff must leave off at the oldest still-uncovered
    change, never skip past it toward newer ones), restricted to those
    merged after `since_merged_at` -- `_sync_reviews`'s own resumable
    cursor is the newest `merged_at` already covered (parsed from the
    outward-facing string cursor into a real `datetime` by its caller,
    since `changes.merged_at` is a real `TIMESTAMPTZ` column, not text),
    so this query is the direct SQL expression of "what's new since
    then." Needs `repositories.name` (not just `changes.repository_id`)
    to build the `GET /repos/{owner}/{repo}/pulls/{number}/reviews` URL,
    hence the join rather than a second per-change lookup.
    """
    with SessionFactory() as session:
        params: dict[str, Any] = {
            "workspace_id": workspace_id,
            "connector_account_id": connector_account_id,
        }
        since_clause = ""
        if since_merged_at is not None:
            since_clause = "AND c.merged_at > :since_merged_at"
            params["since_merged_at"] = since_merged_at
        rows = session.execute(
            text(
                f"""
                SELECT
                    c.id, c.external_id, c.provider_number, c.merged_at,
                    r.name AS repository_name
                FROM changes c
                JOIN repositories r ON r.id = c.repository_id AND r.workspace_id = c.workspace_id
                WHERE c.workspace_id = :workspace_id
                  AND c.connector_account_id = :connector_account_id
                  AND c.provider = 'github'
                  AND c.provider_number IS NOT NULL
                  {since_clause}
                ORDER BY c.merged_at ASC
                """  # noqa: S608
            ),
            params,
        )
        return [dict(row) for row in rows.mappings().all()]


def _upsert_review(
    *,
    workspace_id: Any,
    connector_account_id: Any,
    provider: str,
    change_id: Any,
    review: Mapping[str, Any],
    requested_at: str | None,
) -> None:
    """Opens and commits its own session -- identical discipline to
    `_upsert_repository`'s own docstring. `requested_at` is looked up
    once per change by the caller (`_sync_reviews`, via the change's own
    timeline) and applied to every review synced for that change -- see
    this module's own docstring for why review-request timestamps live
    on the timeline endpoint, not the reviews endpoint itself.
    """
    now = datetime.now(UTC)
    with SessionFactory() as session:
        session.execute(
            text(
                """
                INSERT INTO reviews (
                    id, workspace_id, connector_account_id, change_id, provider,
                    external_id, source_url, reviewer_external_id, review_state,
                    requested_at, submitted_at, permission_state, freshness_state,
                    content_hash, provider_updated_at, observed_at, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, :connector_account_id, :change_id, :provider,
                    :external_id, :source_url, :reviewer_external_id, :review_state,
                    :requested_at, :submitted_at, 'active', 'fresh',
                    :content_hash, :provider_updated_at, :now, :now, :now
                )
                ON CONFLICT (workspace_id, connector_account_id, external_id) DO UPDATE SET
                    review_state = EXCLUDED.review_state,
                    requested_at = EXCLUDED.requested_at,
                    submitted_at = EXCLUDED.submitted_at,
                    permission_state = 'active',
                    freshness_state = 'fresh',
                    content_hash = EXCLUDED.content_hash,
                    provider_updated_at = EXCLUDED.provider_updated_at,
                    observed_at = EXCLUDED.observed_at,
                    updated_at = EXCLUDED.updated_at
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "connector_account_id": connector_account_id,
                "change_id": change_id,
                "provider": provider,
                "external_id": str(review["id"]),
                "source_url": review.get("html_url") or "",
                "requested_at": requested_at,
                "reviewer_external_id": str((review.get("user") or {}).get("id"))
                if (review.get("user") or {}).get("id") is not None
                else None,
                "review_state": (review.get("state") or "").lower() or None,
                "submitted_at": review.get("submitted_at"),
                "content_hash": _content_hash_review(review, requested_at=requested_at),
                "provider_updated_at": review.get("submitted_at"),
                "now": now,
            },
        )
        session.commit()


class GitHubAdapter:
    provider = "github"
    required_scopes: frozenset[str] = _REQUIRED_SCOPES

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 10.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        client_kwargs: dict[str, Any] = {
            "base_url": GITHUB_API_BASE_URL,
            "timeout": timeout_seconds,
        }
        if transport is not None:
            client_kwargs["transport"] = transport
        self._client = httpx.Client(**client_kwargs)
        self._sleep = sleep

    def _headers(self, credential: str) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {credential}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _request_with_rate_limit_retry(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
    ) -> httpx.Response | None:
        """Returns `None` only when rate-limited beyond the bounded wait --
        callers treat that as "give up for now, resume next sync call."
        """
        try:
            response = self._client.request(method, path, headers=headers, params=params)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"GitHub request failed: {exc}") from exc

        is_rate_limited = response.status_code == 429 or (
            response.status_code == 403 and response.headers.get("X-RateLimit-Remaining") == "0"
        )
        if not is_rate_limited:
            return response

        wait_seconds = self._rate_limit_wait_seconds(response)
        if wait_seconds is None or wait_seconds > _RATE_LIMIT_MAX_WAIT_SECONDS:
            return None
        self._sleep(wait_seconds)
        try:
            retry_response = self._client.request(method, path, headers=headers, params=params)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"GitHub request failed: {exc}") from exc

        # A second rate-limit hit right after the bounded wait must not be
        # trusted as a normal response -- review found this previously fell
        # through to the caller's own `status_code != 200` branch, which
        # raises an opaque `RuntimeError` (the whole sync run reported
        # `failed`) instead of the same clean `partial` + preserved-cursor
        # outcome a first-hit rate limit gets. One retry is the bound; a
        # still-limited retry gives up exactly like the first check does.
        retry_is_rate_limited = retry_response.status_code == 429 or (
            retry_response.status_code == 403
            and retry_response.headers.get("X-RateLimit-Remaining") == "0"
        )
        if retry_is_rate_limited:
            return None
        return retry_response

    def _rate_limit_wait_seconds(self, response: httpx.Response) -> float | None:
        retry_after = response.headers.get("Retry-After")
        if retry_after is not None:
            try:
                return max(0.0, float(retry_after))
            except ValueError:
                pass
        reset_header = response.headers.get("X-RateLimit-Reset")
        if reset_header is not None:
            try:
                reset_epoch = float(reset_header)
            except ValueError:
                return None
            return max(0.0, reset_epoch - time.time())
        return None

    def authorize(self, credential: str) -> ConnectorAuthorization:
        """Raises `AdapterAuthorizationError` for an invalid/rejected
        credential, or for a classic token/PAT that is valid but missing a
        required scope. **Cannot verify scopes for a fine-grained PAT** --
        GitHub does not emit `X-OAuth-Scopes` at all for that token type
        (there is no header-based signal for a fine-grained PAT's actual
        repository/permission grants on this endpoint), so this method
        distinguishes "header absent" from "header present but empty":
        only the former is treated as an unverifiable fine-grained PAT
        (accepted, with an honestly-empty `granted_scopes` rather than a
        fabricated full grant); the latter (a classic OAuth token
        authorized with zero scopes) still fails the subset check below
        like any other under-scoped classic token. See
        `CONNECTOR-CONTRACT.md`'s "Accepted limitation" section -- refusing
        every fine-grained PAT outright would make GitHub's own recommended
        modern token type unusable through this connector.
        """
        try:
            response = self._client.get("/user", headers=self._headers(credential))
        except httpx.HTTPError as exc:
            raise AdapterAuthorizationError(f"GitHub authorization request failed: {exc}") from exc
        if response.status_code == 401:
            raise AdapterAuthorizationError("GitHub rejected the credential (401 Unauthorized)")
        if response.status_code != 200:
            raise AdapterAuthorizationError(
                f"GitHub authorization failed with status {response.status_code}"
            )
        body = response.json()

        if "X-OAuth-Scopes" in response.headers:
            granted = frozenset(
                scope.strip()
                for scope in response.headers["X-OAuth-Scopes"].split(",")
                if scope.strip()
            )
            if not _REQUIRED_SCOPES.issubset(granted):
                missing = ", ".join(sorted(_REQUIRED_SCOPES - granted))
                raise AdapterAuthorizationError(
                    f"GitHub token is missing required scope(s): {missing}"
                )
        else:
            granted = frozenset()

        return ConnectorAuthorization(
            external_account_id=str(body["id"]),
            display_name=body.get("login") or f"github-{body['id']}",
            granted_scopes=granted,
        )

    def backfill(self, account: ConnectorAccountContext, resource_type: str) -> SyncOutcome:
        if resource_type == "repository":
            return self._sync_repositories(account, since_cursor=None)
        if resource_type == "change":
            return self._sync_changes(account, since_cursor=None)
        if resource_type == "review":
            return self._sync_reviews(account, since_cursor=None)
        return SyncOutcome(
            resource_type=resource_type, items_processed=0, status="succeeded", next_cursor=None
        )

    def incremental_sync(
        self, account: ConnectorAccountContext, resource_type: str, cursor: str | None
    ) -> SyncOutcome:
        if resource_type == "repository":
            return self._sync_repositories(account, since_cursor=cursor)
        if resource_type == "change":
            return self._sync_changes(account, since_cursor=cursor)
        if resource_type == "review":
            return self._sync_reviews(account, since_cursor=cursor)
        return SyncOutcome(
            resource_type=resource_type,
            items_processed=0,
            status="succeeded",
            next_cursor=cursor,
        )

    def _sync_repositories(
        self, account: ConnectorAccountContext, *, since_cursor: str | None
    ) -> SyncOutcome:
        headers = self._headers(account.credential)
        items_processed = 0
        newest_updated_at = since_cursor
        page = 1
        stopped_early = False

        while page <= _MAX_PAGES_PER_CALL:
            response = self._request_with_rate_limit_retry(
                "GET",
                "/user/repos",
                headers=headers,
                params={
                    "sort": "updated",
                    "direction": "desc",
                    "per_page": _PAGE_SIZE,
                    "page": page,
                },
            )
            if response is None:
                return SyncOutcome(
                    resource_type="repository",
                    items_processed=items_processed,
                    status="partial",
                    next_cursor=newest_updated_at,
                    error_summary="GitHub rate limit exceeded; sync paused, will resume next call",
                )
            if response.status_code != 200:
                raise RuntimeError(
                    f"GitHub repository list failed with status {response.status_code}"
                )

            repos = response.json()
            if not repos:
                break

            for repo in repos:
                updated_at = repo.get("updated_at")
                if (
                    since_cursor is not None
                    and updated_at is not None
                    and updated_at <= since_cursor
                ):
                    stopped_early = True
                    break
                _upsert_repository(
                    workspace_id=account.workspace_id,
                    connector_account_id=account.connector_account_id,
                    provider=self.provider,
                    repo=repo,
                )
                items_processed += 1
                if newest_updated_at is None or (updated_at and updated_at > newest_updated_at):
                    newest_updated_at = updated_at

            # `response.links` (httpx's own RFC 8288 `Link`-header parser)
            # replaces a hand-rolled comma-split here -- review found the
            # naive split could mis-parse a `Link` header whose URL itself
            # contains a comma (a legal, if unusual, query-string value),
            # silently truncating pagination early.
            if stopped_early or "next" not in response.links:
                break
            page += 1
        else:
            # The loop ran `_MAX_PAGES_PER_CALL` iterations without ever
            # `break`-ing -- i.e. the last page fetched still had further
            # pages available (`response.links` still had `"next"`). Review
            # found this previously fell through to the same `succeeded`
            # return below, silently capping a large account's backfill at
            # ~`_MAX_PAGES_PER_CALL * _PAGE_SIZE` repositories with no
            # signal that more remained. `next_cursor` is still the newest
            # `updated_at` observed so far, so a subsequent sync call
            # resumes -- this is `partial`, not `failed`.
            return SyncOutcome(
                resource_type="repository",
                items_processed=items_processed,
                status="partial",
                next_cursor=newest_updated_at,
                error_summary=(
                    f"GitHub repository sync hit the {_MAX_PAGES_PER_CALL}-page "
                    "per-call bound with more pages remaining; sync paused, "
                    "will resume next call"
                ),
            )

        return SyncOutcome(
            resource_type="repository",
            items_processed=items_processed,
            status="succeeded",
            next_cursor=newest_updated_at,
        )

    def _sync_changes(
        self, account: ConnectorAccountContext, *, since_cursor: str | None
    ) -> SyncOutcome:
        """See this module's own docstring ("Changes: per-repository
        fan-out") for the cursor shape and budget-sharing rationale.
        """
        headers = self._headers(account.credential)
        repos = _list_synced_repositories(
            workspace_id=account.workspace_id, connector_account_id=account.connector_account_id
        )
        per_repo_cursor: dict[str, str] = {}
        if since_cursor:
            # A cursor that isn't valid JSON, or doesn't have this
            # method's own `{"repos": {...}}` shape (e.g. a flat-
            # timestamp-style cursor left over from a schema change, or
            # a resource type's cursor read against the wrong one),
            # degrades to "no per-repository progress recorded yet"
            # rather than raising -- review found the previous version
            # let `loads` raise uncaught, turning a recoverable resumable
            # sync into an opaque `failed` run instead of the graceful,
            # bounded-and-resumable degrade this method aims for
            # everywhere else.
            try:
                parsed = loads(since_cursor)
            except ValueError:
                parsed = None
            if isinstance(parsed, dict) and isinstance(parsed.get("repos"), dict):
                per_repo_cursor = dict(parsed["repos"])

        items_processed = 0
        calls_made = 0
        any_incomplete = False

        for repo in repos:
            if calls_made >= _MAX_PAGES_PER_CALL:
                any_incomplete = True
                break

            repo_external_id = repo["external_id"]
            repo_cursor = per_repo_cursor.get(repo_external_id)
            newest_for_repo = repo_cursor
            page = 1
            stopped_early = False

            while calls_made < _MAX_PAGES_PER_CALL:
                calls_made += 1
                response = self._request_with_rate_limit_retry(
                    "GET",
                    f"/repos/{_safe_repo_path_segment(repo['name'])}/pulls",
                    headers=headers,
                    params={
                        "state": "closed",
                        "sort": "updated",
                        "direction": "desc",
                        "per_page": _PAGE_SIZE,
                        "page": page,
                    },
                )
                if response is None:
                    any_incomplete = True
                    break
                if response.status_code != 200:
                    raise RuntimeError(
                        f"GitHub pull request list for {repo['name']} failed with status "
                        f"{response.status_code}"
                    )

                prs = response.json()
                if not prs:
                    break

                for pr in prs:
                    updated_at = pr.get("updated_at")
                    if (
                        repo_cursor is not None
                        and updated_at is not None
                        and updated_at <= repo_cursor
                    ):
                        stopped_early = True
                        break
                    if pr.get("merged_at"):
                        _upsert_change(
                            workspace_id=account.workspace_id,
                            connector_account_id=account.connector_account_id,
                            provider=self.provider,
                            repository_id=repo["id"],
                            pr=pr,
                        )
                        items_processed += 1
                    if newest_for_repo is None or (updated_at and updated_at > newest_for_repo):
                        newest_for_repo = updated_at

                if stopped_early or "next" not in response.links:
                    break
                page += 1
            else:
                # Ran out of the shared per-call budget mid-repository --
                # this repository is not yet caught up; its own cursor
                # below still reflects real progress for the next call.
                any_incomplete = True

            if newest_for_repo is not None:
                per_repo_cursor[repo_external_id] = newest_for_repo

        next_cursor = dumps({"repos": per_repo_cursor}, sort_keys=True, separators=(",", ":"))
        if any_incomplete:
            return SyncOutcome(
                resource_type="change",
                items_processed=items_processed,
                status="partial",
                next_cursor=next_cursor,
                error_summary=(
                    "GitHub change sync hit its per-call budget or a rate limit before "
                    "every repository was caught up; sync paused, will resume next call"
                ),
            )
        return SyncOutcome(
            resource_type="change",
            items_processed=items_processed,
            status="succeeded",
            next_cursor=next_cursor,
        )

    def _sync_reviews(
        self, account: ConnectorAccountContext, *, since_cursor: str | None
    ) -> SyncOutcome:
        """See this module's own docstring ("Reviews: sourced from
        already-synced `changes`") for why the cursor is a single
        `merged_at` timestamp rather than the changes cursor's per-
        repository JSON shape.
        """
        since_merged_at = datetime.fromisoformat(since_cursor) if since_cursor else None
        changes = _list_changes_needing_reviews(
            workspace_id=account.workspace_id,
            connector_account_id=account.connector_account_id,
            since_merged_at=since_merged_at,
        )
        headers = self._headers(account.credential)
        items_processed = 0
        newest_merged_at = since_merged_at
        calls_made = 0
        any_incomplete = False

        for change in changes:
            if calls_made >= _MAX_PAGES_PER_CALL:
                any_incomplete = True
                break
            calls_made += 1
            response = self._request_with_rate_limit_retry(
                "GET",
                f"/repos/{_safe_repo_path_segment(change['repository_name'])}"
                f"/pulls/{change['provider_number']}/reviews",
                headers=headers,
                params={"per_page": _PAGE_SIZE},
            )
            if response is None:
                any_incomplete = True
                break
            if response.status_code != 200:
                raise RuntimeError(
                    f"GitHub review list for PR #{change['provider_number']} failed with "
                    f"status {response.status_code}"
                )
            reviews = response.json()

            # A second call, budgeted the same as the reviews call above --
            # `requested_at` has no analogue on the reviews endpoint itself
            # (see this module's own docstring). Only spent when there is
            # at least one review to attach it to.
            requested_at: str | None = None
            if reviews:
                if calls_made >= _MAX_PAGES_PER_CALL:
                    any_incomplete = True
                    break
                calls_made += 1
                timeline_response = self._request_with_rate_limit_retry(
                    "GET",
                    f"/repos/{_safe_repo_path_segment(change['repository_name'])}/issues/"
                    f"{change['provider_number']}/timeline",
                    headers=headers,
                    params={"per_page": _PAGE_SIZE},
                )
                if timeline_response is None:
                    any_incomplete = True
                    break
                if timeline_response.status_code != 200:
                    raise RuntimeError(
                        f"GitHub timeline for PR #{change['provider_number']} failed with "
                        f"status {timeline_response.status_code}"
                    )
                requested_at = _earliest_review_requested_at(timeline_response.json())

            for review in reviews:
                if review.get("state") == "PENDING" or review.get("submitted_at") is None:
                    continue
                _upsert_review(
                    workspace_id=account.workspace_id,
                    connector_account_id=account.connector_account_id,
                    provider=self.provider,
                    change_id=change["id"],
                    review=review,
                    requested_at=requested_at,
                )
                items_processed += 1

            # Only advance the cursor past a change once both of its calls
            # above have genuinely succeeded -- advancing past a change
            # whose timeline call failed would permanently skip it (the
            # cursor-driven query below only ever looks forward), leaving
            # its `requested_at` stuck `NULL`.
            merged_at = change["merged_at"]
            if merged_at is not None and (newest_merged_at is None or merged_at > newest_merged_at):
                newest_merged_at = merged_at

        next_cursor = newest_merged_at.isoformat() if newest_merged_at is not None else None
        if any_incomplete:
            return SyncOutcome(
                resource_type="review",
                items_processed=items_processed,
                status="partial",
                next_cursor=next_cursor,
                error_summary=(
                    "GitHub review sync hit its per-call budget or a rate limit before "
                    "every merged change was covered; sync paused, will resume next call"
                ),
            )
        return SyncOutcome(
            resource_type="review",
            items_processed=items_processed,
            status="succeeded",
            next_cursor=next_cursor,
        )

    def handle_webhook(
        self, account: ConnectorAccountContext, payload: bytes, headers: Mapping[str, str]
    ) -> SyncOutcome:
        event_type = headers.get("X-GitHub-Event") or headers.get("x-github-event")
        if event_type != "repository" or not payload:
            return SyncOutcome(
                resource_type="repository", items_processed=0, status="succeeded", next_cursor=None
            )
        body = loads(payload)
        repo = body.get("repository")
        if repo is None:
            return SyncOutcome(
                resource_type="repository", items_processed=0, status="succeeded", next_cursor=None
            )
        _upsert_repository(
            workspace_id=account.workspace_id,
            connector_account_id=account.connector_account_id,
            provider=self.provider,
            repo=repo,
        )
        return SyncOutcome(
            resource_type="repository", items_processed=1, status="succeeded", next_cursor=None
        )

    def refresh_permissions(self, account: ConnectorAccountContext) -> PermissionState:
        """**Accepted limitation:** this only re-validates the credential
        itself (`GET /user` still succeeding), not repository-level access
        distinct from validity -- GitHub has no endpoint that reports
        "which of this token's previously-visible repositories can it still
        see" in one call, and this task's scope is repository sync only
        (no stored list of specific repos to individually re-check against
        `GET /repos/{owner}/{repo}` yet). A token that is still valid but
        has lost access to a specific org/repo is not distinguished from
        one that still has full access by this method alone -- that case
        surfaces instead through the ordinary sync path, where a
        previously-visible repository simply stops appearing in `GET
        /user/repos`'s results (Task 2 does not yet mark a no-longer-listed
        `repositories` row `permission_lost`; see `CONNECTOR-CONTRACT.md`'s
        matching "Accepted limitation" entry).
        """
        try:
            response = self._client.get("/user", headers=self._headers(account.credential))
        except httpx.HTTPError:
            return "active"
        if response.status_code == 401:
            return "permission_lost"
        return "active"

    def disconnect(self, account: ConnectorAccountContext) -> None:
        return None
