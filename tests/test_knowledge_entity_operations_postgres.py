from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from json import dumps
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


def _seed_alias(workspace_id: UUID, entity_id: UUID, normalized_value: str) -> UUID:
    source_id = _seed_evidence(workspace_id, entity_id)
    alias_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO entity_aliases (id, workspace_id, entity_id, alias_type, "
                "normalized_value, source_id, created_at) VALUES (:id, :workspace_id, "
                ":entity_id, 'nickname', :normalized_value, :source_id, :created_at)"
            ),
            {
                "id": alias_id,
                "workspace_id": workspace_id,
                "entity_id": entity_id,
                "normalized_value": normalized_value,
                "source_id": source_id,
                "created_at": now,
            },
        )
    return alias_id


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
    client, workspace_id, _user_id, token = entity_operations_test_context
    target_id = _create_entity(client, token, "target", "person", "Ada Lovelace")
    source_id = _create_entity(client, token, "source", "person", "Ada Lovelase")
    source_alias_id = _seed_alias(workspace_id, source_id, "countess of lovelace")
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

    # The alias the source entity had before merge must now be reachable
    # from the target (rehomed), not left orphaned on the redirected source.
    target_aliases = client.get(
        f"/api/v1/knowledge/entities/{target_id}/aliases", headers=_headers(token, "target-aliases")
    )
    assert target_aliases.status_code == 200
    rehomed_items = target_aliases.json()["items"]
    rehomed_ids = [item["id"] for item in rehomed_items]
    assert str(source_alias_id) in rehomed_ids
    assert any(item["normalized_value"] == "countess of lovelace" for item in rehomed_items)


def test_merge_rehome_bumps_alias_version_and_updated_at(
    entity_operations_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    # Schema-hygiene regression test: migration 0019 added updated_at/
    # version to entity_aliases (mutated in place by merge's alias-rehome
    # step) since DATA-MODEL.md's Rules section requires "optimistic
    # version" on every mutable table. Proves _rehome_aliases actually
    # maintains them, not just that the columns exist.
    client, workspace_id, _user_id, token = entity_operations_test_context
    target_id = _create_entity(client, token, "version-target", "person", "Ada Lovelace")
    source_id = _create_entity(client, token, "version-source", "person", "Ada Lovelase")
    alias_id = _seed_alias(workspace_id, source_id, "countess of lovelace v2")
    candidate_id = _create_confirmed_candidate(
        client, token, "version-merge-candidate", target_id, source_id
    )

    with engine.connect() as connection:
        before = connection.execute(
            text("SELECT version, updated_at, created_at FROM entity_aliases WHERE id = :id"),
            {"id": alias_id},
        ).mappings().one()
    assert before["version"] == 1

    response = _merge(client, token, "version-merge", candidate_id, target_id, 1, 1)
    assert response.status_code == 201, response.text

    with engine.connect() as connection:
        after = connection.execute(
            text("SELECT version, updated_at, created_at FROM entity_aliases WHERE id = :id"),
            {"id": alias_id},
        ).mappings().one()
    assert after["version"] == 2
    assert after["updated_at"] > after["created_at"]


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

    # Schema-hygiene regression test (migration 0019): reverse marks the
    # original merge row status='reversed' -- proves that mutation also
    # bumps version/updated_at, not just the columns' existence.
    with engine.connect() as connection:
        merge_row = connection.execute(
            text("SELECT version, updated_at, created_at, status FROM entity_operations WHERE id = :id"),
            {"id": operation_id},
        ).mappings().one()
    assert merge_row["status"] == "reversed"
    assert merge_row["version"] == 2
    assert merge_row["updated_at"] > merge_row["created_at"]


def test_reversal_refreshes_source_retrieval_projection_so_it_is_not_stale(
    entity_operations_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    # Regression test: reverse used to restore pkos_nodes.status/version to
    # active without refreshing retrieval_documents, so the reactivated
    # entity reappeared in search results with a false stale:true (its
    # projection's source_version still reflected the version stamped
    # before the merge redirected it out of search).
    client, _workspace_id, _user_id, token = entity_operations_test_context
    target_id = _create_entity(client, token, "rev-stale-target", "person", "Ada Lovelace")
    source_id = _create_entity(client, token, "rev-stale-source", "person", "Ada Lovelase")
    candidate_id = _create_confirmed_candidate(
        client, token, "rev-stale-candidate", target_id, source_id
    )
    merged = _merge(client, token, "rev-stale-merge", candidate_id, target_id, 1, 1)
    operation_id = merged.json()["id"]

    reversed_response = client.post(
        f"/api/v1/knowledge/entity-operations/{operation_id}/reverse",
        headers=_headers(token, "rev-stale-reverse"),
        json={"reason": "merged in error"},
    )
    assert reversed_response.status_code == 201, reversed_response.text

    search = client.get(
        "/api/v1/knowledge/retrieve",
        headers=_headers(token, "rev-stale-search"),
        params={"q": "Ada Lovelase"},
    )
    assert search.status_code == 200, search.text
    matches = [item for item in search.json()["items"] if item["entity_id"] == str(source_id)]
    assert matches, "reactivated source entity should be searchable again"
    assert matches[0]["stale"] is False


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


def test_split_is_the_manual_path_when_reverse_would_be_unsafe(
    entity_operations_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, _user_id, token = entity_operations_test_context
    target_id = _create_entity(client, token, "split-target", "person", "Ada Lovelace")
    source_id = _create_entity(client, token, "split-source", "person", "Ada Lovelase")
    candidate_id = _create_confirmed_candidate(
        client, token, "split-candidate", target_id, source_id
    )
    merged = _merge(client, token, "split-merge", candidate_id, target_id, 1, 1)
    operation_id = merged.json()["id"]

    # A claim recorded on the target after the merge -- exactly the
    # condition that makes plain reverse unsafe (verified below).
    evidence_id = _seed_evidence(workspace_id, target_id)
    claim = client.post(
        f"/api/v1/knowledge/entities/{target_id}/claims",
        headers=_headers(token, "split-claim"),
        json={
            "predicate": "role",
            "value": {"title": "Mathematician"},
            "source_id": str(evidence_id),
        },
    )
    claim_id = claim.json()["id"]

    unsafe_reverse = client.post(
        f"/api/v1/knowledge/entity-operations/{operation_id}/reverse",
        headers=_headers(token, "split-confirm-unsafe"),
        json={"reason": "attempt"},
    )
    assert unsafe_reverse.status_code == 422
    assert unsafe_reverse.json()["error"]["code"] == "UNSAFE_REVERSAL"

    split = client.post(
        f"/api/v1/knowledge/entity-operations/{operation_id}/split",
        headers=_headers(token, "split-once"),
        json={"reason": "the post-merge claim belongs to the source", "reassign_claim_ids": [claim_id]},
    )
    assert split.status_code == 201, split.text
    assert split.json()["operation_type"] == "split"
    assert split.json()["reverses_operation_id"] == operation_id
    assert split.json()["source_entity_id"] == str(source_id)
    assert split.json()["target_entity_id"] == str(target_id)

    source_get = client.get(
        f"/api/v1/knowledge/entities/{source_id}", headers=_headers(token, "split-check-source")
    )
    assert source_get.json()["status"] == "active"

    source_claims = client.get(
        f"/api/v1/knowledge/entities/{source_id}/claims", headers=_headers(token, "split-source-claims")
    )
    assert any(item["id"] == claim_id for item in source_claims.json()["items"])

    target_claims = client.get(
        f"/api/v1/knowledge/entities/{target_id}/claims", headers=_headers(token, "split-target-claims")
    )
    assert all(item["id"] != claim_id for item in target_claims.json()["items"])


def test_split_reassigns_relationships_too(
    entity_operations_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, _user_id, token = entity_operations_test_context
    target_id = _create_entity(client, token, "split-rel-target", "person", "Ada Lovelace")
    source_id = _create_entity(client, token, "split-rel-source", "person", "Ada Lovelase")
    other_id = _create_entity(client, token, "split-rel-other", "project", "Analytical Engine")
    candidate_id = _create_confirmed_candidate(
        client, token, "split-rel-candidate", target_id, source_id
    )
    merged = _merge(client, token, "split-rel-merge", candidate_id, target_id, 1, 1)
    operation_id = merged.json()["id"]

    evidence_id = _seed_evidence(workspace_id, target_id)
    relationship = client.post(
        f"/api/v1/knowledge/entities/{target_id}/relationships",
        headers=_headers(token, "split-rel-create"),
        json={
            "relationship_type": "WORKS_ON",
            "to_entity_id": str(other_id),
            "evidence_id": str(evidence_id),
        },
    )
    relationship_id = relationship.json()["id"]

    split = client.post(
        f"/api/v1/knowledge/entity-operations/{operation_id}/split",
        headers=_headers(token, "split-rel-once"),
        json={
            "reason": "this relationship belongs to the source",
            "reassign_relationship_ids": [relationship_id],
        },
    )
    assert split.status_code == 201, split.text

    source_relationships = client.get(
        f"/api/v1/knowledge/entities/{source_id}/relationships",
        headers=_headers(token, "split-rel-source-check"),
    )
    assert any(item["id"] == relationship_id for item in source_relationships.json()["items"])


def test_split_rejects_claim_not_belonging_to_target(
    entity_operations_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = entity_operations_test_context
    target_id = _create_entity(client, token, "split-bad-target", "person", "Ada Lovelace")
    source_id = _create_entity(client, token, "split-bad-source", "person", "Ada Lovelase")
    candidate_id = _create_confirmed_candidate(
        client, token, "split-bad-candidate", target_id, source_id
    )
    merged = _merge(client, token, "split-bad-merge", candidate_id, target_id, 1, 1)
    operation_id = merged.json()["id"]

    response = client.post(
        f"/api/v1/knowledge/entity-operations/{operation_id}/split",
        headers=_headers(token, "split-bad-attempt"),
        json={"reason": "attempt", "reassign_claim_ids": [str(uuid4())]},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "CLAIM_NOT_ON_TARGET"


def test_concurrent_splits_reassigning_the_same_claim_do_not_both_succeed(
    entity_operations_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """Coverage for concurrent split calls that both name the same target
    claim: two different merges (source_a and source_b, both redirected
    into the same target) each get their own split operation, and both
    name the SAME target claim in their reassign_claim_ids. The
    target-entity lock both splits take before validating claim ownership
    already serializes them (confirmed by hand: the ownership-validation
    SELECT's FOR UPDATE is defense-in-depth, not what prevents this race --
    see its comment), so exactly one must win with 201 and the other must
    see the claim as no longer belonging to target and get 422, never both
    succeeding or the loser silently reassigning zero rows while still
    reporting success."""
    client, workspace_id, _user_id, token = entity_operations_test_context
    target_id = _create_entity(client, token, "split-race-target", "person", "Ada Lovelace")
    source_a = _create_entity(client, token, "split-race-source-a", "person", "Ada L. A")
    source_b = _create_entity(client, token, "split-race-source-b", "person", "Ada L. B")

    candidate_a = _create_confirmed_candidate(
        client, token, "split-race-candidate-a", target_id, source_a
    )
    merge_a = _merge(client, token, "split-race-merge-a", candidate_a, target_id, 1, 1)
    operation_a = merge_a.json()["id"]

    candidate_b = _create_confirmed_candidate(
        client, token, "split-race-candidate-b", target_id, source_b
    )
    merge_b = _merge(client, token, "split-race-merge-b", candidate_b, target_id, 1, 1)
    operation_b = merge_b.json()["id"]

    evidence_id = _seed_evidence(workspace_id, target_id)
    claim = client.post(
        f"/api/v1/knowledge/entities/{target_id}/claims",
        headers=_headers(token, "split-race-claim"),
        json={
            "predicate": "role",
            "value": {"title": "Mathematician"},
            "source_id": str(evidence_id),
        },
    )
    claim_id = claim.json()["id"]

    def split_once(label: str, operation_id: str) -> int:
        worker = TestClient(app)
        worker.cookies.set("ecc_session", token)
        try:
            response = worker.post(
                f"/api/v1/knowledge/entity-operations/{operation_id}/split",
                headers=_headers(token, f"split-race-attempt-{label}"),
                json={"reason": "race", "reassign_claim_ids": [claim_id]},
            )
            return response.status_code
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(
            executor.map(
                lambda args: split_once(*args),
                [("a", operation_a), ("b", operation_b)],
            )
        )

    assert sorted(results) == [201, 422]

    with engine.connect() as connection:
        subject_id = connection.execute(
            text("SELECT subject_id FROM knowledge_claims WHERE id = :id"), {"id": claim_id}
        ).scalar_one()
    assert str(subject_id) in (str(source_a), str(source_b))


def test_split_requires_a_merge_operation(
    entity_operations_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = entity_operations_test_context
    target_id = _create_entity(client, token, "split-notmerge-target", "person", "Ada Lovelace")
    source_id = _create_entity(client, token, "split-notmerge-source", "person", "Ada Lovelase")
    candidate_id = _create_confirmed_candidate(
        client, token, "split-notmerge-candidate", target_id, source_id
    )
    merged = _merge(client, token, "split-notmerge-merge", candidate_id, target_id, 1, 1)
    operation_id = merged.json()["id"]
    reversed_op = client.post(
        f"/api/v1/knowledge/entity-operations/{operation_id}/reverse",
        headers=_headers(token, "split-notmerge-reverse"),
        json={"reason": "undo"},
    )
    reverse_operation_id = reversed_op.json()["id"]

    response = client.post(
        f"/api/v1/knowledge/entity-operations/{reverse_operation_id}/split",
        headers=_headers(token, "split-notmerge-attempt"),
        json={"reason": "attempt"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "NOT_A_MERGE_OPERATION"


def test_split_an_already_reversed_merge_is_conflict(
    entity_operations_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = entity_operations_test_context
    target_id = _create_entity(client, token, "split-twice-target", "person", "Ada Lovelace")
    source_id = _create_entity(client, token, "split-twice-source", "person", "Ada Lovelase")
    candidate_id = _create_confirmed_candidate(
        client, token, "split-twice-candidate", target_id, source_id
    )
    merged = _merge(client, token, "split-twice-merge", candidate_id, target_id, 1, 1)
    operation_id = merged.json()["id"]
    client.post(
        f"/api/v1/knowledge/entity-operations/{operation_id}/reverse",
        headers=_headers(token, "split-twice-reverse"),
        json={"reason": "undo"},
    )

    response = client.post(
        f"/api/v1/knowledge/entity-operations/{operation_id}/split",
        headers=_headers(token, "split-twice-attempt"),
        json={"reason": "attempt"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "OPERATION_ALREADY_REVERSED"


def _seed_foreign_workspace_merge_operation() -> tuple[UUID, UUID, UUID, UUID]:
    """A second, fully independent workspace with a confirmed candidate and
    a completed merge operation, for proving these mutation endpoints never
    act on a resource that belongs to a different workspace."""
    other_workspace_id = uuid4()
    target_id, source_id = uuid4(), uuid4()
    candidate_id = uuid4()
    operation_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO workspaces (id, name, created_at) VALUES (:id, :name, :created_at)"),
            {"id": other_workspace_id, "name": "Foreign Workspace", "created_at": now},
        )
        for entity_id, name, status in (
            (target_id, "Foreign Target", "active"),
            (source_id, "Foreign Source", "redirected"),
        ):
            connection.execute(
                text(
                    "INSERT INTO pkos_nodes (id, workspace_id, node_type, canonical_name, "
                    "attributes, status, confidence, version, created_at, updated_at) VALUES "
                    "(:id, :workspace_id, 'person', :name, '{}'::jsonb, :status, 1.0, 1, :now, :now)"
                ),
                {
                    "id": entity_id,
                    "workspace_id": other_workspace_id,
                    "name": name,
                    "status": status,
                    "now": now,
                },
            )
        connection.execute(
            text(
                "INSERT INTO resolution_candidates (id, workspace_id, left_entity_id, "
                "right_entity_id, score, factors_json, resolver_version, status, "
                "resolved_at, resolved_by, reason, created_at) VALUES (:id, :workspace_id, "
                ":target_id, :source_id, 0.95, '{}'::jsonb, 'test', 'confirmed', :now, "
                ":resolved_by, 'merged', :now)"
            ),
            {
                "id": candidate_id,
                "workspace_id": other_workspace_id,
                "target_id": target_id,
                "source_id": source_id,
                "resolved_by": uuid4(),
                "now": now,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO entity_operations (
                    id, workspace_id, operation_type, status, inputs_json,
                    outputs_json, actor_id, reason, created_at
                ) VALUES (
                    :id, :workspace_id, 'merge', 'active', CAST(:inputs_json AS jsonb),
                    '{}'::jsonb, :actor_id, 'foreign merge', :now
                )
                """
            ),
            {
                "id": operation_id,
                "workspace_id": other_workspace_id,
                "inputs_json": dumps(
                    {
                        "candidate_id": str(candidate_id),
                        "target_entity_id": str(target_id),
                        "source_entity_id": str(source_id),
                    }
                ),
                "actor_id": uuid4(),
                "now": now,
            },
        )
    return other_workspace_id, candidate_id, operation_id, target_id


def _teardown_foreign_workspace(workspace_id: UUID) -> None:
    with engine.begin() as connection:
        for table in ("entity_operations", "resolution_candidates", "pkos_nodes"):
            connection.execute(
                text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                {"workspace_id": workspace_id},
            )
        connection.execute(
            text("DELETE FROM workspaces WHERE id = :workspace_id"), {"workspace_id": workspace_id}
        )


def test_merge_rejects_candidate_from_another_workspace(
    entity_operations_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = entity_operations_test_context
    other_workspace_id, foreign_candidate_id, _operation_id, foreign_target_id = (
        _seed_foreign_workspace_merge_operation()
    )
    try:
        response = client.post(
            "/api/v1/knowledge/entities/merge",
            headers=_headers(token, "isolation-merge-foreign-candidate"),
            json={
                "candidate_id": str(foreign_candidate_id),
                "target_entity_id": str(foreign_target_id),
                "expected_target_version": 1,
                "expected_source_version": 1,
                "reason": "cross-workspace attempt",
            },
        )
        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == "CANDIDATE_NOT_FOUND"
    finally:
        _teardown_foreign_workspace(other_workspace_id)


def test_reverse_rejects_operation_from_another_workspace(
    entity_operations_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = entity_operations_test_context
    other_workspace_id, _candidate_id, foreign_operation_id, _target_id = (
        _seed_foreign_workspace_merge_operation()
    )
    try:
        response = client.post(
            f"/api/v1/knowledge/entity-operations/{foreign_operation_id}/reverse",
            headers=_headers(token, "isolation-reverse-foreign"),
            json={"reason": "cross-workspace attempt"},
        )
        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == "OPERATION_NOT_FOUND"
    finally:
        _teardown_foreign_workspace(other_workspace_id)


def test_split_rejects_operation_from_another_workspace(
    entity_operations_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = entity_operations_test_context
    other_workspace_id, _candidate_id, foreign_operation_id, _target_id = (
        _seed_foreign_workspace_merge_operation()
    )
    try:
        response = client.post(
            f"/api/v1/knowledge/entity-operations/{foreign_operation_id}/split",
            headers=_headers(token, "isolation-split-foreign"),
            json={"reason": "cross-workspace attempt"},
        )
        assert response.status_code == 404, response.text
        assert response.json()["error"]["code"] == "OPERATION_NOT_FOUND"
    finally:
        _teardown_foreign_workspace(other_workspace_id)
