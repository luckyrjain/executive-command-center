"""Authorization before the idempotency cache is served for the bulk
team-suggestion confirm/dismiss endpoints and `POST /connectors/{id}/sync`.

Team suggestions: both endpoints take the membership lock with only an
active-member requirement and authorize per row, so the cache used to be
served before any of that -- a caller demoted to `viewer`, or who lost the
rows or the team entity, could replay a same-key request and get the cached
success. The team entity is now re-checked ahead of the cache, and a cached
response is served only if the caller still has `write` on every row it
reported `updated` (and `read` on every row it reported skipped).

Sync: the endpoint's read/write pre-checks run in an earlier, rolled-back
transaction, and phase 1 used to serve the cache before re-running them on
the locked account row. A change that committed between the two (here,
injected right after the pre-checks) let a caller who had just lost access
replay a cached run. The cache is now read after the locked checks.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from lock_race_support import RaceWorld, headers, race_world
from sqlalchemy import text

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
from ecc.main import app

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_SIDE_EFFECT_TABLES = (
    "audit_events",
    "event_outbox",
    "idempotency_records",
    "sync_runs",
    "sync_cursors",
)


@dataclass
class _SandboxAdapter:
    provider: str = "sandbox"
    required_scopes: frozenset[str] = field(default_factory=lambda: frozenset({"contents:read"}))

    def authorize(self, credential: str) -> ConnectorAuthorization:
        raise AssertionError("not reached by these tests")

    def backfill(
        self,
        account: ConnectorAccountContext,
        resource_type: str,
        since: datetime | None = None,
        resume_cursor: str | None = None,
    ) -> SyncOutcome:
        return SyncOutcome(
            resource_type=resource_type, items_processed=0, status="succeeded", next_cursor="1"
        )

    def incremental_sync(
        self, account: ConnectorAccountContext, resource_type: str, cursor: str | None
    ) -> SyncOutcome:
        return SyncOutcome(
            resource_type=resource_type, items_processed=0, status="succeeded", next_cursor="1"
        )

    def handle_webhook(
        self, account: ConnectorAccountContext, payload: bytes, headers: object
    ) -> SyncOutcome:
        raise NotImplementedError

    def refresh_permissions(self, account: ConnectorAccountContext) -> PermissionState:
        return "active"

    def disconnect(self, account: ConnectorAccountContext) -> None:
        return None


@pytest.fixture
def world() -> Iterator[RaceWorld]:
    with race_world(
        "Engineering Idempotent Replay",
        (
            "sync_cursors",
            "sync_runs",
            "repositories",
            "engineering_work_items",
            "connector_accounts",
            "pkos_nodes",
        ),
    ) as w:
        yield w


@pytest.fixture(autouse=True)
def _sandbox_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    registry = ConnectorRegistry()
    registry.register(_SandboxAdapter())
    monkeypatch.setattr(connector_accounts_module, "connector_registry", registry)


def _token(w: RaceWorld, user_id: UUID) -> str:
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                "expires_at, last_seen_at) "
                "VALUES (:id, :ws, :user_id, :token_hash, :expires_at, :now)"
            ),
            {
                "id": uuid4(),
                "ws": w.ws,
                "user_id": user_id,
                "token_hash": sha256(token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "now": now,
            },
        )
    return token


def _execute(sql: str, params: dict[str, Any]) -> None:
    with engine.begin() as connection:
        connection.execute(text(sql), params)


def _demote(w: RaceWorld, user_id: UUID) -> None:
    _execute(
        "UPDATE workspace_memberships SET role = 'viewer' "
        "WHERE workspace_id = :ws AND users_id = :user_id",
        {"ws": w.ws, "user_id": user_id},
    )


def _seed_connector(w: RaceWorld, *, owner: UUID, visibility: str) -> UUID:
    account_id = uuid4()
    now = datetime.now(UTC)
    _execute(
        "INSERT INTO connector_accounts (id, workspace_id, provider, external_account_id, "
        "display_name, granted_scopes, encrypted_credentials, status, version, "
        "created_by, updated_by, created_at, updated_at, owner_id, visibility) "
        "VALUES (:id, :ws, 'sandbox', :external, 'Replay connector', "
        "ARRAY['contents:read'], :credential, 'active', 1, :owner, :owner, :now, :now, "
        ":owner, :visibility)",
        {
            "id": account_id,
            "ws": w.ws,
            "external": f"replay-{account_id}",
            "credential": encrypt_credential("replay-credential"),
            "owner": owner,
            "now": now,
            "visibility": visibility,
        },
    )
    return account_id


def _seed_team(w: RaceWorld) -> UUID:
    team_id = uuid4()
    now = datetime.now(UTC)
    _execute(
        "INSERT INTO pkos_nodes (id, workspace_id, node_type, canonical_name, status, "
        "confidence, version, created_at, updated_at, owner_id, visibility) "
        "VALUES (:id, :ws, 'team', 'Replay Team', 'active', 1.0, 1, :now, :now, :a, 'workspace')",
        {"id": team_id, "ws": w.ws, "now": now, "a": w.a},
    )
    return team_id


def _seed_suggested_rows(
    w: RaceWorld, *, owner: UUID, visibility: str, team_name: str
) -> tuple[UUID, UUID]:
    connector_id = _seed_connector(w, owner=w.a, visibility="workspace")
    now = datetime.now(UTC)
    ids = []
    for table, title_column in (("repositories", "name"), ("engineering_work_items", "title")):
        row_id = uuid4()
        _execute(
            f"INSERT INTO {table} (id, workspace_id, connector_account_id, provider, "  # noqa: S608
            f"external_id, {title_column}, source_url, observed_at, created_at, updated_at, "
            "suggested_team_name, owner_id, visibility) "
            "VALUES (:id, :ws, :connector_id, 'github', :external, 'Replay row', "
            "'https://example.invalid/replay', :now, :now, :now, :team_name, :owner, :visibility)",
            {
                "id": row_id,
                "ws": w.ws,
                "connector_id": connector_id,
                "external": f"replay-{row_id}",
                "now": now,
                "team_name": team_name,
                "owner": owner,
                "visibility": visibility,
            },
        )
        ids.append(row_id)
    return ids[0], ids[1]


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


def _without_request_id(body: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in body.items() if key != "request_id"}


_SUGGESTIONS = "/api/v1/engineering/team-suggestions/"


def _suggestion_body(action: str, team_name: str, team_id: UUID) -> dict[str, Any]:
    body: dict[str, Any] = {"suggested_team_name": team_name}
    if action == "confirm":
        body["team_entity_id"] = str(team_id)
    return body


def _act_and_replay(
    client: TestClient,
    action: str,
    request_headers: dict[str, str],
    body: dict[str, Any],
    expected_updated: set[str],
) -> None:
    first = client.post(_SUGGESTIONS + action, headers=request_headers, json=body)
    assert first.status_code == 200, first.text
    assert set(first.json()["updated"]) == expected_updated
    # The rows are no longer candidates: only the cache can repeat this.
    replay = client.post(_SUGGESTIONS + action, headers=request_headers, json=body)
    assert replay.status_code == 200, replay.text
    assert _without_request_id(replay.json()) == _without_request_id(first.json())


@pytest.mark.parametrize("action", ["confirm", "dismiss"])
def test_team_suggestion_replay_after_demotion_to_viewer_is_refused_403(
    world: RaceWorld, action: str
) -> None:
    team_name = f"replay-{uuid4()}"
    team_id = _seed_team(world)
    # Workspace-visible and owned by A: C (`member`) writes them by role
    # alone, and as a `viewer` can still read them.
    repo_id, item_id = _seed_suggested_rows(
        world, owner=world.a, visibility="workspace", team_name=team_name
    )
    c_token = _token(world, world.c)
    body = _suggestion_body(action, team_name, team_id)
    request_headers = headers(c_token)
    client = TestClient(app)
    client.cookies.set("ecc_session", c_token)
    try:
        _act_and_replay(client, action, request_headers, body, {str(repo_id), str(item_id)})
        _demote(world, world.c)
        counts_before = _side_effect_counts(world.ws)
        refused = client.post(_SUGGESTIONS + action, headers=request_headers, json=body)
    finally:
        client.close()

    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "INSUFFICIENT_ROLE"
    assert _side_effect_counts(world.ws) == counts_before


@pytest.mark.parametrize("action", ["confirm", "dismiss"])
def test_team_suggestion_replay_after_rows_transferred_away_is_refused_403(
    world: RaceWorld, action: str
) -> None:
    """A set-based action has no single resource to answer 404 for, so a
    replay whose rows the caller can no longer see is refused like one it
    can no longer write."""
    team_name = f"replay-{uuid4()}"
    team_id = _seed_team(world)
    repo_id, item_id = _seed_suggested_rows(
        world, owner=world.b, visibility="private", team_name=team_name
    )
    body = _suggestion_body(action, team_name, team_id)
    request_headers = headers(world.b_token)
    client = TestClient(app)
    client.cookies.set("ecc_session", world.b_token)
    try:
        _act_and_replay(client, action, request_headers, body, {str(repo_id), str(item_id)})
        # Transferred to C; still private, so B can no longer see them.
        for table, row_id in (("repositories", repo_id), ("engineering_work_items", item_id)):
            _execute(
                f"UPDATE {table} SET owner_id = :c WHERE id = :id",  # noqa: S608
                {"c": world.c, "id": row_id},
            )
        counts_before = _side_effect_counts(world.ws)
        refused = client.post(_SUGGESTIONS + action, headers=request_headers, json=body)
    finally:
        client.close()

    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "INSUFFICIENT_ROLE"
    assert _side_effect_counts(world.ws) == counts_before


def test_team_suggestion_confirm_replay_after_losing_the_team_entity_is_refused_404(
    world: RaceWorld,
) -> None:
    team_name = f"replay-{uuid4()}"
    team_id = _seed_team(world)
    repo_id, item_id = _seed_suggested_rows(
        world, owner=world.b, visibility="private", team_name=team_name
    )
    body = _suggestion_body("confirm", team_name, team_id)
    request_headers = headers(world.b_token)
    client = TestClient(app)
    client.cookies.set("ecc_session", world.b_token)
    try:
        _act_and_replay(client, "confirm", request_headers, body, {str(repo_id), str(item_id)})
        # A's team becomes private: B can no longer see it.
        _execute("UPDATE pkos_nodes SET visibility = 'private' WHERE id = :id", {"id": team_id})
        counts_before = _side_effect_counts(world.ws)
        refused = client.post(_SUGGESTIONS + "confirm", headers=request_headers, json=body)
    finally:
        client.close()

    assert refused.status_code == 404, refused.text
    assert refused.json()["error"]["code"] == "TEAM_ENTITY_NOT_FOUND"
    assert _side_effect_counts(world.ws) == counts_before


def test_team_suggestion_replay_with_nothing_updated_still_replays_for_a_viewer(
    world: RaceWorld,
) -> None:
    """Control: a viewer's own bulk action (every row skipped) replays from
    the cache -- it needs only the `read` it still has."""
    team_name = f"replay-{uuid4()}"
    team_id = _seed_team(world)
    _seed_suggested_rows(world, owner=world.a, visibility="workspace", team_name=team_name)
    _demote(world, world.c)
    c_token = _token(world, world.c)
    body = _suggestion_body("dismiss", team_name, team_id)
    request_headers = headers(c_token)
    client = TestClient(app)
    client.cookies.set("ecc_session", c_token)
    try:
        _act_and_replay(client, "dismiss", request_headers, body, set())
    finally:
        client.close()


_SYNC = "/api/v1/engineering/connectors/{id}/sync"
_SYNC_BODY = {"run_type": "incremental", "resource_type": "repository"}


def _change_after_prechecks(monkeypatch: pytest.MonkeyPatch, change: Callable[[], None]) -> None:
    """Commits `change` right after `sync_connector_endpoint`'s rolled-back
    pre-checks and before `_run_connector_sync`'s phase 1 -- its
    `request_hash` call sits exactly between the two."""
    real = connector_accounts_module.request_hash

    def hooked(*args: Any, **kwargs: Any) -> str:
        change()
        return real(*args, **kwargs)

    monkeypatch.setattr(connector_accounts_module, "request_hash", hooked)


def _sync_and_replay(client: TestClient, account_id: UUID, request_headers: dict[str, str]) -> None:
    first = client.post(_SYNC.format(id=account_id), headers=request_headers, json=_SYNC_BODY)
    assert first.status_code == 201, first.text
    replay = client.post(_SYNC.format(id=account_id), headers=request_headers, json=_SYNC_BODY)
    assert replay.status_code == 201, replay.text
    assert _without_request_id(replay.json()) == _without_request_id(first.json())


def test_sync_replay_after_transfer_past_the_prechecks_is_refused_404(
    world: RaceWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    account_id = _seed_connector(world, owner=world.b, visibility="private")
    request_headers = headers(world.b_token)
    client = TestClient(app)
    client.cookies.set("ecc_session", world.b_token)
    try:
        _sync_and_replay(client, account_id, request_headers)
        _change_after_prechecks(
            monkeypatch,
            lambda: _execute(
                "UPDATE connector_accounts SET owner_id = :c WHERE id = :id",
                {"c": world.c, "id": account_id},
            ),
        )
        counts_before = _side_effect_counts(world.ws)
        refused = client.post(_SYNC.format(id=account_id), headers=request_headers, json=_SYNC_BODY)
    finally:
        client.close()

    assert refused.status_code == 404, refused.text
    assert refused.json()["error"]["code"] == "CONNECTOR_NOT_FOUND"
    assert _side_effect_counts(world.ws) == counts_before


def test_sync_replay_after_demotion_past_the_prechecks_is_refused_403(
    world: RaceWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    account_id = _seed_connector(world, owner=world.a, visibility="workspace")
    c_token = _token(world, world.c)
    request_headers = headers(c_token)
    client = TestClient(app)
    client.cookies.set("ecc_session", c_token)
    try:
        _sync_and_replay(client, account_id, request_headers)
        _change_after_prechecks(monkeypatch, lambda: _demote(world, world.c))
        counts_before = _side_effect_counts(world.ws)
        refused = client.post(_SYNC.format(id=account_id), headers=request_headers, json=_SYNC_BODY)
    finally:
        client.close()

    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "INSUFFICIENT_ROLE"
    assert _side_effect_counts(world.ws) == counts_before
