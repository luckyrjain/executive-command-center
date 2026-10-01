"""Self-test for `lock_race_support`'s lock-waiter probe.

`race()` commits its lock holder as soon as the probe reports a waiter. If
the probe counted any backend blocked on a matching locking read -- another
test sharing the database, a different row of the same table -- the holder
could commit before the request under test ever queued, and a race test
would pass without exercising the race. The probe is scoped to backends
blocked by the holder (`pg_blocking_pids`); this proves a decoy waiter
blocked by a different backend does not count.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from uuid import uuid4

import pytest
from lock_race_support import holder_backend_pid, lock_waiters, wait_for_lock_waiter
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import engine

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


@pytest.fixture
def probe_table() -> Iterator[str]:
    # A real table (not TEMP): the blocked waiter runs on its own connection.
    table = f"lock_race_probe_{uuid4().hex}"
    with engine.begin() as connection:
        connection.execute(text(f"CREATE TABLE {table} (id integer PRIMARY KEY)"))
        connection.execute(text(f"INSERT INTO {table} (id) VALUES (1), (2)"))  # noqa: S608
    try:
        yield table
    finally:
        with engine.begin() as connection:
            connection.execute(text(f"DROP TABLE IF EXISTS {table}"))


def _blocked_lock(table: str, row_id: int, done: threading.Event) -> threading.Thread:
    def run() -> None:
        with engine.connect() as waiter, waiter.begin():
            waiter.execute(
                text(f"SELECT id FROM {table} WHERE id = :id FOR UPDATE"),  # noqa: S608
                {"id": row_id},
            )
        done.set()

    thread = threading.Thread(target=run)
    thread.start()
    return thread


def test_lock_waiter_probe_ignores_waiters_blocked_by_another_backend(probe_table: str) -> None:
    holder = engine.connect()
    decoy = engine.connect()
    holder_tx = holder.begin()
    decoy_tx = decoy.begin()
    threads: list[threading.Thread] = []
    try:
        holder_pid = holder_backend_pid(holder)
        decoy_pid = holder_backend_pid(decoy)
        lock = text(f"SELECT id FROM {probe_table} WHERE id = :id FOR UPDATE")  # noqa: S608
        holder.execute(lock, {"id": 1})
        decoy.execute(lock, {"id": 2})

        # A waiter on the decoy's row matches the table pattern, but is not
        # blocked by the holder, so it must not satisfy the holder's probe.
        decoy_waiter_done = threading.Event()
        threads.append(_blocked_lock(probe_table, 2, decoy_waiter_done))
        wait_for_lock_waiter(probe_table, holder_pid=decoy_pid)
        assert lock_waiters(probe_table, holder_pid=holder_pid) == 0

        # The waiter that really is blocked by the holder does.
        holder_waiter_done = threading.Event()
        threads.append(_blocked_lock(probe_table, 1, holder_waiter_done))
        wait_for_lock_waiter(probe_table, holder_pid=holder_pid)
        assert lock_waiters(probe_table, holder_pid=holder_pid) == 1
        assert lock_waiters(probe_table, holder_pid=decoy_pid) == 1
        assert not decoy_waiter_done.is_set()
        assert not holder_waiter_done.is_set()
    finally:
        decoy_tx.rollback()
        holder_tx.rollback()
        decoy.close()
        holder.close()
        for thread in threads:
            thread.join(timeout=15)
    assert not any(thread.is_alive() for thread in threads)
