from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
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
def entity_operations_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO workspaces (id, name, created_at) VALUES (:id, :name, :created_at)"),
            {"id": workspace_id, "name": "Entity Operations Test", "created_at": now},
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
                "entity_operations",
                "resolution_candidates",
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
    assert response.status_code == 201, response.text
    return UUID(response.json()["id"])


def _seed_evidence(workspace_id: UUID, node_id: UUID) -> UUID:
    # No HTTP endpoint writes pkos_evidence (evidence.py only exposes GET),
    # so seed directly, matching the pattern established in the Task 4 PR.
    evidence_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO pkos_evidence (
                    id, workspace_id, node_id, source_type, source_ref, sha256, captured_at
                ) VALUES (
                    :id, :workspace_id, :node_id, 'seed_fixture', :source_ref, :sha256, :captured_at
                )
                """
            ),
            {
                "id": evidence_id,
                "workspace_id": workspace_id,
                "node_id": node_id,
                "source_ref": f"test://entity-operations/{evidence_id}",
                "sha256": sha256(f"entity-operations-evidence-{evidence_id}".encode()).hexdigest(),
                "captured_at": now,
            },
        )
    return evidence_id


def _create_confirmed_candidate(
    client: TestClient, token: str, key_prefix: str, left_id: UUID, right_id: UUID
) -> UUID:
    created = client.post(
        "/api/v1/knowledge/resolution/candidates",
        headers=_headers(token, f"{key_prefix}-create"),
        json={"left_entity_id": str(left_id), "right_entity_id": str(right_id)},
    )
    assert created.status_code == 201, created.text
    candidate_id = created.json()["candidate"]["id"]
    confirmed = client.post(
        f"/api/v1/knowledge/resolution/candidates/{candidate_id}/confirm",
        headers=_headers(token, f"{key_prefix}-confirm"),
        json={"reason": "verified same identity"},
    )
    assert confirmed.status_code == 200, confirmed.text
    return UUID(candidate_id)


def _merge(
    client: TestClient,
    token: str,
    key: str,
    candidate_id: UUID,
    target_id: UUID,
    target_version: int,
    source_version: int,
) -> object:
    return client.post(
        "/api/v1/knowledge/entities/merge",
        headers=_headers(token, key),
        json={
            "candidate_id": str(candidate_id),
            "target_entity_id": str(target_id),
            "expected_target_version": target_version,
            "expected_source_version": source_version,
            "reason": "confirmed duplicate",
        },
    )


def test_merge_redirects_source_and_rehomes_aliases(
    entity_operations_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = entity_operations_test_context
    target_id = _create_entity(client, token, "target", "person", "Ada Lovelace")
    source_id = _create_entity(client, token, "source", "person", "Ada Lovelase")
    candidate_id = _create_confirmed_candidate(client, token, "merge-basic", target_id, source_id)

    response = _merge(client, token, "merge-once", candidate_id, target_id, 1, 1)
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["operation_type"] == "merge"
    assert body["status"] == "active"
    assert body["source_entity_id"] == str(source_id)
    assert body["target_entity_id"] == str(target_id)

    source_get = client.get(
        f"/api/v1/knowledge/entities/{source_id}", headers=_headers(token, "check-source")
    )
    assert source_get.json()["status"] == "redirected"

    target_get = client.get(
        f"/api/v1/knowledge/entities/{target_id}", headers=_headers(token, "check-target")
    )
    assert target_get.json()["status"] == "active"


def test_merge_requires_confirmed_candidate(
    entity_operations_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = entity_operations_test_context
    target_id = _create_entity(client, token, "unconf-target", "person", "Ada Lovelace")
    source_id = _create_entity(client, token, "unconf-source", "person", "Ada Lovelase")
    created = client.post(
        "/api/v1/knowledge/resolution/candidates",
        headers=_headers(token, "unconf-create"),
        json={"left_entity_id": str(target_id), "right_entity_id": str(source_id)},
    )
    candidate_id = created.json()["candidate"]["id"]

    response = _merge(client, token, "unconf-merge", UUID(candidate_id), target_id, 1, 1)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CANDIDATE_NOT_CONFIRMED"


def test_merge_rejects_target_not_in_candidate_pair(
    entity_operations_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = entity_operations_test_context
    target_id = _create_entity(client, token, "outside-target", "person", "Ada Lovelace")
    source_id = _create_entity(client, token, "outside-source", "person", "Ada Lovelase")
    unrelated_id = _create_entity(client, token, "outside-unrelated", "person", "Grace Hopper")
    candidate_id = _create_confirmed_candidate(
        client, token, "outside-candidate", target_id, source_id
    )

    response = _merge(client, token, "outside-merge", candidate_id, unrelated_id, 1, 1)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "TARGET_NOT_IN_CANDIDATE_PAIR"


def test_merge_rejects_stale_expected_version(
    entity_operations_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = entity_operations_test_context
    target_id = _create_entity(client, token, "stale-target", "person", "Ada Lovelace")
    source_id = _create_entity(client, token, "stale-source", "person", "Ada Lovelase")
    candidate_id = _create_confirmed_candidate(
        client, token, "stale-candidate", target_id, source_id
    )

    response = _merge(client, token, "stale-merge", candidate_id, target_id, 99, 1)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "VERSION_CONFLICT"


def test_merge_deduplicates_active_edges_after_rehome(
    entity_operations_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, _user_id, token = entity_operations_test_context
    target_id = _create_entity(client, token, "dedup-target", "person", "Ada Lovelace")
    source_id = _create_entity(client, token, "dedup-source", "person", "Ada Lovelase")
    shared_id = _create_entity(client, token, "dedup-shared", "project", "Analytical Engine")

    for owner_id, key in ((target_id, "dedup-target-rel"), (source_id, "dedup-source-rel")):
        evidence_id = _seed_evidence(workspace_id, owner_id)
        rel = client.post(
            f"/api/v1/knowledge/entities/{owner_id}/relationships",
            headers=_headers(token, key),
            json={
                "relationship_type": "WORKS_ON",
                "to_entity_id": str(shared_id),
                "evidence_id": str(evidence_id),
            },
        )
        assert rel.status_code == 201, rel.text

    candidate_id = _create_confirmed_candidate(
        client, token, "dedup-candidate", target_id, source_id
    )
    response = _merge(client, token, "dedup-merge", candidate_id, target_id, 1, 1)
    assert response.status_code == 201, response.text

    listed = client.get(
        f"/api/v1/knowledge/entities/{target_id}/relationships",
        headers=_headers(token, "dedup-list"),
    )
    active_to_shared = [
        item
        for item in listed.json()["items"]
        if item["status"] == "active" and item["to_entity_id"] == str(shared_id)
    ]
    assert len(active_to_shared) == 1


def test_concurrent_merges_on_overlapping_source_do_not_double_redirect(
    entity_operations_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """Regression/coverage test for optimistic concurrency on merge, mirroring
    tests/test_task_postgres.py::test_concurrent_updates_with_same_expected_version_do_not_both_succeed.
    Two distinct confirmed candidates both name the same entity as their
    source; only one merge may actually redirect it."""
    client, workspace_id, _user_id, token = entity_operations_test_context
    target_a = _create_entity(client, token, "race-target-a", "person", "Ada Lovelace")
    target_b = _create_entity(client, token, "race-target-b", "person", "A. Lovelace")
    source_id = _create_entity(client, token, "race-source", "person", "Ada L.")

    candidate_a = _create_confirmed_candidate(
        client, token, "race-candidate-a", target_a, source_id
    )
    candidate_b = _create_confirmed_candidate(
        client, token, "race-candidate-b", target_b, source_id
    )

    def merge_once(label: str, candidate_id: UUID, target_id: UUID) -> int:
        worker = TestClient(app)
        worker.cookies.set("ecc_session", token)
        try:
            response = worker.post(
                "/api/v1/knowledge/entities/merge",
                headers=_headers(token, f"race-merge-{label}"),
                json={
                    "candidate_id": str(candidate_id),
                    "target_entity_id": str(target_id),
                    "expected_target_version": 1,
                    "expected_source_version": 1,
                    "reason": "race",
                },
            )
            return response.status_code
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda args: merge_once(*args),
                [("a", candidate_a, target_a), ("b", candidate_b, target_b)],
            )
        )

    assert sorted(results) == [201, 409]

    with engine.connect() as connection:
        status = connection.execute(
            text("SELECT status FROM pkos_nodes WHERE id = :id"), {"id": source_id}
        ).scalar_one()
    assert status == "redirected"

    with engine.connect() as connection:
        merge_count = connection.execute(
            text(
                "SELECT count(*) FROM entity_operations "
                "WHERE workspace_id = :workspace_id AND operation_type = 'merge' "
                "AND status = 'active'"
            ),
            {"workspace_id": workspace_id},
        ).scalar_one()
    assert merge_count == 1


def test_reversal_restores_source_to_active(
    entity_operations_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = entity_operations_test_context
    target_id = _create_entity(client, token, "rev-target", "person", "Ada Lovelace")
    source_id = _create_entity(client, token, "rev-source", "person", "Ada Lovelase")
    candidate_id = _create_confirmed_candidate(client, token, "rev-candidate", target_id, source_id)
    merged = _merge(client, token, "rev-merge", candidate_id, target_id, 1, 1)
    operation_id = merged.json()["id"]

    reversed_response = client.post(
        f"/api/v1/knowledge/entity-operations/{operation_id}/reverse",
        headers=_headers(token, "rev-reverse"),
        json={"reason": "merged in error"},
    )
    assert reversed_response.status_code == 201, reversed_response.text
    assert reversed_response.json()["operation_type"] == "reverse"
    assert reversed_response.json()["reverses_operation_id"] == operation_id

    source_get = client.get(
        f"/api/v1/knowledge/entities/{source_id}", headers=_headers(token, "rev-check-source")
    )
    assert source_get.json()["status"] == "active"


def test_reversal_rejected_when_target_has_post_merge_dependent_activity(
    entity_operations_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, _user_id, token = entity_operations_test_context
    target_id = _create_entity(client, token, "unsafe-target", "person", "Ada Lovelace")
    source_id = _create_entity(client, token, "unsafe-source", "person", "Ada Lovelase")
    candidate_id = _create_confirmed_candidate(
        client, token, "unsafe-candidate", target_id, source_id
    )
    merged = _merge(client, token, "unsafe-merge", candidate_id, target_id, 1, 1)
    operation_id = merged.json()["id"]

    evidence_id = _seed_evidence(workspace_id, target_id)
    claim = client.post(
        f"/api/v1/knowledge/entities/{target_id}/claims",
        headers=_headers(token, "unsafe-claim"),
        json={
            "predicate": "role",
            "value": {"title": "Mathematician"},
            "source_id": str(evidence_id),
        },
    )
    assert claim.status_code == 201, claim.text

    response = client.post(
        f"/api/v1/knowledge/entity-operations/{operation_id}/reverse",
        headers=_headers(token, "unsafe-reverse"),
        json={"reason": "attempt after dependent activity"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "UNSAFE_REVERSAL"


def test_reverse_already_reversed_is_conflict(
    entity_operations_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = entity_operations_test_context
    target_id = _create_entity(client, token, "twice-target", "person", "Ada Lovelace")
    source_id = _create_entity(client, token, "twice-source", "person", "Ada Lovelase")
    candidate_id = _create_confirmed_candidate(
        client, token, "twice-candidate", target_id, source_id
    )
    merged = _merge(client, token, "twice-merge", candidate_id, target_id, 1, 1)
    operation_id = merged.json()["id"]

    first = client.post(
        f"/api/v1/knowledge/entity-operations/{operation_id}/reverse",
        headers=_headers(token, "twice-reverse-1"),
        json={"reason": "first reversal"},
    )
    assert first.status_code == 201

    second = client.post(
        f"/api/v1/knowledge/entity-operations/{operation_id}/reverse",
        headers=_headers(token, "twice-reverse-2"),
        json={"reason": "second reversal"},
    )
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "OPERATION_ALREADY_REVERSED"
