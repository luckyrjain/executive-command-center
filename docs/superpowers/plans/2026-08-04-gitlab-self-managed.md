# GitLab Self-Managed Instance Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a workspace connect a self-managed GitLab instance (e.g. `gitlab-ee.mpokket.org`) in addition to gitlab.com, per-user, over standard TLS, without a database migration.

**Architecture:** GitLab's credential becomes `host|token` (mirroring `jira_adapter.py`'s existing `site|email|api_token` pattern) instead of a bare token. `GitLabAdapter` — a single shared instance serving every workspace and host — derives its API/web base URLs from the parsed host per call instead of a fixed module constant, and rejects hosts that resolve to a private/loopback/link-local address at `authorize()` time. `external_account_id` becomes `host:user_id`, which is already returned to the API/UI, giving free discoverability and free collision-avoidance across hosts with no schema change. `GitLabAddNoteAdapter` (the `gitlab.add_note` write action) gets the identical host-awareness fix.

**Tech Stack:** Python 3.14, FastAPI, `httpx` (adapter HTTP client), pytest, PostgreSQL (integration tests), React/TypeScript (one frontend text change).

## Global Constraints

- No database migration — reuses `connector_accounts.credential`/`encrypted_credentials` (opaque) and `external_account_id` (already free text) exactly as they exist today.
- No `ConnectorAdapter` Protocol change — `authorize(credential: str)`'s signature stays identical across every adapter (GitHub, GitLab, Jira, Datadog, sandbox).
- `mypy --strict` and `ruff check`/`ruff format --check` must pass on every touched file (this repo's existing CI gate, `pyproject.toml`).
- Every new/changed behavior needs a real test — no test file in this repo skips PostgreSQL-backed coverage for adapter code that touches `repositories`/`connector_accounts`.
- Design reference: `docs/superpowers/specs/2026-08-04-gitlab-self-managed-design.md` (Draft, this plan implements it in full including its Security section's SSRF mitigation).

---

## Task 1: Credential parsing and SSRF host guard (pure, no HTTP)

**Files:**
- Modify: `backend/ecc/domains/engineering/gitlab_adapter.py` (add imports, two new module-level helpers, one new adapter method — no existing method changed yet)
- Test: `tests/test_engineering_gitlab_sync_postgres.py` (new pure-unit test functions, no database/HTTP needed)

**Interfaces:**
- Produces: `_parse_credential(credential: str) -> tuple[str, str]` (returns `(host, token)`, raises `_InvalidCredentialError`), `_is_private_address(ip_str: str) -> bool`, `_default_resolve_host(host: str) -> list[str]`, `GitLabAdapter.__init__`'s new `resolve_host: Callable[[str], list[str]]` constructor parameter, `GitLabAdapter._reject_private_host(self, host: str) -> None` (raises `AdapterAuthorizationError`).
- Consumes: `AdapterAuthorizationError` (already imported in `gitlab_adapter.py:112`).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_engineering_gitlab_sync_postgres.py`, in a new section after the existing imports (near the top, before `# --- unit-level: GitLabAdapter.authorize`):

```python
from ecc.domains.engineering.gitlab_adapter import (
    GitLabAdapter,
    _InvalidCredentialError,
    _is_private_address,
    _parse_credential,
)


# --- unit-level: credential parsing and SSRF host guard --------------------


def test_parse_credential_splits_host_and_token() -> None:
    assert _parse_credential("gitlab.com|glpat_test") == ("gitlab.com", "glpat_test")
    assert _parse_credential("gitlab-ee.mpokket.org|glpat-xyz") == (
        "gitlab-ee.mpokket.org",
        "glpat-xyz",
    )


def test_parse_credential_rejects_missing_pipe() -> None:
    with pytest.raises(_InvalidCredentialError, match="host\\|token"):
        _parse_credential("glpat_test")


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
    from ecc.domains.engineering.connectors import AdapterAuthorizationError

    adapter = GitLabAdapter(resolve_host=lambda host: ["169.254.169.254"])
    with pytest.raises(AdapterAuthorizationError, match="private/internal"):
        adapter._reject_private_host("gitlab-internal.example.com")


def test_reject_private_host_allows_public_address() -> None:
    adapter = GitLabAdapter(resolve_host=lambda host: ["140.82.112.3"])
    adapter._reject_private_host("gitlab.com")  # must not raise


def test_reject_private_host_raises_for_unresolvable_host() -> None:
    from ecc.domains.engineering.connectors import AdapterAuthorizationError

    def _fail(host: str) -> list[str]:
        raise AdapterAuthorizationError(f"GitLab host could not be resolved: nxdomain")

    adapter = GitLabAdapter(resolve_host=_fail)
    with pytest.raises(AdapterAuthorizationError, match="could not be resolved"):
        adapter._reject_private_host("does-not-exist.invalid")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_engineering_gitlab_sync_postgres.py -k "parse_credential or is_private_address or reject_private_host" -v`

Expected: FAIL/ERROR — `_InvalidCredentialError`, `_is_private_address`, `_parse_credential` don't exist yet in `gitlab_adapter.py`, and `GitLabAdapter.__init__` has no `resolve_host` parameter.

- [ ] **Step 3: Implement the helpers**

In `gitlab_adapter.py`, add near the top (after the `from __future__ import annotations` block, alongside the other stdlib imports):

```python
import ipaddress
import re
import socket
```

After the existing constants block (`_RATE_LIMIT_MAX_WAIT_SECONDS = 5.0`), add:

```python
class _InvalidCredentialError(Exception):
    pass


# GitLab self-managed hosts are arbitrary customer domains -- unlike Jira's
# `_JIRA_SITE_PATTERN` (locked to `*.atlassian.net`, a suffix Jira itself
# controls), this only enforces "looks like a bare hostname" (RFC 1035
# label rules, dot-separated), never a fixed suffix. It exists to reject a
# scheme/port/path/whitespace smuggled into the credential, not to
# allowlist specific domains -- `_reject_private_host` below is the actual
# SSRF defense (see design doc's Security section for why a hostname regex
# alone cannot be one for an arbitrary domain).
_GITLAB_HOST_PATTERN = re.compile(
    r"\A[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?"
    r"(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*\Z"
)


def _parse_credential(credential: str) -> tuple[str, str]:
    parts = credential.split("|", 1)
    if len(parts) != 2 or not all(parts):
        raise _InvalidCredentialError("GitLab credential must be in the form 'host|token'")
    host, token = parts
    if not _GITLAB_HOST_PATTERN.match(host):
        raise _InvalidCredentialError(
            "GitLab credential's host must be a bare hostname (e.g. 'gitlab.com' or "
            "'gitlab-ee.example.com') -- no scheme, port, path, or whitespace"
        )
    return host, token


def _is_private_address(ip_str: str) -> bool:
    ip = ipaddress.ip_address(ip_str)
    return ip.is_private or ip.is_loopback or ip.is_link_local


def _default_resolve_host(host: str) -> list[str]:
    try:
        addr_info = socket.getaddrinfo(host, None)
    except OSError as exc:
        raise AdapterAuthorizationError(f"GitLab host could not be resolved: {exc}") from exc
    return [info[4][0] for info in addr_info]
```

In `GitLabAdapter.__init__`, add the `resolve_host` parameter and store it, and drop `base_url` from `client_kwargs` (needed starting Task 2, safe to do now since no call site relies on it yet other than `__init__` itself):

```python
    def __init__(
        self,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_seconds: float = 10.0,
        sleep: Callable[[float], None] = time.sleep,
        resolve_host: Callable[[str], list[str]] = _default_resolve_host,
    ) -> None:
        client_kwargs: dict[str, Any] = {"timeout": timeout_seconds}
        if transport is not None:
            client_kwargs["transport"] = transport
        self._client = httpx.Client(**client_kwargs)
        self._sleep = sleep
        self._resolve_host = resolve_host
```

Add the new method right after `_headers`:

```python
    def _reject_private_host(self, host: str) -> None:
        """Connect-time SSRF guard, called once from `authorize()` only --
        see the design doc's Security section for why this cannot use
        Jira's fixed-suffix-allowlist approach (GitLab self-managed hosts
        are arbitrary customer domains), and for the disclosed DNS-
        rebinding limitation of a connect-time-only check.
        """
        for ip_str in self._resolve_host(host):
            if _is_private_address(ip_str):
                raise AdapterAuthorizationError(
                    f"GitLab host '{host}' resolves to a private/internal address; "
                    "refusing to connect"
                )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_engineering_gitlab_sync_postgres.py -k "parse_credential or is_private_address or reject_private_host" -v`

Expected: PASS, all 11 new tests.

- [ ] **Step 5: Type check and lint**

Run: `uv run mypy backend/ecc/domains/engineering/gitlab_adapter.py && uv run ruff check backend/ecc/domains/engineering/gitlab_adapter.py tests/test_engineering_gitlab_sync_postgres.py && uv run ruff format --check backend/ecc/domains/engineering/gitlab_adapter.py tests/test_engineering_gitlab_sync_postgres.py`

Expected: no errors. If `ruff format` reports unformatted files, run `uv run ruff format backend/ecc/domains/engineering/gitlab_adapter.py tests/test_engineering_gitlab_sync_postgres.py` and re-check.

- [ ] **Step 6: Commit**

```bash
git add backend/ecc/domains/engineering/gitlab_adapter.py tests/test_engineering_gitlab_sync_postgres.py
git commit -m "feat(gitlab): add host|token credential parsing and SSRF host guard"
```

---

## Task 2: Wire the parsed host through `GitLabAdapter`

**Files:**
- Modify: `backend/ecc/domains/engineering/gitlab_adapter.py` (`authorize`, `_sync_repositories`, `backfill`, `incremental_sync`, `handle_webhook`, `refresh_permissions`, `disconnect`, `_upsert_repository`, `_safe_source_url`; remove `GITLAB_API_BASE_URL`/`_GITLAB_WEB_BASE_URL`)
- Modify: `tests/test_engineering_gitlab_sync_postgres.py` (update every existing credential literal to the new `host|token` format; add a self-managed-host end-to-end test)

**Interfaces:**
- Consumes: `_parse_credential`, `_is_private_address`, `GitLabAdapter._reject_private_host` (Task 1).
- Produces: `GitLabAdapter.authorize`/`backfill`/`incremental_sync`/`handle_webhook`/`refresh_permissions`/`disconnect` all accept `host|token`-format credentials and operate against the parsed host instead of a fixed constant. `ConnectorAuthorization.external_account_id` is now `f"{host}:{user_id}"`.

- [ ] **Step 1: Update existing tests to the new credential format (still red against old code, but mechanically correct against the target)**

In `tests/test_engineering_gitlab_sync_postgres.py`, change every bare-token credential literal to `gitlab.com|<token>`:

```python
# test_gitlab_adapter_authorize_success
    authorization = adapter.authorize("gitlab.com|glpat_test")
    assert authorization.external_account_id == "gitlab.com:555"
    assert authorization.display_name == "octocat"
```

```python
# test_gitlab_adapter_authorize_rejects_missing_required_scope
        adapter.authorize("gitlab.com|token-missing-scopes")
```

```python
# test_gitlab_adapter_authorize_rejects_revoked_token
        adapter.authorize("gitlab.com|revoked-token")
```

```python
# test_gitlab_adapter_authorize_rejects_inactive_token
        adapter.authorize("gitlab.com|inactive-token")
```

```python
# test_gitlab_adapter_authorize_rejects_401
        adapter.authorize("gitlab.com|bad-token")
```

```python
# test_gitlab_adapter_authorize_rejects_non_200
        adapter.authorize("gitlab.com|token")
```

```python
# test_gitlab_adapter_authorize_rejects_network_error
        adapter.authorize("gitlab.com|token")
```

```python
# test_gitlab_adapter_authorize_rejects_second_call_non_200
        adapter.authorize("gitlab.com|token")
```

```python
# test_gitlab_adapter_authorize_rejects_second_call_network_error
        adapter.authorize("gitlab.com|token")
```

In `_account_context()`:

```python
def _account_context() -> ConnectorAccountContext:
    return ConnectorAccountContext(
        workspace_id=uuid4(),
        connector_account_id=uuid4(),
        external_account_id="gitlab.com:555",
        credential="gitlab.com|glpat_test",
    )
```

In `seeded_account_context()`, both the seeded row and the yielded context:

```python
                "encrypted": encrypt_credential("gitlab.com|glpat_test"),
```

```python
        yield ConnectorAccountContext(
            workspace_id=workspace_id,
            connector_account_id=account_id,
            external_account_id="gl-unit-test",
            credential="gitlab.com|glpat_test",
        )
```

(`external_account_id` here is a fixture-chosen label, not adapter output — leave `"gl-unit-test"` as-is, it is never compared against `authorize()`'s own host-prefixed format.)

- [ ] **Step 2: Add one new self-managed-host end-to-end test**

Append near the end of the "unit-level: GitLabAdapter.authorize" section:

```python
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
```

Add `Callable` to the existing `from collections.abc import Iterator` import line if not already imported at file scope (check the top of the test file first — if `Callable` isn't there, change it to `from collections.abc import Callable, Iterator`).

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_engineering_gitlab_sync_postgres.py -v 2>&1 | tail -60`

Expected: FAIL — every `authorize()` call now sends `"gitlab.com|..."` as the raw `PRIVATE-TOKEN` header (adapter code hasn't changed yet), and the new host-aware/SSRF tests fail outright.

- [ ] **Step 4: Rewrite the adapter methods**

In `gitlab_adapter.py`, remove the two module constants entirely:

```python
GITLAB_API_BASE_URL = "https://gitlab.com/api/v4"
_GITLAB_WEB_BASE_URL = "https://gitlab.com"
```

Change `_safe_source_url` to take the web base URL as a parameter:

```python
def _safe_source_url(raw_url: str | None, *, fallback: str, web_base_url: str) -> str:
    if raw_url and raw_url.startswith(f"{web_base_url}/"):
        return raw_url
    return fallback
```

Change `_upsert_repository` to accept and thread through `web_base_url`:

```python
def _upsert_repository(
    *,
    workspace_id: Any,
    connector_account_id: Any,
    provider: str,
    project: Mapping[str, Any],
    web_base_url: str,
) -> None:
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
                        f"{web_base_url}/{project.get('path_with_namespace') or project['id']}"
                    ),
                    web_base_url=web_base_url,
                ),
                "default_branch": project.get("default_branch"),
                "content_hash": _content_hash(project),
                "provider_updated_at": provider_updated_at,
                "now": now,
                "suggested_team_name": suggested_team_name,
            },
        )
        session.commit()
```

Rewrite `authorize`:

```python
    def authorize(self, credential: str) -> ConnectorAuthorization:
        try:
            host, token = _parse_credential(credential)
        except _InvalidCredentialError as exc:
            raise AdapterAuthorizationError(str(exc)) from exc
        self._reject_private_host(host)
        api_base_url = f"https://{host}/api/v4"
        headers = self._headers(token)
        try:
            token_response = self._client.get(
                f"{api_base_url}/personal_access_tokens/self", headers=headers
            )
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
            user_response = self._client.get(f"{api_base_url}/user", headers=headers)
        except httpx.HTTPError as exc:
            raise AdapterAuthorizationError(f"GitLab authorization request failed: {exc}") from exc
        if user_response.status_code != 200:
            raise AdapterAuthorizationError(
                f"GitLab authorization failed with status {user_response.status_code}"
            )
        user_body = user_response.json()

        return ConnectorAuthorization(
            external_account_id=f"{host}:{user_body['id']}",
            display_name=user_body.get("username") or f"gitlab-{user_body['id']}",
            granted_scopes=granted,
        )
```

Rewrite `_sync_repositories` (`backfill`/`incremental_sync` call this unchanged — no edit needed to those two methods themselves):

```python
    def _sync_repositories(
        self, account: ConnectorAccountContext, *, since_cursor: str | None
    ) -> SyncOutcome:
        try:
            host, token = _parse_credential(account.credential)
        except _InvalidCredentialError as exc:
            raise RuntimeError(str(exc)) from exc
        api_base_url = f"https://{host}/api/v4"
        web_base_url = f"https://{host}"
        headers = self._headers(token)
        items_processed = 0
        newest_updated_at = since_cursor
        page = 1
        stopped_early = False

        while page <= _MAX_PAGES_PER_CALL:
            response = self._request_with_rate_limit_retry(
                "GET",
                f"{api_base_url}/projects",
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
                    web_base_url=web_base_url,
                )
                items_processed += 1
                if newest_updated_at is None or (updated_at and updated_at > newest_updated_at):
                    newest_updated_at = updated_at

            if stopped_early or "next" not in response.links:
                break
            page += 1
        else:
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
```

Rewrite `handle_webhook`:

```python
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
        try:
            host, _token = _parse_credential(account.credential)
        except _InvalidCredentialError as exc:
            raise RuntimeError(str(exc)) from exc
        _upsert_repository(
            workspace_id=account.workspace_id,
            connector_account_id=account.connector_account_id,
            provider=self.provider,
            project=_with_push_event_activity_timestamp(project, body),
            web_base_url=f"https://{host}",
        )
        return SyncOutcome(
            resource_type="repository", items_processed=1, status="succeeded", next_cursor=None
        )
```

Rewrite `refresh_permissions`:

```python
    def refresh_permissions(self, account: ConnectorAccountContext) -> PermissionState:
        try:
            host, token = _parse_credential(account.credential)
        except _InvalidCredentialError:
            return "active"
        try:
            response = self._client.get(
                f"https://{host}/api/v4/personal_access_tokens/self", headers=self._headers(token)
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
```

Rewrite `disconnect`:

```python
    def disconnect(self, account: ConnectorAccountContext) -> None:
        try:
            host, token = _parse_credential(account.credential)
        except _InvalidCredentialError as exc:
            raise RuntimeError(str(exc)) from exc
        try:
            response = self._client.delete(
                f"https://{host}/api/v4/personal_access_tokens/self", headers=self._headers(token)
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"GitLab token revocation request failed: {exc}") from exc
        if response.status_code not in (204, 404):
            raise RuntimeError(f"GitLab token revocation failed with status {response.status_code}")
```

- [ ] **Step 5: Run the full test file to verify everything passes**

Run: `uv run pytest tests/test_engineering_gitlab_sync_postgres.py -v`

Expected: PASS, every test (existing + new).

- [ ] **Step 6: Run the full existing GitLab-adjacent integration surface**

Run: `uv run pytest tests/test_engineering_gitlab_sync_postgres.py tests/test_engineering_authz_postgres.py tests/test_engineering_connectors_postgres.py -v 2>&1 | tail -80`

(If `test_engineering_connectors_postgres.py` doesn't exist under that exact name, run `uv run pytest tests/ -k "engineering and connector" -v` instead and confirm nothing else references a GitLab credential in the old bare-token format — grep first: `grep -rn '"glpat' tests/ | grep -v test_engineering_gitlab_sync_postgres.py | grep -v test_engineering_write_actions_postgres.py` — if that finds any other file, add its credential-literal fixes here before moving on.)

Expected: PASS.

- [ ] **Step 7: Type check and lint**

Run: `uv run mypy backend/ecc/domains/engineering/gitlab_adapter.py && uv run ruff check backend/ecc/domains/engineering/gitlab_adapter.py tests/test_engineering_gitlab_sync_postgres.py && uv run ruff format --check backend/ecc/domains/engineering/gitlab_adapter.py tests/test_engineering_gitlab_sync_postgres.py`

Expected: no errors.

- [ ] **Step 8: Commit**

```bash
git add backend/ecc/domains/engineering/gitlab_adapter.py tests/test_engineering_gitlab_sync_postgres.py
git commit -m "feat(gitlab): support self-managed hosts across authorize/sync/refresh/disconnect"
```

---

## Task 3: `gitlab.add_note` write action host-awareness

**Files:**
- Modify: `backend/ecc/domains/engineering/write_actions.py` (`GitLabAddNoteAdapter.simulate`/`.execute`, remove `GITLAB_API_BASE_URL` import)
- Modify: `tests/test_engineering_write_actions_postgres.py` (update the ~9 GitLab credential literals to `host|token`; add one self-managed-host test)

**Interfaces:**
- Consumes: `_parse_credential` from `gitlab_adapter.py` (Task 1/2), imported as `_parse_gitlab_credential` — mirroring this file's own existing `_parse_jira_credential` alias (`write_actions.py:147`).

- [ ] **Step 1: Update existing test credential literals**

`_insert_connector_account(workspace_id: UUID, user_id: UUID, *, provider: str, credential: str, status: str = "active") -> UUID` (definition at `tests/test_engineering_write_actions_postgres.py:138`) takes `workspace_id`/`user_id` from the `write_actions_test_context: tuple[UUID, UUID]` fixture (`workspace_id, user_id = write_actions_test_context`, the pattern every existing test in this file already uses) and returns only `account_id`.

Every gitlab-provider call (lines ~650, 806, 830, 854, 878, 902, 956) changes its `credential` literal from `"glpat-x"` to `"gitlab.com|glpat-x"`:

```python
    account_id = _insert_connector_account(
        workspace_id, user_id, provider="gitlab", credential="gitlab.com|glpat-x"
    )
```

And the one distinct-value case (~line 756):

```python
        other_account_id = _insert_connector_account(
            other_workspace_id, other_user_id, provider="gitlab", credential="gitlab.com|glpat-other"
        )
```

Every existing GitLab write-action test's mock `handler` asserts against `request.url.raw_path`/`request.headers["PRIVATE-TOKEN"]` (see `test_gitlab_add_note_success_excludes_body_from_output`'s real handler, which asserts `request.url.raw_path == b"/api/v4/projects/acme%2Fwidgets/issues/7/notes"`) — these assertions are unaffected by this change, since `.raw_path`/`.headers` reflect only the path/header regardless of host. No handler assertion changes needed beyond the credential literals above. Grep first to catch any test this plan's own read of the file missed: `grep -n "url.host" tests/test_engineering_write_actions_postgres.py` — if any exists, update it to assert `"gitlab.com"` explicitly.

- [ ] **Step 2: Add a new self-managed-host test**

Add near `test_gitlab_add_note_success_excludes_body_from_output`, following its exact fixture pattern:

```python
def test_gitlab_add_note_execute_self_managed_host(
    write_actions_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = write_actions_test_context
    account_id = _insert_connector_account(
        workspace_id, user_id, provider="gitlab", credential="gitlab-ee.mpokket.org|glpat-private"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "gitlab-ee.mpokket.org"
        assert request.headers["PRIVATE-TOKEN"] == "glpat-private"
        return _json_response(201, {"id": 99})

    adapter = GitLabAddNoteAdapter(transport=httpx.MockTransport(handler))
    result = adapter.execute(
        GitLabAddNoteInput(
            workspace_id=workspace_id,
            actor_id=user_id,
            connector_account_id=account_id,
            project_path="acme/widgets",
            issue_iid=12,
            body="test note",
        )
    )
    assert isinstance(result, GitLabAddNoteOutput)
    assert result.note_external_id == "99"
    assert result.source_url.startswith("https://gitlab-ee.mpokket.org/acme/widgets/-/issues/12")


def test_gitlab_add_note_simulate_self_managed_host_preview_url(
    write_actions_test_context: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = write_actions_test_context
    account_id = _insert_connector_account(
        workspace_id, user_id, provider="gitlab", credential="gitlab-ee.mpokket.org|glpat-private"
    )
    adapter = GitLabAddNoteAdapter()
    result = adapter.simulate(
        GitLabAddNoteInput(
            workspace_id=workspace_id,
            actor_id=user_id,
            connector_account_id=account_id,
            project_path="acme/widgets",
            issue_iid=12,
            body="test note",
        )
    )
    assert isinstance(result, GitLabAddNoteOutput)
    assert result.source_url == "https://gitlab-ee.mpokket.org/acme/widgets/-/issues/12"
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_engineering_write_actions_postgres.py -k gitlab -v`

Expected: FAIL — `simulate()` doesn't hit the database yet so its preview URL is still hardcoded to `gitlab.com`, and `execute()` sends the raw `"gitlab-ee.mpokket.org|glpat-private"` string as the literal `PRIVATE-TOKEN` header today.

- [ ] **Step 4: Implement**

In `write_actions.py`, remove the import:

```python
from ecc.domains.engineering.gitlab_adapter import GITLAB_API_BASE_URL
```

Add, alongside the existing `_parse_jira_credential` alias import:

```python
from ecc.domains.engineering.gitlab_adapter import _parse_credential as _parse_gitlab_credential
```

Rewrite `GitLabAddNoteAdapter.__init__` (drop `base_url`):

```python
    def __init__(
        self, *, transport: httpx.BaseTransport | None = None, timeout_seconds: float = 10.0
    ) -> None:
        client_kwargs: dict[str, Any] = {"timeout": timeout_seconds}
        if transport is not None:
            client_kwargs["transport"] = transport
        self._client = httpx.Client(**client_kwargs)
```

Rewrite `simulate` to decrypt the credential for an accurate preview host — a real, disclosed cost (one extra DB round-trip on every `simulate()` call), accepted over a silently wrong preview URL for a self-managed account:

```python
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
```

Rewrite `execute`:

```python
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
```

Note this `execute()` rewrite does not change GitHub/Jira's own sibling adapters in this same file — only the `gitlab.add_note` section.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_engineering_write_actions_postgres.py -k gitlab -v`

Expected: PASS.

- [ ] **Step 6: Run the full write-actions test file**

Run: `uv run pytest tests/test_engineering_write_actions_postgres.py -v 2>&1 | tail -60`

Expected: PASS — confirms the `GITLAB_API_BASE_URL` import removal didn't break GitHub/Jira tests in the same file (it shouldn't; they never referenced it).

- [ ] **Step 7: Type check and lint**

Run: `uv run mypy backend/ecc/domains/engineering/write_actions.py && uv run ruff check backend/ecc/domains/engineering/write_actions.py tests/test_engineering_write_actions_postgres.py && uv run ruff format --check backend/ecc/domains/engineering/write_actions.py tests/test_engineering_write_actions_postgres.py`

Expected: no errors.

- [ ] **Step 8: Commit**

```bash
git add backend/ecc/domains/engineering/write_actions.py tests/test_engineering_write_actions_postgres.py
git commit -m "feat(gitlab): make gitlab.add_note write action host-aware"
```

---

## Task 4: Documentation and frontend hint text

**Files:**
- Modify: `backend/ecc/domains/engineering/connector_accounts.py` (`ConnectorCreateRequest` docstring)
- Modify: `docs/phases/phase-006/CONNECTOR-CONTRACT.md` (new status section)
- Modify: `frontend/src/features/engineering/ConnectorHealthPanel.tsx` (credential input hint text, provider-conditional)
- Modify: `frontend/src/features/engineering/ConnectorHealthPanel.test.tsx` (one new test for the hint text)

- [ ] **Step 1: Add a docstring to `ConnectorCreateRequest`**

In `connector_accounts.py`, change:

```python
class ConnectorCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=1, max_length=20)
    credential: str = Field(min_length=1, max_length=4096)
```

to:

```python
class ConnectorCreateRequest(BaseModel):
    """`credential`'s shape is provider-defined and opaque to this layer
    (`connector_accounts.encrypted_credentials` stores it as-is, encrypted).
    GitHub: a bare token. Jira: `site|email|api_token` (`site` a bare
    `{subdomain}.atlassian.net` hostname). GitLab: `host|token` (`host` a
    bare hostname -- `gitlab.com` or a self-managed instance, e.g.
    `gitlab-ee.example.com`; never a scheme/port/path). Datadog:
    `site|api_key|app_key` (`datadog_adapter.py`'s own `_parse_credential`).
    """

    model_config = ConfigDict(extra="forbid")
    provider: str = Field(min_length=1, max_length=20)
    credential: str = Field(min_length=1, max_length=4096)
```

- [ ] **Step 2: Add a status section to `CONNECTOR-CONTRACT.md`**

Append after the existing "## Task 4 status" (Jira) section, before "## Task 7 status":

```markdown
## GitLab self-managed instance support (post-Task-3 addition)

`gitlab_adapter.GitLabAdapter` no longer hardcodes `gitlab.com` as the only reachable host. Its credential format changed from a bare token to `host|token` (identical shape to Jira's own `site|email|api_token` -- `_parse_credential`, mirroring `jira_adapter.py`'s function of the same name), and `authorize()` rejects any host that resolves to a private/loopback/link-local address (including the `169.254.169.254` cloud-metadata address) before making any GitLab API call. `ConnectorAuthorization.external_account_id` is now `host:user_id` rather than a bare user ID -- already returned verbatim in `GET /engineering/connectors`, so which instance a connection points to is visible with no new response field, and the existing `(workspace_id, provider, external_account_id)` uniqueness constraint now naturally prevents two different hosts' identical numeric user IDs from colliding. `gitlab.add_note` (Task 7's write action) received the identical host-awareness fix. No schema migration, no `ConnectorAdapter` Protocol change. Disclosed limitation: the private-host check is connect-time only and does not defend against DNS rebinding after a connection is already authorized -- see `docs/superpowers/specs/2026-08-04-gitlab-self-managed-design.md`'s Security section for the full reasoning.
```

- [ ] **Step 3: Read the frontend panel's current provider-select wiring**

Before editing, re-read `frontend/src/features/engineering/ConnectorHealthPanel.tsx` around the `<select>`/`<input aria-label="Credential">` block (currently lines ~240-247) to confirm the exact current JSX, since this plan's earlier read of it may be stale by the time this task executes.

- [ ] **Step 4: Write the failing frontend test**

Add to `frontend/src/features/engineering/ConnectorHealthPanel.test.tsx` (match this file's existing test style/imports/render helper — read a neighboring test in the same file first):

```tsx
it('shows the host|token credential hint when GitLab is selected', async () => {
  render(<ConnectorHealthPanel />)
  await userEvent.selectOptions(screen.getByLabelText('Provider'), 'gitlab')
  expect(screen.getByLabelText('Credential')).toHaveAttribute(
    'placeholder',
    'host|token, e.g. gitlab.com|glpat-xxxx or gitlab-ee.example.com|glpat-xxxx',
  )
})

it('has no credential placeholder for a provider with a bare-token credential', async () => {
  render(<ConnectorHealthPanel />)
  await userEvent.selectOptions(screen.getByLabelText('Provider'), 'github')
  expect(screen.getByLabelText('Credential')).not.toHaveAttribute('placeholder')
})
```

- [ ] **Step 5: Run the frontend test to verify it fails**

Run: `pnpm --filter @ecc/frontend test -- ConnectorHealthPanel`

Expected: FAIL — no `placeholder` attribute exists on the credential input today.

- [ ] **Step 6: Implement**

In `ConnectorHealthPanel.tsx`, add a small lookup above the component (near `PROVIDERS`):

```tsx
const CREDENTIAL_PLACEHOLDER: Partial<Record<ConnectorProvider, string>> = {
  gitlab: 'host|token, e.g. gitlab.com|glpat-xxxx or gitlab-ee.example.com|glpat-xxxx',
}
```

Change the credential `<input>`:

```tsx
        <label>Credential
          <input
            aria-label="Credential"
            type="password"
            value={credential}
            onChange={(e) => setCredential(e.target.value)}
            autoComplete="off"
            placeholder={CREDENTIAL_PLACEHOLDER[provider]}
          />
        </label>
```

- [ ] **Step 7: Run the frontend test to verify it passes**

Run: `pnpm --filter @ecc/frontend test -- ConnectorHealthPanel`

Expected: PASS.

- [ ] **Step 8: Type check and lint (backend docstring-only file, and frontend)**

Run: `uv run mypy backend/ecc/domains/engineering/connector_accounts.py && uv run ruff check backend/ecc/domains/engineering/connector_accounts.py && uv run ruff format --check backend/ecc/domains/engineering/connector_accounts.py`

Run: `pnpm --filter @ecc/frontend lint` and `pnpm --filter @ecc/frontend typecheck` (use this repo's actual configured script names -- read `frontend/package.json`'s `scripts` block first if these names are wrong).

Expected: no errors.

- [ ] **Step 9: Commit**

```bash
git add backend/ecc/domains/engineering/connector_accounts.py docs/phases/phase-006/CONNECTOR-CONTRACT.md frontend/src/features/engineering/ConnectorHealthPanel.tsx frontend/src/features/engineering/ConnectorHealthPanel.test.tsx
git commit -m "docs(gitlab): document self-managed host support, add credential hint text"
```

---

## Task 5: Full verification pass

**Files:** none (verification only).

- [ ] **Step 1: Full backend test suite for touched modules**

Run: `uv run pytest tests/test_engineering_gitlab_sync_postgres.py tests/test_engineering_write_actions_postgres.py tests/test_engineering_connectors_postgres.py tests/test_engineering_authz_postgres.py -v 2>&1 | tail -100`

(Adjust the file list to whatever this repo's actual `tests/test_engineering_*.py` filenames are at execution time — `ls tests/ | grep engineering` first.)

Expected: PASS, no regressions.

- [ ] **Step 2: mypy strict across the whole backend**

Run: `uv run mypy backend/`

Expected: `Success: no issues found`.

- [ ] **Step 3: ruff across the whole repo**

Run: `uv run ruff check . && uv run ruff format --check .`

Expected: no errors.

- [ ] **Step 4: Frontend full test suite**

Run: `pnpm --filter @ecc/frontend test`

Expected: PASS.

- [ ] **Step 5: Confirm no lingering references to the removed constants**

Run: `grep -rn "GITLAB_API_BASE_URL\|_GITLAB_WEB_BASE_URL" --include="*.py" . | grep -v __pycache__`

Expected: no output (both fully removed).

- [ ] **Step 6: Update the design doc's status**

In `docs/superpowers/specs/2026-08-04-gitlab-self-managed-design.md`, change the frontmatter `status: Draft` to `status: Implemented` and bump `version: 0.2.0` to `version: 1.0.0`.

- [ ] **Step 7: Commit**

```bash
git add docs/superpowers/specs/2026-08-04-gitlab-self-managed-design.md
git commit -m "docs(gitlab): mark self-managed instance support design as implemented"
```
