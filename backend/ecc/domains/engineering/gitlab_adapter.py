"""Real GitLab REST API connector adapter (Phase 6 Task 3 -- the second
non-sandbox `ConnectorAdapter`, alongside `github_adapter.GitHubAdapter`,
for the `gitlab` provider slug).

**HTTP client shape mirrors `github_adapter.GitHubAdapter`** (and, before
it, `ecc.domains.ai_runtime.ollama_client.OllamaAdapter`) -- an injectable
`transport: httpx.BaseTransport | None` constructor parameter (used only
by tests, via `httpx.MockTransport`; production code never passes one),
narrow exception handling that re-raises as this module's own typed
exceptions rather than leaking `httpx`'s.

**Repository sync is the only resource type this task implements**,
identical in scope to `github_adapter.py`'s own Task 2 precedent --
`engineering_work_items`/`changes`/`reviews`/`deployments` (`DATA-MODEL.md`)
remain deferred; `backfill`/`incremental_sync` return a zero-item success
outcome for any other `resource_type` rather than raising.

**Still true as of Task 5.** Task 5 ("delivery and reliability metrics")
added GitHub `change`/`review` sync (`github_adapter.py`'s own module
docstring has the full design) but explicitly scoped GitLab's own
merge-request/approval sync to a Task 5 follow-up rather than doubling
this task's already-large scope in one PR -- GitLab's real API shape
genuinely differs enough to need its own design pass, not a copy-paste
of GitHub's: a global `GET /merge_requests?scope=all&state=merged`
endpoint exists (no per-project fan-out needed, unlike GitHub's Search-
API-vs-fan-out tradeoff), but GitLab's approvals endpoint (`GET
/projects/:id/merge_requests/:iid/approvals`) is Premium/Ultimate-only --
a Free-tier-compatible reviews implementation needs a notes/discussions-
based fallback GitHub's design has no analogue for. Disclosed here
rather than silently left unmentioned; a metric computed only from
GitHub-sourced `changes`/`reviews` reports `partial`/`insufficient_
coverage` for any workspace whose repositories are GitLab-only, per
`DELIVERY-INTELLIGENCE-CONTRACT.md`'s own coverage-threshold policy.

**Scope verification is genuinely complete here, unlike GitHub's.**
GitLab's `GET /personal_access_tokens/self` returns the token's actual
granted `scopes` directly in the JSON response body for every personal
access token -- there is no GitLab analogue of GitHub's fine-grained-PAT
gap where the granted-scopes signal is sometimes simply absent.
`authorize` always compares `granted_scopes` against `_REQUIRED_SCOPES`
and rejects an under-scoped or inactive/revoked token;
`CONNECTOR-CONTRACT.md`'s GitHub-specific "Accepted limitation" sections
around scope verification do not apply to this adapter.

**Incremental cursor strategy.** GitLab's `GET /projects` has no `since`
parameter either (mirrors GitHub's own identical gap) -- `_sync_
repositories` requests `order_by=last_activity_at&sort=desc` and walks
pages until a project's own `last_activity_at` falls at or before the
prior cursor, the same "stop once you've caught up" idiom `github_
adapter.py` uses.

**Rate-limit handling.** GitLab signals exhaustion via `429 Too Many
Requests` plus `Retry-After`. `_request_with_rate_limit_retry` waits once,
bounded to `_RATE_LIMIT_MAX_WAIT_SECONDS`; a still-rate-limited retry
degrades to `status="partial"` rather than raising -- matching `github_
adapter.py`'s own review-fixed behavior for a second consecutive rate
limit hit exactly (see that module's docstring for the failure mode this
avoids: a still-limited retry falling through to an unhandled failure
instead of the same clean `partial` outcome a first hit gets).

**`_MAX_PAGES_PER_CALL` bound reports `partial`, not a silent
`succeeded`,** when more pages remain -- identical reasoning and
mechanism to `github_adapter.py`'s own `while`/`else` structure.

**Webhook ingestion has no receiving HTTP endpoint yet, and no signature
verification.** `handle_webhook` implements the parsing/upsert logic the
`ConnectorAdapter` contract asks for (GitLab's `X-Gitlab-Event: Push Hook`
header -- fired on every push, the only project-webhook event whose
payload nests a `project` object; GitLab's own "Project Hook" is not a
real `X-Gitlab-Event` value). That embedded `project` object is GitLab's
smaller webhook-event schema, not the full `GET /projects` REST
representation -- it has no `last_activity_at` field, so `_with_push_
event_activity_timestamp` synthesizes one from the push's most recent
commit timestamp (or the time of receipt, if the push carried no
commits) before this method calls `_upsert_repository`, rather than
silently persisting a `NULL provider_updated_at` for every webhook-
driven upsert. Wiring a real receiving route (and the webhook-secret-
token storage a real signature check needs) is disclosed as deferred,
identical to `github_adapter.py`'s own deferral. Do not wire this method
to a public route before that gap is closed.

**`disconnect()` attempts real revocation, unlike GitHub's hard no-op.**
GitLab exposes `DELETE /personal_access_tokens/self`, a genuine self-
revocation endpoint classic GitHub PATs have no equivalent of. This
connector's own default read-only scopes (`read_api`, `read_repository`,
per `CONNECTOR-CONTRACT.md`) do not include the `api`/`self_rotate` scope
GitLab requires for this call in practice, so the attempt is expected to
fail (403) for a connection authorized at this connector's own default
scope -- attempted anyway (never silently skipped, matching this
connector's own scope-honesty design elsewhere in this module), and the
caller (`connector_accounts.disable_connector_endpoint`) already treats
any `disconnect()` failure as best-effort, never blocking disconnection.
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
    WORKSPACE_ORIGINAL_OWNER_SQL,
    AdapterAuthorizationError,
    ConnectorAccountContext,
    ConnectorAuthorization,
    PermissionState,
    SyncOutcome,
)

GITLAB_API_BASE_URL = "https://gitlab.com/api/v4"
_GITLAB_WEB_BASE_URL = "https://gitlab.com"
# `CONNECTOR-CONTRACT.md`'s resolved GitLab read scopes -- unlike GitHub's
# `repo`, these are real GitLab OAuth/PAT scope names, and (see module
# docstring) always checkable via `GET /personal_access_tokens/self`.
_REQUIRED_SCOPES: frozenset[str] = frozenset({"read_api", "read_repository"})
_PAGE_SIZE = 100
# Bounded per-call page fetch -- identical reasoning to `github_adapter.
# py`'s own `_MAX_PAGES_PER_CALL`: a workspace with more pages than this
# resumes across multiple `/sync` calls via the cursor.
_MAX_PAGES_PER_CALL = 10
_RATE_LIMIT_MAX_WAIT_SECONDS = 5.0


def _safe_source_url(raw_url: str | None, *, fallback: str) -> str:
    """Same allow-list defense `datadog_adapter.py`'s `_upsert_dashboard`
    already applies to its own provider-returned `url` field, and
    `github_adapter.py`'s identical `_safe_source_url` applies to
    `html_url` (review found this adapter's own `web_url` field never
    received it): a value not scoped to GitLab's own web host is never
    trusted verbatim -- it could be `javascript:`/`data:`/an arbitrary
    external host if the connected GitLab instance were compromised or
    malicious -- falling back to a safe, server-constructed default
    instead of rendering it as a clickable link.
    """
    if raw_url and raw_url.startswith(f"{_GITLAB_WEB_BASE_URL}/"):
        return raw_url
    return fallback


def _content_hash(project: Mapping[str, Any]) -> str:
    material = dumps(
        {
            "name": project.get("path_with_namespace"),
            "default_branch": project.get("default_branch"),
            "web_url": project.get("web_url"),
            "last_activity_at": project.get("last_activity_at"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(material.encode("utf-8")).hexdigest()


def _suggested_team_name(project: Mapping[str, Any]) -> str | None:
    """GitLab's own `namespace` field -- the group or user this project
    belongs to -- mirrors `github_adapter.py`'s identical `owner.login`
    hint (see migration `0050_phase6_team_linkage.py`'s own docstring for
    why this is a free-text suggestion, never a link). Its *shape* differs
    between the two payloads this function is called with: the full `GET
    /projects` REST representation nests it as an object (`{"name": ...,
    "path": ..., "kind": ...}`), but a real GitLab `Push Hook` webhook
    payload's own `project.namespace` is a bare display-name string --
    the identical REST-vs-webhook schema mismatch `_with_push_event_
    activity_timestamp`'s own docstring already documents for
    `last_activity_at`. Handling both shapes here, rather than assuming
    the REST dict shape, avoids an `AttributeError` crashing every
    webhook-driven upsert.
    """
    namespace = project.get("namespace")
    if isinstance(namespace, Mapping):
        return namespace.get("name")
    if isinstance(namespace, str) and namespace:
        return namespace
    return None


def _upsert_repository(
    *, workspace_id: Any, connector_account_id: Any, provider: str, project: Mapping[str, Any]
) -> None:
    """Opens and commits its own session -- identical discipline to
    `github_adapter._upsert_repository`'s own (see that function's
    docstring for the full "no session threaded through the adapter
    protocol" reasoning this mirrors).
    """
    now = datetime.now(UTC)
    provider_updated_at = project.get("last_activity_at")
    suggested_team_name = _suggested_team_name(project)
    with SessionFactory() as session:
        session.execute(
            text(
                f"""
                INSERT INTO repositories (
                    id, workspace_id, connector_account_id, provider, external_id,
                    name, source_url, default_branch, permission_state, freshness_state,
                    content_hash, provider_updated_at, observed_at, created_at, updated_at,
                    suggested_team_name, owner_id, visibility
                ) VALUES (
                    :id, :workspace_id, :connector_account_id, :provider, :external_id,
                    :name, :source_url, :default_branch, 'active', 'fresh',
                    :content_hash, :provider_updated_at, :now, :now, :now,
                    :suggested_team_name,
                    {WORKSPACE_ORIGINAL_OWNER_SQL}, 'workspace'
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
                    updated_at = EXCLUDED.updated_at,
                    suggested_team_name = EXCLUDED.suggested_team_name
                """  # noqa: S608 -- see github_adapter._upsert_repository's identical note
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "connector_account_id": connector_account_id,
                "provider": provider,
                "external_id": str(project["id"]),
                "name": project.get("path_with_namespace") or str(project["id"]),
                "source_url": _safe_source_url(
                    project.get("web_url"),
                    fallback=(
                        f"{_GITLAB_WEB_BASE_URL}/"
                        f"{project.get('path_with_namespace') or project['id']}"
                    ),
                ),
                "default_branch": project.get("default_branch"),
                "content_hash": _content_hash(project),
                "provider_updated_at": provider_updated_at,
                "now": now,
                "suggested_team_name": suggested_team_name,
            },
        )
        session.commit()


def _with_push_event_activity_timestamp(
    project: Mapping[str, Any], body: Mapping[str, Any]
) -> dict[str, Any]:
    """A real GitLab `Push Hook` payload's embedded `project` object has no
    `last_activity_at` field -- that field only exists on the full `GET
    /projects` REST representation, not the smaller schema GitLab's
    webhook events share. Without this, `_upsert_repository`'s `provider_
    updated_at` would silently become `NULL` for every webhook-driven
    upsert, diverging from REST-sync-driven upserts (review found this
    exact gap). The last entry in `commits` (GitLab orders this array
    oldest-first) is the most recent commit's own timestamp -- a
    reasonable proxy for "when was this project last updated" -- falling
    back to the time this webhook was received if the push carried no
    commits at all (e.g. a tag-only push or a branch deletion).
    """
    if project.get("last_activity_at"):
        return dict(project)
    commits = body.get("commits") or []
    timestamp = commits[-1].get("timestamp") if commits else None
    merged = dict(project)
    merged["last_activity_at"] = timestamp or datetime.now(UTC).isoformat()
    return merged


class GitLabAdapter:
    provider = "gitlab"
    required_scopes: frozenset[str] = _REQUIRED_SCOPES

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 10.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        client_kwargs: dict[str, Any] = {
            "base_url": GITLAB_API_BASE_URL,
            "timeout": timeout_seconds,
        }
        if transport is not None:
            client_kwargs["transport"] = transport
        self._client = httpx.Client(**client_kwargs)
        self._sleep = sleep

    def _headers(self, credential: str) -> dict[str, str]:
        return {"PRIVATE-TOKEN": credential, "Accept": "application/json"}

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
            raise RuntimeError(f"GitLab request failed: {exc}") from exc

        if response.status_code != 429:
            return response

        wait_seconds = self._rate_limit_wait_seconds(response)
        if wait_seconds is None or wait_seconds > _RATE_LIMIT_MAX_WAIT_SECONDS:
            return None
        self._sleep(wait_seconds)
        try:
            retry_response = self._client.request(method, path, headers=headers, params=params)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"GitLab request failed: {exc}") from exc

        # A second rate-limit hit right after the bounded wait must not be
        # trusted as a normal response -- matches `github_adapter.py`'s
        # own identical review-fixed reasoning exactly.
        if retry_response.status_code == 429:
            return None
        return retry_response

    def _rate_limit_wait_seconds(self, response: httpx.Response) -> float | None:
        retry_after = response.headers.get("Retry-After")
        if retry_after is None:
            return None
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            return None

    def authorize(self, credential: str) -> ConnectorAuthorization:
        """Two calls, unlike GitHub's one: `GET /personal_access_tokens/
        self` (scope/validity check -- GitLab always returns the token's
        real `scopes` here, see module docstring) and `GET /user` (account
        identity). Raises `AdapterAuthorizationError` for an invalid,
        revoked, inactive, or under-scoped token.
        """
        headers = self._headers(credential)
        try:
            token_response = self._client.get("/personal_access_tokens/self", headers=headers)
        except httpx.HTTPError as exc:
            raise AdapterAuthorizationError(f"GitLab authorization request failed: {exc}") from exc
        if token_response.status_code == 401:
            raise AdapterAuthorizationError("GitLab rejected the credential (401 Unauthorized)")
        if token_response.status_code != 200:
            raise AdapterAuthorizationError(
                f"GitLab authorization failed with status {token_response.status_code}"
            )
        token_body = token_response.json()
        if token_body.get("revoked"):
            raise AdapterAuthorizationError("GitLab token has been revoked")
        if token_body.get("active") is False:
            raise AdapterAuthorizationError("GitLab token is not active")

        granted = frozenset(token_body.get("scopes") or [])
        if not _REQUIRED_SCOPES.issubset(granted):
            missing = ", ".join(sorted(_REQUIRED_SCOPES - granted))
            raise AdapterAuthorizationError(f"GitLab token is missing required scope(s): {missing}")

        try:
            user_response = self._client.get("/user", headers=headers)
        except httpx.HTTPError as exc:
            raise AdapterAuthorizationError(f"GitLab authorization request failed: {exc}") from exc
        if user_response.status_code != 200:
            raise AdapterAuthorizationError(
                f"GitLab authorization failed with status {user_response.status_code}"
            )
        user_body = user_response.json()

        return ConnectorAuthorization(
            external_account_id=str(user_body["id"]),
            display_name=user_body.get("username") or f"gitlab-{user_body['id']}",
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
                "/projects",
                headers=headers,
                params={
                    "membership": "true",
                    "order_by": "last_activity_at",
                    "sort": "desc",
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
                    error_summary="GitLab rate limit exceeded; sync paused, will resume next call",
                )
            if response.status_code != 200:
                raise RuntimeError(f"GitLab project list failed with status {response.status_code}")

            projects = response.json()
            if not projects:
                break

            for project in projects:
                updated_at = project.get("last_activity_at")
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
                    project=project,
                )
                items_processed += 1
                if newest_updated_at is None or (updated_at and updated_at > newest_updated_at):
                    newest_updated_at = updated_at

            # `response.links` (httpx's own RFC 8288 `Link`-header parser)
            # -- matches `github_adapter.py`'s own review-fixed choice over
            # a hand-rolled comma-split.
            if stopped_early or "next" not in response.links:
                break
            page += 1
        else:
            # The loop ran `_MAX_PAGES_PER_CALL` iterations without ever
            # `break`-ing -- the last page fetched still had further pages
            # available. `partial`, not `succeeded` -- identical reasoning
            # to `github_adapter.py`'s own equivalent bound.
            return SyncOutcome(
                resource_type="repository",
                items_processed=items_processed,
                status="partial",
                next_cursor=newest_updated_at,
                error_summary=(
                    f"GitLab project sync hit the {_MAX_PAGES_PER_CALL}-page "
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

        event_type = headers.get("X-Gitlab-Event") or headers.get("x-gitlab-event")
        if event_type != "Push Hook" or not payload:
            return SyncOutcome(
                resource_type="repository", items_processed=0, status="succeeded", next_cursor=None
            )
        body = loads(payload)
        project = body.get("project")
        if project is None:
            return SyncOutcome(
                resource_type="repository", items_processed=0, status="succeeded", next_cursor=None
            )
        _upsert_repository(
            workspace_id=account.workspace_id,
            connector_account_id=account.connector_account_id,
            provider=self.provider,
            project=_with_push_event_activity_timestamp(project, body),
        )
        return SyncOutcome(
            resource_type="repository", items_processed=1, status="succeeded", next_cursor=None
        )

    def refresh_permissions(self, account: ConnectorAccountContext) -> PermissionState:
        """Fails open (returns `"active"`) on a network error or any
        response other than a clear `401`/revoked/inactive signal --
        matches `GitHubAdapter.refresh_permissions`'s identical tradeoff:
        a transient failure (a `403`/`5xx` blip, a rate limit) must not be
        mistaken for a real permission-loss signal. `PermissionState`'s
        `"deleted"` value is never returned by either adapter -- GitLab
        (like GitHub) has no single call reporting "this account itself
        was deleted" distinct from "this token no longer works," so this
        adapter can only ever distinguish `active` from `permission_lost`.
        """
        try:
            response = self._client.get(
                "/personal_access_tokens/self", headers=self._headers(account.credential)
            )
        except httpx.HTTPError:
            return "active"
        if response.status_code == 401:
            return "permission_lost"
        if response.status_code == 200:
            body = response.json()
            if body.get("revoked") or body.get("active") is False:
                return "permission_lost"
        return "active"

    def disconnect(self, account: ConnectorAccountContext) -> None:
        """Best-effort self-revocation -- see module docstring for why
        this connector's own default read-only scopes make this call
        expected to fail (403) in practice, and why it is attempted
        anyway rather than skipped. The caller (`disable_connector_
        endpoint`) already treats any exception here as best-effort, so
        both a network failure and a non-2xx status simply raise --
        never silently absorbed at this layer.
        """
        try:
            response = self._client.delete(
                "/personal_access_tokens/self", headers=self._headers(account.credential)
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"GitLab token revocation request failed: {exc}") from exc
        if response.status_code not in (204, 404):
            raise RuntimeError(f"GitLab token revocation failed with status {response.status_code}")
