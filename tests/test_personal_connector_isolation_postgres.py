"""Private personal connectors, their runs/cursors, and the route-level
second layer (Security Remediation Spec A S1.8(a,d), T14a), behind
`ECC_PERSONAL_DATA_ISOLATION`.

Flag on:

- the Gmail OAuth callback INSERTs the `connector_accounts` row `private`,
  owned by the mailbox owner; `sync_runs`/`sync_cursors` of a personal
  connector are inserted `private`, owned by the connector's owner.
  Engineering providers are unchanged (owned by the acting user,
  `workspace`).
- a non-owner (another member, or the workspace's other `owner`) cannot
  list the connector or its runs, gets `404` from effective permissions,
  and `POST .../sync` / `POST .../disable` answer `404 CONNECTOR_NOT_FOUND`
  and count `ecc_connector_access_denied_total{provider, route}` -- also
  for a Gmail row written while the flag was off (still `workspace`, not
  yet backfilled), where authz alone would allow the call.

Flag off: exactly the previous values and responses.

Every Gmail row comes from the real sync (`gmail_sync_fixtures`); the
`TestFlagOn` world is built with the flag on (the committed smoke case of
plan note N7). The two worlds are class-scoped so their harnesses never
nest.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from gmail_sync_fixtures import GmailSyncWorld, build_gmail_sync_world, csrf_headers
from sqlalchemy import text

from ecc import observability
from ecc.config import get_settings
from ecc.database import engine

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_FLAG = "ECC_PERSONAL_DATA_ISOLATION"
_CONNECTORS = "/api/v1/engineering/connectors"
_NOT_FOUND = "CONNECTOR_NOT_FOUND"
# Rows these tests write that the fixture does not clean: `GET
# /engineering/metrics` snapshots and the seeded jira work items.
_EXTRA_CLEANUP_TABLES = ("delivery_metric_snapshots", "engineering_work_items")


# --- fixtures -----------------------------------------------------------------


@pytest.fixture
def set_isolation(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[bool], None]]:
    def _set(enabled: bool) -> None:
        monkeypatch.setenv(_FLAG, "true" if enabled else "false")
        get_settings.cache_clear()

    yield _set
    monkeypatch.undo()
    get_settings.cache_clear()


@pytest.fixture(scope="class")
def world_on() -> Iterator[GmailSyncWorld]:
    with build_gmail_sync_world(
        env={_FLAG: "true"}, bystander=True, extra_cleanup_tables=_EXTRA_CLEANUP_TABLES
    ) as built:
        yield built


@pytest.fixture(scope="class")
def world_off() -> Iterator[GmailSyncWorld]:
    with build_gmail_sync_world(
        env={_FLAG: "false"}, extra_cleanup_tables=_EXTRA_CLEANUP_TABLES
    ) as built:
        yield built


# --- helpers ------------------------------------------------------------------


def _scalar(sql: str, params: dict[str, Any]) -> Any:
    with engine.connect() as connection:
        return connection.execute(text(sql), params).scalar_one()


def _owner_and_visibility(table: str, row_id: UUID) -> tuple[UUID, str]:
    with engine.connect() as connection:
        row = connection.execute(
            text(f"SELECT owner_id, visibility FROM {table} WHERE id = :id"),  # noqa: S608
            {"id": row_id},
        ).one()
    return row[0], row[1]


def _connector_state(connector_id: UUID) -> tuple[Any, ...]:
    with engine.connect() as connection:
        return tuple(
            connection.execute(
                text(
                    "SELECT status, version, updated_at, disconnected_at, updated_by, "
                    "owner_id, visibility, encrypted_credentials "
                    "FROM connector_accounts WHERE id = :id"
                ),
                {"id": connector_id},
            ).one()
        )


def _sync_run_count(connector_id: UUID) -> int:
    return int(
        _scalar(
            "SELECT count(*) FROM sync_runs WHERE connector_account_id = :id", {"id": connector_id}
        )
    )


def _denied(route: str, provider: str = "gmail") -> float:
    return observability.connector_access_denied_total._values.get((provider, route), 0.0)


def _denied_total() -> float:
    return sum(observability.connector_access_denied_total._values.values())


def _as(world: GmailSyncWorld, key: str) -> tuple[TestClient, Callable[[], dict[str, str]]]:
    """Client + fresh-headers factory (new `Idempotency-Key` per call) for
    member `key`, or for the bystander owner when `key == "bystander"`."""
    if key == "bystander":
        assert world.bystander_user_id is not None
        client, token = world.harness.client_for(world.workspace_id, world.bystander_user_id)
        return client, lambda: csrf_headers(token, str(uuid4()))
    return world.client(key), lambda: world.headers(key, idempotency_key=str(uuid4()))


def _list_connector_ids(world: GmailSyncWorld, key: str) -> set[UUID]:
    client, _headers = _as(world, key)
    response = client.get(_CONNECTORS)
    assert response.status_code == 200, response.text
    return {UUID(row["id"]) for row in response.json()["connectors"]}


def _list_sync_run_ids(
    world: GmailSyncWorld, key: str, connector_id: UUID | None = None
) -> set[UUID]:
    client, _headers = _as(world, key)
    params = {"connector_account_id": str(connector_id)} if connector_id else None
    response = client.get("/api/v1/engineering/sync-runs", params=params)
    assert response.status_code == 200, response.text
    return {UUID(row["id"]) for row in response.json()["sync_runs"]}


def _effective_permissions(
    world: GmailSyncWorld, key: str, resource_type: str, row_id: UUID
) -> httpx.Response:
    client, _headers = _as(world, key)
    return client.get(f"/api/v1/sharing/resources/{resource_type}/{row_id}")


def _sync(
    world: GmailSyncWorld, key: str, connector_id: UUID, resource_type: str = "message"
) -> httpx.Response:
    client, headers = _as(world, key)
    return client.post(
        f"{_CONNECTORS}/{connector_id}/sync",
        json={"run_type": "incremental", "resource_type": resource_type},
        headers=headers(),
    )


def _disable(world: GmailSyncWorld, key: str, connector_id: UUID) -> httpx.Response:
    client, headers = _as(world, key)
    return client.post(f"{_CONNECTORS}/{connector_id}/disable", headers=headers())


def _create_sandbox_connector(world: GmailSyncWorld, key: str) -> UUID:
    client, headers = _as(world, key)
    response = client.post(
        _CONNECTORS,
        json={"provider": "sandbox", "credential": f"sandbox-{uuid4().hex}"},
        headers=headers(),
    )
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


def _insert_engineering_connector(world: GmailSyncWorld, provider: str) -> UUID:
    connector_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO connector_accounts (
                    id, workspace_id, provider, external_account_id, display_name,
                    granted_scopes, encrypted_credentials, status, version,
                    created_by, updated_by, created_at, updated_at, owner_id, visibility
                ) VALUES (
                    :id, :ws, :provider, :external_account_id, 'Engineering',
                    ARRAY[]::text[], :credential, 'active', 1,
                    :user_id, :user_id, :now, :now, :user_id, 'workspace'
                )
                """
            ),
            {
                "id": connector_id,
                "ws": world.workspace_id,
                "provider": provider,
                "external_account_id": f"{provider}-{connector_id}",
                "credential": b"x",
                "user_id": world.a.user_id,
                "now": now,
            },
        )
    return connector_id


def _seed_jira_work(world: GmailSyncWorld) -> None:
    """A jira connector with open work items and a fresh `work_item` cursor
    (the shapes `test_engineering_metrics_postgres.py` seeds), so
    `work_ageing`/`blocked_work` are computed, not `insufficient_coverage`.
    Every active jira connector in the workspace gets a fresh cursor, so a
    cursor-less one inserted by another test cannot lower coverage."""
    jira = _insert_engineering_connector(world, "jira")
    now = datetime.now(UTC)
    with engine.begin() as connection:
        for (connector_id,) in connection.execute(
            text(
                "SELECT id FROM connector_accounts WHERE workspace_id = :ws "
                "AND provider = 'jira' AND status = 'active'"
            ),
            {"ws": world.workspace_id},
        ).all():
            connection.execute(
                text(
                    "INSERT INTO sync_cursors (id, workspace_id, connector_account_id, "
                    "resource_type, cursor_value, updated_at) "
                    "VALUES (:id, :ws, :acct, 'work_item', 'x', :now) "
                    "ON CONFLICT (workspace_id, connector_account_id, resource_type) "
                    "DO UPDATE SET updated_at = EXCLUDED.updated_at"
                ),
                {"id": uuid4(), "ws": world.workspace_id, "acct": connector_id, "now": now},
            )
        for external_id, status, age_days in (
            ("open-3d", "In Progress", 3),
            ("blocked-40d", "Blocked", 40),
            ("done-100d", "Done", 100),
        ):
            connection.execute(
                text(
                    """
                    INSERT INTO engineering_work_items (
                        id, workspace_id, connector_account_id, provider, external_id,
                        title, source_url, item_type, status, permission_state,
                        freshness_state, provider_created_at, observed_at, created_at,
                        updated_at
                    ) VALUES (
                        :id, :ws, :acct, 'jira', :ext, :title, 'https://x', 'Bug', :status,
                        'active', 'fresh', :created, :now, :now, :now
                    )
                    """
                ),
                {
                    "id": uuid4(),
                    "ws": world.workspace_id,
                    "acct": jira,
                    "ext": f"{external_id}-{jira}",
                    "title": external_id,
                    "status": status,
                    "created": now - timedelta(days=age_days),
                    "now": now,
                },
            )


def _assert_same_metrics(on: Any, off: Any, path: str = "") -> None:
    """Equal, except that floats (ages in days, measured from `now()` on
    each call) may differ by the seconds between the two requests."""
    if isinstance(on, float) and isinstance(off, float):
        assert math.isclose(on, off, abs_tol=1e-3), (path, on, off)
    elif isinstance(on, dict) and isinstance(off, dict):
        assert on.keys() == off.keys(), path
        for key in on:
            _assert_same_metrics(on[key], off[key], f"{path}.{key}")
    elif isinstance(on, list) and isinstance(off, list):
        assert len(on) == len(off), path
        for index, (left, right) in enumerate(zip(on, off, strict=True)):
            _assert_same_metrics(left, right, f"{path}[{index}]")
    else:
        assert on == off, (path, on, off)


def _metric_values(world: GmailSyncWorld, key: str) -> dict[str, dict[str, Any]]:
    client, headers = _as(world, key)
    response = client.get("/api/v1/engineering/metrics", headers=headers())
    assert response.status_code == 200, response.text
    return {
        row["metric_key"]: {
            field: value
            for field, value in row.items()
            if field != "id" and not field.endswith("_at")
        }
        for row in response.json()["metrics"]
    }


# --- flag on ------------------------------------------------------------------


class TestFlagOn:
    def test_smoke_world_built_with_flag_on_writes_private_connector_runs_cursors(
        self, world_on: GmailSyncWorld
    ) -> None:
        """Plan note N7: the real-sync world builds with the flag on, and
        each member's connector/runs/cursors are private to that member."""
        for member in (world_on.a, world_on.b):
            assert _owner_and_visibility("connector_accounts", member.connector_account_id) == (
                member.user_id,
                "private",
            )
            assert member.sync_run_ids and member.sync_cursor_ids
            for run_id in member.sync_run_ids:
                assert _owner_and_visibility("sync_runs", run_id) == (member.user_id, "private")
            for cursor_id in member.sync_cursor_ids:
                assert _owner_and_visibility("sync_cursors", cursor_id) == (
                    member.user_id,
                    "private",
                )

    @pytest.mark.parametrize("viewer", ["b", "bystander"])
    def test_non_owner_cannot_see_connector_or_runs(
        self, world_on: GmailSyncWorld, viewer: str
    ) -> None:
        a = world_on.a
        assert a.connector_account_id not in _list_connector_ids(world_on, viewer)
        assert not set(a.sync_run_ids) & _list_sync_run_ids(world_on, viewer)
        assert not _list_sync_run_ids(world_on, viewer, a.connector_account_id)
        for resource_type, row_id in (
            ("connector_accounts", a.connector_account_id),
            ("sync_runs", a.sync_run_ids[0]),
            ("sync_cursors", a.sync_cursor_ids[0]),
        ):
            response = _effective_permissions(world_on, viewer, resource_type, row_id)
            assert response.status_code == 404, (resource_type, response.text)
            assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

    def test_owner_sees_own_connector_and_runs_only(self, world_on: GmailSyncWorld) -> None:
        a, b = world_on.a, world_on.b
        listed = _list_connector_ids(world_on, "a")
        assert a.connector_account_id in listed
        assert b.connector_account_id not in listed
        assert set(a.sync_run_ids) <= _list_sync_run_ids(world_on, "a", a.connector_account_id)
        assert not _list_sync_run_ids(world_on, "a", b.connector_account_id)
        response = _effective_permissions(
            world_on, "a", "connector_accounts", a.connector_account_id
        )
        assert response.status_code == 200, response.text
        assert set(response.json()["granted_actions"]) >= {"read", "write"}

    def test_list_response_has_no_owner_id(self, world_on: GmailSyncWorld) -> None:
        """Plan note N1: `ConnectorAccountResponse` stays without `owner_id`."""
        response = world_on.client("a").get(_CONNECTORS)
        assert response.status_code == 200, response.text
        assert response.json()["connectors"]
        for row in response.json()["connectors"]:
            assert "owner_id" not in row
            assert "visibility" not in row

    @pytest.mark.parametrize("viewer", ["b", "bystander"])
    @pytest.mark.parametrize("route", ["sync", "disable"])
    def test_non_owner_mutation_is_404_and_counted(
        self, world_on: GmailSyncWorld, viewer: str, route: str
    ) -> None:
        a = world_on.a
        state_before = _connector_state(a.connector_account_id)
        runs_before = _sync_run_count(a.connector_account_id)
        denied_before = _denied(route)
        other_before = _denied_total() - denied_before

        call = _sync if route == "sync" else _disable
        response = call(world_on, viewer, a.connector_account_id)

        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == _NOT_FOUND
        assert _denied(route) == denied_before + 1
        assert _denied_total() - _denied(route) == other_before
        assert _connector_state(a.connector_account_id) == state_before
        assert _sync_run_count(a.connector_account_id) == runs_before

    def test_denial_is_indistinguishable_from_missing_connector(
        self, world_on: GmailSyncWorld
    ) -> None:
        denied_before = _denied_total()
        missing = _sync(world_on, "b", uuid4())
        hidden = _sync(world_on, "b", world_on.a.connector_account_id)
        assert missing.status_code == hidden.status_code == 404

        def _shape(body: dict[str, Any]) -> dict[str, Any]:
            error = {k: v for k, v in body["error"].items() if k != "request_id"}
            return {"error": error, "keys": sorted(body)}

        assert _shape(missing.json()) == _shape(hidden.json())
        # Only the personal connector is counted -- never a missing id.
        assert _denied_total() == denied_before + 1

    def test_owner_can_sync_and_new_run_is_private(self, world_on: GmailSyncWorld) -> None:
        a = world_on.a
        denied_before = _denied_total()
        response = _sync(world_on, "a", a.connector_account_id)
        assert response.status_code == 201, response.text
        run_id = UUID(response.json()["id"])
        assert _owner_and_visibility("sync_runs", run_id) == (a.user_id, "private")
        assert _denied_total() == denied_before
        assert run_id not in _list_sync_run_ids(world_on, "b")

    def test_owner_disable_reaches_the_endpoint_not_the_404_layer(
        self, world_on: GmailSyncWorld
    ) -> None:
        """A reaches the generic disable (which routes a Gmail account with
        an `email` domain to the domain endpoint, as before) -- no 404, no
        count, row unchanged."""
        a = world_on.a
        state_before = _connector_state(a.connector_account_id)
        denied_before = _denied_total()
        response = _disable(world_on, "a", a.connector_account_id)
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "GMAIL_DISABLE_REQUIRES_DOMAIN_ENDPOINT"
        assert _denied_total() == denied_before
        assert _connector_state(a.connector_account_id) == state_before

    def test_engineering_connectors_unchanged(self, world_on: GmailSyncWorld) -> None:
        """jira/github rows stay workspace-visible; a sandbox connector is
        syncable and disable-able by another member exactly as before, its
        run is owned by the actor and `workspace`, and nothing is counted."""
        denied_before = _denied_total()
        github = _insert_engineering_connector(world_on, "github")
        jira = _insert_engineering_connector(world_on, "jira")
        for viewer in ("b", "bystander"):
            listed = _list_connector_ids(world_on, viewer)
            assert {github, jira} <= listed
            for connector_id in (github, jira):
                response = _effective_permissions(
                    world_on, viewer, "connector_accounts", connector_id
                )
                assert response.status_code == 200, response.text

        sandbox = _create_sandbox_connector(world_on, "a")
        assert _owner_and_visibility("connector_accounts", sandbox) == (
            world_on.a.user_id,
            "workspace",
        )
        synced = _sync(world_on, "b", sandbox, resource_type="repository")
        assert synced.status_code == 201, synced.text
        run_id = UUID(synced.json()["id"])
        assert _owner_and_visibility("sync_runs", run_id) == (world_on.b.user_id, "workspace")
        assert run_id in _list_sync_run_ids(world_on, "a", sandbox)
        with engine.connect() as connection:
            cursor_rows = connection.execute(
                text(
                    "SELECT owner_id, visibility FROM sync_cursors WHERE connector_account_id = :id"
                ),
                {"id": sandbox},
            ).all()
        assert cursor_rows
        assert {(row[0], row[1]) for row in cursor_rows} == {(world_on.b.user_id, "workspace")}

        disabled = _disable(world_on, "b", github)
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["status"] == "disconnected"
        assert _denied_total() == denied_before

    def test_engineering_metrics_unaffected_by_flag(
        self, world_on: GmailSyncWorld, set_isolation: Callable[[bool], None]
    ) -> None:
        """Engineering metrics read only engineering rows: with private
        Gmail connectors/runs/cursors present alongside real jira work,
        the non-empty metrics are the same with the flag on and off."""
        _seed_jira_work(world_on)
        set_isolation(True)
        on = _metric_values(world_on, "b")
        set_isolation(False)
        off = _metric_values(world_on, "b")

        for key in ("work_ageing", "blocked_work"):
            assert on[key]["coverage_status"] == "complete", on[key]
            assert on[key]["population"] == 2, on[key]  # "Done" excluded
            assert on[key]["value"], on[key]
        assert on["blocked_work"]["value"] == 1.0  # one blocked item
        assert 21 <= on["work_ageing"]["value"] <= 22  # median of ~3 and ~40 days
        _assert_same_metrics(on, off)

    def test_flag_turned_off_drops_the_route_layer(
        self, world_on: GmailSyncWorld, set_isolation: Callable[[bool], None]
    ) -> None:
        """Flag off: no route-level check and no count. A's row written
        while the flag was on stays private, so authz alone still hides it."""
        set_isolation(False)
        denied_before = _denied_total()
        for call in (_sync, _disable):
            response = call(world_on, "b", world_on.a.connector_account_id)
            assert response.status_code == 404, response.text
        assert _denied_total() == denied_before


# --- flag off (today) and flag-off-era rows under the flag --------------------


class TestFlagOff:
    def test_flag_off_writes_today_values(self, world_off: GmailSyncWorld) -> None:
        for member in (world_off.a, world_off.b):
            assert _owner_and_visibility("connector_accounts", member.connector_account_id) == (
                member.user_id,
                "workspace",
            )
            for run_id in member.sync_run_ids:
                assert _owner_and_visibility("sync_runs", run_id) == (
                    member.user_id,
                    "workspace",
                )
            for cursor_id in member.sync_cursor_ids:
                assert _owner_and_visibility("sync_cursors", cursor_id) == (
                    member.user_id,
                    "workspace",
                )

    def test_flag_off_other_member_sees_and_acts_as_today(
        self, world_off: GmailSyncWorld, set_isolation: Callable[[bool], None]
    ) -> None:
        set_isolation(False)
        a = world_off.a
        denied_before = _denied_total()
        assert a.connector_account_id in _list_connector_ids(world_off, "b")
        assert set(a.sync_run_ids) <= _list_sync_run_ids(world_off, "b")
        response = _effective_permissions(
            world_off, "b", "connector_accounts", a.connector_account_id
        )
        assert response.status_code == 200, response.text

        # Today's behavior: a member may sync another member's
        # workspace-visible Gmail connector; the run is owned by the actor.
        synced = _sync(world_off, "b", a.connector_account_id)
        assert synced.status_code == 201, synced.text
        assert _owner_and_visibility("sync_runs", UUID(synced.json()["id"])) == (
            world_off.b.user_id,
            "workspace",
        )
        disabled = _disable(world_off, "b", a.connector_account_id)
        assert disabled.status_code == 409, disabled.text
        assert disabled.json()["error"]["code"] == "GMAIL_DISABLE_REQUIRES_DOMAIN_ENDPOINT"
        assert _denied_total() == denied_before

    @pytest.mark.parametrize("route", ["sync", "disable"])
    def test_flag_on_route_layer_protects_unbackfilled_workspace_row(
        self, world_off: GmailSyncWorld, set_isolation: Callable[[bool], None], route: str
    ) -> None:
        """A Gmail row written while the flag was off is still `workspace`
        (the backfill has not run), so authz alone would let B act on it --
        the route layer answers 404 and counts."""
        set_isolation(True)
        a = world_off.a
        assert _owner_and_visibility("connector_accounts", a.connector_account_id)[1] == (
            "workspace"
        )
        state_before = _connector_state(a.connector_account_id)
        runs_before = _sync_run_count(a.connector_account_id)
        denied_before = _denied(route)

        call = _sync if route == "sync" else _disable
        response = call(world_off, "b", a.connector_account_id)

        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == _NOT_FOUND
        assert _denied(route) == denied_before + 1
        assert _connector_state(a.connector_account_id) == state_before
        assert _sync_run_count(a.connector_account_id) == runs_before

    def test_flag_on_owner_sync_of_unbackfilled_connector_writes_private_run(
        self, world_off: GmailSyncWorld, set_isolation: Callable[[bool], None]
    ) -> None:
        """New runs of a personal connector are private to the connector's
        owner even when the connector row itself predates the flag; the
        connector row is not flipped here (the backfill owns that)."""
        set_isolation(True)
        a = world_off.a
        response = _sync(world_off, "a", a.connector_account_id)
        assert response.status_code == 201, response.text
        assert _owner_and_visibility("sync_runs", UUID(response.json()["id"])) == (
            a.user_id,
            "private",
        )
        assert _owner_and_visibility("connector_accounts", a.connector_account_id) == (
            a.user_id,
            "workspace",
        )
