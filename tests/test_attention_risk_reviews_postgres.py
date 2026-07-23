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
def risk_review_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Risk Review Test', 'Asia/Kolkata', :created_at)"
            ),
            {"id": workspace_id, "created_at": now},
        )
        connection.execute(
            text(
                "INSERT INTO users (id, workspace_id, email, password_hash, created_at) "
                "VALUES (:id, :workspace_id, :email, 'test-password-hash', :created_at)"
            ),
            {
                "id": user_id,
                "workspace_id": workspace_id,
                "email": f"{user_id}@example.test",
                "created_at": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                "expires_at, last_seen_at) "
                "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :last_seen_at)"
            ),
            {
                "id": uuid4(),
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
                "attention_items",
                "event_outbox",
                "audit_events",
                "idempotency_records",
                "risk_reviews",
                "risks",
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


def _headers(token: str, key: str | None = None) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    headers = {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _create_evidence(workspace_id: UUID) -> UUID:
    """Seed a real, accessible `pkos_evidence` row (via a throwaway
    `pkos_nodes` row, the FK's other half) for this workspace, matching
    `test_knowledge_claims_postgres.py`'s `_create_evidence` pattern."""
    node_id = uuid4()
    evidence_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO pkos_nodes (id, workspace_id, node_type, canonical_name, "
                "attributes, created_at, updated_at) VALUES (:id, :workspace_id, 'person', "
                "'Evidence Node', '{}'::jsonb, :now, :now)"
            ),
            {"id": node_id, "workspace_id": workspace_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO pkos_evidence (id, workspace_id, node_id, source_type, "
                "source_ref, sha256, captured_at) VALUES (:id, :workspace_id, :node_id, "
                "'manual', 'test-source-ref', :sha256, :captured_at)"
            ),
            {
                "id": evidence_id,
                "workspace_id": workspace_id,
                "node_id": node_id,
                "sha256": sha256(str(evidence_id).encode()).hexdigest(),
                "captured_at": now,
            },
        )
    return evidence_id


def _create_risk(
    client: TestClient, token: str, key: str, *, review_at: datetime | None = None
) -> dict:
    payload = {"description": "Reviewed risk", "probability": 3, "impact": 3}
    if review_at is not None:
        payload["review_at"] = review_at.isoformat()
    response = client.post("/api/v1/risks", headers=_headers(token, key), json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_record_review_updates_risk_review_at_and_version_transactionally(
    risk_review_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, _, token = risk_review_test_context
    now = datetime.now(UTC)
    risk = _create_risk(client, token, "create-risk", review_at=now - timedelta(hours=1))

    next_review = now + timedelta(days=30)
    review = client.post(
        f"/api/v1/risks/{risk['id']}/review",
        headers=_headers(token, "record-review"),
        json={
            "expected_version": 1,
            "outcome": "mitigated",
            "notes": "Added a second payment processor",
            "evidence_refs": ["doc://mitigation-plan"],
            "next_review_at": next_review.isoformat(),
        },
    )
    assert review.status_code == 201, review.text
    body = review.json()
    assert body["risk_id"] == risk["id"]
    assert body["outcome"] == "mitigated"
    assert body["evidence_refs"] == ["doc://mitigation-plan"]

    updated_risk = client.get(f"/api/v1/risks/{risk['id']}")
    assert updated_risk.status_code == 200
    updated = updated_risk.json()
    assert updated["version"] == 2
    assert updated["review_at"] is not None

    with engine.connect() as connection:
        review_count = connection.execute(
            text("SELECT count(*) FROM risk_reviews WHERE workspace_id = :workspace_id"),
            {"workspace_id": workspace_id},
        ).scalar_one()
    assert review_count == 1


# ---------------------------------------------------------------------------
# Gap 5: EVIDENCE_UNAVAILABLE was documented (API-SCHEMAS.md) as a Phase 3
# error code but never wired into risk_reviews.py's evidence_refs field.
# Migration 0024 documents evidence_refs as deliberately free text (URLs,
# doc names, or evidence IDs) -- so only UUID-shaped refs are checked
# against pkos_evidence; non-UUID free text still passes through, matching
# the existing (unmodified) test above that uses "doc://mitigation-plan".
# ---------------------------------------------------------------------------


def test_review_rejects_nonexistent_evidence_ref(
    risk_review_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _, _, token = risk_review_test_context
    risk = _create_risk(client, token, "create-risk-bogus-evidence")

    bogus_evidence_id = uuid4()
    review = client.post(
        f"/api/v1/risks/{risk['id']}/review",
        headers=_headers(token, "bogus-evidence-review"),
        json={
            "expected_version": 1,
            "outcome": "no_change",
            "evidence_refs": [str(bogus_evidence_id)],
        },
    )
    assert review.status_code == 422, review.text
    assert review.json()["error"]["code"] == "EVIDENCE_UNAVAILABLE"


def test_review_rejects_evidence_ref_that_is_not_available(
    risk_review_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, _, token = risk_review_test_context
    risk = _create_risk(client, token, "create-risk-unavailable-evidence")
    evidence_id = _create_evidence(workspace_id)
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE pkos_evidence SET evidence_state = 'deleted' WHERE id = :id"),
            {"id": evidence_id},
        )

    review = client.post(
        f"/api/v1/risks/{risk['id']}/review",
        headers=_headers(token, "unavailable-evidence-review"),
        json={
            "expected_version": 1,
            "outcome": "no_change",
            "evidence_refs": [str(evidence_id)],
        },
    )
    assert review.status_code == 422, review.text
    assert review.json()["error"]["code"] == "EVIDENCE_UNAVAILABLE"


def test_review_accepts_real_accessible_evidence_ref(
    risk_review_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, _, token = risk_review_test_context
    risk = _create_risk(client, token, "create-risk-good-evidence")
    evidence_id = _create_evidence(workspace_id)

    review = client.post(
        f"/api/v1/risks/{risk['id']}/review",
        headers=_headers(token, "good-evidence-review"),
        json={
            "expected_version": 1,
            "outcome": "no_change",
            "evidence_refs": [str(evidence_id)],
        },
    )
    assert review.status_code == 201, review.text
    assert review.json()["evidence_refs"] == [str(evidence_id)]


def test_review_free_text_evidence_ref_is_not_validated_as_a_uuid(
    risk_review_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """Migration 0024's documented design: a non-UUID ref (a URL, a
    document name) is free text by intent, not a pkos_evidence reference,
    so it must pass through unchecked even though it can never resolve to
    a real evidence row."""
    client, _, _, token = risk_review_test_context
    risk = _create_risk(client, token, "create-risk-freetext-evidence")

    review = client.post(
        f"/api/v1/risks/{risk['id']}/review",
        headers=_headers(token, "freetext-evidence-review"),
        json={
            "expected_version": 1,
            "outcome": "no_change",
            "evidence_refs": ["https://example.test/incident-report.pdf"],
        },
    )
    assert review.status_code == 201, review.text
    assert review.json()["evidence_refs"] == ["https://example.test/incident-report.pdf"]


def test_review_without_explicit_next_review_at_preserves_existing_cadence(
    risk_review_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """Finding #2: ``record_risk_review`` unconditionally set
    ``risks.review_at`` to ``payload.next_review_at`` (``None`` when the
    caller didn't set one), silently cancelling any existing scheduled
    review every time an outcome was recorded without also setting a new
    one. A review must only clear/reset the cadence when it explicitly
    establishes a new one, or when the outcome closes the risk out
    entirely -- every other outcome recorded without an explicit
    ``next_review_at`` must leave the existing schedule alone.
    """
    client, _, _, token = risk_review_test_context
    now = datetime.now(UTC)
    existing_review_at = now + timedelta(days=14)
    risk = _create_risk(client, token, "create-risk-preserve-cadence", review_at=existing_review_at)

    review = client.post(
        f"/api/v1/risks/{risk['id']}/review",
        headers=_headers(token, "no-change-review-no-next"),
        json={"expected_version": 1, "outcome": "no_change"},
    )
    assert review.status_code == 201, review.text

    updated = client.get(f"/api/v1/risks/{risk['id']}")
    assert updated.status_code == 200
    assert datetime.fromisoformat(updated.json()["review_at"]) == existing_review_at


def test_review_with_explicit_next_review_at_sets_new_cadence(
    risk_review_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _, _, token = risk_review_test_context
    now = datetime.now(UTC)
    risk = _create_risk(
        client, token, "create-risk-new-cadence", review_at=now - timedelta(hours=1)
    )

    next_review = now + timedelta(days=30)
    review = client.post(
        f"/api/v1/risks/{risk['id']}/review",
        headers=_headers(token, "mitigated-review-with-next"),
        json={
            "expected_version": 1,
            "outcome": "mitigated",
            "next_review_at": next_review.isoformat(),
        },
    )
    assert review.status_code == 201, review.text

    updated = client.get(f"/api/v1/risks/{risk['id']}")
    assert datetime.fromisoformat(updated.json()["review_at"]) == next_review


def test_review_outcome_closed_clears_review_at_even_without_explicit_next(
    risk_review_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _, _, token = risk_review_test_context
    now = datetime.now(UTC)
    risk = _create_risk(
        client, token, "create-risk-close-clears", review_at=now + timedelta(days=5)
    )

    review = client.post(
        f"/api/v1/risks/{risk['id']}/review",
        headers=_headers(token, "close-clears-review-at"),
        json={"expected_version": 1, "outcome": "closed"},
    )
    assert review.status_code == 201, review.text

    updated = client.get(f"/api/v1/risks/{risk['id']}")
    assert updated.json()["review_at"] is None


def test_review_outcome_closed_also_closes_the_risk(
    risk_review_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _, _, token = risk_review_test_context
    risk = _create_risk(client, token, "create-risk-close")

    review = client.post(
        f"/api/v1/risks/{risk['id']}/review",
        headers=_headers(token, "close-via-review"),
        json={"expected_version": 1, "outcome": "closed"},
    )
    assert review.status_code == 201

    updated = client.get(f"/api/v1/risks/{risk['id']}")
    assert updated.json()["status"] == "closed"


def test_review_rejects_stale_version(
    risk_review_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _, _, token = risk_review_test_context
    risk = _create_risk(client, token, "create-risk-stale")

    stale = client.post(
        f"/api/v1/risks/{risk['id']}/review",
        headers=_headers(token, "stale-review"),
        json={"expected_version": 99, "outcome": "no_change"},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"


def test_review_idempotent_on_replay(
    risk_review_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _, _, token = risk_review_test_context
    risk = _create_risk(client, token, "create-risk-idempotent")
    headers = _headers(token, "idempotent-review")

    first = client.post(
        f"/api/v1/risks/{risk['id']}/review",
        headers=headers,
        json={"expected_version": 1, "outcome": "no_change"},
    )
    assert first.status_code == 201

    replay = client.post(
        f"/api/v1/risks/{risk['id']}/review",
        headers=headers,
        json={"expected_version": 1, "outcome": "no_change"},
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]


def test_review_conflicting_replay_returns_409_and_records_metric(
    risk_review_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """Reusing an Idempotency-Key with a materially different payload must
    409 IDEMPOTENCY_CONFLICT, and must record the same
    ``record_idempotency_conflict`` observability signal every other
    idempotency-replay path in the codebase emits on this same conflict.
    """
    from ecc.observability import render_metrics

    client, _, _, token = risk_review_test_context
    risk = _create_risk(client, token, "create-risk-conflicting")
    headers = _headers(token, "conflicting-review")

    first = client.post(
        f"/api/v1/risks/{risk['id']}/review",
        headers=headers,
        json={"expected_version": 1, "outcome": "no_change"},
    )
    assert first.status_code == 201, first.text

    conflicting = client.post(
        f"/api/v1/risks/{risk['id']}/review",
        headers=headers,
        json={"expected_version": 1, "outcome": "escalated"},
    )
    assert conflicting.status_code == 409
    assert conflicting.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert 'ecc_idempotency_conflicts_total{domain="risk_reviews"}' in render_metrics()


def test_review_queue_ordered_by_cadence_urgency(
    risk_review_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _, _, token = risk_review_test_context
    now = datetime.now(UTC)
    overdue = _create_risk(client, token, "overdue-risk", review_at=now - timedelta(hours=2))
    due_soon = _create_risk(client, token, "due-soon-risk", review_at=now + timedelta(hours=10))
    scheduled = _create_risk(client, token, "scheduled-risk", review_at=now + timedelta(days=10))
    _create_risk(client, token, "unscheduled-risk")  # no review_at: excluded from the queue

    queue = client.get("/api/v1/risks/review-queue")
    assert queue.status_code == 200
    items = queue.json()["items"]
    ids_in_order = [item["risk_id"] for item in items]
    assert ids_in_order == [overdue["id"], due_soon["id"], scheduled["id"]]
    urgencies = {item["risk_id"]: item["urgency"] for item in items}
    assert urgencies[overdue["id"]] == "overdue"
    assert urgencies[due_soon["id"]] == "due_soon"
    assert urgencies[scheduled["id"]] == "scheduled"


def test_review_queue_excludes_closed_and_archived_risks(
    risk_review_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _, _, token = risk_review_test_context
    now = datetime.now(UTC)
    risk = _create_risk(client, token, "to-be-closed", review_at=now - timedelta(hours=1))

    close = client.post(
        f"/api/v1/risks/{risk['id']}/review",
        headers=_headers(token, "close-out"),
        json={"expected_version": 1, "outcome": "closed"},
    )
    assert close.status_code == 201

    queue = client.get("/api/v1/risks/review-queue")
    assert all(item["risk_id"] != risk["id"] for item in queue.json()["items"])


def test_review_queue_hidden_across_workspaces(
    risk_review_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """A different, real workspace's session must not see the fixture
    workspace's review-queue entries -- not just a bare ``uuid4()`` 404
    probe (there's no GET-by-id for a review; the queue is the primary
    read here), which would prove nothing about workspace scoping.
    """
    client, _, _, token = risk_review_test_context
    now = datetime.now(UTC)
    risk = _create_risk(client, token, "cross-workspace-review", review_at=now - timedelta(hours=1))

    own_queue = client.get("/api/v1/risks/review-queue")
    assert own_queue.status_code == 200
    assert any(item["risk_id"] == risk["id"] for item in own_queue.json()["items"])

    other_workspace_id = uuid4()
    other_user_id = uuid4()
    other_token = f"session-{uuid4()}"
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Other Workspace', 'UTC', :now)"
            ),
            {"id": other_workspace_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO users (id, workspace_id, email, password_hash, created_at) "
                "VALUES (:id, :workspace_id, :email, 'hash', :now)"
            ),
            {
                "id": other_user_id,
                "workspace_id": other_workspace_id,
                "email": f"{other_user_id}@example.test",
                "now": now,
            },
        )
        connection.execute(
            text(
                "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                "expires_at, last_seen_at) "
                "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :now)"
            ),
            {
                "id": uuid4(),
                "workspace_id": other_workspace_id,
                "user_id": other_user_id,
                "token_hash": sha256(other_token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "now": now,
            },
        )
    other_client = TestClient(app)
    other_client.cookies.set("ecc_session", other_token)
    try:
        other_queue = other_client.get("/api/v1/risks/review-queue")
        assert other_queue.status_code == 200
        assert other_queue.json()["items"] == []
    finally:
        other_client.close()
        with engine.begin() as connection:
            for table in ("sessions", "users"):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                    {"workspace_id": other_workspace_id},
                )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": other_workspace_id},
            )
