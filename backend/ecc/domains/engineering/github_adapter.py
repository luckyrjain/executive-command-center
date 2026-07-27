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

**Repository sync is the only resource type this task implements.**
`engineering_work_items`/`changes`/`reviews`/`deployments` (`DATA-MODEL.md`)
are deferred to a Task 2 follow-up -- `backfill`/`incremental_sync` return
a zero-item success outcome for any `resource_type` other than
`"repository"` rather than raising, since Task 1's `ConnectorAdapter`
contract has no per-adapter way to declare "this resource type isn't
implemented yet" and silently no-op-succeeding is less surprising to a
caller than an unexplained exception for a resource type the contract
itself lists as valid.

**Incremental cursor strategy.** GitHub's `GET /user/repos` has no
`since` parameter. `_sync_repositories` instead requests `sort=updated&
direction=desc` and walks pages until a row's own `updated_at` falls at
or before the prior cursor (the newest `updated_at` observed in the
previous sync call) -- the same "stop once you've caught up" idiom a
`since`-parameterized API would give for free, implemented explicitly
here because this endpoint doesn't offer one.

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
from json import dumps
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
        if resource_type != "repository":
            return SyncOutcome(
                resource_type=resource_type, items_processed=0, status="succeeded", next_cursor=None
            )
        return self._sync_repositories(account, since_cursor=None)

    def incremental_sync(
        self, account: ConnectorAccountContext, resource_type: str, cursor: str | None
    ) -> SyncOutcome:
        if resource_type != "repository":
            return SyncOutcome(
                resource_type=resource_type,
                items_processed=0,
                status="succeeded",
                next_cursor=cursor,
            )
        return self._sync_repositories(account, since_cursor=cursor)

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

    def handle_webhook(
        self, account: ConnectorAccountContext, payload: bytes, headers: Mapping[str, str]
    ) -> SyncOutcome:
        from json import loads

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
