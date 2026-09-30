"""Meeting writes against concurrent changes to who may write the meeting.

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

Siblings of the same race, covered here too:

- `scheduling/meetings.py`'s PATCH, archive and restore authorized before
  taking their own `FOR UPDATE` on the meeting (a transfer does not bump
  `version`, so `expected_version` could not catch it). They now lock first.
- A grantor revoking their own grant never locked the resource row, so it
  did not wait for a writer holding the meeting lock: the revoke returned
  while a write it should have ordered after was still in flight. Every
  revoke now locks the resource row, before the grant row, so it cannot
  deadlock against member removal (which revokes grants after locking the
  member's rows).

Concurrency is real (request thread + separate connection); "is waiting" is
observed in `pg_stat_activity`, scoped to this test's own lock holder, not
assumed from timing.
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
from ecc.domains.attention import meeting_prep as meeting_prep_module
from ecc.domains.attention.meeting_prep import EnrichmentOut
from ecc.main import app

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

settings = get_settings()
_WAIT_SECONDS = 15
_ENRICHMENT_ENV = "ECC_MEETING_PREP_AI_ENRICHMENT_ENABLED"


@dataclass
class RaceWorld:
    """One workspace: A (`owner`), B (`admin`, the racing caller and the
    meeting's original owner) and C (`member`, the transfer target). The
    meeting is private, so once it moves to C, B can no longer see it."""

    ws: UUID
    a: UUID
    b: UUID
    c: UUID
    a_token: str
    b_token: str
    b_account: UUID
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
    a_token, b_token = f"session-{uuid4()}", f"session-{uuid4()}"
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
        _session(connection, ws, a, a_token, now)
        _session(connection, ws, b, b_token, now)
        b_account = UUID(
            str(
                connection.execute(
                    text("SELECT account_id FROM users WHERE id = :id"), {"id": b}
                ).scalar_one()
            )
        )
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
            ws=ws,
            a=a,
            b=b,
            c=c,
            a_token=a_token,
            b_token=b_token,
            b_account=b_account,
            meeting_id=meeting_id,
            entity_id=entity_id,
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
                "resource_grants",
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


def _client(token: str) -> TestClient:
    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    return client


def _set_enrichment(monkeypatch: pytest.MonkeyPatch, *, enabled: bool) -> list[UUID]:
    """Pins the enrichment flag (a developer `.env` must not flip which path
    runs); when on, stubs the model call so nothing reaches Ollama. Returns
    the meeting ids the stub was called for, so a test can tell whether a
    request got past the enrichment path's first transaction."""
    monkeypatch.setenv(_ENRICHMENT_ENV, "true" if enabled else "false")
    get_settings.cache_clear()
    calls: list[UUID] = []
    if enabled:

        def enrich(_session: Any, _auth: Any, meeting_id: UUID, **_kwargs: Any) -> EnrichmentOut:
            calls.append(meeting_id)
            return EnrichmentOut(available=False, summary=None, error_code="model_unavailable")

        monkeypatch.setattr(meeting_prep_module, "_resolve_ollama_adapter", lambda _request: None)
        monkeypatch.setattr(meeting_prep_module, "_compute_enrichment", enrich)
    return calls


@pytest.fixture(autouse=True)
def _reset_settings_cache() -> Iterator[None]:
    yield
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Lock-holder race harness
# ---------------------------------------------------------------------------


def _lock_waiters(holder_pid: int, pattern: str) -> int:
    """Backends blocked on `holder_pid` (this test's lock holder, so an
    unrelated concurrent backend cannot satisfy the wait) in a query matching
    `pattern`."""
    with engine.connect() as probe:
        return int(
            probe.execute(
                text(
                    "SELECT count(*) FROM pg_stat_activity "
                    "WHERE datname = current_database() AND wait_event_type = 'Lock' "
                    "AND pg_blocking_pids(pid) @> ARRAY[CAST(:holder AS integer)] "
                    "AND query ~* :pattern"
                ),
                {"holder": holder_pid, "pattern": pattern},
            ).scalar_one()
        )


def _wait_for_lock_waiter(holder_pid: int, pattern: str) -> None:
    deadline = time.monotonic() + _WAIT_SECONDS
    while _lock_waiters(holder_pid, pattern) < 1:
        if time.monotonic() > deadline:
            raise AssertionError(f"request never blocked on the meetings row lock ({pattern})")
        time.sleep(0.05)


_LOCK_FOR_UPDATE = "SELECT id FROM meetings WHERE id = :id FOR UPDATE"
_LOCK_FOR_SHARE = "SELECT id FROM meetings WHERE id = :id FOR SHARE"
_PREP_WAIT = r"FROM meetings\s.*FOR SHARE"
_UPDATE_WAIT = r"FROM meetings\s.*FOR UPDATE"


def _race(
    token: str,
    method: str,
    path: str,
    body: dict[str, Any] | None,
    *,
    holder_lock: str,
    lock_id: UUID,
    holder_writes: Callable[[Connection], None] | None,
    wait_pattern: str,
    after_wait: Callable[[Connection], None] | None = None,
) -> Any:
    """Holds a meetings row lock in a separate transaction (optionally
    changing who may write the meeting there), fires the request, waits until
    it is blocked on that lock, then commits."""
    client = _client(token)
    result: dict[str, Any] = {}

    def fire() -> None:
        try:
            result["response"] = client.request(method, path, headers=_headers(token), json=body)
        except BaseException as exc:  # surfaced on the main thread below
            result["error"] = exc

    try:
        holder = engine.connect()
        holder_tx = holder.begin()
        thread = threading.Thread(target=fire)
        try:
            holder_pid = int(holder.execute(text("SELECT pg_backend_pid()")).scalar_one())
            holder.execute(text(holder_lock), {"id": lock_id})
            if holder_writes is not None:
                holder_writes(holder)
            thread.start()
            _wait_for_lock_waiter(holder_pid, wait_pattern)
            if after_wait is not None:
                after_wait(holder)
            holder_tx.commit()
        finally:
            if holder_tx.is_active:
                holder_tx.rollback()
            holder.close()
            if thread.ident is not None:  # a setup failure must not be masked
                thread.join(timeout=_WAIT_SECONDS)
        assert not thread.is_alive(), "request never finished"
        if "error" in result:
            raise result["error"]
        return result["response"]
    finally:
        client.close()


def _transfer_private(w: RaceWorld) -> Callable[[Connection], None]:
    """What `authz_grants`' transfer does to the row: B loses sight of it."""

    def write(conn: Connection) -> None:
        conn.execute(
            text("UPDATE meetings SET owner_id = :c WHERE id = :id"),
            {"id": w.meeting_id, "c": w.c},
        )

    return write


def _transfer_read_only(w: RaceWorld) -> Callable[[Connection], None]:
    """B keeps sight of the meeting through a read-only grant but loses the
    right to write it -- pins the post-lock *write* check, not just read."""

    def write(conn: Connection) -> None:
        conn.execute(
            text(
                "UPDATE meetings SET owner_id = :c, visibility = 'shared_explicitly' WHERE id = :id"
            ),
            {"id": w.meeting_id, "c": w.c},
        )
        conn.execute(
            text(
                "INSERT INTO resource_grants (id, workspace_id, grantee_account_id, "
                "resource_type, resource_id, actions, granted_by, created_at) "
                "VALUES (:id, :ws, :account, 'meetings', :meeting, ARRAY['read'], :c, now())"
            ),
            {
                "id": uuid4(),
                "ws": w.ws,
                "account": w.b_account,
                "meeting": w.meeting_id,
                "c": w.c,
            },
        )

    return write


_TRANSFERS: dict[str, tuple[Callable[[RaceWorld], Callable[[Connection], None]], int, str]] = {
    "loses_read": (_transfer_private, 404, "MEETING_NOT_FOUND"),
    "loses_write": (_transfer_read_only, 403, "INSUFFICIENT_ROLE"),
}


# ---------------------------------------------------------------------------
# Meeting-prep writes (attention/meeting_prep.py)
# ---------------------------------------------------------------------------


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


def _path(w: RaceWorld, case: Case) -> str:
    return case.path.format(m=w.meeting_id)


def _body(w: RaceWorld, case: Case) -> dict[str, Any] | None:
    if case.body is None:
        return None
    return {k: v.format(e=w.entity_id) for k, v in case.body.items()}


def _seed_pack(w: RaceWorld) -> UUID:
    client = _client(w.b_token)
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


def _meeting_row(meeting_id: UUID) -> dict[str, Any]:
    with engine.connect() as connection:
        return dict(
            connection.execute(text("SELECT * FROM meetings WHERE id = :id"), {"id": meeting_id})
            .mappings()
            .one()
        )


_PREP_RACES = [
    pytest.param(name, transfer, enrichment, id=f"{name}-{transfer}-enrichment_{enrichment}")
    for name in CASES
    for transfer in _TRANSFERS
    for enrichment in ((False, True) if name != "add_participant" else (False,))
]


@pytest.mark.parametrize(("name", "transfer", "enrichment"), _PREP_RACES)
def test_prep_write_waiting_on_meeting_lock_rechecks_authorization(
    race_world: RaceWorld,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    transfer: str,
    enrichment: bool,
) -> None:
    w, case = race_world, CASES[name]
    make_writes, status, code = _TRANSFERS[transfer]
    _set_enrichment(monkeypatch, enabled=False)
    if case.needs_pack:
        _seed_pack(w)
    enrichment_calls = _set_enrichment(monkeypatch, enabled=enrichment)
    before = _writes(w.ws)

    response = _race(
        w.b_token,
        "POST",
        _path(w, case),
        _body(w, case),
        holder_lock=_LOCK_FOR_UPDATE,
        lock_id=w.meeting_id,
        holder_writes=make_writes(w),
        wait_pattern=_PREP_WAIT,
    )

    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    assert _meeting_row(w.meeting_id)["owner_id"] == w.c
    assert _writes(w.ws) == before
    # The enrichment path must reject in its first transaction, under the
    # lock -- not run the model call and get caught only by the final recheck.
    assert enrichment_calls == []


_PREP_CONTROLS = [
    pytest.param(name, enrichment, id=f"{name}-enrichment_{enrichment}")
    for name in CASES
    for enrichment in ((False, True) if name != "add_participant" else (False,))
]


@pytest.mark.parametrize(("name", "enrichment"), _PREP_CONTROLS)
def test_prep_write_waiting_on_meeting_lock_without_transfer_still_writes(
    race_world: RaceWorld, monkeypatch: pytest.MonkeyPatch, name: str, enrichment: bool
) -> None:
    """Control: the same lock wait with no ownership change succeeds and
    writes -- the 404/403 above come from the transfer, not the wait."""
    w, case = race_world, CASES[name]
    _set_enrichment(monkeypatch, enabled=False)
    old_pack = _seed_pack(w) if case.needs_pack else None
    enrichment_calls = _set_enrichment(monkeypatch, enabled=enrichment)
    before = _writes(w.ws)

    response = _race(
        w.b_token,
        "POST",
        _path(w, case),
        _body(w, case),
        holder_lock=_LOCK_FOR_UPDATE,
        lock_id=w.meeting_id,
        holder_writes=None,
        wait_pattern=_PREP_WAIT,
    )

    assert response.status_code == 201, response.text
    after = _writes(w.ws)
    if name == "add_participant":
        assert after["participants"] == before["participants"] + 1
    else:
        new_id = response.json()["id"]
        expected = {(new_id, "fresh")}
        if old_pack is not None:
            expected.add((str(old_pack), "refreshed"))
        assert set(after["packs"]) == expected
    assert after["audit_events"] > before["audit_events"]
    assert enrichment_calls == ([w.meeting_id] if enrichment else [])


@pytest.mark.parametrize("transfer", list(_TRANSFERS))
@pytest.mark.parametrize("name", ["create_prep", "refresh_prep"])
def test_enrichment_path_rechecks_authorization_before_writing_pack(
    race_world: RaceWorld, name: str, transfer: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With enrichment on, authorization and the pack write sit in separate
    transactions with the model call between them. A transfer committed
    during that call must stop the write."""
    w, case = race_world, CASES[name]
    make_writes, status, code = _TRANSFERS[transfer]
    _set_enrichment(monkeypatch, enabled=False)
    if case.needs_pack:
        _seed_pack(w)
    before = _writes(w.ws)

    def transfer_during_enrichment(*_args: Any, **_kwargs: Any) -> EnrichmentOut:
        with engine.begin() as connection:
            make_writes(w)(connection)
        return EnrichmentOut(available=False, summary=None, error_code="model_unavailable")

    _set_enrichment(monkeypatch, enabled=True)
    monkeypatch.setattr(meeting_prep_module, "_compute_enrichment", transfer_during_enrichment)
    client = _client(w.b_token)
    try:
        response = client.post(_path(w, case), headers=_headers(w.b_token), json=_body(w, case))
    finally:
        client.close()

    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    assert _meeting_row(w.meeting_id)["owner_id"] == w.c
    assert _writes(w.ws) == before


# ---------------------------------------------------------------------------
# Meeting edits (scheduling/meetings.py)
# ---------------------------------------------------------------------------


_EDITS: dict[str, tuple[str, str, dict[str, Any]]] = {
    "patch": ("PATCH", "/api/v1/meetings/{m}", {"expected_version": 1, "title": "Race edit"}),
    "archive": ("POST", "/api/v1/meetings/{m}/archive", {"expected_version": 1}),
    "restore": ("POST", "/api/v1/meetings/{m}/restore", {"expected_version": 1}),
}


def _prepare_edit(w: RaceWorld, name: str) -> None:
    if name == "restore":  # restore needs an archived meeting (version stays 1)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE meetings SET archived_at = now(), pre_archive_status = status "
                    "WHERE id = :id"
                ),
                {"id": w.meeting_id},
            )


@pytest.mark.parametrize("name", list(_EDITS))
@pytest.mark.parametrize("transfer", list(_TRANSFERS))
def test_meeting_edit_waiting_on_meeting_lock_rechecks_authorization(
    race_world: RaceWorld, name: str, transfer: str
) -> None:
    w = race_world
    method, path, body = _EDITS[name]
    make_writes, status, code = _TRANSFERS[transfer]
    _prepare_edit(w, name)
    before = _meeting_row(w.meeting_id)

    response = _race(
        w.b_token,
        method,
        path.format(m=w.meeting_id),
        body,
        holder_lock=_LOCK_FOR_UPDATE,
        lock_id=w.meeting_id,
        holder_writes=make_writes(w),
        wait_pattern=_UPDATE_WAIT,
    )

    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    after = _meeting_row(w.meeting_id)
    assert after["owner_id"] == w.c
    assert (after["version"], after["title"], after["archived_at"]) == (
        before["version"],
        before["title"],
        before["archived_at"],
    )


@pytest.mark.parametrize("name", list(_EDITS))
def test_meeting_edit_waiting_on_meeting_lock_without_transfer_still_writes(
    race_world: RaceWorld, name: str
) -> None:
    w = race_world
    method, path, body = _EDITS[name]
    _prepare_edit(w, name)
    before = _meeting_row(w.meeting_id)

    response = _race(
        w.b_token,
        method,
        path.format(m=w.meeting_id),
        body,
        holder_lock=_LOCK_FOR_UPDATE,
        lock_id=w.meeting_id,
        holder_writes=None,
        wait_pattern=_UPDATE_WAIT,
    )

    assert response.status_code == 200, response.text
    assert _meeting_row(w.meeting_id)["version"] == before["version"] + 1


# ---------------------------------------------------------------------------
# Grant revocation (platform/authz_grants.py)
# ---------------------------------------------------------------------------


def _seed_write_grant(w: RaceWorld) -> UUID:
    """Meeting owned by A, shared explicitly with B through a read+write
    grant A created."""
    grant_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE meetings SET owner_id = :a, visibility = 'shared_explicitly' WHERE id = :id"
            ),
            {"id": w.meeting_id, "a": w.a},
        )
        connection.execute(
            text(
                "INSERT INTO resource_grants (id, workspace_id, grantee_account_id, "
                "resource_type, resource_id, actions, granted_by, created_at) "
                "VALUES (:id, :ws, :account, 'meetings', :meeting, "
                "ARRAY['read', 'write'], :a, now())"
            ),
            {
                "id": grant_id,
                "ws": w.ws,
                "account": w.b_account,
                "meeting": w.meeting_id,
                "a": w.a,
            },
        )
    return grant_id


def test_grantor_revoke_waits_for_in_flight_meeting_write(race_world: RaceWorld) -> None:
    """A grantor revoking their own grant must wait for a writer that holds
    the meeting lock (as `_lock_meeting_for_write` does), so the revoke
    cannot return while a write it should be ordered after is still open."""
    w = race_world
    grant_id = _seed_write_grant(w)

    response = _race(
        w.a_token,
        "DELETE",
        f"/api/v1/sharing/grants/{grant_id}",
        None,
        holder_lock=_LOCK_FOR_SHARE,
        lock_id=w.meeting_id,
        holder_writes=None,
        wait_pattern=_UPDATE_WAIT,
    )

    assert response.status_code == 200, response.text
    assert response.json()["revoked_at"] is not None


def test_revoke_locks_resource_before_grant_so_removal_cannot_deadlock(
    race_world: RaceWorld,
) -> None:
    """The holder is any transaction that follows the codebase's
    resource-then-grant order (member removal: the removed member's rows and
    runs, then their evidence grants; grant creation: resource, then insert).
    It locks the resource -- a meeting here; the lock mechanics do not depend
    on the table -- lets the revoke queue behind it, then updates the grant.
    A revoke that locked the grant row first deadlocked against that. With
    resource-then-grant ordering the revoke holds no grant lock while it
    waits, so the holder's update goes through and the revoke then sees the
    grant already revoked. Removal's own ordering is pinned in
    test_identity_membership_removal_postgres.py."""
    w = race_world
    grant_id = _seed_write_grant(w)

    def revoke_grant_as_removal(conn: Connection) -> None:
        conn.execute(text("SET LOCAL lock_timeout = '3s'"))
        conn.execute(
            text("UPDATE resource_grants SET revoked_at = now() WHERE id = :id"),
            {"id": grant_id},
        )

    response = _race(
        w.a_token,
        "DELETE",
        f"/api/v1/sharing/grants/{grant_id}",
        None,
        holder_lock=_LOCK_FOR_UPDATE,
        lock_id=w.meeting_id,
        holder_writes=None,
        wait_pattern=_UPDATE_WAIT,
        after_wait=revoke_grant_as_removal,
    )

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "GRANT_ALREADY_REVOKED"
