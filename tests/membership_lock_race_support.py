"""Shared harness for membership-lock race tests (ADR-0014).

Removal and role change (`identity/membership_removal.py`) take the
workspace's membership-mutation advisory lock exclusively. A write that
does not take its shared side first (`authz.lock_membership_for_write`)
can pass `authorize()`, have the caller demoted to `viewer` or removed,
see that commit, and still commit the write afterwards.

`race()` reproduces that interleaving for real: a separate connection takes
the membership lock exclusively and applies the change in the same
transaction (as `membership_removal` does), the request is fired on a
thread, the harness waits until `pg_blocking_pids` shows the request
waiting on an advisory lock this holder holds, then commits. Fixed code
blocks, then re-authorizes against the committed membership and writes
nothing. Unfixed code never blocks and writes.

`unlocked_transactions()` is the AST guard each adopting domain runs over
its modules, so a new write transaction that forgets the lock fails CI.
"""

from __future__ import annotations

import ast
import threading
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from identity_fixtures import create_identity
from sqlalchemy import Connection, text

from ecc.config import get_settings
from ecc.database import engine
from ecc.main import app
from ecc.platform.connector_security import membership_mutation_lock_key

WAIT_SECONDS = 15
DOMAINS = Path(__file__).resolve().parents[1] / "backend" / "ecc" / "domains"

Change = Literal["demote", "remove"]
# An expected refusal: (status, error code).
Refusal = tuple[int, str]
FORBIDDEN: Refusal = (403, "INSUFFICIENT_ROLE")
ROLE_GATED: dict[Change, Refusal | None] = {"demote": FORBIDDEN, "remove": FORBIDDEN}


def row_refusals(not_found: str) -> dict[Change, Refusal | None]:
    """A per-row write: a viewer still sees the row (403), a removed member
    sees nothing (404)."""
    return {"demote": FORBIDDEN, "remove": (404, not_found)}


@dataclass
class RaceWorld:
    """One workspace: A (`owner`, owns every seeded row) and B (`member`,
    the racing caller). Seeded rows are `workspace`-visible, so B's write
    access comes from its role alone: as `viewer` B may still read them but
    not write, and once removed B sees nothing."""

    ws: UUID
    a: UUID
    b: UUID
    b_token: str


Ids = dict[str, UUID]
Seed = Callable[[Connection, RaceWorld, datetime], Ids]


@dataclass(frozen=True)
class Case:
    seed: Seed
    method: str
    path: str  # formatted with the seed's ids
    body: Callable[[Ids], dict[str, Any] | None]
    ok_status: int
    # Expected refusal per membership change; `None` means the change does
    # not revoke this write.
    refusals: dict[Change, Refusal | None] = field(default_factory=dict)


def refusal_params(cases: dict[str, Case]) -> list[tuple[str, Change]]:
    return [
        (name, change)
        for name, case in cases.items()
        for change in ("demote", "remove")
        if case.refusals.get(change) is not None
    ]


def seed_nothing(_conn: Connection, _w: RaceWorld, _now: datetime) -> Ids:
    return {}


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
    """Creates the workspace, A and B; on exit deletes every row in
    `cleanup_tables` (in the given order, children first) for the
    workspace, then the identity rows."""
    ws, a, b = uuid4(), uuid4(), uuid4()
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
        create_identity(connection, workspace_id=ws, user_id=b, now=now, role="member")
        _insert_session(connection, ws, b, b_token, now)
    try:
        yield RaceWorld(ws=ws, a=a, b=b, b_token=b_token)
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


def client_for(w: RaceWorld) -> TestClient:
    client = TestClient(app)
    client.cookies.set("ecc_session", w.b_token)
    return client


def seed(w: RaceWorld, case: Case) -> Ids:
    with engine.begin() as connection:
        return case.seed(connection, w, datetime.now(UTC))


def fingerprint(ws: UUID, tables: Iterable[str]) -> dict[str, str | None]:
    """Every listed table's full contents for the workspace, so an UPDATE is
    caught as well as an INSERT. `audit_events` leaves out the
    `task.mutation_rejected` rows `rejected_mutation_audit_middleware`
    writes after a refused `/api/v1/tasks` request's transaction: they are
    the expected trace of the refusal, not a side effect of the write."""
    with engine.connect() as connection:
        return {
            table: connection.execute(
                text(
                    f"SELECT md5(string_agg(t::text, '|' ORDER BY t::text)) "  # noqa: S608
                    f"FROM {table} t WHERE t.workspace_id = :ws"
                    + (
                        " AND t.event_type <> 'task.mutation_rejected'"
                        if table == "audit_events"
                        else ""
                    )
                ),
                {"ws": ws},
            ).scalar_one()
            for table in tables
        }


def membership_waiters(holder_pid: int) -> int:
    """Backends waiting on an advisory lock `holder_pid` (this test's
    membership-lock holder) holds, so an unrelated backend cannot count."""
    with engine.connect() as probe:
        return int(
            probe.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                    "AND wait_event = 'advisory' "
                    "AND pg_blocking_pids(pid) @> ARRAY[CAST(:holder AS integer)]"
                ),
                {"holder": holder_pid},
            ).scalar_one()
        )


def _wait_until_blocked_or_done(holder_pid: int, thread: threading.Thread) -> bool:
    """True once the request is blocked on the membership lock. False if it
    finished without ever blocking (the unfixed behavior), so the caller's
    status assertion reports what the request actually did."""
    deadline = time.monotonic() + WAIT_SECONDS
    while membership_waiters(holder_pid) < 1:
        if not thread.is_alive():
            return False
        if time.monotonic() > deadline:
            raise AssertionError("write neither blocked on the membership lock nor finished")
        time.sleep(0.05)
    return True


def _apply_change(conn: Connection, w: RaceWorld, change: Change | None) -> None:
    if change == "demote":
        conn.execute(
            text(
                "UPDATE workspace_memberships SET role = 'viewer', updated_at = now() "
                "WHERE workspace_id = :ws AND users_id = :b"
            ),
            {"ws": w.ws, "b": w.b},
        )
    elif change == "remove":
        conn.execute(
            text(
                "UPDATE workspace_memberships SET status = 'removed', removed_at = now(), "
                "updated_at = now() WHERE workspace_id = :ws AND users_id = :b"
            ),
            {"ws": w.ws, "b": w.b},
        )


def race(w: RaceWorld, case: Case, ids: Ids, *, change: Change | None) -> tuple[Any, bool]:
    """Takes the membership lock exclusively in a separate transaction and
    applies `change` there (as `membership_removal` does), fires B's write,
    waits until it is blocked on that lock, then commits. Returns the
    response and whether the write blocked."""
    client = client_for(w)
    result: dict[str, Any] = {}

    def fire() -> None:
        try:
            result["response"] = client.request(
                case.method,
                case.path.format(**ids),
                headers=headers(w.b_token),
                json=case.body(ids),
            )
        except BaseException as exc:  # surfaced on the main thread below
            result["error"] = exc

    try:
        holder = engine.connect()
        holder_tx = holder.begin()
        thread = threading.Thread(target=fire)
        try:
            holder_pid = int(holder.execute(text("SELECT pg_backend_pid()")).scalar_one())
            holder.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": membership_mutation_lock_key(w.ws)},
            )
            _apply_change(holder, w, change)
            thread.start()
            blocked = _wait_until_blocked_or_done(holder_pid, thread)
            holder_tx.commit()
        finally:
            if holder_tx.is_active:
                holder_tx.rollback()
            holder.close()
            if thread.ident is not None:  # a setup failure must not be masked
                thread.join(timeout=WAIT_SECONDS)
        assert not thread.is_alive(), "write request never finished"
        if "error" in result:
            raise result["error"]
        return result["response"], blocked
    finally:
        client.close()


def assert_refused(
    w: RaceWorld, case: Case, ids: Ids, change: Change, tables: Iterable[str]
) -> None:
    """The race with `change` answers the case's refusal, blocked on the
    lock, and leaves every listed table unchanged."""
    tables = tuple(tables)
    before = fingerprint(w.ws, tables)

    response, blocked = race(w, case, ids, change=change)

    expected = case.refusals[change]
    assert expected is not None
    assert response.status_code == expected[0], response.text
    assert response.json()["error"]["code"] == expected[1]
    assert blocked
    assert fingerprint(w.ws, tables) == before


def assert_proceeds(w: RaceWorld, case: Case, ids: Ids) -> None:
    """Control: the same lock wait with no membership change succeeds -- the
    refusals come from the change, not from the wait. Also shows the write
    takes the lock at all."""
    response, blocked = race(w, case, ids, change=None)

    assert response.status_code == case.ok_status, response.text
    assert blocked


# ---------------------------------------------------------------------------
# Adoption guard
# ---------------------------------------------------------------------------

_LOCK_CALL = "authz.lock_membership_for_write("


def _session_calls(fn: ast.AST) -> list[ast.Call]:
    """Calls that touch the session: `session.<method>(...)` or any call
    passing `session` as an argument, in source order."""
    calls = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        on_session = (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "session"
        )
        passes_session = any(
            isinstance(arg, ast.Name) and arg.id == "session"
            for arg in (*node.args, *(kw.value for kw in node.keywords))
        )
        if on_session or passes_session:
            calls.append(node)
    return sorted(calls, key=lambda call: (call.lineno, call.col_offset))


def unlocked_transactions(paths: Iterable[Path]) -> Counter[tuple[str, str]]:
    """(module, function) -> number of write transactions that do not take
    the membership lock first. Two shapes are checked:

    - every `with session.begin():` block must start with the lock call;
    - a function that commits an autobegun transaction itself
      (`session.commit()` outside any `session.begin()` block) must make
      the lock call its first session-touching call.
    """
    found: Counter[tuple[str, str]] = Counter()
    for path in sorted(paths):
        tree = ast.parse(path.read_text(), filename=str(path))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            key = (path.name, fn.name)
            begin_blocks = [
                node
                for node in ast.walk(fn)
                if isinstance(node, ast.With)
                and any(ast.unparse(item.context_expr) == "session.begin()" for item in node.items)
            ]
            for block in begin_blocks:
                if not ast.unparse(block.body[0]).startswith(_LOCK_CALL):
                    found[key] += 1
            if begin_blocks:
                continue
            commits = any(ast.unparse(call) == "session.commit()" for call in _session_calls(fn))
            if not commits:
                continue
            first = _session_calls(fn)[0]
            if not ast.unparse(first).startswith(_LOCK_CALL):
                found[key] += 1
    return found
