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
def recommendation_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO workspaces (id,name,timezone,created_at)
                VALUES (:id,'Recommendation Test','Asia/Kolkata',:created_at)
                """
            ),
            {"id": workspace_id, "created_at": now},
        )
        connection.execute(
            text(
                """
                INSERT INTO users (id,workspace_id,email,password_hash,created_at)
                VALUES (:id,:workspace_id,:email,'hash',:created_at)
                """
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
                """
                INSERT INTO sessions (
                    id,workspace_id,user_id,token_hash,expires_at,last_seen_at
                ) VALUES (
                    :id,:workspace_id,:user_id,:token_hash,:expires_at,:last_seen_at
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "user_id": user_id,
                "token_hash": sha256(token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=2),
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
                "recommendation_feedback",
                "recommendations",
                "event_outbox",
                "audit_events",
                "idempotency_records",
                "tasks",
                "commitments",
                "risks",
                "sessions",
                "users",
            ):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id=:workspace_id"),
                    {"workspace_id": workspace_id},
                )
            connection.execute(
                text("DELETE FROM workspaces WHERE id=:workspace_id"),
                {"workspace_id": workspace_id},
            )


def _headers(token: str, key: str | None = None) -> dict[str, str]:
    return {
        "X-CSRF-Token": new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest(),
        "X-Correlation-ID": str(uuid4()),
        "Idempotency-Key": key or str(uuid4()),
    }


def _task(workspace_id: UUID, user_id: UUID, title: str = "Recommendation target") -> UUID:
    task_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO tasks (
                    id,workspace_id,owner_id,title,status,manual_priority,pinned,
                    source_type,created_by,updated_by,created_at,updated_at,version
                ) VALUES (
                    :id,:workspace_id,:user_id,:title,'planned','medium',false,
                    'local',:user_id,:user_id,:now,:now,1
                )
                """
            ),
            {
                "id": task_id,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "title": title,
                "now": now,
            },
        )
    return task_id


def _generate(
    client: TestClient,
    token: str,
    task_id: UUID,
    *,
    expires_at: datetime | None = None,
    key: str | None = None,
) -> dict[str, object]:
    response = client.post(
        "/api/v1/recommendations",
        headers=_headers(token, key),
        json={
            "recommendation_type": "task_priority",
            "target_type": "task",
            "target_id": str(task_id),
            "proposed_action": {"operation": "set_priority", "value": "critical"},
            "expected_version": 1,
            "rationale": "Task is overdue and blocks a key outcome.",
            "confidence": 0.9,
            "evidence_ids": [],
            "expires_at": expires_at.isoformat() if expires_at else None,
            "source": "rule",
        },
    )
    assert response.status_code == 201
    return response.json()


def test_generate_publish_confirm_execute_and_replay(
    recommendation_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = recommendation_context
    task_id = _task(workspace_id, user_id)
    generated = _generate(client, token, task_id)
    recommendation_id = generated["id"]
    assert generated["status"] == "proposed"

    published = client.post(
        f"/api/v1/recommendations/{recommendation_id}/publish",
        headers=_headers(token),
        json={"expected_version": 1},
    )
    assert published.status_code == 200
    assert published.json()["status"] == "pending_confirmation"

    confirm_key = str(uuid4())
    confirmed = client.post(
        f"/api/v1/recommendations/{recommendation_id}/confirm",
        headers=_headers(token, confirm_key),
        json={"expected_version": 2, "target_expected_version": 1},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "executed"
    assert confirmed.json()["version"] == 4

    replay = client.post(
        f"/api/v1/recommendations/{recommendation_id}/confirm",
        headers=_headers(token, confirm_key),
        json={"expected_version": 2, "target_expected_version": 1},
    )
    assert replay.status_code == 200
    assert replay.json()["id"] == confirmed.json()["id"]

    with engine.connect() as connection:
        task = (
            connection.execute(
                text("SELECT manual_priority,version FROM tasks WHERE id=:id"),
                {"id": task_id},
            )
            .mappings()
            .one()
        )
        audit_count = connection.execute(
            text(
                """
                SELECT count(*) FROM audit_events
                WHERE workspace_id=:workspace_id AND aggregate_id=:recommendation_id
                """
            ),
            {"workspace_id": workspace_id, "recommendation_id": UUID(str(recommendation_id))},
        ).scalar_one()
        outbox_count = connection.execute(
            text(
                """
                SELECT count(*) FROM event_outbox
                WHERE workspace_id=:workspace_id
                  AND payload->>'recommendation_id'=:recommendation_id
                """
            ),
            {"workspace_id": workspace_id, "recommendation_id": str(recommendation_id)},
        ).scalar_one()
    assert task["manual_priority"] == "critical"
    assert task["version"] == 2
    assert audit_count == 4
    assert outbox_count == 4


def test_target_conflict_rolls_back_acceptance(
    recommendation_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = recommendation_context
    task_id = _task(workspace_id, user_id)
    generated = _generate(client, token, task_id)
    recommendation_id = generated["id"]
    client.post(
        f"/api/v1/recommendations/{recommendation_id}/publish",
        headers=_headers(token),
        json={"expected_version": 1},
    )
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE tasks SET version=2 WHERE id=:id"),
            {"id": task_id},
        )
    failed = client.post(
        f"/api/v1/recommendations/{recommendation_id}/confirm",
        headers=_headers(token),
        json={"expected_version": 2, "target_expected_version": 1},
    )
    assert failed.status_code == 409
    current = client.get(f"/api/v1/recommendations/{recommendation_id}")
    assert current.status_code == 200
    assert current.json()["status"] == "pending_confirmation"
    assert current.json()["version"] == 2


def test_reject_defer_pin_expiry_and_isolation(
    recommendation_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = recommendation_context
    task_id = _task(workspace_id, user_id)
    generated = _generate(client, token, task_id)
    recommendation_id = generated["id"]

    pinned = client.post(
        f"/api/v1/recommendations/{recommendation_id}/pin",
        headers=_headers(token),
        json={"expected_version": 1, "pinned": True},
    )
    assert pinned.status_code == 200
    assert pinned.json()["pinned"] is True

    deferred = client.post(
        f"/api/v1/recommendations/{recommendation_id}/defer",
        headers=_headers(token),
        json={
            "expected_version": 2,
            "defer_until": (datetime.now(UTC) + timedelta(hours=2)).isoformat(),
        },
    )
    assert deferred.status_code == 200

    published = client.post(
        f"/api/v1/recommendations/{recommendation_id}/publish",
        headers=_headers(token),
        json={"expected_version": 3},
    )
    assert published.status_code == 200
    rejected = client.post(
        f"/api/v1/recommendations/{recommendation_id}/reject",
        headers=_headers(token),
        json={"expected_version": 4, "reason": "Not the right priority."},
    )
    assert rejected.status_code == 200
    assert rejected.json()["status"] == "rejected"

    expired_task = _task(workspace_id, user_id, "Expired target")
    expired = _generate(
        client,
        token,
        expired_task,
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    loaded = client.get(f"/api/v1/recommendations/{expired['id']}")
    assert loaded.status_code == 200
    assert loaded.json()["status"] == "expired"

    other_workspace = uuid4()
    other_user = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO workspaces (id,name,timezone,created_at)
                VALUES (:id,'Other','UTC',:now)
                """
            ),
            {"id": other_workspace, "now": now},
        )
        connection.execute(
            text(
                """
                INSERT INTO users (id,workspace_id,email,password_hash,created_at)
                VALUES (:id,:workspace_id,:email,'hash',:now)
                """
            ),
            {
                "id": other_user,
                "workspace_id": other_workspace,
                "email": f"{other_user}@example.test",
                "now": now,
            },
        )
    try:
        cross = client.get(f"/api/v1/recommendations/{uuid4()}")
        assert cross.status_code == 404
        listed = client.get("/api/v1/recommendations")
        assert listed.status_code == 200
        assert all(item["id"] != str(uuid4()) for item in listed.json()["items"])
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM users WHERE workspace_id=:id"), {"id": other_workspace}
            )
            connection.execute(text("DELETE FROM workspaces WHERE id=:id"), {"id": other_workspace})
