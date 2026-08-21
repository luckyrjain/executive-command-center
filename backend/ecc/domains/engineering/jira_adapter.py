"""Real Jira Cloud REST API connector adapter (Phase 6 Task 4 -- the third
non-sandbox `ConnectorAdapter`, alongside `github_adapter.GitHubAdapter`
and `gitlab_adapter.GitLabAdapter`, for the `jira` provider slug).

**Work-item sync is the only resource type this task implements** --
Jira is not a source-control provider (design doc, Decision 1's release-
set section), so there is no repository/change/review/deployment
analogue here at all, unlike GitHub/GitLab. `backfill`/`incremental_sync`
return a zero-item success outcome for any `resource_type` other than
`"work_item"`.

**Jira is multi-tenant; GitHub/GitLab are not -- this is a real design
difference this task had to resolve, not a corner cut.** Every GitHub
account shares `api.github.com`; every GitLab.com account shares
`gitlab.com/api/v4`. A Jira Cloud connection, in contrast, lives at a
customer-chosen site (`https://{site}.atlassian.net`), and this framework
has no per-connector-account field for that beyond the single opaque
`credential` string `ConnectorCreateRequest` accepts (design doc Decision
2's single-token storage model, built against GitHub/GitLab's single-
global-host shape). Rather than adding a schema field this task's own
scope does not otherwise need, the credential itself encodes the triple
this adapter needs, pipe-delimited: `"{site}|{email}|{api_token}"` --
`site` is the bare hostname (`acme.atlassian.net`, no scheme), `email` is
the Atlassian account email Jira Cloud's API-token auth requires
alongside the token itself (see below), and `api_token` is the token
value from `https://id.atlassian.com/manage-profile/security/api-tokens`.
This adapter builds its own full request URL per call
(`https://{site}/rest/api/3/...`) rather than setting `httpx.Client`'s
`base_url` once at construction, the way `GitHubAdapter`/`GitLabAdapter`
both can -- a single shared adapter instance in the registry serves every
workspace's Jira connection, each potentially at a different site.

**Jira Cloud's REST API v3 uses HTTP Basic auth (email + API token), not
a bearer token** -- `Authorization: Basic base64(email:api_token)`,
Atlassian's own documented mechanism for personal API tokens (distinct
from a full OAuth 2.0 (3LO) app installation, which this framework's
single-stored-credential model does not support at all: OAuth 2.0 3LO
needs an authorization-code redirect flow and refresh-token rotation,
architecture this activation does not build).

**Accepted limitation: Jira API-token auth has no scope-verification
mechanism at all, not even GitHub's degraded fine-grained-PAT case.**
`CONNECTOR-CONTRACT.md`'s `read:jira-work`/`write:jira-work` scope labels
describe OAuth 2.0 3LO app-installation permissions -- a personal API
token (this adapter's own auth mechanism, per the above) carries no
scope grant of its own at all; it simply carries whatever permissions
the underlying Atlassian account already has, with nothing for `GET
/myself` or any other endpoint to report back. `required_scopes` is
therefore the empty set, and `authorize` always returns an empty
`granted_scopes` -- an honest "nothing to verify," not a fabricated
match, matching this codebase's now-established "never claim a scope
grant this adapter cannot actually observe" precedent from `github_
adapter.py`'s own review-fixed history.

**Pagination is offset-based (`startAt`/`maxResults`/`total`), not
`Link`-header-based** -- Jira's `/rest/api/3/search` endpoint has no
concept of a `Link` header; a response reports its own `total` count,
and a caller advances `startAt` by `maxResults` until it has seen `total`
issues or a page comes back empty.

**Incremental cursor strategy.** JQL's `ORDER BY updated DESC` plus
walking pages until an issue's own `fields.updated` falls at or before
the prior cursor -- the same "stop once you've caught up" idiom `github_
adapter.py`/`gitlab_adapter.py` both use, adapted to JQL instead of a
`sort`/`order_by` query parameter.

**Rate-limit handling.** Jira Cloud signals exhaustion via `429 Too Many
Requests` plus `Retry-After`, identical in shape to `gitlab_adapter.py`'s
own handling (including the fixed-from-the-start "second consecutive
rate-limit hit also degrades to `partial`" behavior, and the
`_MAX_PAGES_PER_CALL` bound reporting `partial` rather than a silent
`succeeded`).

**Webhook ingestion has no receiving HTTP endpoint yet.** `handle_webhook`
implements the parsing/upsert logic for Jira's own webhook shape
(`webhookEvent: "jira:issue_created"`/`"jira:issue_updated"`, an `issue`
key at the payload's top level -- unlike GitHub/GitLab, Jira's webhook
payload already carries the full issue representation needed, no
Push-Hook-style abbreviated-schema gap to work around), but wiring a real
receiving route is disclosed as deferred, identical to the other two
adapters' own deferral.

**`disconnect()` is a documented no-op, like GitHub's (not GitLab's).**
Atlassian exposes no public REST API for a token to revoke itself --
unlike GitLab's genuine `DELETE /personal_access_tokens/self`, a Jira API
token can only be revoked by a human, manually, from `https://
id.atlassian.com/manage-profile/security/api-tokens`. This matches
`CONNECTOR-CONTRACT.md`'s "must not raise for provider does not support
revocation" expected outcome.

**Also captures `fields.created` as of Task 5** (migration
`0048_phase6_delivery_metrics.py`'s `engineering_work_items.provider_
created_at` column, added by that later task, not this one). Task 4's
own `_SEARCH_FIELDS`/`_upsert_work_item` never selected or stored an
issue's own creation date -- harmless until Task 5's `work_ageing` metric
needed "how long has this item been open," which cannot be derived from
`provider_updated_at`/this row's own `observed_at` alone (the latter is
"when this system first saw it," not "when it was actually opened," and
a backfilled old issue synced today would otherwise report a
misleadingly small age). See `metrics.py`'s own module docstring.
"""

from __future__ import annotations

import re
import time
from base64 import b64encode
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from hashlib import sha256
from json import dumps
from typing import Any
from uuid import uuid4

import httpx
from sqlalchemy import text

from ecc.database import SessionFactory

from . import rate_limit_retry
from .connectors import (
    WORKSPACE_ORIGINAL_OWNER_SQL,
    AdapterAuthorizationError,
    ConnectorAccountContext,
    ConnectorAuthorization,
    PermissionState,
    SyncOutcome,
)

_PAGE_SIZE = 100
# Bounded per-call page fetch -- identical reasoning to `github_adapter.
# py`/`gitlab_adapter.py`'s own `_MAX_PAGES_PER_CALL`.
_MAX_PAGES_PER_CALL = 10
_RATE_LIMIT_MAX_WAIT_SECONDS = 5.0
_SEARCH_FIELDS = "summary,issuetype,status,reporter,assignee,created,updated,project"


class _InvalidCredentialError(Exception):
    """Raised when a stored credential is not the `site|email|api_token`
    shape this adapter requires -- always caught and re-raised as
    `AdapterAuthorizationError`, never leaked as a raw internal exception.
    """


# `site` becomes the host of every request URL this adapter builds
# (`f"https://{site}/rest/api/3/..."`), and the credential supplying it is
# workspace-operator-controlled input -- unlike `github_adapter.py`/
# `gitlab_adapter.py`, whose base URLs are hardcoded constants, not user
# data. Restricting `site` to the real shape of a Jira Cloud tenant host
# (`{subdomain}.atlassian.net`, matching this module's own module-docstring
# description of the credential format) closes an otherwise-real SSRF risk
# where an operator-supplied `site` could point this adapter's outbound
# request at an arbitrary internal host instead.
# `\Z` (not `$`) so a trailing newline can't slip through -- `$` matches
# immediately before a trailing "\n" in Python's `re`, which `fullmatch`
# alone does not protect against.
_JIRA_SITE_PATTERN = re.compile(
    r"\A[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.atlassian\.net\Z"
)


def parse_credential(credential: str) -> tuple[str, str, str]:
    parts = credential.split("|", 2)
    if len(parts) != 3 or not all(parts):
        raise _InvalidCredentialError("Jira credential must be in the form 'site|email|api_token'")
    site, email, api_token = parts
    if not _JIRA_SITE_PATTERN.match(site):
        raise _InvalidCredentialError(
            "Jira credential's site must be a bare '{subdomain}.atlassian.net' hostname"
        )
    return site, email, api_token


def _parse_jira_timestamp(value: str | None) -> datetime | None:
    """Jira's `fields.updated` is an ISO-8601 timestamp carrying the
    issuing site's own configured timezone offset (not always UTC/`Z`,
    unlike GitHub/GitLab's serialization) -- comparing two such strings
    lexicographically is unsound across a DST transition, where a later
    instant can serialize with a *smaller* numeric offset and therefore
    sort as lexicographically earlier. Parsing into a real timezone-aware
    `datetime` and comparing those instead avoids that. Returns `None` on
    an unparseable value rather than raising -- `_sync_work_items` treats
    that as "not comparable," never stopping early on it, which is the
    safe direction to fail in (a possible extra sync, never a silently
    dropped update).
    """
    if value is None:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _content_hash(issue: Mapping[str, Any]) -> str:
    fields = issue.get("fields") or {}
    material = dumps(
        {
            "key": issue.get("key"),
            "summary": fields.get("summary"),
            "status": (fields.get("status") or {}).get("name"),
            "created": fields.get("created"),
            "updated": fields.get("updated"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(material.encode("utf-8")).hexdigest()


def _upsert_work_item(
    *,
    workspace_id: Any,
    connector_account_id: Any,
    provider: str,
    site: str,
    issue: Mapping[str, Any],
) -> None:
    """Opens and commits its own session -- identical discipline to
    `github_adapter._upsert_repository`/`gitlab_adapter._upsert_repository`
    (see either function's docstring for the full "no session threaded
    through the adapter protocol" reasoning this mirrors).
    """
    now = datetime.now(UTC)
    fields = issue.get("fields") or {}
    status = fields.get("status") or {}
    issuetype = fields.get("issuetype") or {}
    reporter = fields.get("reporter") or {}
    assignee = fields.get("assignee") or {}
    project = fields.get("project") or {}
    key = issue.get("key") or str(issue["id"])
    with SessionFactory() as session:
        session.execute(
            text(
                f"""
                INSERT INTO engineering_work_items (
                    id, workspace_id, connector_account_id, provider, external_id,
                    title, source_url, item_type, status, reporter_external_id,
                    assignee_external_id, permission_state, freshness_state,
                    content_hash, provider_created_at, provider_updated_at,
                    observed_at, created_at, updated_at, suggested_team_name,
                    owner_id, visibility
                ) VALUES (
                    :id, :workspace_id, :connector_account_id, :provider, :external_id,
                    :title, :source_url, :item_type, :status, :reporter_external_id,
                    :assignee_external_id, 'active', 'fresh',
                    :content_hash, :provider_created_at, :provider_updated_at,
                    :now, :now, :now, :suggested_team_name,
                    {WORKSPACE_ORIGINAL_OWNER_SQL}, 'workspace'
                )
                ON CONFLICT (workspace_id, connector_account_id, external_id) DO UPDATE SET
                    title = EXCLUDED.title,
                    source_url = EXCLUDED.source_url,
                    item_type = EXCLUDED.item_type,
                    status = EXCLUDED.status,
                    reporter_external_id = EXCLUDED.reporter_external_id,
                    assignee_external_id = EXCLUDED.assignee_external_id,
                    permission_state = 'active',
                    freshness_state = 'fresh',
                    content_hash = EXCLUDED.content_hash,
                    provider_created_at = EXCLUDED.provider_created_at,
                    provider_updated_at = EXCLUDED.provider_updated_at,
                    observed_at = EXCLUDED.observed_at,
                    updated_at = EXCLUDED.updated_at,
                    suggested_team_name = EXCLUDED.suggested_team_name,
                    team_suggestion_dismissed_at = CASE
                        WHEN engineering_work_items.suggested_team_name
                            IS DISTINCT FROM EXCLUDED.suggested_team_name
                        THEN NULL
                        ELSE engineering_work_items.team_suggestion_dismissed_at
                    END
                """  # noqa: S608 -- see github_adapter._upsert_repository's identical note
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "connector_account_id": connector_account_id,
                "provider": provider,
                "external_id": str(issue["id"]),
                "title": fields.get("summary") or key,
                "source_url": f"https://{site}/browse/{key}",
                "item_type": issuetype.get("name"),
                "status": status.get("name"),
                "reporter_external_id": reporter.get("accountId"),
                "assignee_external_id": assignee.get("accountId"),
                "content_hash": _content_hash(issue),
                "provider_created_at": fields.get("created"),
                "provider_updated_at": fields.get("updated"),
                "now": now,
                # Jira has no native "team" construct at all -- the issue's
                # own `project.name` is the closest real signal available
                # (an explicit heuristic, not a claim that a Jira project
                # *is* a team; see migration `0050_phase6_team_linkage.py`'s
                # own docstring and `CONNECTOR-CONTRACT.md`'s updated
                # per-provider suggestion note).
                "suggested_team_name": project.get("name"),
            },
        )
        session.commit()


class JiraAdapter:
    provider = "jira"
    # See module docstring's "Accepted limitation" section -- a personal
    # API token has no scope grant of its own to verify.
    required_scopes: frozenset[str] = frozenset()

    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 10.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        client_kwargs: dict[str, Any] = {"timeout": timeout_seconds}
        if transport is not None:
            client_kwargs["transport"] = transport
        self._client = httpx.Client(**client_kwargs)
        self._sleep = sleep

    def _headers(self, email: str, api_token: str) -> dict[str, str]:
        basic = b64encode(f"{email}:{api_token}".encode()).decode()
        return {"Authorization": f"Basic {basic}", "Accept": "application/json"}

    def _request_with_rate_limit_retry(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
    ) -> httpx.Response | None:
        """Jira's own request wrapping over `rate_limit_retry.bounded_
        single_retry`'s shared bounded-wait-then-one-retry envelope -- see
        that function's docstring for the give-up-on-still-limited
        reasoning.
        """

        def make_request() -> httpx.Response:
            try:
                return self._client.request(method, url, headers=headers, params=params)
            except httpx.HTTPError as exc:
                raise RuntimeError(f"Jira request failed: {exc}") from exc

        return rate_limit_retry.bounded_single_retry(
            make_request,
            is_rate_limited=lambda response: response.status_code == 429,
            wait_seconds=self._rate_limit_wait_seconds,
            max_wait_seconds=_RATE_LIMIT_MAX_WAIT_SECONDS,
            sleep=self._sleep,
        )

    def _rate_limit_wait_seconds(self, response: httpx.Response) -> float | None:
        retry_after = response.headers.get("Retry-After")
        if retry_after is None:
            return None
        try:
            return max(0.0, float(retry_after))
        except ValueError:
            return None

    def authorize(self, credential: str) -> ConnectorAuthorization:
        try:
            site, email, api_token = parse_credential(credential)
        except _InvalidCredentialError as exc:
            raise AdapterAuthorizationError(str(exc)) from exc

        try:
            response = self._client.get(
                f"https://{site}/rest/api/3/myself", headers=self._headers(email, api_token)
            )
        except httpx.HTTPError as exc:
            raise AdapterAuthorizationError(f"Jira authorization request failed: {exc}") from exc
        if response.status_code == 401:
            raise AdapterAuthorizationError("Jira rejected the credential (401 Unauthorized)")
        if response.status_code != 200:
            raise AdapterAuthorizationError(
                f"Jira authorization failed with status {response.status_code}"
            )
        body = response.json()

        return ConnectorAuthorization(
            external_account_id=str(body["accountId"]),
            display_name=body.get("displayName") or f"jira-{body['accountId']}",
            granted_scopes=frozenset(),
        )

    def backfill(
        self,
        account: ConnectorAccountContext,
        resource_type: str,
        since: datetime | None = None,
    ) -> SyncOutcome:
        if resource_type != "work_item":
            return SyncOutcome(
                resource_type=resource_type, items_processed=0, status="succeeded", next_cursor=None
            )
        return self._sync_work_items(account, since_cursor=None)

    def incremental_sync(
        self, account: ConnectorAccountContext, resource_type: str, cursor: str | None
    ) -> SyncOutcome:
        if resource_type != "work_item":
            return SyncOutcome(
                resource_type=resource_type,
                items_processed=0,
                status="succeeded",
                next_cursor=cursor,
            )
        return self._sync_work_items(account, since_cursor=cursor)

    def _sync_work_items(
        self, account: ConnectorAccountContext, *, since_cursor: str | None
    ) -> SyncOutcome:
        try:
            site, email, api_token = parse_credential(account.credential)
        except _InvalidCredentialError as exc:
            raise RuntimeError(str(exc)) from exc
        headers = self._headers(email, api_token)
        items_processed = 0
        newest_updated_at = since_cursor
        newest_updated_dt = _parse_jira_timestamp(since_cursor)
        since_cursor_dt = _parse_jira_timestamp(since_cursor)
        start_at = 0
        page = 1
        stopped_early = False

        while page <= _MAX_PAGES_PER_CALL:
            response = self._request_with_rate_limit_retry(
                "GET",
                f"https://{site}/rest/api/3/search",
                headers=headers,
                params={
                    "jql": "ORDER BY updated DESC",
                    "fields": _SEARCH_FIELDS,
                    "startAt": start_at,
                    "maxResults": _PAGE_SIZE,
                },
            )
            if response is None:
                return SyncOutcome(
                    resource_type="work_item",
                    items_processed=items_processed,
                    status="partial",
                    next_cursor=newest_updated_at,
                    error_summary="Jira rate limit exceeded; sync paused, will resume next call",
                )
            if response.status_code != 200:
                raise RuntimeError(f"Jira issue search failed with status {response.status_code}")

            body = response.json()
            issues = body.get("issues") or []
            total = body.get("total", 0)
            if not issues:
                break

            for issue in issues:
                fields = issue.get("fields") or {}
                updated_at = fields.get("updated")
                updated_dt = _parse_jira_timestamp(updated_at)
                if (
                    since_cursor_dt is not None
                    and updated_dt is not None
                    and updated_dt <= since_cursor_dt
                ):
                    stopped_early = True
                    break
                _upsert_work_item(
                    workspace_id=account.workspace_id,
                    connector_account_id=account.connector_account_id,
                    provider=self.provider,
                    site=site,
                    issue=issue,
                )
                items_processed += 1
                if updated_dt is not None and (
                    newest_updated_dt is None or updated_dt > newest_updated_dt
                ):
                    newest_updated_at = updated_at
                    newest_updated_dt = updated_dt

            start_at += len(issues)
            if stopped_early or start_at >= total:
                break
            page += 1
        else:
            # The loop ran `_MAX_PAGES_PER_CALL` iterations without ever
            # `break`-ing -- more issues remain beyond `start_at`. `partial`,
            # not `succeeded` -- identical reasoning to `github_adapter.py`/
            # `gitlab_adapter.py`'s own equivalent bound.
            return SyncOutcome(
                resource_type="work_item",
                items_processed=items_processed,
                status="partial",
                next_cursor=newest_updated_at,
                error_summary=(
                    f"Jira work-item sync hit the {_MAX_PAGES_PER_CALL}-page "
                    "per-call bound with more issues remaining; sync paused, "
                    "will resume next call"
                ),
            )

        return SyncOutcome(
            resource_type="work_item",
            items_processed=items_processed,
            status="succeeded",
            next_cursor=newest_updated_at,
        )

    def handle_webhook(
        self, account: ConnectorAccountContext, payload: bytes, headers: Mapping[str, str]
    ) -> SyncOutcome:
        from json import loads

        if not payload:
            return SyncOutcome(
                resource_type="work_item", items_processed=0, status="succeeded", next_cursor=None
            )
        body = loads(payload)
        if body.get("webhookEvent") not in {"jira:issue_created", "jira:issue_updated"}:
            return SyncOutcome(
                resource_type="work_item", items_processed=0, status="succeeded", next_cursor=None
            )
        issue = body.get("issue")
        if issue is None:
            return SyncOutcome(
                resource_type="work_item", items_processed=0, status="succeeded", next_cursor=None
            )
        try:
            site, _email, _api_token = parse_credential(account.credential)
        except _InvalidCredentialError as exc:
            raise RuntimeError(str(exc)) from exc
        _upsert_work_item(
            workspace_id=account.workspace_id,
            connector_account_id=account.connector_account_id,
            provider=self.provider,
            site=site,
            issue=issue,
        )
        return SyncOutcome(
            resource_type="work_item", items_processed=1, status="succeeded", next_cursor=None
        )

    def refresh_permissions(self, account: ConnectorAccountContext) -> PermissionState:
        try:
            site, email, api_token = parse_credential(account.credential)
        except _InvalidCredentialError:
            return "permission_lost"
        try:
            response = self._client.get(
                f"https://{site}/rest/api/3/myself", headers=self._headers(email, api_token)
            )
        except httpx.HTTPError:
            return "active"
        if response.status_code == 401:
            return "permission_lost"
        return "active"

    def disconnect(self, account: ConnectorAccountContext) -> None:
        return None
