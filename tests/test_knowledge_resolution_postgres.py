from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import engine
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


@pytest.fixture
def resolution_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO workspaces (id, name, created_at) VALUES (:id, :name, :created_at)"),
            {"id": workspace_id, "name": "Knowledge Resolution Test", "created_at": now},
        )
        connection.execute(
            text(
                "INSERT INTO users (id, workspace_id, email, password_hash, created_at) "
                "VALUES (:id, :workspace_id, :email, :password_hash, :created_at)"
            ),
            {
                "id": user_id,
                "workspace_id": workspace_id,
                "email": f"{user_id}@example.test",
                "password_hash": "test-password-hash",
                "created_at": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                "expires_at, last_seen_at) VALUES (:id, :workspace_id, :user_id, "
                ":token_hash, :expires_at, :last_seen_at)"
            ),
            {
                "id": session_id,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "token_hash": sha256(token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "last_seen_at": now,
            },
        )

    client = TestClient(app)
    client.cookies.set("ecc_session", token)

    try:
        yield client, workspace_id, user_id, token
    finally:
        client.close()
        with engine.begin() as connection:
            for table in (
                "event_outbox",
                "audit_events",
                "idempotency_records",
                "resolution_candidates",
                "entity_operations",
                "timeline_entries",
                "knowledge_claims",
                "entity_aliases",
                "pkos_edges",
                "pkos_evidence",
                "pkos_nodes",
                "sessions",
                "users",
            ):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                    {"workspace_id": workspace_id},
                )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": workspace_id},
            )


def _headers(token: str, key: str) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    return {
        "Idempotency-Key": key,
        "X-CSRF-Token": csrf,
        "X-Correlation-ID": str(uuid4()),
    }


def _create_entity(client: TestClient, token: str, key: str, kind: str, name: str) -> UUID:
    response = client.post(
        "/api/v1/knowledge/entities",
        headers=_headers(token, key),
        json={"kind": kind, "canonical_name": name},
    )
    assert response.status_code == 201
    return UUID(response.json()["id"])


def test_candidate_creation_scores_similar_entities(
    resolution_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = resolution_test_context
    left_id = _create_entity(client, token, "create-left", "person", "Ada Lovelace")
    right_id = _create_entity(client, token, "create-right", "person", "Ada Lovelase")

    response = client.post(
        "/api/v1/knowledge/resolution/candidates",
        headers=_headers(token, "create-candidate"),
        json={"left_entity_id": str(left_id), "right_entity_id": str(right_id)},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["deterministic"] is False
    assert body["candidate"] is not None
    assert body["candidate"]["status"] == "open"
    assert 0.0 < body["candidate"]["score"] <= 1.0
    assert body["candidate"]["resolver_version"]
    assert {body["candidate"]["left_entity_id"], body["candidate"]["right_entity_id"]} == {
        str(left_id),
        str(right_id),
    }


def test_candidate_list_returns_created_candidate(
    resolution_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = resolution_test_context
    left_id = _create_entity(client, token, "list-left", "person", "Grace Hopper")
    right_id = _create_entity(client, token, "list-right", "person", "Grace Hoper")
    client.post(
        "/api/v1/knowledge/resolution/candidates",
        headers=_headers(token, "list-create"),
        json={"left_entity_id": str(left_id), "right_entity_id": str(right_id)},
    )

    listed = client.get(
        "/api/v1/knowledge/resolution/candidates", headers=_headers(token, "list-fetch")
    )
    assert listed.status_code == 200
    items = listed.json()["items"]
    assert len(items) == 1
    assert {items[0]["left_entity_id"], items[0]["right_entity_id"]} == {
        str(left_id),
        str(right_id),
    }


def test_confirm_is_idempotent(
    resolution_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = resolution_test_context
    left_id = _create_entity(client, token, "confirm-left", "person", "Ada Lovelace")
    right_id = _create_entity(client, token, "confirm-right", "person", "Ada Lovelase")
    created = client.post(
        "/api/v1/knowledge/resolution/candidates",
        headers=_headers(token, "confirm-create"),
        json={"left_entity_id": str(left_id), "right_entity_id": str(right_id)},
    )
    candidate_id = created.json()["candidate"]["id"]

    first = client.post(
        f"/api/v1/knowledge/resolution/candidates/{candidate_id}/confirm",
        headers=_headers(token, "confirm-once"),
        json={"reason": "same person, verified manually"},
    )
    assert first.status_code == 200
    assert first.json()["status"] == "confirmed"

    second = client.post(
        f"/api/v1/knowledge/resolution/candidates/{candidate_id}/confirm",
        headers=_headers(token, "confirm-twice"),
        json={"reason": "same person, verified manually"},
    )
    assert second.status_code == 200
    assert second.json()["status"] == "confirmed"
    assert second.json()["resolved_at"] == first.json()["resolved_at"]


def test_reject_is_idempotent(
    resolution_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = resolution_test_context
    left_id = _create_entity(client, token, "reject-left", "person", "Ada Lovelace")
    right_id = _create_entity(client, token, "reject-right", "person", "Ada Lovelase")
    created = client.post(
        "/api/v1/knowledge/resolution/candidates",
        headers=_headers(token, "reject-create"),
        json={"left_entity_id": str(left_id), "right_entity_id": str(right_id)},
    )
    candidate_id = created.json()["candidate"]["id"]

    first = client.post(
        f"/api/v1/knowledge/resolution/candidates/{candidate_id}/reject",
        headers=_headers(token, "reject-once"),
        json={"reason": "different people"},
    )
    assert first.status_code == 200
    assert first.json()["status"] == "rejected"

    second = client.post(
        f"/api/v1/knowledge/resolution/candidates/{candidate_id}/reject",
        headers=_headers(token, "reject-twice"),
        json={"reason": "different people"},
    )
    assert second.status_code == 200
    assert second.json()["status"] == "rejected"


def test_confirm_after_reject_is_conflict(
    resolution_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = resolution_test_context
    left_id = _create_entity(client, token, "conflict-left", "person", "Ada Lovelace")
    right_id = _create_entity(client, token, "conflict-right", "person", "Ada Lovelase")
    created = client.post(
        "/api/v1/knowledge/resolution/candidates",
        headers=_headers(token, "conflict-create"),
        json={"left_entity_id": str(left_id), "right_entity_id": str(right_id)},
    )
    candidate_id = created.json()["candidate"]["id"]
    client.post(
        f"/api/v1/knowledge/resolution/candidates/{candidate_id}/reject",
        headers=_headers(token, "conflict-reject"),
        json={"reason": "different people"},
    )

    confirmed_after_rejected = client.post(
        f"/api/v1/knowledge/resolution/candidates/{candidate_id}/confirm",
        headers=_headers(token, "conflict-confirm"),
        json={"reason": "changed my mind"},
    )
    assert confirmed_after_rejected.status_code == 409


def test_rejection_prevents_the_same_unchanged_pair_from_being_re_proposed(
    resolution_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = resolution_test_context
    left_id = _create_entity(client, token, "reproposed-left", "person", "Ada Lovelace")
    right_id = _create_entity(client, token, "reproposed-right", "person", "Ada Lovelase")
    created = client.post(
        "/api/v1/knowledge/resolution/candidates",
        headers=_headers(token, "reproposed-create"),
        json={"left_entity_id": str(left_id), "right_entity_id": str(right_id)},
    )
    candidate_id = created.json()["candidate"]["id"]
    client.post(
        f"/api/v1/knowledge/resolution/candidates/{candidate_id}/reject",
        headers=_headers(token, "reproposed-reject"),
        json={"reason": "different people"},
    )

    reproposed = client.post(
        "/api/v1/knowledge/resolution/candidates",
        headers=_headers(token, "reproposed-recreate"),
        json={"left_entity_id": str(right_id), "right_entity_id": str(left_id)},
    )
    assert reproposed.status_code == 201
    assert reproposed.json()["candidate"]["id"] == candidate_id
    assert reproposed.json()["candidate"]["status"] == "rejected"

    listed = client.get(
        "/api/v1/knowledge/resolution/candidates", headers=_headers(token, "reproposed-list")
    )
    assert len(listed.json()["items"]) == 1


def test_exact_name_match_with_compatible_kind_never_creates_a_candidate_row(
    resolution_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    # entity_aliases carries a workspace-wide unique constraint on
    # (alias_type, normalized_value) (migration 0011), so two distinct
    # entities can never actually share an alias -- an exact, byte-for-byte
    # normalized canonical-name match is the deterministic signal this
    # schema can produce between two already-distinct entities instead.
    client, workspace_id, _user_id, token = resolution_test_context
    left_id = _create_entity(client, token, "det-left", "person", "Ada Lovelace")
    right_id = _create_entity(client, token, "det-right", "person", "ADA LOVELACE")

    response = client.post(
        "/api/v1/knowledge/resolution/candidates",
        headers=_headers(token, "det-create"),
        json={"left_entity_id": str(left_id), "right_entity_id": str(right_id)},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["deterministic"] is True
    assert body["candidate"] is None

    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM resolution_candidates WHERE workspace_id = :workspace_id"),
            {"workspace_id": workspace_id},
        ).scalar_one()
    assert count == 0


def test_self_candidate_rejected(
    resolution_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = resolution_test_context
    entity_id = _create_entity(client, token, "self-entity", "person", "Ada Lovelace")

    response = client.post(
        "/api/v1/knowledge/resolution/candidates",
        headers=_headers(token, "self-create"),
        json={"left_entity_id": str(entity_id), "right_entity_id": str(entity_id)},
    )
    assert response.status_code == 422


def _create_open_candidate(
    client: TestClient, token: str, key_prefix: str, left_name: str, right_name: str
) -> UUID:
    left_id = _create_entity(client, token, f"{key_prefix}-left", "person", left_name)
    right_id = _create_entity(client, token, f"{key_prefix}-right", "person", right_name)
    created = client.post(
        "/api/v1/knowledge/resolution/candidates",
        headers=_headers(token, f"{key_prefix}-create"),
        json={"left_entity_id": str(left_id), "right_entity_id": str(right_id)},
    )
    return UUID(created.json()["candidate"]["id"])


def test_defer_hides_candidate_from_default_list_until_it_expires(
    resolution_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, _user_id, token = resolution_test_context
    candidate_id = _create_open_candidate(client, token, "defer", "Ada Lovelace", "Ada Lovelase")
    deferred_until = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    response = client.post(
        f"/api/v1/knowledge/resolution/candidates/{candidate_id}/defer",
        headers=_headers(token, "defer-once"),
        json={"deferred_until": deferred_until},
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "open"
    assert response.json()["deferred_until"] is not None

    listed = client.get(
        "/api/v1/knowledge/resolution/candidates", headers=_headers(token, "list-after-defer")
    )
    assert all(item["id"] != str(candidate_id) for item in listed.json()["items"])

    # Once the deferral window has passed, the candidate is visible again --
    # simulated directly (no HTTP endpoint sets deferred_until to the past,
    # since the payload validator requires a future timestamp).
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE resolution_candidates SET deferred_until = :past "
                "WHERE workspace_id = :workspace_id AND id = :candidate_id"
            ),
            {
                "past": datetime.now(UTC) - timedelta(minutes=1),
                "workspace_id": workspace_id,
                "candidate_id": candidate_id,
            },
        )
    listed_after_expiry = client.get(
        "/api/v1/knowledge/resolution/candidates", headers=_headers(token, "list-after-expiry")
    )
    assert any(item["id"] == str(candidate_id) for item in listed_after_expiry.json()["items"])


def test_defer_is_idempotent(
    resolution_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = resolution_test_context
    candidate_id = _create_open_candidate(
        client, token, "defer-idem", "Grace Hopper", "Grace Hoper"
    )
    deferred_until = (datetime.now(UTC) + timedelta(hours=1)).isoformat()

    first = client.post(
        f"/api/v1/knowledge/resolution/candidates/{candidate_id}/defer",
        headers=_headers(token, "defer-idem-once"),
        json={"deferred_until": deferred_until},
    )
    second = client.post(
        f"/api/v1/knowledge/resolution/candidates/{candidate_id}/defer",
        headers=_headers(token, "defer-idem-twice"),
        json={"deferred_until": deferred_until},
    )
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["deferred_until"] == second.json()["deferred_until"]


def test_defer_rejects_a_non_future_timestamp(
    resolution_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = resolution_test_context
    candidate_id = _create_open_candidate(client, token, "defer-past", "A Name", "A Nam")

    response = client.post(
        f"/api/v1/knowledge/resolution/candidates/{candidate_id}/defer",
        headers=_headers(token, "defer-past-attempt"),
        json={"deferred_until": (datetime.now(UTC) - timedelta(hours=1)).isoformat()},
    )
    assert response.status_code == 422


def test_defer_a_decided_candidate_is_conflict(
    resolution_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = resolution_test_context
    candidate_id = _create_open_candidate(
        client, token, "defer-decided", "Ada Lovelace", "Ada Lovelase"
    )
    client.post(
        f"/api/v1/knowledge/resolution/candidates/{candidate_id}/confirm",
        headers=_headers(token, "defer-decided-confirm"),
        json={"reason": "verified"},
    )

    response = client.post(
        f"/api/v1/knowledge/resolution/candidates/{candidate_id}/defer",
        headers=_headers(token, "defer-decided-attempt"),
        json={"deferred_until": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CANDIDATE_NOT_OPEN"
