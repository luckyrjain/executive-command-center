"""Real Datadog REST API connector adapter (Phase 6, post-Task-8 follow-up
-- the fourth non-sandbox `ConnectorAdapter`, alongside `github_adapter.
GitHubAdapter`, `gitlab_adapter.GitLabAdapter` and `jira_adapter.
JiraAdapter`, for the `datadog` provider slug).

**All three requested resource types are implemented together, not just
one** -- the user's own explicit choice ("all of the above") when asked
which of Monitors/Service Catalog/Dashboards to build first. `monitor`,
`service_definition` and `dashboard` are the three `resource_type` values
this adapter recognizes; `backfill`/`incremental_sync` return a zero-item
success outcome for any other value, matching `jira_adapter.py`'s own
precedent for a resource type outside an adapter's scope.

**Datadog is multi-region, like Jira is multi-tenant -- the same real
design difference `jira_adapter.py`'s own docstring already resolved for
this framework's single-opaque-`credential`-string model.** Unlike
GitHub/GitLab's single global host, a Datadog account lives at one of a
small, closed set of regional API sites (`api.datadoghq.com`,
`api.us3.datadoghq.com`, `api.us5.datadoghq.com`, `api.datadoghq.eu`,
`api.ap1.datadoghq.com`, `api.ddog-gov.com`) -- Datadog's own documented
site list, not an open-ended customer subdomain the way Jira Cloud's
`{subdomain}.atlassian.net` is. The credential therefore pipe-delimits a
triple: `"{site}|{api_key}|{app_key}"` -- `site` is one of that closed
set (validated against it directly, not a regex, since the set is small
and fixed -- a tighter SSRF-prevention bound than Jira's own pattern-based
check can offer), `api_key` is a Datadog API key (`DD-API-KEY`), and
`app_key` is a Datadog application key (`DD-APPLICATION-KEY`) -- Datadog's
REST API requires both headers for any read call beyond the bare
`/api/v1/validate` endpoint.

**Accepted limitation: no stable account-identity endpoint is used for
`external_account_id`.** Unlike GitHub/GitLab/Jira, none of which required
guessing, this adapter could not confirm a Datadog endpoint that reliably
returns a stable per-key account/organization identifier from just an API
key and app key (an org-level `GET /api/v2/org` route exists, but its
response schema could not be confirmed). Rather than guess at an unverified
field path, `external_account_id` is a disclosed `sha256(api_key)` hash
prefix -- stable across calls for the same key (so re-authorizing the same
credential resolves to the same connector account, matching this
framework's own dedupe expectation), but not a real Datadog-issued
identifier. `display_name` includes the site and the key's own last four
characters (`f"Datadog {site} (...{api_key[-4:]})"`) so an operator can
still tell connections apart in `GET /engineering/connectors`.

**Accepted limitation: no scope-verification mechanism, like Jira's own
API-token case (see `jira_adapter.py`'s own "Accepted limitation" for the
identical reasoning), for a different underlying cause.** A Datadog
application key can optionally be scoped, but its scope list is only
readable via `GET /api/v2/current_user/application_keys/{app_key_id}`,
which requires the key's own opaque ID -- not derivable from the raw key
value this adapter is handed. `required_scopes` is therefore the empty
set, and `authorize` always returns an empty `granted_scopes` -- an honest
"nothing to verify," not a fabricated match.

**No incremental delta filter exists for any of the three list endpoints
this adapter uses** -- unlike GitHub/GitLab/Jira, which all expose a
"give me items changed since X" query capability this framework's cursor
design was built around, none of `GET /api/v1/monitor`, `GET /api/v2/
services/definitions` or `GET /api/v1/dashboard` accepts an
`updated_since`-shaped parameter. `incremental_sync` therefore behaves
identically to `backfill` for all three resource types -- a full
re-walk of the list, bounded by `_MAX_PAGES_PER_CALL` the same way
backfill is, relying on each row's own `ON CONFLICT ... DO UPDATE` to make
re-processing an unchanged item a no-op at the database level.
`next_cursor` is always `None`: there is no server-side position to
resume from, so nothing is persisted to `sync_cursors.cursor_value` for
these three resource types (a disclosed gap, not a bug -- `items_
processed` still accurately reports how many rows were re-synced).

**Pagination differs across the three resource types, and one is
genuinely unconfirmed.** `GET /api/v1/monitor` returns a bare JSON array
(no envelope) and accepts `page`/`page_size`; a page shorter than `page_
size` ends the walk. `GET /api/v2/services/definitions` wraps its list in
`{"data": [...], "meta": {...}}` and accepts `page[size]`/`page[number]`;
this adapter stops the same way (a short page), rather than trust an
unconfirmed `meta.pagination` shape. `GET /api/v1/dashboard` returns
`{"dashboards": [...]}` with **no documented pagination parameters at
all** -- this adapter fetches it in one unbounded call and discloses that
directly rather than inventing page parameters Datadog's own dashboard-
list endpoint may not support.

**`suggested_team_name` per resource type -- one real field, one
heuristic tag parse, one likely-always-`None`.** Service definitions carry
a genuine `team` field in Datadog's own schema (`attributes.schema.team`)
-- copied verbatim, still requiring human confirmation like every other
provider's suggestion (this codebase's "never auto-create entities from
external evidence" principle, `migration 0050_phase6_team_linkage.py`'s
own docstring). Monitors have no first-class team field; Datadog's own
convention is a `team:<name>` tag, parsed from the monitor's `tags` array
when present. Dashboards' list endpoint is not confirmed to return a
`tags` field at all (only the full single-dashboard `GET` does, which
this adapter does not call per-dashboard to avoid an N+1 sync) -- the
lookup is defensive (`.get("tags")`) and will, in practice, very likely
always resolve to `None`; a disclosed limitation, not a broken feature.

**Rate-limit handling.** Datadog signals exhaustion via `429 Too Many
Requests` plus an `X-RateLimit-Reset` header (seconds until the window
resets) -- not a `Retry-After` date the way GitLab/Jira both use. Same
bounded-wait-then-degrade-to-`partial` shape as the other three adapters,
including the "second consecutive rate-limit hit also degrades to
`partial`" behavior.

**`handle_webhook` is a documented no-op, unlike Jira's own (which parses
a fixed payload shape even without a receiving route wired up).** Datadog
webhook notifications are monitor-notification-only (configured per-
monitor via a `@webhook-<name>` recipient in the monitor's own `message`
field), and the payload body is a customer-defined template
(`$ID`/`$ALERT_TITLE`/... substitution), not a fixed schema this adapter
could parse without a workspace-specific template contract this task does
not define. Returning a zero-item success outcome unconditionally is the
honest behavior here, not a stopgap.

**`refresh_permissions`/`disconnect` mirror Jira's own documented-no-op
shape.** `refresh_permissions` re-validates the API key via `GET /api/v1/
validate` (an app key is not required for that specific endpoint);
`disconnect` is a no-op -- Datadog exposes no public REST endpoint for a
key to revoke itself by its own value (only `DELETE /api/v2/api_keys/{id}`
/`DELETE /api/v2/application_keys/{id}`, both requiring the key's own
opaque ID, not derivable from the raw key string this adapter holds).
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

_PAGE_SIZE = 100
# Bounded per-call page fetch -- identical reasoning to `github_adapter.
# py`/`gitlab_adapter.py`/`jira_adapter.py`'s own `_MAX_PAGES_PER_CALL`.
_MAX_PAGES_PER_CALL = 10
_RATE_LIMIT_MAX_WAIT_SECONDS = 5.0

# Datadog's own documented closed set of regional API/UI site pairs
# (https://docs.datadoghq.com/getting_started/site/). A fixed membership
# check, not a hostname regex -- this set is small and Datadog-controlled,
# so validating against it directly is a tighter SSRF-prevention bound
# than `jira_adapter.py`'s own pattern-based check can offer for Jira's
# genuinely open-ended customer subdomains.
_SITE_TO_UI_HOST: dict[str, str] = {
    "api.datadoghq.com": "app.datadoghq.com",
    "api.us3.datadoghq.com": "us3.datadoghq.com",
    "api.us5.datadoghq.com": "us5.datadoghq.com",
    "api.datadoghq.eu": "app.datadoghq.eu",
    "api.ap1.datadoghq.com": "ap1.datadoghq.com",
    "api.ddog-gov.com": "app.ddog-gov.com",
}


class _InvalidCredentialError(Exception):
    """Raised when a stored credential is not the `site|api_key|app_key`
    shape this adapter requires, or `site` is not one of Datadog's own
    documented regional API hosts -- always caught and re-raised as
    `AdapterAuthorizationError`, never leaked as a raw internal exception.
    """


def _parse_credential(credential: str) -> tuple[str, str, str]:
    parts = credential.split("|", 2)
    if len(parts) != 3 or not all(parts):
        raise _InvalidCredentialError(
            "Datadog credential must be in the form 'site|api_key|app_key'"
        )
    site, api_key, app_key = parts
    if site not in _SITE_TO_UI_HOST:
        raise _InvalidCredentialError(
            f"Datadog credential's site must be one of {sorted(_SITE_TO_UI_HOST)}"
        )
    return site, api_key, app_key


def _external_account_id(api_key: str) -> str:
    return sha256(api_key.encode("utf-8")).hexdigest()[:32]


def _monitor_team_tag(tags: list[str]) -> str | None:
    for tag in tags:
        if tag.startswith("team:"):
            return tag[len("team:") :] or None
    return None


def _monitor_content_hash(monitor: Mapping[str, Any]) -> str:
    material = dumps(
        {
            "name": monitor.get("name"),
            "type": monitor.get("type"),
            "query": monitor.get("query"),
            "overall_state": monitor.get("overall_state"),
            "tags": sorted(monitor.get("tags") or []),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(material.encode("utf-8")).hexdigest()


def _upsert_monitor(
    *,
    workspace_id: Any,
    connector_account_id: Any,
    provider: str,
    site: str,
    monitor: Mapping[str, Any],
) -> None:
    now = datetime.now(UTC)
    ui_host = _SITE_TO_UI_HOST[site]
    monitor_id = monitor["id"]
    tags = monitor.get("tags") or []
    with SessionFactory() as session:
        session.execute(
            text(
                """
                INSERT INTO datadog_monitors (
                    id, workspace_id, connector_account_id, provider, external_id,
                    source_url, permission_state, freshness_state, content_hash,
                    provider_updated_at, observed_at, created_at, updated_at,
                    suggested_team_name, name, monitor_type, query, overall_state
                ) VALUES (
                    :id, :workspace_id, :connector_account_id, :provider, :external_id,
                    :source_url, 'active', 'fresh', :content_hash,
                    :provider_updated_at, :now, :now, :now,
                    :suggested_team_name, :name, :monitor_type, :query, :overall_state
                )
                ON CONFLICT (workspace_id, connector_account_id, external_id) DO UPDATE SET
                    source_url = EXCLUDED.source_url,
                    permission_state = 'active',
                    freshness_state = 'fresh',
                    content_hash = EXCLUDED.content_hash,
                    provider_updated_at = EXCLUDED.provider_updated_at,
                    observed_at = EXCLUDED.observed_at,
                    updated_at = EXCLUDED.updated_at,
                    suggested_team_name = EXCLUDED.suggested_team_name,
                    name = EXCLUDED.name,
                    monitor_type = EXCLUDED.monitor_type,
                    query = EXCLUDED.query,
                    overall_state = EXCLUDED.overall_state
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "connector_account_id": connector_account_id,
                "provider": provider,
                "external_id": str(monitor_id),
                "source_url": f"https://{ui_host}/monitors/{monitor_id}",
                "content_hash": _monitor_content_hash(monitor),
                "provider_updated_at": monitor.get("modified"),
                "suggested_team_name": _monitor_team_tag(tags),
                "name": monitor.get("name") or str(monitor_id),
                "monitor_type": monitor.get("type"),
                "query": monitor.get("query"),
                "overall_state": monitor.get("overall_state"),
                "now": now,
            },
        )
        session.commit()


def _service_definition_content_hash(schema: Mapping[str, Any]) -> str:
    material = dumps(
        {
            "dd-service": schema.get("dd-service"),
            "team": schema.get("team"),
            "tier": schema.get("tier"),
            "description": schema.get("description"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(material.encode("utf-8")).hexdigest()


def _upsert_service_definition(
    *,
    workspace_id: Any,
    connector_account_id: Any,
    provider: str,
    site: str,
    definition: Mapping[str, Any],
) -> None:
    now = datetime.now(UTC)
    ui_host = _SITE_TO_UI_HOST[site]
    attributes = definition.get("attributes") or {}
    schema = attributes.get("schema") or {}
    # The user-authored `dd-service` name is the real, stable identifying
    # key for a service definition (these are workspace-authored YAML/JSON
    # docs keyed by service name, not database-generated records) -- used
    # as `external_id` rather than the envelope's own `id` field, which is
    # not confirmed to exist consistently across API versions.
    dd_service = schema.get("dd-service") or definition.get("id")
    with SessionFactory() as session:
        session.execute(
            text(
                """
                INSERT INTO datadog_service_definitions (
                    id, workspace_id, connector_account_id, provider, external_id,
                    source_url, permission_state, freshness_state, content_hash,
                    observed_at, created_at, updated_at,
                    suggested_team_name, name, team, tier, description
                ) VALUES (
                    :id, :workspace_id, :connector_account_id, :provider, :external_id,
                    :source_url, 'active', 'fresh', :content_hash,
                    :now, :now, :now,
                    :suggested_team_name, :name, :team, :tier, :description
                )
                ON CONFLICT (workspace_id, connector_account_id, external_id) DO UPDATE SET
                    source_url = EXCLUDED.source_url,
                    permission_state = 'active',
                    freshness_state = 'fresh',
                    content_hash = EXCLUDED.content_hash,
                    observed_at = EXCLUDED.observed_at,
                    updated_at = EXCLUDED.updated_at,
                    suggested_team_name = EXCLUDED.suggested_team_name,
                    name = EXCLUDED.name,
                    team = EXCLUDED.team,
                    tier = EXCLUDED.tier,
                    description = EXCLUDED.description
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "connector_account_id": connector_account_id,
                "provider": provider,
                "external_id": str(dd_service),
                "source_url": f"https://{ui_host}/services/{dd_service}",
                "content_hash": _service_definition_content_hash(schema),
                "suggested_team_name": schema.get("team"),
                "name": schema.get("dd-service") or str(dd_service),
                "team": schema.get("team"),
                "tier": schema.get("tier"),
                "description": schema.get("description"),
                "now": now,
            },
        )
        session.commit()


def _dashboard_content_hash(dashboard: Mapping[str, Any]) -> str:
    material = dumps(
        {
            "title": dashboard.get("title"),
            "description": dashboard.get("description"),
            "modified_at": dashboard.get("modified_at"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(material.encode("utf-8")).hexdigest()


def _upsert_dashboard(
    *,
    workspace_id: Any,
    connector_account_id: Any,
    provider: str,
    site: str,
    dashboard: Mapping[str, Any],
) -> None:
    now = datetime.now(UTC)
    ui_host = _SITE_TO_UI_HOST[site]
    dashboard_id = dashboard["id"]
    url = dashboard.get("url") or f"/dashboard/{dashboard_id}"
    # `tags` is not confirmed present on the list endpoint's own summary
    # shape (see module docstring) -- `.get` is defensive, not load-bearing.
    tags = dashboard.get("tags") or []
    suggested_team_name = _monitor_team_tag(tags) if tags else None
    with SessionFactory() as session:
        session.execute(
            text(
                """
                INSERT INTO datadog_dashboards (
                    id, workspace_id, connector_account_id, provider, external_id,
                    source_url, permission_state, freshness_state, content_hash,
                    provider_updated_at, observed_at, created_at, updated_at,
                    suggested_team_name, title, description
                ) VALUES (
                    :id, :workspace_id, :connector_account_id, :provider, :external_id,
                    :source_url, 'active', 'fresh', :content_hash,
                    :provider_updated_at, :now, :now, :now,
                    :suggested_team_name, :title, :description
                )
                ON CONFLICT (workspace_id, connector_account_id, external_id) DO UPDATE SET
                    source_url = EXCLUDED.source_url,
                    permission_state = 'active',
                    freshness_state = 'fresh',
                    content_hash = EXCLUDED.content_hash,
                    provider_updated_at = EXCLUDED.provider_updated_at,
                    observed_at = EXCLUDED.observed_at,
                    updated_at = EXCLUDED.updated_at,
                    suggested_team_name = EXCLUDED.suggested_team_name,
                    title = EXCLUDED.title,
                    description = EXCLUDED.description
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "connector_account_id": connector_account_id,
                "provider": provider,
                "external_id": str(dashboard_id),
                "source_url": f"https://{ui_host}{url}" if url.startswith("/") else url,
                "content_hash": _dashboard_content_hash(dashboard),
                "provider_updated_at": dashboard.get("modified_at"),
                "suggested_team_name": suggested_team_name,
                "title": dashboard.get("title") or str(dashboard_id),
                "description": dashboard.get("description"),
                "now": now,
            },
        )
        session.commit()


class DatadogAdapter:
    provider = "datadog"
    # See module docstring's "Accepted limitation" section -- an
    # application key's scopes cannot be inspected without its own opaque
    # ID, which this adapter is never handed.
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

    def _headers(self, api_key: str, app_key: str | None = None) -> dict[str, str]:
        headers = {"DD-API-KEY": api_key, "Accept": "application/json"}
        if app_key is not None:
            headers["DD-APPLICATION-KEY"] = app_key
        return headers

    def _request_with_rate_limit_retry(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
    ) -> httpx.Response | None:
        """Returns `None` only when rate-limited beyond the bounded wait --
        callers treat that as "give up for now, resume next sync call."
        Matches `jira_adapter.py`/`gitlab_adapter.py`'s identical method,
        adapted to Datadog's own `X-RateLimit-Reset` header instead of
        `Retry-After`.
        """
        try:
            response = self._client.request(method, url, headers=headers, params=params)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Datadog request failed: {exc}") from exc

        if response.status_code != 429:
            return response

        wait_seconds = self._rate_limit_wait_seconds(response)
        if wait_seconds is None or wait_seconds > _RATE_LIMIT_MAX_WAIT_SECONDS:
            return None
        self._sleep(wait_seconds)
        try:
            retry_response = self._client.request(method, url, headers=headers, params=params)
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Datadog request failed: {exc}") from exc

        if retry_response.status_code == 429:
            return None
        return retry_response

    def _rate_limit_wait_seconds(self, response: httpx.Response) -> float | None:
        reset = response.headers.get("X-RateLimit-Reset")
        if reset is None:
            return None
        try:
            return max(0.0, float(reset))
        except ValueError:
            return None

    def authorize(self, credential: str) -> ConnectorAuthorization:
        try:
            site, api_key, app_key = _parse_credential(credential)
        except _InvalidCredentialError as exc:
            raise AdapterAuthorizationError(str(exc)) from exc

        try:
            response = self._client.get(
                f"https://{site}/api/v1/validate", headers=self._headers(api_key)
            )
        except httpx.HTTPError as exc:
            raise AdapterAuthorizationError(f"Datadog authorization request failed: {exc}") from exc
        if response.status_code == 403:
            raise AdapterAuthorizationError("Datadog rejected the API key (403 Forbidden)")
        if response.status_code != 200:
            raise AdapterAuthorizationError(
                f"Datadog API key validation failed with status {response.status_code}"
            )

        try:
            app_key_response = self._client.get(
                f"https://{site}/api/v1/monitor",
                headers=self._headers(api_key, app_key),
                params={"page": 0, "page_size": 1},
            )
        except httpx.HTTPError as exc:
            raise AdapterAuthorizationError(f"Datadog authorization request failed: {exc}") from exc
        if app_key_response.status_code == 403:
            raise AdapterAuthorizationError("Datadog rejected the application key (403 Forbidden)")
        if app_key_response.status_code != 200:
            raise AdapterAuthorizationError(
                "Datadog application key validation failed with status "
                f"{app_key_response.status_code}"
            )

        return ConnectorAuthorization(
            external_account_id=_external_account_id(api_key),
            display_name=f"Datadog {site} (...{api_key[-4:]})",
            granted_scopes=frozenset(),
        )

    def backfill(self, account: ConnectorAccountContext, resource_type: str) -> SyncOutcome:
        return self._sync(account, resource_type)

    def incremental_sync(
        self, account: ConnectorAccountContext, resource_type: str, cursor: str | None
    ) -> SyncOutcome:
        # See module docstring -- no server-side delta filter exists for
        # any of the three resource types, so incremental behaves exactly
        # like backfill. `cursor` is accepted (protocol conformance) but
        # unused.
        del cursor
        return self._sync(account, resource_type)

    def _sync(self, account: ConnectorAccountContext, resource_type: str) -> SyncOutcome:
        if resource_type == "monitor":
            return self._sync_monitors(account)
        if resource_type == "service_definition":
            return self._sync_service_definitions(account)
        if resource_type == "dashboard":
            return self._sync_dashboards(account)
        return SyncOutcome(
            resource_type=resource_type, items_processed=0, status="succeeded", next_cursor=None
        )

    def _sync_monitors(self, account: ConnectorAccountContext) -> SyncOutcome:
        try:
            site, api_key, app_key = _parse_credential(account.credential)
        except _InvalidCredentialError as exc:
            raise RuntimeError(str(exc)) from exc
        headers = self._headers(api_key, app_key)
        items_processed = 0

        for page in range(_MAX_PAGES_PER_CALL):
            response = self._request_with_rate_limit_retry(
                "GET",
                f"https://{site}/api/v1/monitor",
                headers=headers,
                params={"page": page, "page_size": _PAGE_SIZE},
            )
            if response is None:
                return SyncOutcome(
                    resource_type="monitor",
                    items_processed=items_processed,
                    status="partial",
                    next_cursor=None,
                    error_summary="Datadog rate limit exceeded; sync paused, will resume next call",
                )
            if response.status_code != 200:
                raise RuntimeError(
                    f"Datadog monitor list failed with status {response.status_code}"
                )

            monitors = response.json() or []
            for monitor in monitors:
                _upsert_monitor(
                    workspace_id=account.workspace_id,
                    connector_account_id=account.connector_account_id,
                    provider=self.provider,
                    site=site,
                    monitor=monitor,
                )
                items_processed += 1

            if len(monitors) < _PAGE_SIZE:
                return SyncOutcome(
                    resource_type="monitor",
                    items_processed=items_processed,
                    status="succeeded",
                    next_cursor=None,
                )

        return SyncOutcome(
            resource_type="monitor",
            items_processed=items_processed,
            status="partial",
            next_cursor=None,
            error_summary=(
                f"Datadog monitor sync hit the {_MAX_PAGES_PER_CALL}-page per-call "
                "bound with more monitors remaining; sync paused, will resume next call"
            ),
        )

    def _sync_service_definitions(self, account: ConnectorAccountContext) -> SyncOutcome:
        try:
            site, api_key, app_key = _parse_credential(account.credential)
        except _InvalidCredentialError as exc:
            raise RuntimeError(str(exc)) from exc
        headers = self._headers(api_key, app_key)
        items_processed = 0

        for page_number in range(_MAX_PAGES_PER_CALL):
            response = self._request_with_rate_limit_retry(
                "GET",
                f"https://{site}/api/v2/services/definitions",
                headers=headers,
                params={"page[size]": _PAGE_SIZE, "page[number]": page_number},
            )
            if response is None:
                return SyncOutcome(
                    resource_type="service_definition",
                    items_processed=items_processed,
                    status="partial",
                    next_cursor=None,
                    error_summary="Datadog rate limit exceeded; sync paused, will resume next call",
                )
            if response.status_code != 200:
                raise RuntimeError(
                    f"Datadog service definition list failed with status {response.status_code}"
                )

            body = response.json()
            definitions = body.get("data") or []
            for definition in definitions:
                _upsert_service_definition(
                    workspace_id=account.workspace_id,
                    connector_account_id=account.connector_account_id,
                    provider=self.provider,
                    site=site,
                    definition=definition,
                )
                items_processed += 1

            if len(definitions) < _PAGE_SIZE:
                return SyncOutcome(
                    resource_type="service_definition",
                    items_processed=items_processed,
                    status="succeeded",
                    next_cursor=None,
                )

        return SyncOutcome(
            resource_type="service_definition",
            items_processed=items_processed,
            status="partial",
            next_cursor=None,
            error_summary=(
                f"Datadog service definition sync hit the {_MAX_PAGES_PER_CALL}-page "
                "per-call bound with more definitions remaining; sync paused, will "
                "resume next call"
            ),
        )

    def _sync_dashboards(self, account: ConnectorAccountContext) -> SyncOutcome:
        """A single unbounded call -- see module docstring's disclosure
        that Datadog's own `GET /api/v1/dashboard` documents no pagination
        parameters at all, unlike monitors/service definitions.
        """
        try:
            site, api_key, app_key = _parse_credential(account.credential)
        except _InvalidCredentialError as exc:
            raise RuntimeError(str(exc)) from exc
        headers = self._headers(api_key, app_key)

        response = self._request_with_rate_limit_retry(
            "GET", f"https://{site}/api/v1/dashboard", headers=headers
        )
        if response is None:
            return SyncOutcome(
                resource_type="dashboard",
                items_processed=0,
                status="partial",
                next_cursor=None,
                error_summary="Datadog rate limit exceeded; sync paused, will resume next call",
            )
        if response.status_code != 200:
            raise RuntimeError(f"Datadog dashboard list failed with status {response.status_code}")

        body = response.json()
        dashboards = body.get("dashboards") or []
        items_processed = 0
        for dashboard in dashboards:
            _upsert_dashboard(
                workspace_id=account.workspace_id,
                connector_account_id=account.connector_account_id,
                provider=self.provider,
                site=site,
                dashboard=dashboard,
            )
            items_processed += 1

        return SyncOutcome(
            resource_type="dashboard",
            items_processed=items_processed,
            status="succeeded",
            next_cursor=None,
        )

    def handle_webhook(
        self, account: ConnectorAccountContext, payload: bytes, headers: Mapping[str, str]
    ) -> SyncOutcome:
        # See module docstring -- Datadog webhook payloads are a
        # per-monitor customer-defined template, not a fixed schema this
        # adapter can parse without a workspace-specific contract this
        # task does not define. A documented no-op, not a stopgap.
        del account, payload, headers
        return SyncOutcome(
            resource_type="monitor", items_processed=0, status="succeeded", next_cursor=None
        )

    def refresh_permissions(self, account: ConnectorAccountContext) -> PermissionState:
        try:
            site, api_key, _app_key = _parse_credential(account.credential)
        except _InvalidCredentialError:
            return "permission_lost"
        try:
            response = self._client.get(
                f"https://{site}/api/v1/validate", headers=self._headers(api_key)
            )
        except httpx.HTTPError:
            return "active"
        if response.status_code == 403:
            return "permission_lost"
        return "active"

    def disconnect(self, account: ConnectorAccountContext) -> None:
        return None
