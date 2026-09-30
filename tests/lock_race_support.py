"""Shared harness for lock-before-authorize race tests.

A mutation that authorizes the caller *before* taking `SELECT ... FOR UPDATE`
on the row it then writes can be overtaken by an ownership transfer
(`authz_grants` transfer locks the row, rewrites `owner_id` and does not bump
`version`, so `expected_version` cannot catch it). The request waits on the
row lock, the transfer commits, and the request writes to a row its caller
can no longer see.

`race()` reproduces that interleaving for real: a separate connection locks
the row (optionally rewriting `owner_id` to C), the request is fired on a
thread, the harness waits until `pg_stat_activity` shows the request blocked
on a row lock, then commits. Fixed code locks first and authorizes against
the committed post-transfer row, so the request answers 404 and writes
nothing.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from typing import Any
from uuid import UUID, uuid4

import httpx
from fastapi.testclient import TestClient
from identity_fixtures import create_identity
from sqlalchemy import Connection, text

from ecc.config import get_settings
from ecc.database import engine
from ecc.main import app

WAIT_SECONDS = 15


@dataclass
class RaceWorld:
    """One workspace: A (`owner`), B (`admin`, the racing caller and the
    rows' original owner) and C (`member`, the transfer target)."""

    ws: UUID
    a: UUID
    b: UUID
    c: UUID
    b_token: str


def _insert_session(
    connection: Connection, ws: UUID, user_id: UUID, token: str, now: datetime
) -> None:
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


@contextmanager
def race_world(name: str, cleanup_tables: Iterable[str]) -> Iterator[RaceWorld]:
    """Creates the workspace and its three members; on exit deletes every
    row in `cleanup_tables` (in the given order, children first) for the
    workspace, then the identity rows."""
    ws, a, b, c = uuid4(), uuid4(), uuid4(), uuid4()
    b_token = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, :name, 'UTC', :now)"
            ),
            {"id": ws, "name": name, "now": now},
        )
        create_identity(connection, workspace_id=ws, user_id=a, now=now)
        create_identity(connection, workspace_id=ws, user_id=b, now=now, role="admin")
        create_identity(connection, workspace_id=ws, user_id=c, now=now, role="member")
        _insert_session(connection, ws, b, b_token, now)
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
                *cleanup_tables,
                "event_outbox",
                "audit_events",
                "idempotency_records",
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


def headers(token: str) -> dict[str, str]:
    csrf = new(get_settings().session_secret.encode(), token.encode(), "sha256").hexdigest()
    return {
        "X-CSRF-Token": csrf,
        "X-Correlation-ID": str(uuid4()),
        "Idempotency-Key": str(uuid4()),
    }


def lock_waiters(table: str) -> int:
    with engine.connect() as probe:
        return int(
            probe.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                    "AND wait_event IN ('transactionid', 'tuple') "
                    "AND query ~* :pattern"
                ),
                {"pattern": f"FROM {table}\\s.*FOR UPDATE"},
            ).scalar_one()
        )


def wait_for_lock_waiter(table: str) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while lock_waiters(table) < 1:
        if time.monotonic() > deadline:
            raise AssertionError(f"mutation never blocked on the {table} row lock")
        time.sleep(0.05)


def row_snapshot(table: str, row_id: UUID) -> dict[str, Any]:
    with engine.connect() as connection:
        return dict(
            connection.execute(
                text(f"SELECT * FROM {table} WHERE id = :id"),  # noqa: S608 -- test literal
                {"id": row_id},
            )
            .mappings()
            .one()
        )


def race(
    w: RaceWorld,
    *,
    table: str,
    row_id: UUID,
    send: Callable[[TestClient], httpx.Response],
    transfer: bool,
) -> tuple[httpx.Response, dict[str, Any]]:
    """Holds `table`'s `row_id` lock in a separate transaction (optionally
    transferring the row to C there), calls `send` with a client signed in
    as B, waits until that request is blocked on the lock, then commits.
    Returns the response and the row as it was before the transfer."""
    client = TestClient(app)
    client.cookies.set("ecc_session", w.b_token)
    result: dict[str, Any] = {}

    def fire() -> None:
        try:
            result["response"] = send(client)
        except BaseException as exc:  # surfaced on the main thread below
            result["error"] = exc

    try:
        holder = engine.connect()
        holder_tx = holder.begin()
        thread = threading.Thread(target=fire)
        try:
            holder.execute(
                text(f"SELECT id FROM {table} WHERE id = :id FOR UPDATE"),  # noqa: S608
                {"id": row_id},
            )
            before = row_snapshot(table, row_id)
            if transfer:
                holder.execute(
                    text(f"UPDATE {table} SET owner_id = :c WHERE id = :id"),  # noqa: S608
                    {"id": row_id, "c": w.c},
                )
            thread.start()
            wait_for_lock_waiter(table)
            holder_tx.commit()
        finally:
            if holder_tx.is_active:
                holder_tx.rollback()
            holder.close()
            thread.join(timeout=WAIT_SECONDS)
        assert not thread.is_alive(), "mutation request never finished"
        if "error" in result:
            raise result["error"]
        return result["response"], before
    finally:
        client.close()
