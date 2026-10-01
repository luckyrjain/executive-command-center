"""Meeting-prep writes against a concurrent meeting ownership transfer.

`add_participant`, `create_prep` and `refresh_prep` authorized the caller
against the meeting and then read the meeting row without a lock, so nothing
serialized them against `authz_grants`' transfer (which locks the meeting
`FOR UPDATE` and rewrites `owner_id`). A transfer that committed between the
check and the write left the caller writing a participant or pack into a
meeting it could no longer see. With meeting-prep AI enrichment on, the
window was seconds long: the pack insert ran in a later transaction, after
the model call, with no recheck at all.

Now each write locks the meeting row (`FOR SHARE`) before authorizing, and
the enrichment path re-locks and re-authorizes in its final transaction.

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
from sqlalchemy import Connection, text

from ecc.config import get_settings
from ecc.database import engine
from ecc.domains.attention import meeting_prep as meeting_prep_module
from ecc.domains.attention.meeting_prep import EnrichmentOut
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
    meeting's original owner) and C (`member`, the transfer target). The
    meeting is private, so once it moves to C, B can no longer see it."""

    ws: UUID
    a: UUID
    b: UUID
    c: UUID
    b_token: str
    meeting_id: UUID
    entity_id: UUID


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
    meeting_id, entity_id = uuid4(), uuid4()
    b_token = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Meeting Prep Lock Race', 'UTC', :now)"
            ),
            {"id": ws, "now": now},
        )
        create_identity(connection, workspace_id=ws, user_id=a, now=now)
        create_identity(connection, workspace_id=ws, user_id=b, now=now, role="admin")
        create_identity(connection, workspace_id=ws, user_id=c, now=now, role="member")
        _session(connection, ws, b, b_token, now)
        connection.execute(
            text(
                """
                INSERT INTO meetings (
                    id, workspace_id, title, standalone_starts_at, standalone_ends_at,
                    standalone_timezone, status, agenda, created_by, updated_by,
                    created_at, updated_at, version, owner_id, visibility
                ) VALUES (
                    :id, :ws, 'Race review', :starts_at, :ends_at, 'UTC',
                    'planned', 'Race agenda', :b, :b, :now, :now, 1, :b, 'private'
                )
                """
            ),
            {
                "id": meeting_id,
                "ws": ws,
                "b": b,
                "starts_at": now + timedelta(days=1),
                "ends_at": now + timedelta(days=1, hours=1),
                "now": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO pkos_nodes (id, workspace_id, node_type, canonical_name, "
                "created_at, updated_at, owner_id) "
                "VALUES (:id, :ws, 'person', 'Race Attendee', :now, :now, :b)"
            ),
            {"id": entity_id, "ws": ws, "b": b, "now": now},
        )
    try:
        yield RaceWorld(
            ws=ws, a=a, b=b, c=c, b_token=b_token, meeting_id=meeting_id, entity_id=entity_id
        )
    finally:
        with engine.begin() as connection:
            account_ids = list(
                connection.execute(
                    text("SELECT account_id FROM users WHERE workspace_id = :ws"), {"ws": ws}
                ).scalars()
            )
            for table in (
                "meeting_packs",
                "meeting_participants",
                "pkos_nodes",
                "meetings",
                "ai_runs",
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


def _client(w: RaceWorld) -> TestClient:
    client = TestClient(app)
    client.cookies.set("ecc_session", w.b_token)
    return client


@dataclass(frozen=True)
class Case:
    path: str
    body: dict[str, Any] | None
    needs_pack: bool


CASES: dict[str, Case] = {
    "add_participant": Case("/api/v1/meetings/{m}/participants", {"entity_id": "{e}"}, False),
    "create_prep": Case("/api/v1/meetings/{m}/prep", None, False),
    "refresh_prep": Case("/api/v1/meetings/{m}/prep/refresh", None, True),
}


def _post(client: TestClient, w: RaceWorld, case: Case) -> Any:
    body = (
        {k: v.format(e=w.entity_id) for k, v in case.body.items()}
        if case.body is not None
        else None
    )
    return client.post(case.path.format(m=w.meeting_id), headers=_headers(w.b_token), json=body)


def _seed_pack(w: RaceWorld) -> UUID:
    client = _client(w)
    try:
        response = client.post(f"/api/v1/meetings/{w.meeting_id}/prep", headers=_headers(w.b_token))
    finally:
        client.close()
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


def _writes(ws: UUID) -> dict[str, Any]:
    with engine.connect() as connection:
        return {
            "participants": int(
                connection.execute(
                    text("SELECT count(*) FROM meeting_participants WHERE workspace_id = :ws"),
                    {"ws": ws},
                ).scalar_one()
            ),
            "packs": sorted(
                (str(r["id"]), r["status"])
                for r in connection.execute(
                    text("SELECT id, status FROM meeting_packs WHERE workspace_id = :ws"),
                    {"ws": ws},
                ).mappings()
            ),
            "audit_events": int(
                connection.execute(
                    text("SELECT count(*) FROM audit_events WHERE workspace_id = :ws"),
                    {"ws": ws},
                ).scalar_one()
            ),
        }


def _meeting_owner(meeting_id: UUID) -> UUID:
    with engine.connect() as connection:
        return UUID(
            str(
                connection.execute(
                    text("SELECT owner_id FROM meetings WHERE id = :id"), {"id": meeting_id}
                ).scalar_one()
            )
        )


def _lock_waiters() -> int:
    with engine.connect() as probe:
        return int(
            probe.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                    "AND wait_event IN ('transactionid', 'tuple') "
                    "AND query ~* :pattern"
                ),
                {"pattern": r"FROM meetings\s.*FOR SHARE"},
            ).scalar_one()
        )


def _wait_for_lock_waiter() -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    while _lock_waiters() < 1:
        if time.monotonic() > deadline:
            raise AssertionError("write never blocked on the meetings row lock")
        time.sleep(0.05)


def _race(w: RaceWorld, case: Case, *, transfer: bool) -> Any:
    """Holds the meeting row lock in a separate transaction (optionally
    transferring the meeting to C there, as `authz_grants`' transfer does),
    fires B's write, waits until it is blocked on that lock, then commits."""
    client = _client(w)
    result: dict[str, Any] = {}

    def fire() -> None:
        try:
            result["response"] = _post(client, w, case)
        except BaseException as exc:  # surfaced on the main thread below
            result["error"] = exc

    try:
        holder = engine.connect()
        holder_tx = holder.begin()
        thread = threading.Thread(target=fire)
        try:
            holder.execute(
                text("SELECT id FROM meetings WHERE id = :id FOR UPDATE"), {"id": w.meeting_id}
            )
            if transfer:
                holder.execute(
                    text("UPDATE meetings SET owner_id = :c WHERE id = :id"),
                    {"id": w.meeting_id, "c": w.c},
                )
            thread.start()
            _wait_for_lock_waiter()
            holder_tx.commit()
        finally:
            if holder_tx.is_active:
                holder_tx.rollback()
            holder.close()
            thread.join(timeout=_WAIT_SECONDS)
        assert not thread.is_alive(), "write request never finished"
        if "error" in result:
            raise result["error"]
        return result["response"]
    finally:
        client.close()


@pytest.mark.parametrize("name", list(CASES))
def test_write_waiting_on_meeting_lock_rechecks_authorization(
    race_world: RaceWorld, name: str
) -> None:
    w, case = race_world, CASES[name]
    if case.needs_pack:
        _seed_pack(w)
    before = _writes(w.ws)

    response = _race(w, case, transfer=True)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "MEETING_NOT_FOUND"
    assert _meeting_owner(w.meeting_id) == w.c
    assert _writes(w.ws) == before


@pytest.mark.parametrize("name", list(CASES))
def test_write_waiting_on_meeting_lock_without_transfer_still_proceeds(
    race_world: RaceWorld, name: str
) -> None:
    """Control: the same lock wait with no ownership change succeeds -- the
    404 above comes from the transfer, not from the wait itself."""
    w, case = race_world, CASES[name]
    if case.needs_pack:
        _seed_pack(w)

    response = _race(w, case, transfer=False)

    assert response.status_code == 201, response.text


@pytest.mark.parametrize("name", ["create_prep", "refresh_prep"])
def test_enrichment_path_rechecks_authorization_before_writing_pack(
    race_world: RaceWorld, name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With enrichment on, authorization and the pack write sit in separate
    transactions with the model call between them. A transfer committed
    during that call must stop the write."""
    w, case = race_world, CASES[name]
    if case.needs_pack:
        _seed_pack(w)
    before = _writes(w.ws)

    def transfer_during_enrichment(*_args: Any, **_kwargs: Any) -> EnrichmentOut:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE meetings SET owner_id = :c WHERE id = :id"),
                {"id": w.meeting_id, "c": w.c},
            )
        return EnrichmentOut(available=False, summary=None, error_code="model_unavailable")

    monkeypatch.setenv("ECC_MEETING_PREP_AI_ENRICHMENT_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(meeting_prep_module, "_resolve_ollama_adapter", lambda _request: None)
    monkeypatch.setattr(meeting_prep_module, "_compute_enrichment", transfer_during_enrichment)
    client = _client(w)
    try:
        response = _post(client, w, case)
    finally:
        client.close()
        get_settings.cache_clear()

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "MEETING_NOT_FOUND"
    assert _meeting_owner(w.meeting_id) == w.c
    assert _writes(w.ws) == before
