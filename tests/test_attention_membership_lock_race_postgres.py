"""Attention and meeting-prep writes against a concurrent member removal or
role change (ADR-0014).

Removal and role change (`identity/membership_removal.py`) take the
workspace's membership-mutation advisory lock exclusively. These write
paths never took its shared side, so a caller could pass `authorize()`, be
demoted to `viewer` or removed by an admin, have that commit, and still
commit the write afterwards. Create endpoints gated on role only before
their transaction began, so they never re-checked it at all.

Now every write transaction takes the shared lock first
(`authz.lock_membership_for_write`) and authorizes after it, re-checking
the role in-transaction where the only gate was the pre-transaction one.
A removal that holds the lock makes the write wait, and once it commits the
write answers 403/404 and writes nothing.

Concurrency is real (request thread + separate connection). "Is waiting" is
observed through `pg_blocking_pids` scoped to this test's lock holder, not
assumed from timing.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from hmac import new
from typing import Any, Literal
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
from ecc.platform.connector_security import membership_mutation_lock_key

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

settings = get_settings()
_WAIT_SECONDS = 15

Change = Literal["demote", "remove"]

# Tables whose contents must not change when the write is refused.
_WRITE_TABLES = (
    "attention_items",
    "attention_feedback",
    "waiting_links",
    "risks",
    "risk_reviews",
    "plans",
    "plan_blocks",
    "planning_constraints",
    "capacity_profiles",
    "meetings",
    "meeting_participants",
    "meeting_packs",
    "audit_events",
    "idempotency_records",
)


@dataclass
class RaceWorld:
    """One workspace: A (`owner`, owns every seeded row) and B (`member`,
    the racing caller). Rows are `workspace`-visible, so B's write access
    comes from its role alone: as `viewer` B may still read them but not
    write, and once removed B sees nothing."""

    ws: UUID
    a: UUID
    b: UUID
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
    ws, a, b = uuid4(), uuid4(), uuid4()
    b_token = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Membership Lock Race', 'UTC', :now)"
            ),
            {"id": ws, "now": now},
        )
        create_identity(connection, workspace_id=ws, user_id=a, now=now)
        create_identity(connection, workspace_id=ws, user_id=b, now=now, role="member")
        _session(connection, ws, b, b_token, now)
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
                "meeting_packs",
                "meeting_participants",
                "meetings",
                "attention_feedback",
                "attention_items",
                "risk_reviews",
                "risks",
                "waiting_links",
                "pkos_nodes",
                "plan_blocks",
                "plans",
                "planning_constraints",
                "capacity_profiles",
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


# ---------------------------------------------------------------------------
# Seeds: each inserts rows owned by A, `workspace`-visible, and returns the
# ids the request path and body need.
# ---------------------------------------------------------------------------

Ids = dict[str, UUID]


def _seed_nothing(_conn: Connection, _w: RaceWorld, _now: datetime) -> Ids:
    return {}


def _seed_node(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    node_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO pkos_nodes (id, workspace_id, node_type, canonical_name, "
            "created_at, updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, 'person', 'Race Person', :now, :now, :a, 'workspace')"
        ),
        {"id": node_id, "ws": w.ws, "a": w.a, "now": now},
    )
    return {"node": node_id}


def _seed_meeting(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    meeting_id = uuid4()
    conn.execute(
        text(
            """
            INSERT INTO meetings (
                id, workspace_id, title, standalone_starts_at, standalone_ends_at,
                standalone_timezone, status, agenda, created_by, updated_by,
                created_at, updated_at, version, owner_id, visibility
            ) VALUES (
                :id, :ws, 'Race review', :starts_at, :ends_at, 'UTC',
                'planned', 'Race agenda', :a, :a, :now, :now, 1, :a, 'workspace'
            )
            """
        ),
        {
            "id": meeting_id,
            "ws": w.ws,
            "a": w.a,
            "starts_at": now + timedelta(days=1),
            "ends_at": now + timedelta(days=1, hours=1),
            "now": now,
        },
    )
    return {"meeting": meeting_id, **_seed_node(conn, w, now)}


def _seed_attention_item(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    item_id = uuid4()
    conn.execute(
        text(
            """
            INSERT INTO attention_items (
                id, workspace_id, entity_type, entity_id, source_entity_version,
                score, confidence, factors, explanation, generated_at, expires_at,
                pinned, policy_version, owner_id, visibility
            ) VALUES (
                :id, :ws, 'task', :entity_id, 1, 90, 1.0,
                '[]'::jsonb, 'Race item', :now, :expires_at,
                false, 1, :a, 'workspace'
            )
            """
        ),
        {
            "id": item_id,
            "ws": w.ws,
            "entity_id": uuid4(),
            "now": now,
            "expires_at": now + timedelta(minutes=30),
            "a": w.a,
        },
    )
    return {"id": item_id}


def _seed_waiting_link(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    node_id = _seed_node(conn, w, now)["node"]
    link_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO waiting_links (id, workspace_id, subject_type, subject_id, "
            "counterparty_entity_id, direction, since_at, created_by, updated_by, "
            "created_at, updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, 'knowledge_entity', :node, :node, 'waiting_on_them', "
            ":now, :a, :a, :now, :now, :a, 'workspace')"
        ),
        {"id": link_id, "ws": w.ws, "node": node_id, "a": w.a, "now": now},
    )
    return {"id": link_id}


def _seed_risk(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    risk_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO risks (id, workspace_id, description, probability, impact, "
            "owner_id, created_by, updated_by, created_at, updated_at, visibility) "
            "VALUES (:id, :ws, 'Race risk', 3, 3, :a, :a, :a, :now, :now, 'workspace')"
        ),
        {"id": risk_id, "ws": w.ws, "a": w.a, "now": now},
    )
    return {"id": risk_id}


def _seed_constraint(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    constraint_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO planning_constraints (id, workspace_id, user_id, kind, label, "
            "hardness, created_at, updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, :a, 'preference', 'Race constraint', 'soft', "
            ":now, :now, :a, 'workspace')"
        ),
        {"id": constraint_id, "ws": w.ws, "a": w.a, "now": now},
    )
    return {"id": constraint_id}


def _seed_plan(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    plan_id, block_id = uuid4(), uuid4()
    today = date.today()
    conn.execute(
        text(
            "INSERT INTO plans (id, workspace_id, user_id, period_start, period_end, "
            "policy_version, capacity_minutes, created_by, updated_by, created_at, "
            "updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, :a, :start, :end, 1, 480, :a, :a, :now, :now, :a, 'workspace')"
        ),
        {
            "id": plan_id,
            "ws": w.ws,
            "a": w.a,
            "now": now,
            "start": today,
            "end": today + timedelta(days=6),
        },
    )
    starts = datetime.combine(today + timedelta(days=1), datetime.min.time(), UTC).replace(hour=10)
    conn.execute(
        text(
            "INSERT INTO plan_blocks (id, workspace_id, plan_id, source_type, starts_at, "
            "ends_at, rationale, created_at, updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, :plan, 'constraint', :starts, :ends, 'Race block', "
            ":now, :now, :a, 'workspace')"
        ),
        {
            "id": block_id,
            "ws": w.ws,
            "plan": plan_id,
            "starts": starts,
            "ends": starts + timedelta(hours=1),
            "now": now,
            "a": w.a,
        },
    )
    return {"id": plan_id, "block": block_id}


def _seed_meeting_with_pack(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    ids = _seed_meeting(conn, w, now)
    # The pack is created through the API (as B, before the race) once the
    # seed transaction has committed; see `_prepare`.
    return {**ids, "needs_pack": ids["meeting"]}


def _v1(_: Ids) -> dict[str, Any]:
    return {"expected_version": 1}


def _capacity_body(_: Ids) -> dict[str, Any]:
    return {
        "expected_version": 0,
        "timezone": "UTC",
        "days": [{"weekday": d, "available_minutes": 480, "focus_minutes": 120} for d in range(7)],
    }


# An expected refusal: (status, error code).
Refusal = tuple[int, str]
_FORBIDDEN: Refusal = (403, "INSUFFICIENT_ROLE")


@dataclass(frozen=True)
class Case:
    seed: Callable[[Connection, RaceWorld, datetime], Ids]
    method: str
    path: str  # formatted with the seed's ids
    body: Callable[[Ids], dict[str, Any] | None]
    ok_status: int
    # Expected refusal per membership change; `None` means the change does
    # not revoke this write (a viewer may still edit their own capacity).
    refusals: dict[Change, Refusal | None] = field(default_factory=dict)


def _row_refusals(not_found: str) -> dict[Change, Refusal | None]:
    """A per-row write: a viewer still sees the row (403), a removed member
    sees nothing (404)."""
    return {"demote": _FORBIDDEN, "remove": (404, not_found)}


_ROLE_GATED: dict[Change, Refusal | None] = {"demote": _FORBIDDEN, "remove": _FORBIDDEN}

CASES: dict[str, Case] = {
    "meeting_add_participant": Case(
        _seed_meeting,
        "POST",
        "/api/v1/meetings/{meeting}/participants",
        lambda ids: {"entity_id": str(ids["node"])},
        201,
        _row_refusals("MEETING_NOT_FOUND"),
    ),
    "meeting_create_prep": Case(
        _seed_meeting,
        "POST",
        "/api/v1/meetings/{meeting}/prep",
        lambda _: None,
        201,
        _row_refusals("MEETING_NOT_FOUND"),
    ),
    "meeting_refresh_prep": Case(
        _seed_meeting_with_pack,
        "POST",
        "/api/v1/meetings/{meeting}/prep/refresh",
        lambda _: None,
        201,
        _row_refusals("MEETING_NOT_FOUND"),
    ),
    "attention_dismiss": Case(
        _seed_attention_item,
        "POST",
        "/api/v1/attention/{id}/dismiss",
        lambda _: {},
        200,
        _row_refusals("ATTENTION_ITEM_NOT_FOUND"),
    ),
    "attention_feedback": Case(
        _seed_attention_item,
        "POST",
        "/api/v1/attention/{id}/feedback",
        lambda _: {"label": "useful"},
        201,
        _ROLE_GATED,
    ),
    "attention_regenerate": Case(
        _seed_nothing, "POST", "/api/v1/attention/regenerate", lambda _: None, 200, _ROLE_GATED
    ),
    "capacity_put": Case(
        _seed_nothing,
        "PUT",
        "/api/v1/planning/capacity",
        _capacity_body,
        200,
        {"demote": None, "remove": _FORBIDDEN},
    ),
    "waiting_create": Case(
        _seed_node,
        "POST",
        "/api/v1/waiting",
        lambda ids: {
            "subject_type": "knowledge_entity",
            "subject_id": str(ids["node"]),
            "counterparty_entity_id": str(ids["node"]),
            "direction": "waiting_on_them",
        },
        201,
        _ROLE_GATED,
    ),
    "waiting_patch": Case(
        _seed_waiting_link,
        "PATCH",
        "/api/v1/waiting/{id}",
        lambda _: {"expected_version": 1, "note": "race probe"},
        200,
        _row_refusals("WAITING_LINK_NOT_FOUND"),
    ),
    "waiting_cancel": Case(
        _seed_waiting_link,
        "POST",
        "/api/v1/waiting/{id}/cancel",
        _v1,
        200,
        _row_refusals("WAITING_LINK_NOT_FOUND"),
    ),
    "risk_review": Case(
        _seed_risk,
        "POST",
        "/api/v1/risks/{id}/review",
        lambda _: {"expected_version": 1, "outcome": "no_change"},
        201,
        _row_refusals("RISK_NOT_FOUND"),
    ),
    "constraint_create": Case(
        _seed_nothing,
        "POST",
        "/api/v1/planning/constraints",
        lambda _: {"kind": "preference", "label": "Race constraint", "hardness": "soft"},
        201,
        _ROLE_GATED,
    ),
    "constraint_archive": Case(
        _seed_constraint,
        "POST",
        "/api/v1/planning/constraints/{id}/archive",
        lambda _: {},
        200,
        _row_refusals("PLANNING_CONSTRAINT_NOT_FOUND"),
    ),
    "plan_create": Case(
        _seed_nothing,
        "POST",
        "/api/v1/plans",
        lambda _: {
            "period_start": date.today().isoformat(),
            "period_end": (date.today() + timedelta(days=6)).isoformat(),
        },
        201,
        _ROLE_GATED,
    ),
    "plan_accept": Case(
        _seed_plan,
        "POST",
        "/api/v1/plans/{id}/accept",
        _v1,
        200,
        _row_refusals("PLAN_NOT_FOUND"),
    ),
    "plan_block_remove": Case(
        _seed_plan,
        "POST",
        "/api/v1/plans/{id}/blocks/{block}/remove",
        _v1,
        200,
        _row_refusals("PLAN_NOT_FOUND"),
    ),
}

_REFUSAL_PARAMS = [
    (name, change)
    for name, case in CASES.items()
    for change in ("demote", "remove")
    if case.refusals.get(change) is not None
]


def _prepare(w: RaceWorld, case: Case) -> Ids:
    with engine.begin() as connection:
        ids = case.seed(connection, w, datetime.now(UTC))
    if "needs_pack" in ids:
        client = _client(w)
        try:
            response = client.post(
                f"/api/v1/meetings/{ids['needs_pack']}/prep", headers=_headers(w.b_token)
            )
        finally:
            client.close()
        assert response.status_code == 201, response.text
    return ids


def _fingerprint(ws: UUID) -> dict[str, str | None]:
    """Every write-side table's full contents for the workspace, so an
    UPDATE is caught as well as an INSERT."""
    with engine.connect() as connection:
        return {
            table: connection.execute(
                text(
                    f"SELECT md5(string_agg(t::text, '|' ORDER BY t::text)) "  # noqa: S608
                    f"FROM {table} t WHERE t.workspace_id = :ws"
                ),
                {"ws": ws},
            ).scalar_one()
            for table in _WRITE_TABLES
        }


def _membership_waiters(holder_pid: int) -> int:
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
    deadline = time.monotonic() + _WAIT_SECONDS
    while _membership_waiters(holder_pid) < 1:
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


def _race(w: RaceWorld, case: Case, ids: Ids, *, change: Change | None) -> tuple[Any, bool]:
    """Takes the membership lock exclusively in a separate transaction and
    applies `change` there (as `membership_removal` does), fires B's write,
    waits until it is blocked on that lock, then commits. Returns the
    response and whether the write blocked."""
    client = _client(w)
    result: dict[str, Any] = {}

    def fire() -> None:
        try:
            result["response"] = client.request(
                case.method,
                case.path.format(**ids),
                headers=_headers(w.b_token),
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
                thread.join(timeout=_WAIT_SECONDS)
        assert not thread.is_alive(), "write request never finished"
        if "error" in result:
            raise result["error"]
        return result["response"], blocked
    finally:
        client.close()


@pytest.mark.parametrize(("name", "change"), _REFUSAL_PARAMS)
def test_write_waiting_on_membership_lock_rechecks_authorization(
    race_world: RaceWorld, name: str, change: Change
) -> None:
    w, case = race_world, CASES[name]
    ids = _prepare(w, case)
    before = _fingerprint(w.ws)

    response, blocked = _race(w, case, ids, change=change)

    expected = case.refusals[change]
    assert expected is not None
    assert response.status_code == expected[0], response.text
    assert response.json()["error"]["code"] == expected[1]
    assert blocked
    assert _fingerprint(w.ws) == before


@pytest.mark.parametrize("name", list(CASES))
def test_write_waiting_on_membership_lock_without_change_still_proceeds(
    race_world: RaceWorld, name: str
) -> None:
    """Control: the same lock wait with no membership change succeeds -- the
    refusals above come from the change, not from the wait. Also shows the
    write takes the lock at all."""
    w, case = race_world, CASES[name]
    ids = _prepare(w, case)

    response, blocked = _race(w, case, ids, change=None)

    assert response.status_code == case.ok_status, response.text
    assert blocked


def test_viewer_demotion_does_not_block_own_capacity_edit(race_world: RaceWorld) -> None:
    """`role_action="read"`: a viewer may still edit their own capacity."""
    w, case = race_world, CASES["capacity_put"]

    response, blocked = _race(w, case, {}, change="demote")

    assert response.status_code == 200, response.text
    assert blocked


@pytest.mark.parametrize("name", ["meeting_create_prep", "meeting_refresh_prep"])
@pytest.mark.parametrize("change", ["demote", "remove"])
def test_enrichment_pack_write_waits_on_membership_lock(
    race_world: RaceWorld, name: str, change: Change, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With enrichment on, the pack lands in a later transaction than the
    first authorization. That transaction takes the membership lock too, so
    a removal or demotion pending while enrichment ran stops the write."""
    w, case = race_world, CASES[name]
    ids = _prepare(w, case)
    before = _fingerprint(w.ws)

    monkeypatch.setenv("ECC_MEETING_PREP_AI_ENRICHMENT_ENABLED", "true")
    get_settings.cache_clear()
    monkeypatch.setattr(meeting_prep_module, "_resolve_ollama_adapter", lambda _request: None)
    monkeypatch.setattr(
        meeting_prep_module,
        "_compute_enrichment",
        lambda *_a, **_k: EnrichmentOut(
            available=False, summary=None, error_code="model_unavailable"
        ),
    )
    try:
        response, blocked = _race(w, case, ids, change=change)
    finally:
        monkeypatch.delenv("ECC_MEETING_PREP_AI_ENRICHMENT_ENABLED")
        get_settings.cache_clear()

    expected = case.refusals[change]
    assert expected is not None
    assert response.status_code == expected[0], response.text
    assert response.json()["error"]["code"] == expected[1]
    assert blocked
    assert _fingerprint(w.ws) == before
