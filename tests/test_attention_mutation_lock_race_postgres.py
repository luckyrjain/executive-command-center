"""FX2 deep review (data-integrity persona, probe
`test_r5_dismiss_defer_during_batch`): `_mutate_attention` authorized the
caller *before* taking `SELECT ... FOR UPDATE` on the attention item. A
dismiss/defer that started just before a concurrent owner/visibility change
(the personal-visibility backfill, an ownership transfer, a regenerate)
passed authorization against the old row, waited on the row lock, then
committed against an item the caller could no longer see -- and echoed the
item's fields back.

Now the item is locked first and authorization is evaluated afterwards, on
the committed post-change row, so the waiting request answers 404 and writes
nothing.

Concurrency is real (request thread + separate connection); "is waiting" is
observed in `pg_stat_activity`, not assumed from timing.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from identity_fixtures import create_identity
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import engine
from ecc.main import app

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

settings = get_settings()
_WAIT_SECONDS = 15


@dataclass
class RaceWorld:
    """One workspace: A (`owner`), B (`admin`, the racing caller) and C
    (`member`, the transfer target)."""

    ws: UUID
    a: UUID
    b: UUID
    c: UUID
    b_token: str


def _session(connection: Any, ws: UUID, user_id: UUID, token: str, now: datetime) -> None:
    connection.execute(
        text(
            "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
            "expires_at, last_seen_at) "
            "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :last_seen_at)"
        ),
        {
            "id": uuid4(),
            "workspace_id": ws,
            "user_id": user_id,
            "token_hash": sha256(token.encode()).hexdigest(),
            "expires_at": now + timedelta(hours=1),
            "last_seen_at": now,
        },
    )


@pytest.fixture
def race_world() -> Iterator[RaceWorld]:
    ws, a, b, c = uuid4(), uuid4(), uuid4(), uuid4()
    b_token = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Attention Lock Race', 'UTC', :now)"
            ),
            {"id": ws, "now": now},
        )
        create_identity(connection, workspace_id=ws, user_id=a, now=now)
        create_identity(connection, workspace_id=ws, user_id=b, now=now, role="admin")
        create_identity(connection, workspace_id=ws, user_id=c, now=now, role="member")
        _session(connection, ws, b, b_token, now)
    try:
        yield RaceWorld(ws=ws, a=a, b=b, c=c, b_token=b_token)
    finally:
        with engine.begin() as connection:
            account_ids = list(
                connection.execute(
                    text("SELECT account_id FROM users WHERE workspace_id = :ws"), {"ws": ws}
                ).scalars()
            )
            for table in (
                "attention_items",
                "event_outbox",
                "audit_events",
                "sessions",
                "workspace_memberships",
                "users",
            ):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :ws"),  # noqa: S608
                    {"ws": ws},
                )
            connection.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": ws})
            connection.execute(
                text("DELETE FROM accounts WHERE id = ANY(:ids)"), {"ids": account_ids}
            )


def _headers(token: str) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    return {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}


def _seed_item(ws: UUID, owner_id: UUID, visibility: str) -> UUID:
    item_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO attention_items (
                    id, workspace_id, entity_type, entity_id, source_entity_version,
                    score, confidence, factors, explanation, generated_at, expires_at,
                    pinned, policy_version, owner_id, visibility
                ) VALUES (
                    :id, :ws, 'task', :entity_id, 1, 90, 1.0,
                    '[]'::jsonb, 'Race item', :now, :expires_at,
                    false, 1, :owner_id, :visibility
                )
                """
            ),
            {
                "id": item_id,
                "ws": ws,
                "entity_id": uuid4(),
                "now": now,
                "expires_at": now + timedelta(minutes=30),
                "owner_id": owner_id,
                "visibility": visibility,
            },
        )
    return item_id


def _item_lock_waiters() -> int:
    with engine.connect() as probe:
        return int(
            probe.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                    "AND wait_event IN ('transactionid', 'tuple') "
                    "AND query ~* 'FROM attention_items ai.*FOR UPDATE'"
                )
            ).scalar_one()
        )


def _wait_for_item_lock_waiter() -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    while _item_lock_waiters() < 1:
        if time.monotonic() > deadline:
            raise AssertionError("mutation never blocked on the attention item's row lock")
        time.sleep(0.05)


def _item_state(item_id: UUID) -> dict[str, Any]:
    with engine.connect() as connection:
        return dict(
            connection.execute(
                text(
                    "SELECT dismissed_at, deferred_until, override_reason "
                    "FROM attention_items WHERE id = :id"
                ),
                {"id": item_id},
            )
            .mappings()
            .one()
        )


def _audit_count(ws: UUID, item_id: UUID) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                text(
                    "SELECT count(*) FROM audit_events WHERE workspace_id = :ws "
                    "AND event_type LIKE 'attention_item.%' AND aggregate_id = :id"
                ),
                {"ws": ws, "id": item_id},
            ).scalar_one()
        )


@pytest.mark.parametrize("action", ["dismiss", "defer"])
@pytest.mark.parametrize("change", ["made_private", "transferred"])
def test_mutation_waiting_on_item_lock_rechecks_authorization(
    race_world: RaceWorld, action: str, change: str
) -> None:
    w = race_world
    if change == "made_private":
        # A's workspace-visible item, visible to B; the batch makes it private.
        item_id = _seed_item(w.ws, w.a, "workspace")
        update_sql = "UPDATE attention_items SET visibility = 'private' WHERE id = :id"
    else:
        # B's own private item; the batch transfers it to C.
        item_id = _seed_item(w.ws, w.b, "private")
        update_sql = "UPDATE attention_items SET owner_id = :c WHERE id = :id"

    client = TestClient(app)
    client.cookies.set("ecc_session", w.b_token)
    try:
        # Control: before the change B can see the item.
        before = client.get(f"/api/v1/attention/{item_id}", headers=_headers(w.b_token))
        assert before.status_code == 200, before.text

        body: dict[str, Any] = {"reason": "race probe"}
        if action == "defer":
            body["deferred_until"] = (datetime.now(UTC) + timedelta(days=1)).isoformat()

        result: dict[str, Any] = {}

        def fire() -> None:
            try:
                result["response"] = client.post(
                    f"/api/v1/attention/{item_id}/{action}",
                    headers=_headers(w.b_token),
                    json=body,
                )
            except BaseException as exc:  # surfaced on the main thread below
                result["error"] = exc

        batch = engine.connect()
        batch_tx = batch.begin()
        thread = threading.Thread(target=fire)
        try:
            batch.execute(
                text("SELECT id FROM attention_items WHERE id = :id FOR UPDATE"),
                {"id": item_id},
            )
            batch.execute(text(update_sql), {"id": item_id, "c": w.c})
            thread.start()
            _wait_for_item_lock_waiter()
            batch_tx.commit()
        finally:
            if batch_tx.is_active:
                batch_tx.rollback()
            batch.close()
            thread.join(timeout=_WAIT_SECONDS)
        assert not thread.is_alive(), "mutation request never finished"
        if "error" in result:
            raise result["error"]

        response = result["response"]
        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == "ATTENTION_ITEM_NOT_FOUND"
        assert _item_state(item_id) == {
            "dismissed_at": None,
            "deferred_until": None,
            "override_reason": None,
        }
        assert _audit_count(w.ws, item_id) == 0
    finally:
        client.close()
