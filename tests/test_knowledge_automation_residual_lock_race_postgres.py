"""Lock-before-authorize sweep, the sites the per-domain fixes (#317-#324)
did not cover: resolution candidate confirm/defer, entity merge / reverse /
split (both the operation row and the entity pair) and workflow run
cancel/pause/resume all authorized the caller *before* taking
`SELECT ... FOR UPDATE` on the row the decision is about. An ownership
transfer (`authz_grants` transfer locks the row, rewrites `owner_id`, and
does not bump `version`) that committed while the request waited on that
row lock left the request writing to a row the caller could no longer see.

Now each row is locked first and authorization is evaluated afterwards, on
the committed post-transfer row, so the waiting request answers 404 and
writes nothing.

Concurrency is real (request thread + separate connection); "is waiting" is
observed in `pg_stat_activity` (scoped to backends blocked by this test's
lock holder), not assumed from timing.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from json import dumps
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from identity_fixtures import create_identity
from sqlalchemy import Connection, text

from ecc.config import get_settings
from ecc.database import engine
from ecc.domains.automation import workflows as automation_workflows
from ecc.main import app

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

settings = get_settings()
Snapshot = dict[str, list[dict[str, Any]]]
_WAIT_SECONDS = 15

# Every table a seed below writes a row the racing caller must not see once
# it is transferred -- all flipped to `private` after seeding.
_PRIVATE_TABLES = (
    "pkos_nodes",
    "pkos_evidence",
    "pkos_edges",
    "entity_aliases",
    "resolution_candidates",
    "entity_operations",
    "workflow_definitions",
    "workflow_versions",
    "workflow_runs",
)


@dataclass
class RaceWorld:
    """One workspace: A (`owner`), B (`admin`, the racing caller and the
    rows' original owner) and C (`member`, the transfer target)."""

    ws: UUID
    a: UUID
    b: UUID
    c: UUID
    b_token: str


def _session_row(
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


def _workspace_tables() -> list[str]:
    with engine.connect() as connection:
        return list(
            connection.execute(
                text(
                    "SELECT c.table_name FROM information_schema.columns c "
                    "JOIN information_schema.tables t USING (table_schema, table_name) "
                    "WHERE c.table_schema = 'public' AND c.column_name = 'workspace_id' "
                    "AND t.table_type = 'BASE TABLE' AND c.table_name <> 'workspaces'"
                )
            ).scalars()
        )


def _delete_workspace(ws: UUID) -> None:
    """Deletes every workspace-scoped row, retrying in passes so foreign
    keys between the many tables these seeds touch resolve without a
    hand-maintained order."""
    with engine.connect() as connection:
        account_ids = list(
            connection.execute(
                text("SELECT account_id FROM users WHERE workspace_id = :ws"), {"ws": ws}
            ).scalars()
        )
    pending = _workspace_tables()
    for _ in range(10):
        failed: list[str] = []
        with engine.begin() as connection:
            for table in pending:
                savepoint = connection.begin_nested()
                try:
                    connection.execute(
                        text(f"DELETE FROM {table} WHERE workspace_id = :ws"),  # noqa: S608
                        {"ws": ws},
                    )
                    savepoint.commit()
                except Exception:  # noqa: BLE001 -- FK order, retried next pass
                    savepoint.rollback()
                    failed.append(table)
        if not failed:
            break
        pending = failed
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM workspaces WHERE id = :ws"), {"ws": ws})
        connection.execute(text("DELETE FROM accounts WHERE id = ANY(:ids)"), {"ids": account_ids})


@pytest.fixture
def race_world() -> Iterator[RaceWorld]:
    ws, a, b, c = uuid4(), uuid4(), uuid4(), uuid4()
    b_token = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Lock Before Authorize Race', 'UTC', :now)"
            ),
            {"id": ws, "now": now},
        )
        create_identity(connection, workspace_id=ws, user_id=a, now=now)
        create_identity(connection, workspace_id=ws, user_id=b, now=now, role="admin")
        create_identity(connection, workspace_id=ws, user_id=c, now=now, role="member")
        _session_row(connection, ws, b, b_token, now)
    try:
        yield RaceWorld(ws=ws, a=a, b=b, c=c, b_token=b_token)
    finally:
        _delete_workspace(ws)


def _headers(token: str) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    return {
        "X-CSRF-Token": csrf,
        "X-Correlation-ID": str(uuid4()),
        "Idempotency-Key": str(uuid4()),
    }


def _client(w: RaceWorld) -> TestClient:
    client = TestClient(app)
    client.cookies.set("ecc_session", w.b_token)
    return client


def _api(w: RaceWorld, path: str, body: dict[str, Any]) -> dict[str, Any]:
    client = _client(w)  # no `with`: skip lifespan startup work
    try:
        response = client.post(path, headers=_headers(w.b_token), json=body)
    finally:
        client.close()
    assert response.status_code in (200, 201), response.text
    return dict(response.json())


def _sql(sql: str, **params: Any) -> None:
    with engine.begin() as connection:
        connection.execute(text(sql), params)


# ---------------------------------------------------------------------------
# Seeds: each inserts rows owned by B and returns the ids the request path
# needs. `id` is always the row the race locks and transfers.
# ---------------------------------------------------------------------------


def _node(w: RaceWorld, name: str = "Race Entity") -> UUID:
    node_id = uuid4()
    _sql(
        "INSERT INTO pkos_nodes (id, workspace_id, node_type, canonical_name, "
        "created_at, updated_at, owner_id) VALUES (:id, :ws, 'person', :name, now(), now(), :b)",
        id=node_id,
        ws=w.ws,
        name=name,
        b=w.b,
    )
    return node_id


def _candidate(w: RaceWorld, status: str = "open") -> dict[str, UUID]:
    left, right, candidate_id = _node(w, "Ada Lovelace"), _node(w, "Ada Lovelase"), uuid4()
    _sql(
        "INSERT INTO resolution_candidates (id, workspace_id, left_entity_id, right_entity_id, "
        "score, factors_json, resolver_version, status, created_at, owner_id) "
        "VALUES (:id, :ws, :left, :right, 0.9, '{}'::jsonb, 'race-v1', :status, now(), :b)",
        id=candidate_id,
        ws=w.ws,
        left=left,
        right=right,
        status=status,
        b=w.b,
    )
    return {"id": candidate_id, "left": left, "right": right}


def _merged(w: RaceWorld) -> dict[str, UUID]:
    """A real merge (through the API) the reverse/split cases act on."""
    pair = _candidate(w, status="confirmed")
    operation = _api(
        w,
        "/api/v1/knowledge/entities/merge",
        {
            "candidate_id": str(pair["id"]),
            "target_entity_id": str(pair["left"]),
            "expected_target_version": 1,
            "expected_source_version": 1,
            "reason": "confirmed duplicate",
        },
    )
    return {"operation": UUID(operation["id"]), "target": pair["left"], "source": pair["right"]}


def _workflow_version(w: RaceWorld, status: str, workflow_id: str | None = None) -> UUID:
    workflow_id = workflow_id or f"race.workflow.{uuid4().hex[:8]}"
    graph = {
        "steps": [
            {
                "step_id": "s1",
                "step_type": "condition",
                "input_mapping": {},
                "on_success": "succeeded",
                "on_failure": "failed",
            }
        ]
    }
    version_id = uuid4()
    _sql(
        "INSERT INTO workflow_definitions (id, workspace_id, workflow_id, created_by, "
        "created_at, updated_at, owner_id) VALUES (:id, :ws, :wf, :b, now(), now(), :b)",
        id=uuid4(),
        ws=w.ws,
        wf=workflow_id,
        b=w.b,
    )
    _sql(
        "INSERT INTO workflow_versions (id, workspace_id, workflow_id, version, graph, "
        "trigger_refs, policy_ref, definition_hash, status, created_by, updated_by, "
        "created_at, updated_at, owner_id) VALUES (:id, :ws, :wf, 1, CAST(:graph AS jsonb), "
        "'[]'::jsonb, NULL, :digest, :status, :b, :b, now(), now(), :b)",
        id=version_id,
        ws=w.ws,
        wf=workflow_id,
        graph=dumps(graph),
        digest=automation_workflows.compute_definition_hash(
            graph=graph, trigger_refs=[], policy_ref=None
        ),
        status=status,
        b=w.b,
    )
    return version_id


def _run(w: RaceWorld, status: str) -> UUID:
    run_id = uuid4()
    _workflow_version(w, "active", "race.workflow")
    _sql(
        "INSERT INTO workflow_runs (id, workspace_id, workflow_id, workflow_version, status, "
        "queued_at, created_by, created_at, updated_at, owner_id) "
        "VALUES (:id, :ws, 'race.workflow', 1, :status, now(), :b, now(), now(), :b)",
        id=run_id,
        ws=w.ws,
        status=status,
        b=w.b,
    )
    return run_id


def _seed_merge(w: RaceWorld) -> dict[str, UUID]:
    pair = _candidate(w, status="confirmed")
    # The target entity is the row the race transfers.
    return {"id": pair["left"], "candidate": pair["id"], "source": pair["right"]}


def _seed_merged_by_operation(w: RaceWorld) -> dict[str, UUID]:
    merged = _merged(w)
    return {"id": merged["operation"]}


def _seed_merged_by_entity(w: RaceWorld) -> dict[str, UUID]:
    merged = _merged(w)
    return {"id": merged["source"], "operation": merged["operation"]}


def _one(seed: Callable[[RaceWorld], UUID]) -> Callable[[RaceWorld], dict[str, UUID]]:
    return lambda w: {"id": seed(w)}


@dataclass(frozen=True)
class Case:
    table: str  # the table of the row `id` names -- locked and transferred
    seed: Callable[[RaceWorld], dict[str, UUID]]
    path: str  # formatted with the seed's ids
    body: dict[str, Any] | None
    not_found: str
    method: str = "POST"
    ok_status: int = 200  # uncontested success, asserted by the control


_FUTURE = (datetime.now(UTC) + timedelta(days=3)).isoformat()


CASES: dict[str, Case] = {
    "candidate_confirm": Case(
        "resolution_candidates",
        _candidate,
        "/api/v1/knowledge/resolution/candidates/{id}/confirm",
        {"reason": "race probe"},
        "CANDIDATE_NOT_FOUND",
    ),
    "candidate_defer": Case(
        "resolution_candidates",
        _candidate,
        "/api/v1/knowledge/resolution/candidates/{id}/defer",
        {"deferred_until": _FUTURE},
        "CANDIDATE_NOT_FOUND",
    ),
    "merge_target_entity": Case(
        "pkos_nodes",
        _seed_merge,
        "/api/v1/knowledge/entities/merge",
        None,  # see _body
        "ENTITY_NOT_FOUND",
        ok_status=201,
    ),
    "reverse_operation": Case(
        "entity_operations",
        _seed_merged_by_operation,
        "/api/v1/knowledge/entity-operations/{id}/reverse",
        {"reason": "race probe"},
        "OPERATION_NOT_FOUND",
        ok_status=201,
    ),
    "reverse_source_entity": Case(
        "pkos_nodes",
        _seed_merged_by_entity,
        "/api/v1/knowledge/entity-operations/{operation}/reverse",
        {"reason": "race probe"},
        "ENTITY_NOT_FOUND",
        ok_status=201,
    ),
    "split_operation": Case(
        "entity_operations",
        _seed_merged_by_operation,
        "/api/v1/knowledge/entity-operations/{id}/split",
        {"reason": "race probe"},
        "OPERATION_NOT_FOUND",
        ok_status=201,
    ),
    "split_source_entity": Case(
        "pkos_nodes",
        _seed_merged_by_entity,
        "/api/v1/knowledge/entity-operations/{operation}/split",
        {"reason": "race probe"},
        "ENTITY_NOT_FOUND",
        ok_status=201,
    ),
    "run_cancel": Case(
        "workflow_runs",
        _one(lambda w: _run(w, "queued")),
        "/api/v1/automations/runs/{id}/cancel",
        None,
        "RUN_NOT_FOUND",
    ),
    "run_pause": Case(
        "workflow_runs",
        _one(lambda w: _run(w, "running")),
        "/api/v1/automations/runs/{id}/pause",
        None,
        "RUN_NOT_FOUND",
    ),
    "run_resume": Case(
        "workflow_runs",
        _one(lambda w: _run(w, "paused")),
        "/api/v1/automations/runs/{id}/resume",
        None,
        "RUN_NOT_FOUND",
    ),
}


def _body(name: str, case: Case, ids: dict[str, UUID]) -> dict[str, Any] | None:
    if name == "merge_target_entity":
        return {
            "candidate_id": str(ids["candidate"]),
            "target_entity_id": str(ids["id"]),
            "expected_target_version": 1,
            "expected_source_version": 1,
            "reason": "race probe",
        }
    return case.body


def _seed(w: RaceWorld, case: Case) -> dict[str, UUID]:
    ids = case.seed(w)
    with engine.begin() as connection:
        for table in _PRIVATE_TABLES:
            connection.execute(
                text(f"UPDATE {table} SET visibility = 'private' WHERE workspace_id = :ws"),  # noqa: S608
                {"ws": w.ws},
            )
    return ids


def _lock_waiters(table: str, holder_pid: int) -> int:
    """Backends blocked on `holder_pid` (this test's lock holder, so an
    unrelated concurrent backend cannot satisfy the wait) in a locking read
    of `table`."""
    with engine.connect() as probe:
        return int(
            probe.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                    "AND pg_blocking_pids(pid) @> ARRAY[CAST(:holder AS integer)] "
                    "AND query ~* :pattern"
                ),
                {"holder": holder_pid, "pattern": f"FROM {table}\\s.*FOR (NO KEY )?UPDATE"},
            ).scalar_one()
        )


def _wait_for_lock_waiter(table: str, holder_pid: int) -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    while _lock_waiters(table, holder_pid) < 1:
        if time.monotonic() > deadline:
            raise AssertionError(f"mutation never blocked on the {table} row lock")
        time.sleep(0.05)


def _row_snapshot(table: str, row_id: UUID) -> dict[str, Any]:
    with engine.connect() as connection:
        return dict(
            connection.execute(
                text(f"SELECT * FROM {table} WHERE id = :id"),  # noqa: S608 -- CASES literal
                {"id": row_id},
            )
            .mappings()
            .one()
        )


def _workspace_snapshot(ws: UUID, connection: Connection | None = None) -> Snapshot:
    """Every row of every workspace-scoped table except the per-request
    bookkeeping ones -- any write the request made shows up here."""
    if connection is None:
        with engine.connect() as own:
            return _workspace_snapshot(ws, own)
    snapshot: Snapshot = {}
    for table in sorted(set(_workspace_tables()) - {"idempotency_records", "sessions"}):
        rows = connection.execute(
            text(f"SELECT * FROM {table} WHERE workspace_id = :ws"),  # noqa: S608
            {"ws": ws},
        ).mappings()
        snapshot[table] = sorted((dict(row) for row in rows), key=str)
    return snapshot


def _race(
    w: RaceWorld, name: str, case: Case, ids: dict[str, UUID], *, transfer: bool
) -> tuple[Any, Snapshot]:
    """Holds the row lock in a separate transaction (optionally transferring
    the row to C there), fires B's mutation, waits until it is blocked on
    that lock, then commits. Returns the response and the workspace
    snapshot taken right after the transfer committed."""
    client = _client(w)
    result: dict[str, Any] = {}

    def fire() -> None:
        try:
            result["response"] = client.request(
                case.method,
                case.path.format(**ids),
                headers=_headers(w.b_token),
                json=_body(name, case, ids),
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
                {"id": ids["id"]},
            )
            if transfer:
                holder.execute(
                    text(f"UPDATE {case.table} SET owner_id = :c WHERE id = :id"),  # noqa: S608
                    {"id": ids["id"], "c": w.c},
                )
            # Snapshot through the holder (the transfer included) before the
            # request starts, so nothing it writes can be in it and the
            # snapshot does not eat into its statement timeout.
            before = _workspace_snapshot(w.ws, holder)
            thread.start()
            _wait_for_lock_waiter(case.table, holder_pid)
            holder_tx.commit()
        finally:
            if holder_tx.is_active:
                holder_tx.rollback()
            holder.close()
            if thread.ident is not None:  # a setup failure must not be masked
                thread.join(timeout=_WAIT_SECONDS)
        assert not thread.is_alive(), "mutation request never finished"
        if "error" in result:
            raise result["error"]
        return result["response"], before
    finally:
        client.close()


@pytest.mark.parametrize("name", list(CASES))
def test_mutation_waiting_on_row_lock_rechecks_authorization(
    race_world: RaceWorld, name: str
) -> None:
    w, case = race_world, CASES[name]
    ids = _seed(w, case)

    response, before = _race(w, name, case, ids, transfer=True)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == case.not_found
    assert _row_snapshot(case.table, ids["id"])["owner_id"] == w.c
    after = _workspace_snapshot(w.ws)
    # A rejected mutation is itself audited (`*.mutation_rejected`); that
    # row records the refusal and is the one write allowed here.
    after["audit_events"] = [
        row for row in after["audit_events"] if row["authorization_result"] != "rejected"
    ]
    assert after == before


@pytest.mark.parametrize("name", list(CASES))
def test_mutation_waiting_on_row_lock_without_transfer_still_proceeds(
    race_world: RaceWorld, name: str
) -> None:
    """Control: the same lock wait with no ownership change succeeds and
    writes -- the 404 above comes from the transfer, not from the wait."""
    w, case = race_world, CASES[name]
    ids = _seed(w, case)

    response, before = _race(w, name, case, ids, transfer=False)

    assert response.status_code == case.ok_status, response.text
    assert _workspace_snapshot(w.ws) != before
