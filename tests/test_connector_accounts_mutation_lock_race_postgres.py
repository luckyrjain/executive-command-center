"""Lock-before-authorize sweep for the engineering connector mutations:
`POST .../connectors/{id}/sync`, `POST .../connectors/{id}/disable` and the
repository / work-item team assignments authorized the caller in a
*separate* transaction that was rolled back, then took `SELECT ... FOR
UPDATE` on the row in a later write transaction without re-checking. An
ownership transfer (locks the row, rewrites `owner_id`, does not bump
`version`) that committed in between -- here, while the request waits on
that row lock -- left the request writing to a row the caller could no
longer see (and, for sync, calling the provider with its credential).

Now each write transaction locks the row first and authorizes afterwards,
on the committed post-transfer row, so the waiting request answers 404 and
writes nothing. See `lock_race_support` for the harness.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from lock_race_support import RaceWorld, headers, race, race_world, row_snapshot
from sqlalchemy import Connection, text

from ecc.config import get_settings
from ecc.database import engine
from ecc.domains.engineering import connector_accounts as connector_accounts_module
from ecc.domains.engineering.connectors import (
    ConnectorAccountContext,
    ConnectorAuthorization,
    ConnectorRegistry,
    SyncOutcome,
)
from ecc.domains.engineering.crypto import encrypt_credential

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_FLAG = "ECC_PERSONAL_DATA_ISOLATION"
_SIDE_EFFECT_TABLES = (
    "audit_events",
    "event_outbox",
    "idempotency_records",
    "sync_runs",
    "sync_cursors",
)


@dataclass
class _SpyAdapter:
    """In-process stand-in for the provider: records every call that would
    reach it, so a refused request can be shown never to have got there."""

    provider: str
    required_scopes: frozenset[str] = field(default_factory=lambda: frozenset({"contents:read"}))
    calls: list[str] = field(default_factory=list)

    def authorize(self, credential: str) -> ConnectorAuthorization:
        raise AssertionError("not reached by these tests")

    def backfill(
        self,
        account: ConnectorAccountContext,
        resource_type: str,
        since: datetime | None = None,
        resume_cursor: str | None = None,
    ) -> SyncOutcome:
        self.calls.append("backfill")
        return SyncOutcome(
            resource_type=resource_type, items_processed=0, status="succeeded", next_cursor="1"
        )

    def incremental_sync(
        self, account: ConnectorAccountContext, resource_type: str, cursor: str | None
    ) -> SyncOutcome:
        self.calls.append("incremental_sync")
        return SyncOutcome(
            resource_type=resource_type, items_processed=0, status="succeeded", next_cursor="1"
        )

    def handle_webhook(
        self, account: ConnectorAccountContext, payload: bytes, headers: object
    ) -> SyncOutcome:
        raise NotImplementedError

    def refresh_permissions(self, account: ConnectorAccountContext) -> str:
        self.calls.append("refresh_permissions")
        return "active"

    def disconnect(self, account: ConnectorAccountContext) -> None:
        self.calls.append("disconnect")


@pytest.fixture
def world() -> Iterator[RaceWorld]:
    with race_world(
        "Connector Accounts Lock Race",
        (
            "sync_cursors",
            "sync_runs",
            "repositories",
            "engineering_work_items",
            "connector_accounts",
        ),
    ) as w:
        yield w


@pytest.fixture
def spies(monkeypatch: pytest.MonkeyPatch) -> dict[str, _SpyAdapter]:
    registry = ConnectorRegistry()
    by_provider = {provider: _SpyAdapter(provider) for provider in ("sandbox", "gmail")}
    for adapter in by_provider.values():
        registry.register(adapter)
    monkeypatch.setattr(connector_accounts_module, "connector_registry", registry)
    return by_provider


@pytest.fixture(autouse=True)
def _reset_settings() -> Iterator[None]:
    yield
    get_settings.cache_clear()


def _seed_connector(
    conn: Connection, w: RaceWorld, now: datetime, *, provider: str, visibility: str
) -> UUID:
    account_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO connector_accounts (id, workspace_id, provider, external_account_id, "
            "display_name, granted_scopes, encrypted_credentials, status, version, "
            "created_by, updated_by, created_at, updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, :provider, :external, 'Race connector', "
            "ARRAY['contents:read'], :credential, 'active', 1, :b, :b, :now, :now, "
            ":b, :visibility)"
        ),
        {
            "id": account_id,
            "ws": w.ws,
            "provider": provider,
            "external": f"race-{account_id}",
            "credential": encrypt_credential("race-credential"),
            "b": w.b,
            "now": now,
            "visibility": visibility,
        },
    )
    return account_id


def _seed_projection(conn: Connection, w: RaceWorld, now: datetime, table: str) -> UUID:
    connector_id = _seed_connector(conn, w, now, provider="sandbox", visibility="workspace")
    row_id = uuid4()
    title_column = "name" if table == "repositories" else "title"
    conn.execute(
        text(
            f"INSERT INTO {table} (id, workspace_id, connector_account_id, provider, "  # noqa: S608
            f"external_id, {title_column}, source_url, observed_at, created_at, updated_at, "
            "owner_id, visibility) "
            "VALUES (:id, :ws, :connector_id, 'github', :external, 'Race row', "
            "'https://example.invalid/race', :now, :now, :now, :b, 'private')"
        ),
        {
            "id": row_id,
            "ws": w.ws,
            "connector_id": connector_id,
            "external": f"race-{row_id}",
            "now": now,
            "b": w.b,
        },
    )
    return row_id


@dataclass(frozen=True)
class Case:
    table: str
    path: str
    body: dict[str, Any] | None
    not_found: str
    ok_status: int
    seed: Callable[[Connection, RaceWorld, datetime], UUID]
    personal_isolation: bool = False


_CONNECTORS = "/api/v1/engineering/connectors/{id}"
_TEAM_BODY = {"expected_version": 1, "team_entity_id": None}

CASES: dict[str, Case] = {
    "sync": Case(
        "connector_accounts",
        _CONNECTORS + "/sync",
        {"run_type": "incremental", "resource_type": "repository"},
        "CONNECTOR_NOT_FOUND",
        201,
        lambda c, w, now: _seed_connector(c, w, now, provider="sandbox", visibility="private"),
    ),
    "disable": Case(
        "connector_accounts",
        _CONNECTORS + "/disable",
        None,
        "CONNECTOR_NOT_FOUND",
        200,
        lambda c, w, now: _seed_connector(c, w, now, provider="sandbox", visibility="private"),
    ),
    # The personal-connector owner layer (`ECC_PERSONAL_DATA_ISOLATION`) is
    # keyed on `owner_id` too: a `workspace`-visible Gmail connector stays
    # authz-writable by an admin after the transfer, so only that layer's
    # re-check on the locked row refuses it.
    "disable_personal_gmail": Case(
        "connector_accounts",
        _CONNECTORS + "/disable",
        None,
        "CONNECTOR_NOT_FOUND",
        200,
        lambda c, w, now: _seed_connector(c, w, now, provider="gmail", visibility="workspace"),
        personal_isolation=True,
    ),
    "repository_team": Case(
        "repositories",
        "/api/v1/engineering/repositories/{id}/team",
        _TEAM_BODY,
        "REPOSITORY_NOT_FOUND",
        200,
        lambda c, w, now: _seed_projection(c, w, now, "repositories"),
    ),
    "work_item_team": Case(
        "engineering_work_items",
        "/api/v1/engineering/work-items/{id}/team",
        _TEAM_BODY,
        "WORK_ITEM_NOT_FOUND",
        200,
        lambda c, w, now: _seed_projection(c, w, now, "engineering_work_items"),
    ),
}


def _side_effect_counts(ws: UUID) -> dict[str, int]:
    with engine.connect() as connection:
        return {
            table: int(
                connection.execute(
                    text(f"SELECT count(*) FROM {table} WHERE workspace_id = :ws"),  # noqa: S608
                    {"ws": ws},
                ).scalar_one()
            )
            for table in _SIDE_EFFECT_TABLES
        }


def _prepare(world: RaceWorld, case: Case, monkeypatch: pytest.MonkeyPatch) -> UUID:
    monkeypatch.setenv(_FLAG, "true" if case.personal_isolation else "false")
    get_settings.cache_clear()
    with engine.begin() as connection:
        return case.seed(connection, world, datetime.now(UTC))


def _run(w: RaceWorld, case: Case, row_id: UUID, *, transfer: bool) -> tuple[Any, Any]:
    return race(
        w,
        table=case.table,
        row_id=row_id,
        send=lambda client: client.post(
            case.path.format(id=row_id), headers=headers(w.b_token), json=case.body
        ),
        transfer=transfer,
    )


@pytest.mark.parametrize("name", list(CASES))
def test_mutation_waiting_on_row_lock_rechecks_authorization(
    world: RaceWorld,
    spies: dict[str, _SpyAdapter],
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    case = CASES[name]
    row_id = _prepare(world, case, monkeypatch)
    counts_before = _side_effect_counts(world.ws)

    response, before = _run(world, case, row_id, transfer=True)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == case.not_found
    assert row_snapshot(case.table, row_id) == {**before, "owner_id": world.c}
    assert _side_effect_counts(world.ws) == counts_before
    assert {provider: spy.calls for provider, spy in spies.items()} == {
        "sandbox": [],
        "gmail": [],
    }


@pytest.mark.parametrize("name", list(CASES))
def test_mutation_waiting_on_row_lock_without_transfer_still_proceeds(
    world: RaceWorld,
    spies: dict[str, _SpyAdapter],
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    """Control: the same lock wait with no ownership change succeeds -- the
    404 above comes from the transfer, not from the wait itself."""
    case = CASES[name]
    row_id = _prepare(world, case, monkeypatch)

    response, before = _run(world, case, row_id, transfer=False)

    assert response.status_code == case.ok_status, response.text
    after = row_snapshot(case.table, row_id)
    if case.path.endswith("/sync"):
        assert spies["sandbox"].calls == ["incremental_sync"]
        assert after["last_synced_at"] is not None
    elif case.path.endswith("/disable"):
        assert after["status"] == "disconnected"
        assert after["version"] == before["version"] + 1
    else:
        assert after["team_assignment_version"] == before["team_assignment_version"] + 1
