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
