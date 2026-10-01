"""scripts/backfill_personal_visibility.py -- batch retry (FX2 deep review
INT-2 / DBA-D2): a batch that hits a deadlock (40P01), lock timeout (55P03)
or statement timeout (57014) is rolled back and retried from the same
keyset cursor, counted once.

The race test drives a real grant revoke by the resource's owner into a
batch that holds its row locks (the integrity probe's harness: the request
runs in a thread while the batch pauses in `_active_grants`); whichever
side PostgreSQL picks as the deadlock victim, the run converges.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from typing import Any
from uuid import UUID, uuid4

import psycopg
import pytest
import test_backfill_personal_visibility_derived_rows_postgres as base
from gmail_sync_fixtures import GmailSyncWorld, csrf_headers, gmail_sync_world_factory  # noqa: F401
from sqlalchemy import Engine, event, text
from sqlalchemy.exc import IntegrityError, OperationalError
from test_backfill_personal_visibility_derived_rows_postgres import (
    _FIELDS,
    _confirm,
    _counts,
    _email_rec,
    _flag,
    _grant,
    _grant_revoked,
    _log_rows,
    _owner_vis,
    _run,
)

from ecc.config import get_settings
from ecc.database import engine

backfill = base.backfill
pytestmark = base.pytestmark


@pytest.fixture(autouse=True)
def _drop_cached_settings() -> Iterator[None]:
    yield
    get_settings.cache_clear()


@pytest.fixture
def run_ids() -> Iterator[list[UUID]]:
    ids: list[UUID] = []
    yield ids
    if ids:
        with engine.begin() as conn:
            conn.execute(
                text("DELETE FROM personal_visibility_backfill_log WHERE run_id = ANY(:ids)"),
                {"ids": ids},
            )


@pytest.fixture
def world(
    monkeypatch: pytest.MonkeyPatch,
    gmail_sync_world_factory: Any,  # noqa: F811
) -> GmailSyncWorld:
    monkeypatch.setenv("ECC_DATABASE_URL", base.settings.database_url)
    monkeypatch.delenv("ECC_PERSONAL_DATA_ISOLATION", raising=False)
    get_settings.cache_clear()
    built: GmailSyncWorld = gmail_sync_world_factory(
        bystander=True,
        extra_cleanup_tables=("resource_grants", "recommendation_feedback", "attention_feedback"),
    )
    return built


def _commitment_confirmed_by_b(world: GmailSyncWorld) -> UUID:
    rec = _email_rec(world, world.a.user_id, "commitment", fields=_FIELDS["commitment"])
    return _confirm(world, world.b.user_id, rec)


@pytest.mark.parametrize(
    "error",
    [
        psycopg.errors.DeadlockDetected,
        psycopg.errors.LockNotAvailable,
        psycopg.errors.QueryCanceled,
    ],
)
def test_a_retryable_batch_failure_is_retried_and_counted_once(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
    error: type[psycopg.Error],
) -> None:
    """The commitments batch fails once after doing all its work (log
    insert, grant revoke, update) -- rolled back, then retried: the run
    succeeds, the counts equal a clean run's, one log row, grant revoked."""
    commitment = _commitment_confirmed_by_b(world)
    grant = _grant(world, "commitments", commitment)
    monkeypatch.setattr(backfill, "RETRY_DELAYS_SECONDS", (0.0, 0.0, 0.0, 0.0))
    original = backfill._process_batch
    failures = {"left": 1}

    def fail_once(conn: Any, table: str, *args: Any, **kwargs: Any) -> Any:
        result = original(conn, table, *args, **kwargs)
        if table == "commitments" and failures["left"]:
            failures["left"] -= 1
            raise OperationalError("simulated", {}, error("simulated"))
        return result

    monkeypatch.setattr(backfill, "_process_batch", fail_once)
    _flag(monkeypatch, on=True)
    code, rows, err, run_id = _run(capsys, run_ids, "--workspace-id", str(world.workspace_id))
    assert code in (backfill.EXIT_CLEAN, backfill.EXIT_UNRESOLVED), err
    assert failures["left"] == 0
    assert f"sqlstate={error.sqlstate}" in err and "retry 1/4" in err
    assert _counts(rows)["commitments"] == (1, 1, 0)
    assert run_id is not None
    assert _log_rows(run_id)[("commitments", commitment)] == ("workspace", world.b.user_id, 1)
    assert _owner_vis("commitments", commitment) == (world.a.user_id, "private")
    assert _grant_revoked(grant)


def _failing_batches(
    monkeypatch: pytest.MonkeyPatch, error: psycopg.Error, table: str = "commitments"
) -> dict[str, int]:
    """Every `table` batch raises `error` (wrapped as SQLAlchemy does)."""
    attempts = {"n": 0}
    original = backfill._process_batch

    def failing(conn: Any, batch_table: str, *args: Any, **kwargs: Any) -> Any:
        if batch_table == table:
            attempts["n"] += 1
            wrapper = (
                IntegrityError if isinstance(error, psycopg.IntegrityError) else OperationalError
            )
            raise wrapper("simulated", {}, error)
        return original(conn, batch_table, *args, **kwargs)

    monkeypatch.setattr(backfill, "_process_batch", failing)
    return attempts


def test_exhausted_retries_abort_after_the_documented_backoff(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """QA3-6: a deadlock every time -> 5 attempts, sleeping 0.5/1/2/4 s in
    between (recorded, not slept), then exit 2 with nothing changed."""
    commitment = _commitment_confirmed_by_b(world)
    slept: list[float] = []
    monkeypatch.setattr(backfill.time, "sleep", slept.append)
    attempts = _failing_batches(monkeypatch, psycopg.errors.DeadlockDetected("x"))
    _flag(monkeypatch, on=True)
    code, _rows, err, _ = _run(capsys, run_ids, "--workspace-id", str(world.workspace_id))
    assert code == backfill.EXIT_ERROR
    assert attempts["n"] == 5
    assert slept == [0.5, 1.0, 2.0, 4.0]
    assert "sqlstate=40P01" in err
    assert _owner_vis("commitments", commitment) == (world.b.user_id, "workspace")


def test_a_non_retryable_error_is_not_retried(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """QA3-6: a unique violation (23505) is not in RETRYABLE_SQLSTATES: one
    attempt, exit 2."""
    _commitment_confirmed_by_b(world)
    attempts = _failing_batches(monkeypatch, psycopg.errors.UniqueViolation("x"))
    _flag(monkeypatch, on=True)
    code, _rows, err, _ = _run(capsys, run_ids, "--workspace-id", str(world.workspace_id))
    assert code == backfill.EXIT_ERROR
    assert attempts["n"] == 1
    assert "sqlstate=23505" in err and "retry" not in err


def _join(thread: threading.Thread | None, seconds: float = 30.0) -> None:
    """Joins a race thread with a timeout: a thread still running is a
    failure (a hung lock), never an indefinite hang of the suite."""
    assert thread is not None, "the race was never started"
    thread.join(seconds)
    assert not thread.is_alive(), f"race thread still running after {seconds}s"


class _Paused:
    """Runs `action` in a thread the first time the batch reaches `table`'s
    grant lock (inside the batch transaction: after the unlocked candidate
    read, BEFORE the grant and row locks), holding the batch for `hold`
    seconds -- so the lock ORDER decides whether the two can deadlock."""

    def __init__(self, table: str, action: Callable[[], Any], hold: float) -> None:
        self.table, self.action, self.hold = table, action, hold
        self.thread: threading.Thread | None = None
        self.result: list[Any] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        original = backfill._lock_grants

        def hooked(conn: Any, table: str, ws: UUID, ids: list[UUID]) -> Any:
            if table == self.table and self.thread is None:
                self.thread = threading.Thread(target=self._run, daemon=True)
                self.thread.start()
                time.sleep(self.hold)
            return original(conn, table, ws, ids)

        monkeypatch.setattr(backfill, "_lock_grants", hooked)

    def _run(self) -> None:
        try:
            self.result.append(self.action())
        except Exception as exc:  # noqa: BLE001 -- reported by the test
            self.result.append(exc)


@pytest.mark.parametrize("attempt", range(3))
def test_grant_revoke_during_a_batch_waits_and_never_deadlocks(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
    attempt: int,
) -> None:
    """INT-3 (integrity probe): B (the commitment's owner, not the granter)
    revokes A's grant while the batch holds its locks. The revoke locks the
    grant, then the resource row; the batch now locks grants before rows
    (same order), so the revoke waits for the batch and then finds the grant
    already revoked (409) -- never a deadlock victim (500)."""
    del attempt
    commitment = _commitment_confirmed_by_b(world)
    grant = _grant(world, "commitments", commitment)  # granted by A
    client, token = world.harness.client_for(world.workspace_id, world.b.user_id)
    paused = _Paused(
        "commitments",
        lambda: client.delete(
            f"/api/v1/sharing/grants/{grant}", headers=csrf_headers(token, str(uuid4()))
        ),
        hold=1.0,
    )
    paused.install(monkeypatch)
    _flag(monkeypatch, on=True)
    code, rows, err, run_id = _run(capsys, run_ids, "--workspace-id", str(world.workspace_id))
    _join(paused.thread)
    response = paused.result[0]
    assert not isinstance(response, Exception), response
    assert response.status_code in (200, 204, 409), response.text
    assert "retry" not in err  # the batch was not a victim either
    assert code in (backfill.EXIT_CLEAN, backfill.EXIT_UNRESOLVED), err
    assert _owner_vis("commitments", commitment) == (world.a.user_id, "private")
    assert _grant_revoked(grant)
    assert run_id is not None
    logged = _log_rows(run_id)[("commitments", commitment)]
    assert logged[:2] == ("workspace", world.b.user_id)
    assert _counts(rows)["commitments"][1] == logged[2]


def _attention_item_of(task: UUID) -> tuple[UUID, str] | None:
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT owner_id, visibility FROM attention_items "
                "WHERE entity_type = 'task' AND entity_id = :id"
            ),
            {"id": task},
        ).one_or_none()
    return None if row is None else (row[0], row[1])


def test_regenerate_racing_a_batch_cannot_revert_the_attention_mirror(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """INT-4 (integrity probe): a regenerate that read the task (B's,
    workspace) before the batch, paused before its upsert, must not write
    those values back over the batch's mirror. The batch takes the
    regenerate lock, so it waits for the regenerate, then mirrors."""
    import ecc.domains.attention.attention as attention

    a, b, ws = world.a, world.b, world.workspace_id
    task = _confirm(world, b.user_id, a.recommendation_ids[0])
    world.harness.regenerate_attention(workspace_id=ws, user_id=b.user_id)
    assert _attention_item_of(task) == (b.user_id, "workspace")
    reached, release = threading.Event(), threading.Event()
    original: Any = attention._upsert_batch

    def paused_upsert(session: Any, auth: Any, entity_type: str, *args: Any, **kw: Any) -> Any:
        if entity_type == "task":
            reached.set()
            release.wait(30)
        return original(session, auth, entity_type, *args, **kw)

    monkeypatch.setattr(attention, "_upsert_batch", paused_upsert)
    assert world.bystander_user_id is not None
    client, token = world.harness.client_for(ws, world.bystander_user_id)
    regenerate = threading.Thread(
        target=lambda: client.post(
            "/api/v1/attention/regenerate", headers=csrf_headers(token), json={}
        ),
        daemon=True,
    )
    regenerate.start()
    assert reached.wait(30)
    threading.Timer(1.0, release.set).start()  # the batch is waiting on the lock by then
    _flag(monkeypatch, on=True)
    code, _rows, err, _ = _run(capsys, run_ids, "--workspace-id", str(ws))
    _join(regenerate)
    assert code in (backfill.EXIT_CLEAN, backfill.EXIT_UNRESOLVED), err
    assert _owner_vis("tasks", task) == (a.user_id, "private")
    assert _attention_item_of(task) == (a.user_id, "private")


def test_grants_are_locked_in_id_order_before_the_rows(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """QA3-8 / R21: the grant lock is `ORDER BY id FOR UPDATE` (one stable
    order for every batch and a revoke) and comes before the row lock."""
    commitment = _commitment_confirmed_by_b(world)
    _grant(world, "commitments", commitment)
    statements: list[str] = []

    def record(_conn: Any, _cursor: Any, statement: str, *_args: Any) -> None:
        statements.append(statement)

    # The command builds its own engine: listen on every Engine.
    event.listen(Engine, "before_cursor_execute", record)
    _flag(monkeypatch, on=True)
    try:
        _run(capsys, run_ids, "--workspace-id", str(world.workspace_id))
    finally:
        event.remove(Engine, "before_cursor_execute", record)
    grant_locks = [
        i
        for i, sql in enumerate(statements)
        if "FROM resource_grants" in sql and "FOR UPDATE" in sql
    ]
    row_locks = [i for i, sql in enumerate(statements) if "FOR UPDATE OF commitments" in sql]
    assert grant_locks and row_locks
    assert all("ORDER BY id FOR UPDATE" in statements[i] for i in grant_locks)
    assert any(g < r for g in grant_locks for r in row_locks)


def test_a_row_that_leaves_the_set_before_the_lock_is_skipped(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """QA3-8 / R18: between the unlocked candidate read and the locked
    re-read, the commitment's source loses its `execution_result`
    (simulated): the predicate re-check drops it -- not changed, not logged."""
    commitment = _commitment_confirmed_by_b(world)
    original = backfill._lock_grants

    def drop_source(conn: Any, table: str, ws: UUID, ids: list[UUID]) -> Any:
        if table == "commitments":
            with engine.begin() as other:
                other.execute(
                    text(
                        "UPDATE recommendations SET execution_result = NULL "
                        "WHERE workspace_id = :ws AND execution_result ->> 'target_id' = :id"
                    ),
                    {"ws": ws, "id": str(commitment)},
                )
        return original(conn, table, ws, ids)

    monkeypatch.setattr(backfill, "_lock_grants", drop_source)
    _flag(monkeypatch, on=True)
    code, rows, err, run_id = _run(capsys, run_ids, "--workspace-id", str(world.workspace_id))
    assert code in (backfill.EXIT_CLEAN, backfill.EXIT_UNRESOLVED), err
    assert _owner_vis("commitments", commitment) == (world.b.user_id, "workspace")
    assert run_id is not None and ("commitments", commitment) not in _log_rows(run_id)
    assert _counts(rows)["commitments"] == (0, 0, 0)


def test_the_batch_holds_its_row_locks_while_deciding(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """QA3-3 / R20: while the batch decides, a concurrent owner change of
    its row cannot land (it waits, then times out) -- the decision holds."""
    commitment = _commitment_confirmed_by_b(world)
    original = backfill._decide
    outcomes: list[str | None] = []

    def contested(conn: Any, table: str, *args: Any, **kwargs: Any) -> Any:
        if table == "commitments" and not outcomes:
            try:
                with engine.begin() as other:
                    other.execute(text("SET LOCAL lock_timeout = '500ms'"))
                    other.execute(
                        text("UPDATE commitments SET owner_id = :by WHERE id = :id"),
                        {"by": world.bystander_user_id, "id": commitment},
                    )
                outcomes.append(None)
            except OperationalError as exc:
                outcomes.append(getattr(exc.orig, "sqlstate", None))
        return original(conn, table, *args, **kwargs)

    monkeypatch.setattr(backfill, "_decide", contested)
    _flag(monkeypatch, on=True)
    code, _rows, err, _ = _run(capsys, run_ids, "--workspace-id", str(world.workspace_id))
    assert code in (backfill.EXIT_CLEAN, backfill.EXIT_UNRESOLVED), err
    assert outcomes == ["55P03"]
    assert _owner_vis("commitments", commitment) == (world.a.user_id, "private")


def _paused_regenerate(
    world: GmailSyncWorld, monkeypatch: pytest.MonkeyPatch
) -> tuple[threading.Thread, threading.Event]:
    """Starts `POST /attention/regenerate` (as the bystander) and pauses it
    before its task upsert -- it has read the tasks and holds its lock.
    Returns the thread and the event that releases it."""
    import ecc.domains.attention.attention as attention

    reached, release = threading.Event(), threading.Event()
    original: Any = attention._upsert_batch

    def paused_upsert(session: Any, auth: Any, entity_type: str, *args: Any, **kw: Any) -> Any:
        if entity_type == "task":
            reached.set()
            release.wait(30)
        return original(session, auth, entity_type, *args, **kw)

    monkeypatch.setattr(attention, "_upsert_batch", paused_upsert)
    assert world.bystander_user_id is not None
    client, token = world.harness.client_for(world.workspace_id, world.bystander_user_id)
    thread = threading.Thread(
        target=lambda: client.post(
            "/api/v1/attention/regenerate", headers=csrf_headers(token), json={}
        ),
        daemon=True,
    )
    thread.start()
    assert reached.wait(30)
    threading.Timer(1.0, release.set).start()  # the restore is waiting on the lock by then
    return thread, release


def test_regenerate_racing_a_restore_cannot_revert_the_attention_mirror(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """QA3-4 / R14: a regenerate that read the task while it was (A,
    private) must not overwrite the restore's mirror: the attention item
    ends up matching the restored task (B, workspace)."""
    a, b, ws = world.a, world.b, world.workspace_id
    task = _confirm(world, b.user_id, a.recommendation_ids[0])
    world.harness.regenerate_attention(workspace_id=ws, user_id=b.user_id)
    _flag(monkeypatch, on=True)
    _code, _rows, _err, run_id = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert run_id is not None and _attention_item_of(task) == (a.user_id, "private")
    _flag(monkeypatch, on=False)
    regenerate, _release = _paused_regenerate(world, monkeypatch)
    code, _rows, err, _ = _run(capsys, run_ids, "--restore", str(run_id))
    _join(regenerate)
    assert _owner_vis("tasks", task) == (b.user_id, "workspace"), err
    assert _attention_item_of(task) == (b.user_id, "workspace")


def test_a_restore_page_that_times_out_on_a_lock_is_retried(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """INT-5: another session holds the attention-regenerate lock past the
    restore's lock_timeout once; the page is rolled back, retried, and the
    restore converges."""
    task = _confirm(world, world.b.user_id, world.a.recommendation_ids[0])
    _flag(monkeypatch, on=True)
    _code, _rows, _err, run_id = _run(capsys, run_ids, "--workspace-id", str(world.workspace_id))
    assert run_id is not None
    monkeypatch.setattr(backfill, "_LOCK_TIMEOUT", "200ms")
    monkeypatch.setattr(backfill, "RETRY_DELAYS_SECONDS", (0.5, 0.5, 0.5))
    _flag(monkeypatch, on=False)
    holder = engine.connect()
    holder.begin()
    holder.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"attention-regenerate:{world.workspace_id}"},
    )
    # Release the lock from the retry's own backoff sleep, i.e. only after
    # the first attempt has really timed out -- a wall-clock timer could
    # fire before a slow (loaded) restore ever reaches the lock.
    real_sleep = time.sleep

    def release_then_sleep(seconds: float) -> None:
        if not holder.closed:
            holder.close()  # rolls back: the lock goes
        real_sleep(seconds)

    monkeypatch.setattr(backfill.time, "sleep", release_then_sleep)
    try:
        code, _rows, err, _ = _run(capsys, run_ids, "--restore", str(run_id))
    finally:
        if not holder.closed:
            holder.close()
    assert "restore page rolled back (sqlstate=55P03)" in err
    assert code == backfill.EXIT_CLEAN, err
    assert _owner_vis("tasks", task) == (world.b.user_id, "workspace")


def test_a_dismiss_during_the_restore_waits_for_it(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """INT-7: the restore locks the task's attention items before it reads
    their snapshot; A's dismiss arriving in that window waits for the
    restore (it can neither change the item between the check and the
    update nor be lost)."""
    a, b, ws = world.a, world.b, world.workspace_id
    task = _confirm(world, b.user_id, a.recommendation_ids[0])
    world.harness.regenerate_attention(workspace_id=ws, user_id=b.user_id)
    with engine.begin() as conn:
        item = conn.execute(
            text("SELECT id FROM attention_items WHERE entity_type = 'task' AND entity_id = :t"),
            {"t": task},
        ).scalar_one()
    _flag(monkeypatch, on=True)
    _code, _rows, _err, run_id = _run(capsys, run_ids, "--workspace-id", str(ws))
    assert run_id is not None
    _flag(monkeypatch, on=False)
    client, token = world.harness.client_for(ws, a.user_id)
    paused = _Paused(
        "tasks",
        lambda: client.post(
            f"/api/v1/attention/{item}/dismiss", json={}, headers=csrf_headers(token, str(uuid4()))
        ),
        hold=1.0,
    )
    original = backfill._lock_attention_items
    blocked: list[bool] = []

    def hooked(conn: Any, table: str, row_ids: list[UUID]) -> None:
        original(conn, table, row_ids)
        if table == "tasks" and paused.thread is None:
            paused.thread = threading.Thread(target=paused._run, daemon=True)
            paused.thread.start()
            time.sleep(paused.hold)
            blocked.append(paused.thread.is_alive())

    monkeypatch.setattr(backfill, "_lock_attention_items", hooked)
    code, rows, err, _ = _run(capsys, run_ids, "--restore", str(run_id))
    _join(paused.thread)
    assert blocked == [True], "the dismiss did not wait for the restore's lock"
    assert ("tasks", task) not in {
        (r["table_name"], UUID(r["row_id"])) for r in rows if r["record"] == "unresolved"
    }, err
    assert _owner_vis("tasks", task) == (b.user_id, "workspace")


def test_a_dismiss_during_the_backfill_batch_waits_for_it(
    world: GmailSyncWorld,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    run_ids: list[UUID],
) -> None:
    """QA5-2 (INT-7, backfill side): the batch locks the task's attention
    items before it snapshots them; B's dismiss fired while the batch is in
    `_snapshots` waits for the batch to commit."""
    b, ws = world.b, world.workspace_id
    task = _confirm(world, b.user_id, world.a.recommendation_ids[0])
    world.harness.regenerate_attention(workspace_id=ws, user_id=b.user_id)
    with engine.begin() as conn:
        item = conn.execute(
            text("SELECT id FROM attention_items WHERE entity_type = 'task' AND entity_id = :t"),
            {"t": task},
        ).scalar_one()
    client, token = world.harness.client_for(ws, b.user_id)
    paused = _Paused(
        "tasks",
        lambda: client.post(
            f"/api/v1/attention/{item}/dismiss", json={}, headers=csrf_headers(token, str(uuid4()))
        ),
        hold=1.0,
    )
    original = backfill._snapshots
    blocked: list[bool] = []

    def hooked(conn: Any, table: str, row_ids: list[UUID]) -> Any:
        if table == "tasks" and paused.thread is None:
            paused.thread = threading.Thread(target=paused._run, daemon=True)
            paused.thread.start()
            time.sleep(paused.hold)
            blocked.append(paused.thread.is_alive())
        return original(conn, table, row_ids)

    monkeypatch.setattr(backfill, "_snapshots", hooked)
    _flag(monkeypatch, on=True)
    code, _rows, err, _ = _run(capsys, run_ids, "--workspace-id", str(ws))
    _join(paused.thread)
    assert code in (backfill.EXIT_CLEAN, backfill.EXIT_UNRESOLVED), err
    assert blocked == [True], "the dismiss did not wait for the batch's lock"
    assert _owner_vis("tasks", task) == (world.a.user_id, "private")
