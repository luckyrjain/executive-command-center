"""Authorization before the idempotency cache for `POST /delegations` and
the delegation accept/reject/revoke/complete transitions.

`create_delegation_endpoint` read the `Idempotency-Key` cache right after
the membership lock (which only requires an active member), before its
`authorize(..., "write")` on the obligation -- the endpoint's real role
gate. A delegator who had since been demoted to `viewer`, or who could no
longer see the obligation after an ownership transfer, could replay a
same-key proposal and get the cached 201. The cache is now read only after
the locked obligation checks pass.

The transitions are party-gated: the parties of a delegation never change
and the membership lock already requires an active member, so a replay
there could not reach anyone the endpoint would refuse. Their cache read
moved behind the party checks too, for one uniform order; the tests below
pin that a still-entitled replay keeps getting the cached 200 (not a 409
from the already-transitioned row) and a suspended party gets nothing.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from lock_race_support import RaceWorld, headers, race_world
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import engine
from ecc.main import app

pytestmark = pytest.mark.skipif(
    not get_settings().database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

_SIDE_EFFECT_TABLES = (
    "audit_events",
    "event_outbox",
    "idempotency_records",
    "delegations",
    "resource_grants",
    "member_notifications",
)


@pytest.fixture
def world() -> Iterator[RaceWorld]:
    with race_world(
        "Collaboration Idempotent Replay",
        ("member_notifications", "resource_grants", "delegations", "incidents"),
    ) as w:
        yield w


def _token(w: RaceWorld, user_id: UUID) -> str:
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                "expires_at, last_seen_at) "
                "VALUES (:id, :ws, :user_id, :token_hash, :expires_at, :now)"
            ),
            {
                "id": uuid4(),
                "ws": w.ws,
                "user_id": user_id,
                "token_hash": sha256(token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "now": now,
            },
        )
    return token


def _account(w: RaceWorld, user_id: UUID) -> UUID:
    with engine.connect() as connection:
        return UUID(
            str(
                connection.execute(
                    text("SELECT account_id FROM users WHERE workspace_id = :ws AND id = :id"),
                    {"ws": w.ws, "id": user_id},
                ).scalar_one()
            )
        )


def _seed_incident(w: RaceWorld, *, owner: UUID, visibility: str) -> UUID:
    incident_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO incidents (id, workspace_id, title, severity, status, detected_at, "
                "created_by, updated_by, created_at, updated_at, owner_id, visibility) "
                "VALUES (:id, :ws, 'Delegated incident', 'high', 'open', :now, "
                ":owner, :owner, :now, :now, :owner, :visibility)"
            ),
            {"id": incident_id, "ws": w.ws, "now": now, "owner": owner, "visibility": visibility},
        )
    return incident_id


def _set_membership(w: RaceWorld, user_id: UUID, **values: str) -> None:
    assignments = ", ".join(f"{column} = :{column}" for column in values)
    with engine.begin() as connection:
        connection.execute(
            text(
                f"UPDATE workspace_memberships SET {assignments} "  # noqa: S608 -- test-only literals
                "WHERE workspace_id = :ws AND users_id = :user_id"
            ),
            {"ws": w.ws, "user_id": user_id, **values},
        )


def _side_effect_counts(ws: UUID) -> dict[str, int]:
    with engine.connect() as connection:
        return {
            table: int(
                connection.execute(
                    text(f"SELECT count(*) FROM {table} WHERE workspace_id = :ws"),  # noqa: S608
                    {"ws": ws},
                ).scalar_one()
            )
            for table in _SIDE_EFFECT_TABLES
        }


def _without_request_id(body: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in body.items() if key != "request_id"}


def _proposal(recipient_account_id: UUID, incident_id: UUID) -> dict[str, Any]:
    return {
        "recipient_account_id": str(recipient_account_id),
        "obligation_type": "incidents",
        "obligation_resource_id": str(incident_id),
        "expected_outcome": "Resolve the incident",
        "due_at": (datetime.now(UTC) + timedelta(days=1)).isoformat(),
        "evidence": [],
    }


def _propose_and_replay(
    client: TestClient, request_headers: dict[str, str], body: dict[str, Any]
) -> None:
    first = client.post("/api/v1/delegations", headers=request_headers, json=body)
    assert first.status_code == 201, first.text
    replay = client.post("/api/v1/delegations", headers=request_headers, json=body)
    assert replay.status_code == 201, replay.text
    assert _without_request_id(replay.json()) == _without_request_id(first.json())


def test_create_replay_after_demotion_to_viewer_is_refused_403(world: RaceWorld) -> None:
    # Workspace-visible and owned by A: C (`member`) writes it by role alone,
    # and as a `viewer` can still read it.
    incident_id = _seed_incident(world, owner=world.a, visibility="workspace")
    c_token = _token(world, world.c)
    body = _proposal(_account(world, world.b), incident_id)
    request_headers = headers(c_token)
    client = TestClient(app)
    client.cookies.set("ecc_session", c_token)
    try:
        _propose_and_replay(client, request_headers, body)
        _set_membership(world, world.c, role="viewer")
        counts_before = _side_effect_counts(world.ws)
        refused = client.post("/api/v1/delegations", headers=request_headers, json=body)
    finally:
        client.close()

    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "INSUFFICIENT_ROLE"
    assert _side_effect_counts(world.ws) == counts_before


def test_create_replay_after_obligation_transfer_is_refused_404(world: RaceWorld) -> None:
    incident_id = _seed_incident(world, owner=world.b, visibility="private")
    body = _proposal(_account(world, world.c), incident_id)
    request_headers = headers(world.b_token)
    client = TestClient(app)
    client.cookies.set("ecc_session", world.b_token)
    try:
        _propose_and_replay(client, request_headers, body)
        # Transferred to C; still private, so B can no longer see it.
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE incidents SET owner_id = :c WHERE id = :id"),
                {"c": world.c, "id": incident_id},
            )
        counts_before = _side_effect_counts(world.ws)
        refused = client.post("/api/v1/delegations", headers=request_headers, json=body)
    finally:
        client.close()

    assert refused.status_code == 404, refused.text
    assert refused.json()["error"]["code"] == "OBLIGATION_NOT_FOUND"
    assert _side_effect_counts(world.ws) == counts_before


def test_create_replay_after_suspension_is_refused(world: RaceWorld) -> None:
    incident_id = _seed_incident(world, owner=world.b, visibility="private")
    body = _proposal(_account(world, world.c), incident_id)
    request_headers = headers(world.b_token)
    client = TestClient(app)
    client.cookies.set("ecc_session", world.b_token)
    try:
        _propose_and_replay(client, request_headers, body)
        _set_membership(world, world.b, status="suspended")
        counts_before = _side_effect_counts(world.ws)
        refused = client.post("/api/v1/delegations", headers=request_headers, json=body)
    finally:
        client.close()

    assert refused.status_code == 403, refused.text
    assert _side_effect_counts(world.ws) == counts_before


# (transition, the party who performs it, the status it starts from)
_TRANSITIONS = {
    "accept": ("recipient", "proposed"),
    "reject": ("recipient", "proposed"),
    "revoke": ("delegator", "accepted"),
    "complete": ("recipient", "accepted"),
}


@pytest.mark.parametrize("transition", list(_TRANSITIONS))
def test_transition_replay_is_cached_for_the_party_and_refused_once_suspended(
    world: RaceWorld, transition: str
) -> None:
    party, start = _TRANSITIONS[transition]
    incident_id = _seed_incident(world, owner=world.b, visibility="private")
    c_token = _token(world, world.c)
    b_client = TestClient(app)
    b_client.cookies.set("ecc_session", world.b_token)
    c_client = TestClient(app)
    c_client.cookies.set("ecc_session", c_token)
    try:
        proposed = b_client.post(
            "/api/v1/delegations",
            headers=headers(world.b_token),
            json=_proposal(_account(world, world.c), incident_id),
        )
        assert proposed.status_code == 201, proposed.text
        delegation_id = proposed.json()["id"]
        if start == "accepted":
            accepted = c_client.post(
                f"/api/v1/delegations/{delegation_id}/accept", headers=headers(c_token)
            )
            assert accepted.status_code == 200, accepted.text

        actor_id, client, token = (
            (world.c, c_client, c_token)
            if party == "recipient"
            else (world.b, b_client, world.b_token)
        )
        path = f"/api/v1/delegations/{delegation_id}/{transition}"
        request_headers = headers(token)
        first = client.post(path, headers=request_headers)
        assert first.status_code == 200, first.text
        # The delegation has left `start`: only the cache can answer 200 now.
        replay = client.post(path, headers=request_headers)
        assert replay.status_code == 200, replay.text
        assert _without_request_id(replay.json()) == _without_request_id(first.json())

        _set_membership(world, actor_id, status="suspended")
        counts_before = _side_effect_counts(world.ws)
        refused = client.post(path, headers=request_headers)
    finally:
        b_client.close()
        c_client.close()

    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "INSUFFICIENT_ROLE"
    assert _side_effect_counts(world.ws) == counts_before
