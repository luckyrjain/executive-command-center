"""Engineering writes against a concurrent member removal or role change
(ADR-0014).

Every authorized write transaction in `engineering/connector_accounts.py`
and `engineering/decisions_incidents.py` now takes the shared membership
lock first (`authz.lock_membership_for_write`) and authorizes after it; the
creates (connector, incident, decision) and `GET /metrics` (which writes
snapshots) re-check the role in-transaction (`role_action="write"`), since
their only role gate ran before the transaction began. Connector creation
re-checks it again in its persist phase, after the adapter's network call.
A removal or demotion holding the lock makes the write wait, and once it
commits the write is refused and writes nothing. See
`membership_lock_race_support` for the harness.

`POST .../sync` already took the lock (Spec A S1.11); its cases pin that.
The two team-suggestion bulk actions authorize per row and answer 200 with
the refused rows in `skipped_unauthorized`, so a revoked caller gets a 200
that updates nothing (and still stores its idempotency record); those are
asserted locally rather than through `assert_refused`.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from membership_lock_race_support import (
    FORBIDDEN,
    ROLE_GATED,
    Case,
    Change,
    Ids,
    RaceWorld,
    assert_proceeds,
    assert_refused,
    client_for,
    fingerprint,
    headers,
    race,
    race_world,
    refusal_params,
    row_refusals,
    seed,
    seed_nothing,
)
from sqlalchemy import Connection, text

from ecc.config import get_settings
from ecc.database import engine
from ecc.domains.engineering.connectors import ConnectorAuthorization
from ecc.domains.engineering.crypto import encrypt_credential
from ecc.domains.engineering.sandbox_adapter import SandboxGithubAdapter
from ecc.platform.connector_security import membership_mutation_lock_key

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_BOOKKEEPING = ("audit_events", "event_outbox", "idempotency_records")
_WRITE_TABLES = (
    "connector_accounts",
    "sync_runs",
    "sync_cursors",
    "repositories",
    "engineering_work_items",
    "changes",
    "reviews",
    "datadog_monitors",
    "datadog_service_definitions",
    "datadog_dashboards",
    "incidents",
    "incident_changes",
    "engineering_decisions",
    "decision_changes",
    "delivery_metric_snapshots",
    *_BOOKKEEPING,
)
# The bulk team-suggestion actions store an idempotency record even when
# every candidate row was refused; everything else must stay unchanged.
_SUGGESTION_TABLES = tuple(t for t in _WRITE_TABLES if t != "idempotency_records")

_ENG = "/api/v1/engineering"


@pytest.fixture
def world() -> Iterator[RaceWorld]:
    with race_world(
        "Engineering Membership Race",
        (
            "incident_changes",
            "decision_changes",
            "incidents",
            "engineering_decisions",
            "delivery_metric_snapshots",
            "sync_cursors",
            "sync_runs",
            "repositories",
            "engineering_work_items",
            "pkos_nodes",
            "connector_accounts",
        ),
    ) as w:
        yield w


def _insert_connector(conn: Connection, w: RaceWorld, now: datetime) -> UUID:
    account_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO connector_accounts (id, workspace_id, provider, external_account_id, "
            "display_name, granted_scopes, encrypted_credentials, status, version, "
            "created_by, updated_by, created_at, updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, 'sandbox', :external, 'Race connector', "
            "ARRAY['contents:read'], :credential, 'active', 1, :a, :a, :now, :now, "
            ":a, 'workspace')"
        ),
        {
            "id": account_id,
            "ws": w.ws,
            "external": f"race-{account_id}",
            "credential": encrypt_credential("race-credential"),
            "a": w.a,
            "now": now,
        },
    )
    return account_id


def _insert_projection(conn: Connection, w: RaceWorld, now: datetime, table: str) -> UUID:
    connector_id = _insert_connector(conn, w, now)
    row_id = uuid4()
    title_column = "name" if table == "repositories" else "title"
    conn.execute(
        text(
            f"INSERT INTO {table} (id, workspace_id, connector_account_id, provider, "  # noqa: S608
            f"external_id, {title_column}, source_url, observed_at, created_at, updated_at, "
            "suggested_team_name, owner_id, visibility) "
            "VALUES (:id, :ws, :connector_id, 'github', :external, 'Race row', "
            "'https://example.invalid/race', :now, :now, :now, 'Race Team', :a, 'workspace')"
        ),
        {
            "id": row_id,
            "ws": w.ws,
            "connector_id": connector_id,
            "external": f"race-{row_id}",
            "now": now,
            "a": w.a,
        },
    )
    return row_id


def _seed_connector(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    return {"id": _insert_connector(conn, w, now)}


def _seed_repository(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    return {"id": _insert_projection(conn, w, now, "repositories")}


def _seed_work_item(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    return {"id": _insert_projection(conn, w, now, "engineering_work_items")}


def _seed_suggestion(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    team_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO pkos_nodes (id, workspace_id, node_type, canonical_name, status, "
            "confidence, version, created_at, updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, 'team', 'Race Team', 'active', 1.0, 1, :now, :now, :a, "
            "'workspace')"
        ),
        {"id": team_id, "ws": w.ws, "now": now, "a": w.a},
    )
    return {"team": team_id, "id": _insert_projection(conn, w, now, "repositories")}


def _seed_incident(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    incident_id = uuid4()
    conn.execute(
        text(
            "INSERT INTO incidents (id, workspace_id, title, severity, status, detected_at, "
            "version, created_by, updated_by, created_at, updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, 'Race incident', 'high', 'open', :detected, 1, :a, :a, "
            ":now, :now, :a, 'workspace')"
        ),
        {"id": incident_id, "ws": w.ws, "detected": now - timedelta(hours=1), "a": w.a, "now": now},
    )
    return {"id": incident_id}


def _seed_decision(conn: Connection, w: RaceWorld, now: datetime) -> Ids:
    decision_id = uuid4()
    created = now - timedelta(hours=1)
    conn.execute(
        text(
            "INSERT INTO engineering_decisions (id, workspace_id, title, status, version, "
            "created_by, updated_by, created_at, updated_at, owner_id, visibility) "
            "VALUES (:id, :ws, 'Race decision', 'proposed', 1, :a, :a, :created, :created, "
            ":a, 'workspace')"
        ),
        {"id": decision_id, "ws": w.ws, "a": w.a, "created": created},
    )
    return {"id": decision_id}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _create_connector_body(_: Ids) -> dict[str, Any]:
    return {"provider": "sandbox", "credential": f"race-{uuid4()}"}


_TEAM_BODY = {"expected_version": 1, "team_entity_id": None}
_CONNECTOR_ROW = row_refusals("CONNECTOR_NOT_FOUND")

CASES: dict[str, Case] = {
    "connector_create": Case(
        seed_nothing, "POST", f"{_ENG}/connectors", _create_connector_body, 201, ROLE_GATED
    ),
    "connector_disable": Case(
        _seed_connector,
        "POST",
        f"{_ENG}/connectors/{{id}}/disable",
        lambda _: None,
        200,
        _CONNECTOR_ROW,
    ),
    # Already locked before ADR-0014 (Spec A S1.11: `require_active_members_
    # locked` in phase 1); a removed caller is skipped as MEMBERSHIP_INACTIVE.
    "connector_sync": Case(
        _seed_connector,
        "POST",
        f"{_ENG}/connectors/{{id}}/sync",
        lambda _: {"run_type": "incremental", "resource_type": "repository"},
        201,
        {"demote": FORBIDDEN, "remove": (403, "MEMBERSHIP_INACTIVE")},
    ),
    "metrics": Case(seed_nothing, "GET", f"{_ENG}/metrics", lambda _: None, 200, ROLE_GATED),
    "repository_team": Case(
        _seed_repository,
        "POST",
        f"{_ENG}/repositories/{{id}}/team",
        lambda _: _TEAM_BODY,
        200,
        row_refusals("REPOSITORY_NOT_FOUND"),
    ),
    "work_item_team": Case(
        _seed_work_item,
        "POST",
        f"{_ENG}/work-items/{{id}}/team",
        lambda _: _TEAM_BODY,
        200,
        row_refusals("WORK_ITEM_NOT_FOUND"),
    ),
    # A demoted caller is answered 200 with the row skipped (asserted in
    # `test_team_suggestion_...` below); a removed one can no longer see the
    # team entity.
    "team_suggestion_confirm": Case(
        _seed_suggestion,
        "POST",
        f"{_ENG}/team-suggestions/confirm",
        lambda ids: {"suggested_team_name": "Race Team", "team_entity_id": str(ids["team"])},
        200,
        {"demote": None, "remove": (404, "TEAM_ENTITY_NOT_FOUND")},
    ),
    "team_suggestion_dismiss": Case(
        _seed_suggestion,
        "POST",
        f"{_ENG}/team-suggestions/dismiss",
        lambda _: {"suggested_team_name": "Race Team"},
        200,
    ),
    "incident_create": Case(
        seed_nothing,
        "POST",
        f"{_ENG}/incidents",
        lambda _: {"title": "Race incident", "detected_at": _now_iso()},
        201,
        ROLE_GATED,
    ),
    "incident_resolve": Case(
        _seed_incident,
        "POST",
        f"{_ENG}/incidents/{{id}}/resolve",
        lambda _: {"resolved_at": _now_iso()},
        200,
        row_refusals("INCIDENT_NOT_FOUND"),
    ),
    "decision_create": Case(
        seed_nothing,
        "POST",
        f"{_ENG}/decisions",
        lambda _: {"title": "Race decision"},
        201,
        ROLE_GATED,
    ),
    "decision_decide": Case(
        _seed_decision,
        "POST",
        f"{_ENG}/decisions/{{id}}/decide",
        lambda _: {"decided_at": _now_iso()},
        200,
        row_refusals("DECISION_NOT_FOUND"),
    ),
}

# (case, change) pairs where the bulk action answers 200 and updates nothing.
_SUGGESTION_SKIPS: list[tuple[str, Change]] = [
    ("team_suggestion_confirm", "demote"),
    ("team_suggestion_dismiss", "demote"),
    ("team_suggestion_dismiss", "remove"),
]


@pytest.mark.parametrize(("name", "change"), refusal_params(CASES))
def test_write_waiting_on_membership_lock_rechecks_authorization(
    world: RaceWorld, name: str, change: Change
) -> None:
    case = CASES[name]
    assert_refused(world, case, seed(world, case), change, _WRITE_TABLES)


@pytest.mark.parametrize(("name", "change"), _SUGGESTION_SKIPS)
def test_team_suggestion_waiting_on_membership_lock_updates_nothing(
    world: RaceWorld, name: str, change: Change
) -> None:
    case = CASES[name]
    ids = seed(world, case)
    before = fingerprint(world.ws, _SUGGESTION_TABLES)

    response, blocked = race(world, case, ids, change=change)

    assert response.status_code == 200, response.text
    assert response.json()["updated"] == []
    assert blocked
    assert fingerprint(world.ws, _SUGGESTION_TABLES) == before


@pytest.mark.parametrize("name", list(CASES))
def test_write_waiting_on_membership_lock_without_change_still_proceeds(
    world: RaceWorld, name: str
) -> None:
    case = CASES[name]
    assert_proceeds(world, case, seed(world, case))


def test_connector_create_rechecks_role_after_the_adapter_call(
    world: RaceWorld, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Connector creation calls the provider between its two transactions,
    holding no lock. A demotion that commits during that call is seen by the
    persist phase's in-transaction role check: 403, no connector row."""
    real_authorize = SandboxGithubAdapter.authorize

    def authorize_then_demote(
        self: SandboxGithubAdapter, credential: str
    ) -> ConnectorAuthorization:
        with engine.begin() as conn:
            conn.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": membership_mutation_lock_key(world.ws)},
            )
            conn.execute(
                text(
                    "UPDATE workspace_memberships SET role = 'viewer', updated_at = now() "
                    "WHERE workspace_id = :ws AND users_id = :b"
                ),
                {"ws": world.ws, "b": world.b},
            )
        return real_authorize(self, credential)

    monkeypatch.setattr(SandboxGithubAdapter, "authorize", authorize_then_demote)
    before = fingerprint(world.ws, _WRITE_TABLES)

    client: TestClient = client_for(world)
    try:
        response = client.post(
            f"{_ENG}/connectors",
            headers=headers(world.b_token),
            json=_create_connector_body({}),
        )
    finally:
        client.close()

    assert response.status_code == 403, response.text
    assert response.json()["error"]["code"] == "INSUFFICIENT_ROLE"
    assert fingerprint(world.ws, _WRITE_TABLES) == before
