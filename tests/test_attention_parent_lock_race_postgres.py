"""Parent-boundary TOCTOU on attention creates: recording attention feedback
and creating a waiting link authorize the caller against a *parent* row (the
attention item; the link's subject task / commitment / pkos node and its
counterparty pkos node) and then insert a child that references it. The
parents used to be read without a lock, so an ownership transfer
(`authz_grants` transfer locks the row `FOR UPDATE`, rewrites `owner_id`)
that committed in between left the caller writing a child under a parent
they could no longer see.

Now each parent is locked `FOR SHARE` (which conflicts with the transfer's
`FOR UPDATE`) before authorizing, so a request racing a transfer waits for
it, then authorizes against the committed post-transfer row: 404, nothing
inserted.

Concurrency is real (request thread + separate connection); "is waiting" is
observed in `pg_stat_activity`, not assumed from timing.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from identity_fixtures import create_identity
from sqlalchemy import Connection, text

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
    """One workspace: A (`owner`), B (`admin`, the racing caller and the
    parents' original owner) and C (`member`, the transfer target)."""

    ws: UUID
    a: UUID
    b: UUID
    c: UUID
    b_token: str


def _session(connection: Connection, ws: UUID, user_id: UUID, token: str, now: datetime) -> None:
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
                "VALUES (:id, 'Attention Parent Lock Race', 'UTC', :now)"
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
                "attention_feedback",
                "attention_items",
                "waiting_links",
                "tasks",
                "commitments",
                "pkos_nodes",
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


def _headers(token: str) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    return {
        "X-CSRF-Token": csrf,
        "X-Correlation-ID": str(uuid4()),
        "Idempotency-Key": str(uuid4()),
    }


# ---------------------------------------------------------------------------
# Seeds: each inserts B-owned private parents and returns their ids; `target`
# is the parent the race transfers.
# ---------------------------------------------------------------------------


def _node(conn: Connection, w: RaceWorld, now: datetime, node_type: str) -> UUID:
    node_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO pkos_nodes (id, workspace_id, node_type, canonical_name, "
            "created_at, updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, :node_type, 'Race node', :now, :now, :b, 'private')"
        ),
        {"id": node_id, "ws": w.ws, "node_type": node_type, "b": w.b, "now": now},
    )
    return node_id


def _task(conn: Connection, w: RaceWorld, now: datetime) -> UUID:
    task_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO tasks (id, workspace_id, owner_id, title, status, manual_priority, "
            "pinned, source_type, created_by, updated_by, created_at, updated_at, version, "
            "visibility) "
            "VALUES (:id, :ws, :b, 'Race task', 'planned', 'medium', false, 'local', "
            ":b, :b, :now, :now, 1, 'private')"
        ),
        {"id": task_id, "ws": w.ws, "b": w.b, "now": now},
    )
    return task_id


def _commitment(conn: Connection, w: RaceWorld, now: datetime) -> UUID:
    commitment_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO commitments (id, workspace_id, owner_id, summary, direction, "
            "status, importance, pinned, created_by, updated_by, created_at, updated_at, "
            "version, visibility) "
            "VALUES (:id, :ws, :b, 'Race commitment', 'made_by_me', 'active', 'medium', "
            "false, :b, :b, :now, :now, 1, 'private')"
        ),
        {"id": commitment_id, "ws": w.ws, "b": w.b, "now": now},
    )
    return commitment_id


def _seed_attention_item(conn: Connection, w: RaceWorld, now: datetime) -> dict[str, UUID]:
    item_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO attention_items (id, workspace_id, entity_type, entity_id, "
            "source_entity_version, score, confidence, factors, explanation, generated_at, "
            "expires_at, pinned, policy_version, owner_id, visibility) "
            "VALUES (:id, :ws, 'task', :entity_id, 1, 50, 0.9, '[]'::jsonb, 'race item', "
            ":now, :expires_at, false, 1, :b, 'private')"
        ),
        {
            "id": item_id,
            "ws": w.ws,
            "entity_id": uuid4(),
            "now": now,
            "expires_at": now + timedelta(days=1),
            "b": w.b,
        },
    )
    return {"target": item_id}


def _seed_task_subject(conn: Connection, w: RaceWorld, now: datetime) -> dict[str, UUID]:
    task_id = _task(conn, w, now)
    return {"target": task_id, "subject": task_id, "counterparty": _node(conn, w, now, "person")}


def _seed_commitment_subject(conn: Connection, w: RaceWorld, now: datetime) -> dict[str, UUID]:
    commitment_id = _commitment(conn, w, now)
    return {
        "target": commitment_id,
        "subject": commitment_id,
        "counterparty": _node(conn, w, now, "person"),
    }


def _seed_node_subject(conn: Connection, w: RaceWorld, now: datetime) -> dict[str, UUID]:
    subject = _node(conn, w, now, "project")
    return {"target": subject, "subject": subject, "counterparty": _node(conn, w, now, "person")}


def _seed_counterparty(conn: Connection, w: RaceWorld, now: datetime) -> dict[str, UUID]:
    counterparty = _node(conn, w, now, "person")
    return {"target": counterparty, "subject": _task(conn, w, now), "counterparty": counterparty}


def _link_body(subject_type: str) -> Callable[[dict[str, UUID]], dict[str, Any]]:
    def body(ids: dict[str, UUID]) -> dict[str, Any]:
        return {
            "subject_type": subject_type,
            "subject_id": str(ids["subject"]),
            "counterparty_entity_id": str(ids["counterparty"]),
            "direction": "waiting_on_them",
        }

    return body


@dataclass(frozen=True)
class Case:
    table: str  # the transferred parent's table
    child: str  # the table the request inserts into
    seed: Callable[[Connection, RaceWorld, datetime], dict[str, UUID]]
    path: str  # formatted with the seed's ids
    body: Callable[[dict[str, UUID]], dict[str, Any]]
    not_found: str


CASES: dict[str, Case] = {
    "attention_feedback": Case(
        "attention_items",
        "attention_feedback",
        _seed_attention_item,
        "/api/v1/attention/{target}/feedback",
        lambda _: {"label": "useful"},
        "ATTENTION_ITEM_NOT_FOUND",
    ),
    "waiting_subject_task": Case(
        "tasks",
        "waiting_links",
        _seed_task_subject,
        "/api/v1/waiting",
        _link_body("task"),
        "WAITING_SUBJECT_NOT_FOUND",
    ),
    "waiting_subject_commitment": Case(
        "commitments",
        "waiting_links",
        _seed_commitment_subject,
        "/api/v1/waiting",
        _link_body("commitment"),
        "WAITING_SUBJECT_NOT_FOUND",
    ),
    "waiting_subject_node": Case(
        "pkos_nodes",
        "waiting_links",
        _seed_node_subject,
        "/api/v1/waiting",
        _link_body("knowledge_entity"),
        "WAITING_SUBJECT_NOT_FOUND",
    ),
    "waiting_counterparty": Case(
        "pkos_nodes",
        "waiting_links",
        _seed_counterparty,
        "/api/v1/waiting",
        _link_body("task"),
        "WAITING_COUNTERPARTY_NOT_FOUND",
    ),
}


def _blocked_queries(holder_pid: int) -> list[str]:
    """Queries of backends blocked on `holder_pid` (this test's lock holder,
    so an unrelated concurrent backend cannot satisfy the wait)."""
    with engine.connect() as probe:
        return list(
            probe.execute(
                text(
                    "SELECT query FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                    "AND pg_blocking_pids(pid) @> ARRAY[CAST(:holder AS integer)]"
                ),
                {"holder": holder_pid},
            ).scalars()
        )


def _wait_for_blocked_or_done(holder_pid: int, thread: threading.Thread) -> list[str]:
    """Returns the blocked queries once the request blocks on the holder, or
    [] if it finishes without ever blocking (the unfixed unlocked read), so
    that case fails on the response rather than on a timeout."""
    deadline = time.monotonic() + _WAIT_SECONDS
    while True:
        blocked = _blocked_queries(holder_pid)
        if blocked or not thread.is_alive():
            return blocked
        if time.monotonic() > deadline:
            raise AssertionError("request neither blocked on the holder nor finished")
        time.sleep(0.05)


def _child_count(child: str, ws: UUID) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                text(f"SELECT count(*) FROM {child} WHERE workspace_id = :ws"),  # noqa: S608
                {"ws": ws},
            ).scalar_one()
        )


def _race(
    w: RaceWorld, case: Case, ids: dict[str, UUID], *, transfer: bool
) -> tuple[Any, list[str]]:
    """Holds the parent row `FOR UPDATE` in a separate transaction (as the
    transfer does, optionally rewriting `owner_id` to C there), fires B's
    create, waits until it is blocked on that lock, then commits."""
    client = TestClient(app)
    client.cookies.set("ecc_session", w.b_token)
    result: dict[str, Any] = {}

    def fire() -> None:
        try:
            result["response"] = client.post(
                case.path.format(**ids), headers=_headers(w.b_token), json=case.body(ids)
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
                text(f"SELECT id FROM {case.table} WHERE id = :id FOR UPDATE"),  # noqa: S608
                {"id": ids["target"]},
            )
            if transfer:
                holder.execute(
                    text(f"UPDATE {case.table} SET owner_id = :c WHERE id = :id"),  # noqa: S608
                    {"id": ids["target"], "c": w.c},
                )
            thread.start()
            blocked = _wait_for_blocked_or_done(holder_pid, thread)
            holder_tx.commit()
        finally:
            if holder_tx.is_active:
                holder_tx.rollback()
            holder.close()
            if thread.ident is not None:  # a setup failure must not be masked
                thread.join(timeout=_WAIT_SECONDS)
        assert not thread.is_alive(), "create request never finished"
        if "error" in result:
            raise result["error"]
        return result["response"], blocked
    finally:
        client.close()


def _assert_blocked_on_parent_share_lock(case: Case, blocked: list[str]) -> None:
    assert any(f"FROM {case.table} " in query and "FOR SHARE" in query for query in blocked), (
        blocked
    )


@pytest.mark.parametrize("name", list(CASES))
def test_create_waiting_on_parent_transfer_rechecks_authorization(
    race_world: RaceWorld, name: str
) -> None:
    w, case = race_world, CASES[name]
    with engine.begin() as connection:
        ids = case.seed(connection, w, datetime.now(UTC))
    children_before = _child_count(case.child, w.ws)

    response, blocked = _race(w, case, ids, transfer=True)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == case.not_found
    assert _child_count(case.child, w.ws) == children_before
    _assert_blocked_on_parent_share_lock(case, blocked)


@pytest.mark.parametrize("name", list(CASES))
def test_create_waiting_on_parent_lock_without_transfer_still_proceeds(
    race_world: RaceWorld, name: str
) -> None:
    """Control: the same lock wait with no ownership change succeeds and
    inserts -- the 404 above comes from the transfer, not from the wait."""
    w, case = race_world, CASES[name]
    with engine.begin() as connection:
        ids = case.seed(connection, w, datetime.now(UTC))
    children_before = _child_count(case.child, w.ws)

    response, blocked = _race(w, case, ids, transfer=False)

    assert response.status_code == 201, response.text
    assert _child_count(case.child, w.ws) == children_before + 1
    _assert_blocked_on_parent_share_lock(case, blocked)
