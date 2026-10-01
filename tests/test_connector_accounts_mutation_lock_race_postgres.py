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
writes nothing. The same re-check covers a grant downgraded to read-only
(403) or revoked (404) while the request waits, and the auto-backfill's
sequential per-resource-type syncs. See `lock_race_support` for the harness.
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

from ecc import observability
from ecc.config import get_settings
from ecc.database import engine
from ecc.domains.engineering import connector_accounts as connector_accounts_module
from ecc.domains.engineering.connectors import (
    ConnectorAccountContext,
    ConnectorAuthorization,
    ConnectorRegistry,
    PermissionState,
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

    def refresh_permissions(self, account: ConnectorAccountContext) -> PermissionState:
        self.calls.append("refresh_permissions")
        return "active"

    def disconnect(self, account: ConnectorAccountContext) -> None:
        self.calls.append("disconnect")


@pytest.fixture
def world() -> Iterator[RaceWorld]:
    with race_world(
        "Connector Accounts Lock Race",
        (
            "resource_grants",
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
    by_provider = {provider: _SpyAdapter(provider) for provider in ("sandbox", "gmail", "github")}
    for adapter in by_provider.values():
        registry.register(adapter)
    monkeypatch.setattr(connector_accounts_module, "connector_registry", registry)
    return by_provider


@pytest.fixture(autouse=True)
def _reset_settings() -> Iterator[None]:
    yield
    get_settings.cache_clear()


def _seed_connector(
    conn: Connection,
    w: RaceWorld,
    now: datetime,
    *,
    provider: str,
    visibility: str,
    owner: UUID | None = None,
) -> UUID:
    account_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO connector_accounts (id, workspace_id, provider, external_account_id, "
            "display_name, granted_scopes, encrypted_credentials, status, version, "
            "created_by, updated_by, created_at, updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, :provider, :external, 'Race connector', "
            "ARRAY['contents:read'], :credential, 'active', 1, :b, :b, :now, :now, "
            ":owner, :visibility)"
        ),
        {
            "id": account_id,
            "ws": w.ws,
            "provider": provider,
            "external": f"race-{account_id}",
            "credential": encrypt_credential("race-credential"),
            "b": w.b,
            "owner": owner or w.b,
            "now": now,
            "visibility": visibility,
        },
    )
    return account_id


def _seed_projection(
    conn: Connection,
    w: RaceWorld,
    now: datetime,
    table: str,
    *,
    owner: UUID | None = None,
    visibility: str = "private",
) -> UUID:
    connector_id = _seed_connector(conn, w, now, provider="sandbox", visibility="workspace")
    row_id = uuid4()
    title_column = "name" if table == "repositories" else "title"
    conn.execute(
        text(
            f"INSERT INTO {table} (id, workspace_id, connector_account_id, provider, "  # noqa: S608
            f"external_id, {title_column}, source_url, observed_at, created_at, updated_at, "
            "owner_id, visibility) "
            "VALUES (:id, :ws, :connector_id, 'github', :external, 'Race row', "
            "'https://example.invalid/race', :now, :now, :now, :owner, :visibility)"
        ),
        {
            "id": row_id,
            "ws": w.ws,
            "connector_id": connector_id,
            "external": f"race-{row_id}",
            "now": now,
            "owner": owner or w.b,
            "visibility": visibility,
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
    # The no-transfer control needs a provider call that succeeds; a Gmail
    # sync refreshes an OAuth credential first, which this stand-in lacks.
    control: bool = True
    # `ecc_connector_access_denied_total{provider, route}` the transfer case
    # must increment (the personal-connector owner layer); None: no change.
    denied_metric: tuple[str, str] | None = None


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
        denied_metric=("gmail", "disable"),
    ),
    # Sync's fast-fail pre-check passes (B still owns the row then); only
    # phase 1's re-check of the personal-connector owner layer on the
    # locked row refuses it.
    "sync_personal_gmail": Case(
        "connector_accounts",
        _CONNECTORS + "/sync",
        {"run_type": "incremental", "resource_type": "message"},
        "CONNECTOR_NOT_FOUND",
        201,
        lambda c, w, now: _seed_connector(c, w, now, provider="gmail", visibility="workspace"),
        personal_isolation=True,
        control=False,
        denied_metric=("gmail", "sync"),
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


def _run(
    w: RaceWorld,
    case: Case,
    row_id: UUID,
    *,
    transfer: bool,
    mutate: Callable[[Connection], object] | None = None,
) -> tuple[Any, Any]:
    return race(
        w,
        table=case.table,
        row_id=row_id,
        send=lambda client: client.post(
            case.path.format(id=row_id), headers=headers(w.b_token), json=case.body
        ),
        transfer=transfer,
        mutate=mutate,
    )


def _denied_counts() -> dict[tuple[str, ...], float]:
    return dict(observability.connector_access_denied_total._values)


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
    denied_before = _denied_counts()

    response, before = _run(world, case, row_id, transfer=True)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == case.not_found
    assert row_snapshot(case.table, row_id) == {**before, "owner_id": world.c}
    assert _side_effect_counts(world.ws) == counts_before
    assert {provider: spy.calls for provider, spy in spies.items()} == {
        "sandbox": [],
        "gmail": [],
        "github": [],
    }
    expected_denied = dict(denied_before)
    if case.denied_metric is not None:
        expected_denied[case.denied_metric] = expected_denied.get(case.denied_metric, 0.0) + 1
    assert _denied_counts() == expected_denied


@pytest.mark.parametrize("name", [name for name, case in CASES.items() if case.control])
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


# --- grant changes while the request waits ------------------------------------
#
# The row belongs to C and is `shared_explicitly` with B through a read+write
# grant, so B passes every check made before the lock. While B's request
# waits on the row lock the holder downgrades the grant to read-only (B can
# still see the row: 403) or revokes it (B cannot: 404).

_GRANT_CASES: dict[str, Case] = {
    "sync": Case(
        "connector_accounts",
        _CONNECTORS + "/sync",
        {"run_type": "incremental", "resource_type": "repository"},
        "CONNECTOR_NOT_FOUND",
        201,
        lambda c, w, now: _seed_connector(
            c, w, now, provider="sandbox", visibility="shared_explicitly", owner=w.c
        ),
    ),
    "disable": Case(
        "connector_accounts",
        _CONNECTORS + "/disable",
        None,
        "CONNECTOR_NOT_FOUND",
        200,
        lambda c, w, now: _seed_connector(
            c, w, now, provider="sandbox", visibility="shared_explicitly", owner=w.c
        ),
    ),
    "repository_team": Case(
        "repositories",
        "/api/v1/engineering/repositories/{id}/team",
        _TEAM_BODY,
        "REPOSITORY_NOT_FOUND",
        200,
        lambda c, w, now: _seed_projection(
            c, w, now, "repositories", owner=w.c, visibility="shared_explicitly"
        ),
    ),
    "work_item_team": Case(
        "engineering_work_items",
        "/api/v1/engineering/work-items/{id}/team",
        _TEAM_BODY,
        "WORK_ITEM_NOT_FOUND",
        200,
        lambda c, w, now: _seed_projection(
            c, w, now, "engineering_work_items", owner=w.c, visibility="shared_explicitly"
        ),
    ),
}


def _insert_grant(
    conn: Connection, w: RaceWorld, table: str, row_id: UUID, actions: list[str]
) -> UUID:
    grant_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO resource_grants (id, workspace_id, grantee_account_id, "
            "resource_type, resource_id, actions, granted_by, created_at) "
            "SELECT :id, :ws, account_id, :table, :row_id, :actions, :c, now() "
            "FROM users WHERE id = :b"
        ),
        {
            "id": grant_id,
            "ws": w.ws,
            "table": table,
            "row_id": row_id,
            "actions": actions,
            "c": w.c,
            "b": w.b,
        },
    )
    return grant_id


def _revoke(grant_id: UUID) -> Callable[[Connection], object]:
    def mutate(conn: Connection) -> object:
        return conn.execute(
            text("UPDATE resource_grants SET revoked_at = now() WHERE id = :id"),
            {"id": grant_id},
        )

    return mutate


def _downgrade_to_read(
    w: RaceWorld, case: Case, row_id: UUID, grant_id: UUID
) -> Callable[[Connection], object]:
    def mutate(conn: Connection) -> object:
        _revoke(grant_id)(conn)
        return _insert_grant(conn, w, case.table, row_id, ["read"])

    return mutate


def _prepare_granted(
    world: RaceWorld, case: Case, monkeypatch: pytest.MonkeyPatch
) -> tuple[UUID, UUID]:
    row_id = _prepare(world, case, monkeypatch)
    with engine.begin() as connection:
        grant_id = _insert_grant(connection, world, case.table, row_id, ["read", "write"])
    return row_id, grant_id


@pytest.mark.parametrize("change", ["loses_write", "revoked"])
@pytest.mark.parametrize("name", list(_GRANT_CASES))
def test_mutation_waiting_on_row_lock_rechecks_a_changed_grant(
    world: RaceWorld,
    spies: dict[str, _SpyAdapter],
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    change: str,
) -> None:
    case = _GRANT_CASES[name]
    row_id, grant_id = _prepare_granted(world, case, monkeypatch)
    counts_before = _side_effect_counts(world.ws)
    if change == "loses_write":
        mutate, status, code = (
            _downgrade_to_read(world, case, row_id, grant_id),
            403,
            ("INSUFFICIENT_ROLE"),
        )
    else:
        mutate, status, code = _revoke(grant_id), 404, case.not_found

    response, before = _run(world, case, row_id, transfer=False, mutate=mutate)

    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    assert row_snapshot(case.table, row_id) == before
    assert _side_effect_counts(world.ws) == counts_before
    assert all(spy.calls == [] for spy in spies.values())


@pytest.mark.parametrize("name", list(_GRANT_CASES))
def test_mutation_waiting_on_row_lock_with_unchanged_grant_still_proceeds(
    world: RaceWorld,
    spies: dict[str, _SpyAdapter],
    monkeypatch: pytest.MonkeyPatch,
    name: str,
) -> None:
    """Control: the read+write grant alone lets B through the same wait --
    the 403/404 above come from the grant change, not the setup."""
    case = _GRANT_CASES[name]
    row_id, _grant_id = _prepare_granted(world, case, monkeypatch)

    response, _before = _run(world, case, row_id, transfer=False)

    assert response.status_code == case.ok_status, response.text


# --- auto-backfill -------------------------------------------------------------


def _transfer_on_first_backfill(spy: _SpyAdapter, w: RaceWorld, account_id: UUID) -> None:
    """Commits an ownership transfer from inside the first resource type's
    provider call -- phase 2, with no lock held -- the window between one
    resource type's sync and the next."""
    original = spy.backfill

    def backfill(*args: Any, **kwargs: Any) -> SyncOutcome:
        if not spy.calls:
            with engine.begin() as connection:
                connection.execute(
                    text("UPDATE connector_accounts SET owner_id = :c WHERE id = :id"),
                    {"c": w.c, "id": account_id},
                )
        return original(*args, **kwargs)

    spy.backfill = backfill  # type: ignore[method-assign]


def _sync_run_count(account_id: UUID) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                text("SELECT count(*) FROM sync_runs WHERE connector_account_id = :id"),
                {"id": account_id},
            ).scalar_one()
        )


@pytest.mark.parametrize("transfer", [True, False])
def test_auto_backfill_rechecks_authorization_before_each_resource_type(
    world: RaceWorld,
    spies: dict[str, _SpyAdapter],
    monkeypatch: pytest.MonkeyPatch,
    transfer: bool,
) -> None:
    """GitHub auto-backfills three resource types one after another. A
    transfer that commits during the first must stop the other two (each
    phase 1 re-authorizes the creator on the locked row); without one, all
    three run (control)."""
    monkeypatch.setenv(_FLAG, "false")
    get_settings.cache_clear()
    with engine.begin() as connection:
        account_id = _seed_connector(
            connection, world, datetime.now(UTC), provider="github", visibility="private"
        )
    if transfer:
        _transfer_on_first_backfill(spies["github"], world, account_id)

    connector_accounts_module._run_auto_backfill(
        workspace_id=world.ws,
        user_id=world.b,
        timezone="UTC",
        account_id=account_id,
        provider="github",
    )

    resource_types = connector_accounts_module._AUTO_SYNC_RESOURCE_TYPES["github"]
    expected_runs = 1 if transfer else len(resource_types)
    assert spies["github"].calls == ["backfill"] * expected_runs
    assert _sync_run_count(account_id) == expected_runs
