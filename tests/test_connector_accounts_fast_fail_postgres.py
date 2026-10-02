"""Read fast-fail before the row lock for the engineering connector
mutations (`POST .../connectors/{id}/disable` and the repository /
work-item team assignments).

Lock-before-authorize (#324) made these take the membership, idempotency
and row locks before deciding, so a caller who cannot even see the row
queued behind whoever held it -- e.g. a sync holding the connector across
an OAuth refresh -- before getting its 404, and the delay told them the row
existed and was busy. Each now runs an unlocked read check (plus, for a
connector, the personal-connector owner layer) right after the shared
membership lock, ahead of the idempotency and row locks: such a caller gets
its 404 while the row is still locked, without waiting. The authoritative
check stays on the locked row (`test_connector_accounts_mutation_lock_race_
postgres.py`).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from lock_race_support import (
    WAIT_SECONDS,
    RaceWorld,
    headers,
    holder_backend_pid,
    lock_waiters,
    race_world,
    row_snapshot,
)
from sqlalchemy import Connection, text

from ecc import observability
from ecc.config import get_settings
from ecc.database import engine
from ecc.domains.engineering.crypto import encrypt_credential
from ecc.main import app

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_FLAG = "ECC_PERSONAL_DATA_ISOLATION"
_FAST_SECONDS = 5
_SIDE_EFFECT_TABLES = ("audit_events", "event_outbox", "idempotency_records")


@pytest.fixture
def world() -> Iterator[RaceWorld]:
    with race_world(
        "Connector Accounts Fast Fail",
        ("repositories", "engineering_work_items", "connector_accounts"),
    ) as w:
        yield w


@pytest.fixture(autouse=True)
def _reset_settings() -> Iterator[None]:
    yield
    get_settings.cache_clear()


def _c_token(w: RaceWorld) -> str:
    """A session for C (`member`), who cannot see B's private rows."""
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
                "user_id": w.c,
                "token_hash": sha256(token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "now": now,
            },
        )
    return token


def _seed_connector(conn: Connection, w: RaceWorld, *, provider: str, visibility: str) -> UUID:
    account_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO connector_accounts (id, workspace_id, provider, external_account_id, "
            "display_name, granted_scopes, encrypted_credentials, status, version, "
            "created_by, updated_by, created_at, updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, :provider, :external, 'Fast-fail connector', "
            "ARRAY['contents:read'], :credential, 'active', 1, :b, :b, now(), now(), "
            ":b, :visibility)"
        ),
        {
            "id": account_id,
            "ws": w.ws,
            "provider": provider,
            "external": f"fast-{account_id}",
            "credential": encrypt_credential("fast-credential"),
            "b": w.b,
            "visibility": visibility,
        },
    )
    return account_id


def _seed_projection(conn: Connection, w: RaceWorld, table: str) -> UUID:
    connector_id = _seed_connector(conn, w, provider="sandbox", visibility="workspace")
    row_id = uuid4()
    title_column = "name" if table == "repositories" else "title"
    conn.execute(
        text(
            f"INSERT INTO {table} (id, workspace_id, connector_account_id, provider, "  # noqa: S608
            f"external_id, {title_column}, source_url, observed_at, created_at, updated_at, "
            "owner_id, visibility) "
            "VALUES (:id, :ws, :connector_id, 'github', :external, 'Fast-fail row', "
            "'https://example.invalid/fast', now(), now(), now(), :b, 'private')"
        ),
        {
            "id": row_id,
            "ws": w.ws,
            "connector_id": connector_id,
            "external": f"fast-{row_id}",
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
    seed: Callable[[Connection, RaceWorld], UUID]
    personal_isolation: bool = False
    # `ecc_connector_access_denied_total{provider, route}` the refusal must
    # count exactly once; None: unchanged.
    denied_metric: tuple[str, str] | None = None


_TEAM_BODY = {"expected_version": 1, "team_entity_id": None}

CASES: dict[str, Case] = {
    # Private, B's: authz read hides it from C.
    "disable": Case(
        "connector_accounts",
        "/api/v1/engineering/connectors/{id}/disable",
        None,
        "CONNECTOR_NOT_FOUND",
        lambda c, w: _seed_connector(c, w, provider="sandbox", visibility="private"),
    ),
    # Workspace-visible personal Gmail, B's: authz read lets C see it, so
    # only the personal-connector owner layer refuses (and counts) it.
    "disable_personal_gmail": Case(
        "connector_accounts",
        "/api/v1/engineering/connectors/{id}/disable",
        None,
        "CONNECTOR_NOT_FOUND",
        lambda c, w: _seed_connector(c, w, provider="gmail", visibility="workspace"),
        personal_isolation=True,
        denied_metric=("gmail", "disable"),
    ),
    "repository_team": Case(
        "repositories",
        "/api/v1/engineering/repositories/{id}/team",
        _TEAM_BODY,
        "REPOSITORY_NOT_FOUND",
        lambda c, w: _seed_projection(c, w, "repositories"),
    ),
    "work_item_team": Case(
        "engineering_work_items",
        "/api/v1/engineering/work-items/{id}/team",
        _TEAM_BODY,
        "WORK_ITEM_NOT_FOUND",
        lambda c, w: _seed_projection(c, w, "engineering_work_items"),
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


def _denied_counts() -> dict[tuple[str, ...], float]:
    return dict(observability.connector_access_denied_total._values)


@pytest.mark.parametrize("name", list(CASES))
def test_caller_who_cannot_see_the_row_is_refused_without_waiting_on_its_lock(
    world: RaceWorld, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    case = CASES[name]
    monkeypatch.setenv(_FLAG, "true" if case.personal_isolation else "false")
    get_settings.cache_clear()
    with engine.begin() as connection:
        row_id = case.seed(connection, world)
    c_token = _c_token(world)
    counts_before = _side_effect_counts(world.ws)
    denied_before = _denied_counts()

    client = TestClient(app)
    client.cookies.set("ecc_session", c_token)
    result: dict[str, Any] = {}
    finished = threading.Event()

    def fire() -> None:
        try:
            result["response"] = client.post(
                case.path.format(id=row_id), headers=headers(c_token), json=case.body
            )
        except BaseException as exc:  # surfaced on the main thread below
            result["error"] = exc
        finally:
            finished.set()

    holder = engine.connect()
    holder_tx = holder.begin()
    thread = threading.Thread(target=fire)
    try:
        holder_pid = holder_backend_pid(holder)
        holder.execute(
            text(f"SELECT id FROM {case.table} WHERE id = :id FOR UPDATE"),  # noqa: S608
            {"id": row_id},
        )
        before = row_snapshot(case.table, row_id)
        thread.start()
        # Answered while the row is still locked: the request never queued.
        answered_while_locked = finished.wait(timeout=_FAST_SECONDS)
        waiting_on_holder = lock_waiters(case.table, holder_pid=holder_pid)
    finally:
        holder_tx.rollback()
        holder.close()
        thread.join(timeout=WAIT_SECONDS)
        client.close()
    assert not thread.is_alive(), "request never finished"
    if "error" in result:
        raise result["error"]

    assert answered_while_locked, "request waited on the row lock before being refused"
    assert waiting_on_holder == 0
    response = result["response"]
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == case.not_found
    assert row_snapshot(case.table, row_id) == before
    assert _side_effect_counts(world.ws) == counts_before
    expected_denied = dict(denied_before)
    if case.denied_metric is not None:
        expected_denied[case.denied_metric] = expected_denied.get(case.denied_metric, 0.0) + 1
    assert _denied_counts() == expected_denied


@pytest.mark.parametrize("name", list(CASES))
def test_nonexistent_id_is_refused_with_the_same_404(
    world: RaceWorld, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    case = CASES[name]
    monkeypatch.setenv(_FLAG, "true" if case.personal_isolation else "false")
    get_settings.cache_clear()
    c_token = _c_token(world)
    client = TestClient(app)
    client.cookies.set("ecc_session", c_token)
    try:
        response = client.post(
            case.path.format(id=uuid4()), headers=headers(c_token), json=case.body
        )
    finally:
        client.close()

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == case.not_found
