"""Phase 6 Engineering Workspace: Datadog connector (`backend/ecc/domains/
engineering/datadog_adapter.py`) -- monitors, service definitions and
dashboards, per the user's explicit "all of the above" choice of resource
types to build together.

Covers, per `datadog_adapter.py`'s own module docstring:

1. `DatadogAdapter.authorize`: success (API-key validate + app-key monitor
   check), malformed credential, unknown site (closed-set rejection),
   API-key rejection (403), app-key rejection (403), network error.
2. `_sync_monitors`/`_sync_service_definitions`/`_sync_dashboards`: single
   page, pagination (monitors/service definitions only -- dashboards has
   no documented pagination, see the adapter's own disclosure),
   `suggested_team_name` sourcing (monitor `team:` tag, service
   definition's real `team` field, dashboard's defensive/absent `tags`),
   rate-limit handling (`X-RateLimit-Reset`, not `Retry-After`), and the
   `_MAX_PAGES_PER_CALL` bound reporting `partial`.
3. `refresh_permissions`/`disconnect` (documented no-op)/`handle_webhook`
   (documented no-op) contract coverage.
4. End-to-end through the real `/sync` endpoint (monkeypatched registry)
   for all three resource types, and the three new read-only list
   endpoints (`GET /engineering/monitors|service-definitions|dashboards`).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from json import dumps
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
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
from ecc.domains.engineering.datadog_adapter import DatadogAdapter
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_SITE = "api.datadoghq.com"
_CREDENTIAL = f"{_SITE}|dd-api-key|dd-app-key"


def _json_response(
    body: Any, *, status_code: int = 200, headers: dict[str, str] | None = None
) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        headers={"content-type": "application/json", **(headers or {})},
        content=dumps(body),
    )


def _monitor(monitor_id: int, *, name: str, tags: list[str] | None = None) -> dict[str, Any]:
    return {
        "id": monitor_id,
        "name": name,
        "type": "metric alert",
        "query": "avg(last_5m):avg:system.load.1{*} > 1",
        "overall_state": "OK",
        "tags": tags if tags is not None else [],
        "modified": "2024-01-01T00:00:00Z",
    }


def _service_definition(
    dd_service: str, *, team: str | None = "team-shopping-cart"
) -> dict[str, Any]:
    return {
        "type": "service_definitions",
        "attributes": {
            "schema": {
                "dd-service": dd_service,
                "team": team,
                "tier": "1",
                "description": "Shopping cart service",
            }
        },
    }


def _dashboard(dashboard_id: str, *, title: str) -> dict[str, Any]:
    return {
        "id": dashboard_id,
        "title": title,
        "description": "A dashboard",
        "url": f"/dashboard/{dashboard_id}/widgets",
        "modified_at": "2024-01-01T00:00:00Z",
    }


# --- unit-level: DatadogAdapter.authorize ------------------------------------


def test_datadog_adapter_authorize_success() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["DD-API-KEY"] == "dd-api-key"
        if request.url.path == "/api/v1/validate":
            return _json_response({"valid": True})
        assert request.url.path == "/api/v1/monitor"
        assert request.headers["DD-APPLICATION-KEY"] == "dd-app-key"
        return _json_response([])

    adapter = DatadogAdapter(transport=httpx.MockTransport(handler))
    authorization = adapter.authorize(_CREDENTIAL)
    assert authorization.external_account_id == sha256(b"dd-api-key").hexdigest()[:32]
    assert authorization.display_name == "Datadog api.datadoghq.com (...-key)"
    assert authorization.granted_scopes == frozenset()


def test_datadog_adapter_authorize_rejects_malformed_credential() -> None:
    adapter = DatadogAdapter(transport=httpx.MockTransport(lambda r: _json_response({})))
    with pytest.raises(AdapterAuthorizationError, match="site\\|api_key\\|app_key"):
        adapter.authorize("not-the-right-shape")


def test_datadog_adapter_authorize_rejects_empty_credential_segment() -> None:
    adapter = DatadogAdapter(transport=httpx.MockTransport(lambda r: _json_response({})))
    with pytest.raises(AdapterAuthorizationError, match="site\\|api_key\\|app_key"):
        adapter.authorize("|dd-api-key|dd-app-key")


def test_datadog_adapter_authorize_rejects_unknown_site() -> None:
    """`site` becomes the host of every request URL this adapter builds --
    validated against Datadog's own closed set of regional API hosts
    (not a regex, since the set is small and Datadog-controlled), closing
    an otherwise-real SSRF risk from an operator-supplied arbitrary host.
    """
    adapter = DatadogAdapter(transport=httpx.MockTransport(lambda r: _json_response({})))
    with pytest.raises(AdapterAuthorizationError, match="datadoghq.com"):
        adapter.authorize("internal.example.test|dd-api-key|dd-app-key")


def test_backfill_rejects_unknown_site_too() -> None:
    adapter = DatadogAdapter(transport=httpx.MockTransport(lambda r: _json_response({})))
    context = ConnectorAccountContext(
        workspace_id=uuid4(),
        connector_account_id=uuid4(),
        external_account_id="acc-1",
        credential="internal.example.test|dd-api-key|dd-app-key",
    )
    with pytest.raises(RuntimeError, match="datadoghq.com"):
        adapter.backfill(context, "monitor")


def test_datadog_adapter_authorize_rejects_403_api_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"errors": ["Forbidden"]}, status_code=403)

    adapter = DatadogAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(AdapterAuthorizationError, match="API key"):
        adapter.authorize(_CREDENTIAL)


def test_datadog_adapter_authorize_rejects_403_app_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/validate":
            return _json_response({"valid": True})
        return _json_response({"errors": ["Forbidden"]}, status_code=403)

    adapter = DatadogAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(AdapterAuthorizationError, match="application key"):
        adapter.authorize(_CREDENTIAL)


def test_datadog_adapter_authorize_rejects_non_200_app_key_status() -> None:
    """Mirrors the API-key check's own equivalent branch -- a non-403,
    non-200 status (e.g. a transient 500) on the app-key check must also
    reject, not just the specific 403 case above.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/validate":
            return _json_response({"valid": True})
        return _json_response({"errors": ["Server error"]}, status_code=500)

    adapter = DatadogAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(AdapterAuthorizationError, match="500"):
        adapter.authorize(_CREDENTIAL)


def test_datadog_adapter_authorize_rejects_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    adapter = DatadogAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(AdapterAuthorizationError):
        adapter.authorize(_CREDENTIAL)


def test_datadog_adapter_authorize_rejects_network_error_on_second_call() -> None:
    """`authorize` makes two sequential requests (api_key then app_key
    validation) -- the test above only ever exercises the FIRST raising,
    since a mock transport that raises unconditionally never reaches the
    second URL. GitLab's own structurally identical two-call `authorize`
    had this exact gap once (its own docstring says so) before it was
    added; Datadog never got the equivalent test.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/validate":
            return _json_response({"valid": True})
        raise httpx.ConnectError("connection refused")

    adapter = DatadogAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(AdapterAuthorizationError):
        adapter.authorize(_CREDENTIAL)


# --- unit-level: resource sync (real database rows required) ----------------


def _account_context() -> ConnectorAccountContext:
    return ConnectorAccountContext(
        workspace_id=uuid4(),
        connector_account_id=uuid4(),
        external_account_id="acc-1",
        credential=_CREDENTIAL,
    )


@pytest.fixture
def seeded_account_context() -> Iterator[ConnectorAccountContext]:
    workspace_id = uuid4()
    user_id = uuid4()
    account_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Datadog Adapter Unit Test', 'UTC', :now)"
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
        connection.execute(
            text(
                """
                INSERT INTO connector_accounts (
                    id, workspace_id, provider, external_account_id, display_name,
                    granted_scopes, encrypted_credentials, status, version,
                    created_by, updated_by, created_at, updated_at
                ) VALUES (
                    :id, :workspace_id, 'datadog', 'datadog-unit-test', 'Datadog unit test',
                    ARRAY[]::text[], :encrypted, 'active', 1,
                    :actor_id, :actor_id, :now, :now
                )
                """
            ),
            {
                "id": account_id,
                "workspace_id": workspace_id,
                "encrypted": encrypt_credential(_CREDENTIAL),
                "actor_id": user_id,
                "now": now,
            },
        )
    try:
        yield ConnectorAccountContext(
            workspace_id=workspace_id,
            connector_account_id=account_id,
            external_account_id="datadog-unit-test",
            credential=_CREDENTIAL,
        )
    finally:
        with engine.begin() as connection:
            for table in (
                "datadog_monitors",
                "datadog_service_definitions",
                "datadog_dashboards",
                "connector_accounts",
                "users",
            ):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                    {"workspace_id": workspace_id},
                )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": workspace_id},
            )


def test_backfill_monitors_single_page(seeded_account_context: ConnectorAccountContext) -> None:
    monitors = [
        _monitor(1, name="High load", tags=["team:platform", "env:prod"]),
        _monitor(2, name="Disk usage", tags=[]),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/monitor"
        page = int(request.url.params["page"])
        return _json_response(monitors if page == 0 else [])

    adapter = DatadogAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(seeded_account_context, "monitor")
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 2

    with engine.begin() as connection:
        rows = {
            row.name: row.suggested_team_name
            for row in connection.execute(
                text(
                    "SELECT name, suggested_team_name FROM datadog_monitors "
                    "WHERE workspace_id = :workspace_id"
                ),
                {"workspace_id": seeded_account_context.workspace_id},
            )
        }
    assert rows == {"High load": "platform", "Disk usage": None}


def test_backfill_monitors_paginates(seeded_account_context: ConnectorAccountContext) -> None:
    from ecc.domains.engineering import datadog_adapter as datadog_adapter_module

    page0 = [_monitor(i, name=f"Monitor {i}") for i in range(datadog_adapter_module._PAGE_SIZE)]
    page1 = [_monitor(1000, name="Last monitor")]

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        if page == 0:
            return _json_response(page0)
        if page == 1:
            return _json_response(page1)
        raise AssertionError(f"unexpected page {page}")

    adapter = DatadogAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(seeded_account_context, "monitor")
    assert outcome.status == "succeeded"
    assert outcome.items_processed == datadog_adapter_module._PAGE_SIZE + 1


def test_monitor_page_cap_reports_partial_with_more_pages_remaining(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    from ecc.domains.engineering import datadog_adapter as datadog_adapter_module

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return _json_response(
            [
                _monitor(page * datadog_adapter_module._PAGE_SIZE + i, name=f"M{page}-{i}")
                for i in range(datadog_adapter_module._PAGE_SIZE)
            ]
        )

    adapter = DatadogAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(seeded_account_context, "monitor")
    assert outcome.status == "partial"
    assert outcome.items_processed == (
        datadog_adapter_module._MAX_PAGES_PER_CALL * datadog_adapter_module._PAGE_SIZE
    )
    assert outcome.error_summary is not None
    assert "page" in outcome.error_summary.lower()


def test_incremental_sync_behaves_like_backfill_no_delta_filter(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    """See the adapter's own module docstring -- no Datadog list endpoint
    used here documents an incremental delta filter, so `incremental_sync`
    is identical to `backfill` and always returns `next_cursor=None`.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return _json_response([_monitor(1, name="Only monitor")] if page == 0 else [])

    adapter = DatadogAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.incremental_sync(seeded_account_context, "monitor", cursor="irrelevant")
    assert outcome.items_processed == 1
    assert outcome.next_cursor is None


def test_backfill_then_resync_with_changed_query_updates_content_hash(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    def first_handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return _json_response([_monitor(1, name="Watched")] if page == 0 else [])

    adapter = DatadogAdapter(transport=httpx.MockTransport(first_handler))
    adapter.backfill(seeded_account_context, "monitor")

    with engine.begin() as connection:
        before = connection.execute(
            text(
                "SELECT content_hash, observed_at FROM datadog_monitors "
                "WHERE workspace_id = :workspace_id"
            ),
            {"workspace_id": seeded_account_context.workspace_id},
        ).one()

    changed = _monitor(1, name="Watched")
    changed["overall_state"] = "Alert"

    def second_handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return _json_response([changed] if page == 0 else [])

    adapter2 = DatadogAdapter(transport=httpx.MockTransport(second_handler))
    adapter2.backfill(seeded_account_context, "monitor")

    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT content_hash, overall_state, observed_at FROM datadog_monitors "
                "WHERE workspace_id = :workspace_id"
            ),
            {"workspace_id": seeded_account_context.workspace_id},
        ).all()
    assert len(rows) == 1
    assert rows[0].overall_state == "Alert"
    assert rows[0].content_hash != before.content_hash
    # Regression lock: `_upsert_monitor`'s own `ON CONFLICT ... DO UPDATE`
    # once omitted `observed_at`, leaving it frozen at first-sync time
    # forever despite every other field correctly refreshing on resync.
    assert rows[0].observed_at > before.observed_at


def test_backfill_service_definitions_single_page(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    definitions = [
        _service_definition("shopping-cart", team="team-shopping-cart"),
        _service_definition("checkout", team=None),
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/services/definitions"
        page_number = int(request.url.params["page[number]"])
        return _json_response({"data": definitions if page_number == 0 else []})

    adapter = DatadogAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(seeded_account_context, "service_definition")
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 2

    with engine.begin() as connection:
        rows = {
            row.name: (row.team, row.suggested_team_name)
            for row in connection.execute(
                text(
                    "SELECT name, team, suggested_team_name FROM datadog_service_definitions "
                    "WHERE workspace_id = :workspace_id"
                ),
                {"workspace_id": seeded_account_context.workspace_id},
            )
        }
    assert rows == {
        "shopping-cart": ("team-shopping-cart", "team-shopping-cart"),
        "checkout": (None, None),
    }


def test_backfill_skips_service_definition_missing_both_identifying_fields(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    """A malformed definition with neither `schema.dd-service` nor a
    top-level `id` has no real, stable identifying key at all -- upserting
    it anyway would fall back to a literal `external_id="None"`, and a
    second, unrelated malformed definition in the same page would then
    silently overwrite the first via `ON CONFLICT ... DO UPDATE`. Must be
    skipped, not written under a shared placeholder key.
    """
    malformed = {"type": "service_definitions", "attributes": {"schema": {"team": "team-a"}}}
    real = _service_definition("checkout", team="team-b")

    def handler(request: httpx.Request) -> httpx.Response:
        page_number = int(request.url.params["page[number]"])
        return _json_response({"data": [malformed, real] if page_number == 0 else []})

    adapter = DatadogAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(seeded_account_context, "service_definition")
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1

    with engine.begin() as connection:
        rows = list(
            connection.execute(
                text(
                    "SELECT name FROM datadog_service_definitions "
                    "WHERE workspace_id = :workspace_id"
                ),
                {"workspace_id": seeded_account_context.workspace_id},
            )
        )
    assert [row.name for row in rows] == ["checkout"]


def test_backfill_service_definitions_paginates(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    from ecc.domains.engineering import datadog_adapter as datadog_adapter_module

    page0 = [_service_definition(f"service-{i}") for i in range(datadog_adapter_module._PAGE_SIZE)]
    page1 = [_service_definition("last-service")]

    def handler(request: httpx.Request) -> httpx.Response:
        page_number = int(request.url.params["page[number]"])
        if page_number == 0:
            return _json_response({"data": page0})
        if page_number == 1:
            return _json_response({"data": page1})
        raise AssertionError(f"unexpected page[number] {page_number}")

    adapter = DatadogAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(seeded_account_context, "service_definition")
    assert outcome.status == "succeeded"
    assert outcome.items_processed == datadog_adapter_module._PAGE_SIZE + 1


def test_service_definition_page_cap_reports_partial_with_more_pages_remaining(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    from ecc.domains.engineering import datadog_adapter as datadog_adapter_module

    def handler(request: httpx.Request) -> httpx.Response:
        page_number = int(request.url.params["page[number]"])
        return _json_response(
            {
                "data": [
                    _service_definition(f"service-{page_number}-{i}")
                    for i in range(datadog_adapter_module._PAGE_SIZE)
                ]
            }
        )

    adapter = DatadogAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(seeded_account_context, "service_definition")
    assert outcome.status == "partial"
    assert outcome.items_processed == (
        datadog_adapter_module._MAX_PAGES_PER_CALL * datadog_adapter_module._PAGE_SIZE
    )
    assert outcome.error_summary is not None
    assert "page" in outcome.error_summary.lower()


def test_backfill_then_resync_service_definition_with_changed_team_updates_content_hash(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    """Mirrors the monitor-side resync regression lock above -- proves
    `_upsert_service_definition`'s own `ON CONFLICT ... DO UPDATE` clause
    genuinely updates an existing row (including `observed_at`) rather
    than silently no-op'ing, a code path the single-page backfill test
    above never exercises since it only ever inserts.
    """

    def first_handler(request: httpx.Request) -> httpx.Response:
        page_number = int(request.url.params["page[number]"])
        definition = [_service_definition("shopping-cart", team="team-a")]
        return _json_response({"data": definition if page_number == 0 else []})

    adapter = DatadogAdapter(transport=httpx.MockTransport(first_handler))
    adapter.backfill(seeded_account_context, "service_definition")

    with engine.begin() as connection:
        before = connection.execute(
            text(
                "SELECT content_hash, observed_at FROM datadog_service_definitions "
                "WHERE workspace_id = :workspace_id"
            ),
            {"workspace_id": seeded_account_context.workspace_id},
        ).one()

    def second_handler(request: httpx.Request) -> httpx.Response:
        page_number = int(request.url.params["page[number]"])
        definition = [_service_definition("shopping-cart", team="team-b")]
        return _json_response({"data": definition if page_number == 0 else []})

    adapter2 = DatadogAdapter(transport=httpx.MockTransport(second_handler))
    adapter2.backfill(seeded_account_context, "service_definition")

    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT content_hash, team, observed_at FROM datadog_service_definitions "
                "WHERE workspace_id = :workspace_id"
            ),
            {"workspace_id": seeded_account_context.workspace_id},
        ).all()
    assert len(rows) == 1
    assert rows[0].team == "team-b"
    assert rows[0].content_hash != before.content_hash
    assert rows[0].observed_at > before.observed_at


def test_backfill_then_resync_dashboard_with_changed_title_updates_content_hash(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    """Mirrors the monitor/service-definition resync regression locks
    above for `_upsert_dashboard`'s own `ON CONFLICT ... DO UPDATE` clause.
    """

    def first_handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"dashboards": [_dashboard("abc-123", title="Original")]})

    adapter = DatadogAdapter(transport=httpx.MockTransport(first_handler))
    adapter.backfill(seeded_account_context, "dashboard")

    with engine.begin() as connection:
        before = connection.execute(
            text(
                "SELECT content_hash, observed_at FROM datadog_dashboards "
                "WHERE workspace_id = :workspace_id"
            ),
            {"workspace_id": seeded_account_context.workspace_id},
        ).one()

    def second_handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"dashboards": [_dashboard("abc-123", title="Renamed")]})

    adapter2 = DatadogAdapter(transport=httpx.MockTransport(second_handler))
    adapter2.backfill(seeded_account_context, "dashboard")

    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT content_hash, title, observed_at FROM datadog_dashboards "
                "WHERE workspace_id = :workspace_id"
            ),
            {"workspace_id": seeded_account_context.workspace_id},
        ).all()
    assert len(rows) == 1
    assert rows[0].title == "Renamed"
    assert rows[0].content_hash != before.content_hash
    assert rows[0].observed_at > before.observed_at


def test_dashboard_sync_raises_on_generic_failure_status(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    """The generic-failure-status test elsewhere in this file only
    exercises `monitor`'s own status check -- `_sync_dashboards` parses a
    different envelope after its own independent `!= 200` check, so this
    proves that branch specifically raises rather than mis-parsing.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"errors": ["Server error"]}, status_code=500)

    adapter = DatadogAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="500"):
        adapter.backfill(seeded_account_context, "dashboard")


def test_backfill_dashboard_rejects_non_relative_url_from_provider(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    """A dashboard's `url` field is normally a relative path -- everything
    else this adapter syncs builds `source_url` entirely server-side from
    `ui_host`, but dashboards trust the provider's own `url` for the path
    segment. A malformed or malicious value that doesn't start with `/`
    (e.g. `javascript:alert(1)`, or an arbitrary external host) must never
    be stored verbatim; it should fall back to the safe, server-constructed
    default instead.
    """
    malicious = {
        "id": "abc-123",
        "title": "Overview",
        "description": "A dashboard",
        "url": "javascript:alert(1)",
        "modified_at": "2024-01-01T00:00:00Z",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"dashboards": [malicious]})

    adapter = DatadogAdapter(transport=httpx.MockTransport(handler))
    adapter.backfill(seeded_account_context, "dashboard")

    with engine.begin() as connection:
        source_url = connection.execute(
            text("SELECT source_url FROM datadog_dashboards WHERE workspace_id = :workspace_id"),
            {"workspace_id": seeded_account_context.workspace_id},
        ).scalar_one()
    assert source_url == "https://app.datadoghq.com/dashboard/abc-123"


def test_backfill_dashboards_single_unbounded_call(
    seeded_account_context: ConnectorAccountContext,
) -> None:
    dashboards = [_dashboard("abc-123", title="Overview"), _dashboard("def-456", title="Errors")]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/dashboard"
        return _json_response({"dashboards": dashboards})

    adapter = DatadogAdapter(transport=httpx.MockTransport(handler))
    outcome = adapter.backfill(seeded_account_context, "dashboard")
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 2

    with engine.begin() as connection:
        rows = {
            row.title: row.suggested_team_name
            for row in connection.execute(
                text(
                    "SELECT title, suggested_team_name FROM datadog_dashboards "
                    "WHERE workspace_id = :workspace_id"
                ),
                {"workspace_id": seeded_account_context.workspace_id},
            )
        }
    # No `tags` field in these fixtures -- disclosed as likely absent from
    # the real list endpoint too (module docstring).
    assert rows == {"Overview": None, "Errors": None}


def test_non_datadog_resource_type_is_a_zero_item_no_op() -> None:
    adapter = DatadogAdapter(transport=httpx.MockTransport(lambda r: _json_response({})))
    outcome = adapter.backfill(_account_context(), "repository")
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
                {"errors": ["rate limited"]}, status_code=429, headers={"X-RateLimit-Reset": "1"}
            )
        page = int(request.url.params["page"])
        return _json_response([_monitor(1, name="Recovered")] if page == 0 else [])

    adapter = DatadogAdapter(
        transport=httpx.MockTransport(handler), sleep=lambda seconds: sleeps.append(seconds)
    )
    outcome = adapter.backfill(seeded_account_context, "monitor")
    assert outcome.status == "succeeded"
    assert outcome.items_processed == 1
    assert sleeps == [1.0]


def test_rate_limit_gives_up_beyond_bounded_wait() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {"errors": ["rate limited"]}, status_code=429, headers={"X-RateLimit-Reset": "3600"}
        )

    adapter = DatadogAdapter(transport=httpx.MockTransport(handler), sleep=lambda seconds: None)
    outcome = adapter.backfill(_account_context(), "monitor")
    assert outcome.status == "partial"
    assert outcome.items_processed == 0
    assert outcome.error_summary is not None
    assert "rate limit" in outcome.error_summary.lower()


def test_rate_limit_gives_up_immediately_when_reset_header_absent() -> None:
    """`_rate_limit_wait_seconds` returns `None` when `X-RateLimit-Reset`
    is absent entirely -- a distinct code path from "present but too
    large," untested here despite GitLab/Jira having the equivalent test
    for their own identical `Retry-After`-absent case.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"errors": ["rate limited"]}, status_code=429)

    adapter = DatadogAdapter(transport=httpx.MockTransport(handler), sleep=lambda seconds: None)
    outcome = adapter.backfill(_account_context(), "monitor")
    assert outcome.status == "partial"
    assert outcome.items_processed == 0


def test_rate_limit_gives_up_immediately_when_reset_header_malformed() -> None:
    """`_rate_limit_wait_seconds`'s `except ValueError: return None` branch
    for a non-numeric `X-RateLimit-Reset` value -- untested here despite
    GitLab/Jira having the equivalent test.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {"errors": ["rate limited"]}, status_code=429, headers={"X-RateLimit-Reset": "soon"}
        )

    adapter = DatadogAdapter(transport=httpx.MockTransport(handler), sleep=lambda seconds: None)
    outcome = adapter.backfill(_account_context(), "monitor")
    assert outcome.status == "partial"
    assert outcome.items_processed == 0


def test_rate_limit_still_limited_after_retry_reports_partial_not_failure() -> None:
    calls = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return _json_response(
            {"errors": ["rate limited"]}, status_code=429, headers={"X-RateLimit-Reset": "0"}
        )

    adapter = DatadogAdapter(transport=httpx.MockTransport(handler), sleep=lambda seconds: None)
    outcome = adapter.backfill(_account_context(), "monitor")
    assert outcome.status == "partial"
    assert outcome.items_processed == 0
    assert calls["count"] == 2


def test_sync_raises_on_generic_failure_status() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"errors": ["Server error"]}, status_code=500)

    adapter = DatadogAdapter(transport=httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="500"):
        adapter.backfill(_account_context(), "monitor")


def test_refresh_permissions() -> None:
    def unauthorized(request: httpx.Request) -> httpx.Response:
        return _json_response({"errors": ["Forbidden"]}, status_code=403)

    def authorized(request: httpx.Request) -> httpx.Response:
        return _json_response({"valid": True})

    lost_adapter = DatadogAdapter(transport=httpx.MockTransport(unauthorized))
    assert lost_adapter.refresh_permissions(_account_context()) == "permission_lost"

    active_adapter = DatadogAdapter(transport=httpx.MockTransport(authorized))
    assert active_adapter.refresh_permissions(_account_context()) == "active"


def test_refresh_permissions_fails_open_on_network_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    adapter = DatadogAdapter(transport=httpx.MockTransport(handler))
    assert adapter.refresh_permissions(_account_context()) == "active"


def test_disconnect_is_a_no_op() -> None:
    adapter = DatadogAdapter()
    assert adapter.disconnect(_account_context()) is None


def test_handle_webhook_is_a_documented_no_op() -> None:
    adapter = DatadogAdapter()
    outcome = adapter.handle_webhook(_account_context(), b'{"anything": true}', {})
    assert outcome.items_processed == 0
    assert outcome.status == "succeeded"


# --- integration: real /sync endpoint, mocked Datadog transport ------------


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
                "VALUES (:id, 'Datadog Sync Test', 'UTC', :now)"
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
            "datadog_monitors",
            "datadog_service_definitions",
            "datadog_dashboards",
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


def _insert_datadog_connector_account(workspace_id: UUID, user_id: UUID) -> UUID:
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
                    :id, :workspace_id, 'datadog', :external_account_id, 'Datadog fixture',
                    ARRAY[]::text[], :encrypted, 'active', 1,
                    :actor_id, :actor_id, :now, :now
                )
                """
            ),
            {
                "id": account_id,
                "workspace_id": workspace_id,
                "external_account_id": f"datadog-fixture-account-{account_id}",
                "encrypted": encrypt_credential(_CREDENTIAL),
                "actor_id": user_id,
                "now": now,
            },
        )
    return account_id


@pytest.mark.parametrize(
    ("resource_type", "handler_factory", "table", "name_column"),
    [
        (
            "monitor",
            lambda: (
                lambda request: _json_response(
                    [_monitor(1, name="Only monitor")]
                    if int(request.url.params["page"]) == 0
                    else []
                )
            ),
            "datadog_monitors",
            "name",
        ),
        (
            "service_definition",
            lambda: (
                lambda request: _json_response(
                    {"data": [_service_definition("only-service")]}
                    if int(request.url.params["page[number]"]) == 0
                    else {"data": []}
                )
            ),
            "datadog_service_definitions",
            "name",
        ),
        (
            "dashboard",
            lambda: (
                lambda request: _json_response(
                    {"dashboards": [_dashboard("abc-123", title="Only dashboard")]}
                )
            ),
            "datadog_dashboards",
            "title",
        ),
    ],
)
def test_sync_endpoint_backfills_each_resource_type(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
    resource_type: str,
    handler_factory: Any,
    table: str,
    name_column: str,
) -> None:
    client, workspace_id, user_id, token = engineering_test_context
    account_id = _insert_datadog_connector_account(workspace_id, user_id)

    registry = ConnectorRegistry()
    registry.register(DatadogAdapter(transport=httpx.MockTransport(handler_factory())))
    monkeypatch.setattr(connector_accounts_module, "connector_registry", registry)

    response = client.post(
        f"/api/v1/engineering/connectors/{account_id}/sync",
        json={"run_type": "backfill", "resource_type": resource_type},
        headers=_headers(token, key=str(uuid4())),
    )
    assert response.status_code == 201, response.text
    assert response.json()["items_processed"] == 1

    with engine.begin() as connection:
        count = connection.execute(
            text(f"SELECT COUNT(*) FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
            {"workspace_id": workspace_id},
        ).scalar_one()
    assert count == 1


def test_sync_reports_partial_on_rate_limit_and_records_it(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, workspace_id, user_id, token = engineering_test_context
    account_id = _insert_datadog_connector_account(workspace_id, user_id)

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {"errors": ["rate limited"]}, status_code=429, headers={"X-RateLimit-Reset": "3600"}
        )

    registry = ConnectorRegistry()
    registry.register(
        DatadogAdapter(transport=httpx.MockTransport(handler), sleep=lambda seconds: None)
    )
    monkeypatch.setattr(connector_accounts_module, "connector_registry", registry)

    response = client.post(
        f"/api/v1/engineering/connectors/{account_id}/sync",
        json={"run_type": "backfill", "resource_type": "monitor"},
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
    assert account_status == "active"


# --- integration: the three new read-only list endpoints -------------------


def test_list_monitors_endpoint_filters_by_connector_account(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, workspace_id, user_id, token = engineering_test_context
    account_id = _insert_datadog_connector_account(workspace_id, user_id)
    other_account_id = _insert_datadog_connector_account(workspace_id, user_id)

    def handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            [_monitor(1, name="Watched")] if int(request.url.params["page"]) == 0 else []
        )

    registry = ConnectorRegistry()
    registry.register(DatadogAdapter(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(connector_accounts_module, "connector_registry", registry)

    client.post(
        f"/api/v1/engineering/connectors/{account_id}/sync",
        json={"run_type": "backfill", "resource_type": "monitor"},
        headers=_headers(token, key=str(uuid4())),
    )

    response = client.get(
        "/api/v1/engineering/monitors",
        params={"connector_account_id": str(account_id)},
        headers=_headers(token),
    )
    assert response.status_code == 200, response.text
    monitors = response.json()["monitors"]
    assert len(monitors) == 1
    assert monitors[0]["name"] == "Watched"
    assert monitors[0]["team_entity_id"] is None

    response_other = client.get(
        "/api/v1/engineering/monitors",
        params={"connector_account_id": str(other_account_id)},
        headers=_headers(token),
    )
    assert response_other.json()["monitors"] == []


def test_list_service_definitions_and_dashboards_endpoints_return_synced_rows(
    engineering_test_context: tuple[TestClient, UUID, UUID, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, workspace_id, user_id, token = engineering_test_context
    account_id = _insert_datadog_connector_account(workspace_id, user_id)

    def service_handler(request: httpx.Request) -> httpx.Response:
        return _json_response(
            {"data": [_service_definition("checkout", team="team-checkout")]}
            if int(request.url.params["page[number]"]) == 0
            else {"data": []}
        )

    registry = ConnectorRegistry()
    registry.register(DatadogAdapter(transport=httpx.MockTransport(service_handler)))
    monkeypatch.setattr(connector_accounts_module, "connector_registry", registry)
    client.post(
        f"/api/v1/engineering/connectors/{account_id}/sync",
        json={"run_type": "backfill", "resource_type": "service_definition"},
        headers=_headers(token, key=str(uuid4())),
    )

    service_response = client.get(
        "/api/v1/engineering/service-definitions", headers=_headers(token)
    )
    assert service_response.status_code == 200, service_response.text
    definitions = service_response.json()["service_definitions"]
    assert len(definitions) == 1
    assert definitions[0]["team"] == "team-checkout"
    assert definitions[0]["suggested_team_name"] == "team-checkout"

    def dashboard_handler(request: httpx.Request) -> httpx.Response:
        return _json_response({"dashboards": [_dashboard("abc-123", title="Overview")]})

    registry2 = ConnectorRegistry()
    registry2.register(DatadogAdapter(transport=httpx.MockTransport(dashboard_handler)))
    monkeypatch.setattr(connector_accounts_module, "connector_registry", registry2)
    client.post(
        f"/api/v1/engineering/connectors/{account_id}/sync",
        json={"run_type": "backfill", "resource_type": "dashboard"},
        headers=_headers(token, key=str(uuid4())),
    )

    dashboard_response = client.get("/api/v1/engineering/dashboards", headers=_headers(token))
    assert dashboard_response.status_code == 200, dashboard_response.text
    dashboards = dashboard_response.json()["dashboards"]
    assert len(dashboards) == 1
    assert dashboards[0]["title"] == "Overview"
