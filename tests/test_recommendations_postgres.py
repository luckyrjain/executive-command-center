from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from identity_fixtures import create_identity
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
        create_identity(
            connection,
            workspace_id=workspace_id,
            user_id=user_id,
            email=f"{user_id}@example.test",
            now=now,
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


def _commitment(
    workspace_id: UUID,
    user_id: UUID,
    status: str = "active",
    summary: str = "Recommendation target",
) -> UUID:
    commitment_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO commitments (
                    id,workspace_id,owner_id,summary,direction,status,importance,pinned,
                    created_by,updated_by,created_at,updated_at,version
                ) VALUES (
                    :id,:workspace_id,:user_id,:summary,'made_by_me',:status,'medium',false,
                    :user_id,:user_id,:now,:now,1
                )
                """
            ),
            {
                "id": commitment_id,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "summary": summary,
                "status": status,
                "now": now,
            },
        )
    return commitment_id


def _generate(
    client: TestClient,
    token: str,
    task_id: UUID,
    *,
    expires_at: datetime | None = None,
    key: str | None = None,
    recommendation_type: str = "task_priority",
) -> dict[str, object]:
    response = client.post(
        "/api/v1/recommendations",
        headers=_headers(token, key),
        json={
            "recommendation_type": recommendation_type,
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


def test_idempotency_record_ttl_matches_every_other_domain(
    recommendation_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """`recommendation_storage.save_cached` hardcoded `timedelta(hours=24)`
    for `idempotency_records.expires_at` -- every other domain (including
    `ecc.platform.idempotency` itself) uses `timedelta(days=365)`. A
    recommendation replay arriving more than 24h after the original request
    silently lost idempotency protection and would double-execute, unlike
    the structurally identical `risks`/`commitments`/`tasks` endpoints.
    """
    client, workspace_id, user_id, token = recommendation_context
    task_id = _task(workspace_id, user_id)
    idempotency_key = str(uuid4())
    before = datetime.now(UTC)
    response = client.post(
        "/api/v1/recommendations",
        headers=_headers(token, idempotency_key),
        json={
            "recommendation_type": "task_priority",
            "target_type": "task",
            "target_id": str(task_id),
            "proposed_action": {"operation": "set_priority", "value": "critical"},
            "expected_version": 1,
            "rationale": "TTL regression coverage.",
            "confidence": 0.9,
            "evidence_ids": [],
            "expires_at": None,
            "source": "rule",
        },
    )
    assert response.status_code == 201

    with engine.connect() as connection:
        expires_at = connection.execute(
            text(
                """
                SELECT expires_at FROM idempotency_records
                WHERE workspace_id=:workspace_id AND actor_id=:actor_id AND key=:key
                """
            ),
            {"workspace_id": workspace_id, "actor_id": user_id, "key": idempotency_key},
        ).scalar_one()
    ttl = expires_at - before
    assert ttl > timedelta(days=300)


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
        create_identity(
            connection,
            workspace_id=other_workspace,
            user_id=other_user,
            email=f"{other_user}@example.test",
            now=now,
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


def test_list_filters_by_recommendation_type_server_side(
    recommendation_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    # Loop 2 round 5 review (PR #130): a domain-scoped embed of the shared
    # recommendation review UI (e.g. GmailPanel filtering to
    # `email_action_detected`) used to filter the already-paginated page
    # client-side, which could silently hide its own items behind a
    # workspace-wide backlog of other-typed recommendations. `recommendation_
    # type` is now filtered server-side, before `limit` applies.
    client, workspace_id, user_id, token = recommendation_context
    priority_task = _task(workspace_id, user_id, "Priority target")
    other_task = _task(workspace_id, user_id, "Other target")
    priority = _generate(client, token, priority_task, recommendation_type="task_priority")
    other = _generate(client, token, other_task, recommendation_type="email_action_detected")

    filtered = client.get(
        "/api/v1/recommendations", params={"recommendation_type": "email_action_detected"}
    )
    assert filtered.status_code == 200
    filtered_ids = [item["id"] for item in filtered.json()["items"]]
    assert filtered_ids == [other["id"]]
    assert priority["id"] not in filtered_ids

    unfiltered = client.get("/api/v1/recommendations")
    assert unfiltered.status_code == 200
    unfiltered_ids = {item["id"] for item in unfiltered.json()["items"]}
    assert {priority["id"], other["id"]} <= unfiltered_ids


def test_supersession_and_stored_target_version_enforcement(
    recommendation_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = recommendation_context
    task_id = _task(workspace_id, user_id)
    first = _generate(client, token, task_id)
    second = _generate(client, token, task_id)

    first_loaded = client.get(f"/api/v1/recommendations/{first['id']}")
    assert first_loaded.status_code == 200
    assert first_loaded.json()["status"] == "superseded"

    published = client.post(
        f"/api/v1/recommendations/{second['id']}/publish",
        headers=_headers(token),
        json={"expected_version": 1},
    )
    assert published.status_code == 200

    rejected_override = client.post(
        f"/api/v1/recommendations/{second['id']}/confirm",
        headers=_headers(token),
        json={"expected_version": 2, "target_expected_version": 2},
    )
    assert rejected_override.status_code == 409


def test_expiry_emits_audit_and_outbox(
    recommendation_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = recommendation_context
    task_id = _task(workspace_id, user_id)
    expired = _generate(
        client,
        token,
        task_id,
        expires_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    loaded = client.get(f"/api/v1/recommendations/{expired['id']}")
    assert loaded.status_code == 200
    assert loaded.json()["status"] == "expired"
    with engine.connect() as connection:
        audit_count = connection.execute(
            text(
                """
                SELECT count(*) FROM audit_events
                WHERE workspace_id=:workspace_id
                  AND aggregate_id=:recommendation_id
                  AND event_type='recommendation.expired'
                """
            ),
            {
                "workspace_id": workspace_id,
                "recommendation_id": UUID(str(expired["id"])),
            },
        ).scalar_one()
        outbox_count = connection.execute(
            text(
                """
                SELECT count(*) FROM event_outbox
                WHERE workspace_id=:workspace_id
                  AND event_type='recommendation.expired.v1'
                """
            ),
            {"workspace_id": workspace_id},
        ).scalar_one()
    assert audit_count == 1
    assert outbox_count == 1


def _generate_create(
    client: TestClient,
    token: str,
    target_type: str,
    proposed_fields: dict[str, object],
    *,
    key: str | None = None,
) -> httpx.Response:
    return client.post(
        "/api/v1/recommendations",
        headers=_headers(token, key),
        json={
            "recommendation_type": f"{target_type}_detected",
            "target_type": target_type,
            "target_id": None,
            "proposed_action": {"operation": "create", "value": None},
            "proposed_fields": proposed_fields,
            "rationale": "Detected from an email thread requiring action.",
            "confidence": 0.87,
            "evidence_ids": [],
            "source": "ai",
        },
    )


def test_generate_rejects_create_with_target_id_or_expected_version(
    recommendation_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = recommendation_context
    with_target_id = client.post(
        "/api/v1/recommendations",
        headers=_headers(token),
        json={
            "recommendation_type": "task_detected",
            "target_type": "task",
            "target_id": str(uuid4()),
            "proposed_action": {"operation": "create", "value": None},
            "proposed_fields": {"title": "Follow up with vendor"},
            "rationale": "Detected action item.",
            "confidence": 0.8,
            "evidence_ids": [],
            "source": "ai",
        },
    )
    assert with_target_id.status_code == 422

    with_expected_version = client.post(
        "/api/v1/recommendations",
        headers=_headers(token),
        json={
            "recommendation_type": "task_detected",
            "target_type": "task",
            "target_id": None,
            "proposed_action": {"operation": "create", "value": None},
            "proposed_fields": {"title": "Follow up with vendor"},
            "expected_version": 1,
            "rationale": "Detected action item.",
            "confidence": 0.8,
            "evidence_ids": [],
            "source": "ai",
        },
    )
    assert with_expected_version.status_code == 422

    without_proposed_fields = client.post(
        "/api/v1/recommendations",
        headers=_headers(token),
        json={
            "recommendation_type": "task_detected",
            "target_type": "task",
            "target_id": None,
            "proposed_action": {"operation": "create", "value": None},
            "rationale": "Detected action item.",
            "confidence": 0.8,
            "evidence_ids": [],
            "source": "ai",
        },
    )
    assert without_proposed_fields.status_code == 422


def test_generate_rejects_non_create_missing_target_id_or_expected_version(
    recommendation_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = recommendation_context
    task_id = _task(workspace_id, user_id)
    missing_target_id = client.post(
        "/api/v1/recommendations",
        headers=_headers(token),
        json={
            "recommendation_type": "task_priority",
            "target_type": "task",
            "target_id": None,
            "proposed_action": {"operation": "set_priority", "value": "critical"},
            "expected_version": 1,
            "rationale": "Task is overdue.",
            "confidence": 0.9,
            "evidence_ids": [],
            "source": "rule",
        },
    )
    assert missing_target_id.status_code == 422

    missing_expected_version = client.post(
        "/api/v1/recommendations",
        headers=_headers(token),
        json={
            "recommendation_type": "task_priority",
            "target_type": "task",
            "target_id": str(task_id),
            "proposed_action": {"operation": "set_priority", "value": "critical"},
            "rationale": "Task is overdue.",
            "confidence": 0.9,
            "evidence_ids": [],
            "source": "rule",
        },
    )
    assert missing_expected_version.status_code == 422

    with_proposed_fields = client.post(
        "/api/v1/recommendations",
        headers=_headers(token),
        json={
            "recommendation_type": "task_priority",
            "target_type": "task",
            "target_id": str(task_id),
            "proposed_action": {"operation": "set_priority", "value": "critical"},
            "expected_version": 1,
            "proposed_fields": {"title": "Should not be here"},
            "rationale": "Task is overdue.",
            "confidence": 0.9,
            "evidence_ids": [],
            "source": "rule",
        },
    )
    assert with_proposed_fields.status_code == 422


def test_generate_rejects_proposed_fields_that_fail_the_targets_own_create_schema(
    recommendation_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = recommendation_context
    response = _generate_create(client, token, "task", {"title": ""})
    assert response.status_code == 422


def test_create_task_recommendation_end_to_end(
    recommendation_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = recommendation_context
    generated = _generate_create(
        client,
        token,
        "task",
        {"title": "Send the signed contract back", "manual_priority": "high"},
    ).json()
    assert generated["status"] == "proposed"
    assert generated["target_id"] is None
    assert generated["expected_version"] is None
    recommendation_id = generated["id"]

    # `RecommendationPanel`'s `fetchRecommendations` reads the list endpoint,
    # not the single-recommendation GET the other assertions in this file
    # exercise -- confirm `proposed_fields` actually survives that path too
    # (it shares `FIELDS`/`project` with the single-recommendation GET, but
    # nothing exercised the list endpoint for a create-type row before this).
    listed = client.get("/api/v1/recommendations")
    assert listed.status_code == 200
    listed_item = next(item for item in listed.json()["items"] if item["id"] == recommendation_id)
    assert listed_item["proposed_fields"] == {
        "title": "Send the signed contract back",
        "manual_priority": "high",
    }
    assert listed_item["target_id"] is None
    assert listed_item["expected_version"] is None

    published = client.post(
        f"/api/v1/recommendations/{recommendation_id}/publish",
        headers=_headers(token),
        json={"expected_version": 1},
    )
    assert published.status_code == 200

    confirmed = client.post(
        f"/api/v1/recommendations/{recommendation_id}/confirm",
        headers=_headers(token),
        json={"expected_version": 2, "target_expected_version": None},
    )
    assert confirmed.status_code == 200
    body = confirmed.json()
    assert body["status"] == "executed"
    assert body["execution_result"]["operation"] == "create"
    new_task_id = UUID(body["execution_result"]["target_id"])

    with engine.connect() as connection:
        task = (
            connection.execute(
                text("SELECT title, manual_priority, owner_id, version FROM tasks WHERE id=:id"),
                {"id": new_task_id},
            )
            .mappings()
            .one()
        )
        audit_count = connection.execute(
            text(
                """
                SELECT count(*) FROM audit_events
                WHERE workspace_id=:workspace_id AND aggregate_type='task'
                  AND aggregate_id=:task_id AND event_type='task.created'
                """
            ),
            {"workspace_id": workspace_id, "task_id": new_task_id},
        ).scalar_one()
    assert task["title"] == "Send the signed contract back"
    assert task["manual_priority"] == "high"
    assert task["owner_id"] == user_id
    assert task["version"] == 1
    assert audit_count == 1


def test_create_commitment_recommendation_end_to_end(
    recommendation_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = recommendation_context
    generated = _generate_create(
        client,
        token,
        "commitment",
        {"summary": "Deliver the Q3 report", "direction": "made_by_me"},
    ).json()
    recommendation_id = generated["id"]
    client.post(
        f"/api/v1/recommendations/{recommendation_id}/publish",
        headers=_headers(token),
        json={"expected_version": 1},
    )
    confirmed = client.post(
        f"/api/v1/recommendations/{recommendation_id}/confirm",
        headers=_headers(token),
        json={"expected_version": 2, "target_expected_version": None},
    )
    assert confirmed.status_code == 200
    new_commitment_id = UUID(confirmed.json()["execution_result"]["target_id"])
    with engine.connect() as connection:
        commitment = (
            connection.execute(
                text("SELECT summary, direction, owner_id FROM commitments WHERE id=:id"),
                {"id": new_commitment_id},
            )
            .mappings()
            .one()
        )
        audit_count = connection.execute(
            text(
                """
                SELECT count(*) FROM audit_events
                WHERE workspace_id=:workspace_id AND aggregate_type='commitment'
                  AND aggregate_id=:commitment_id AND event_type='commitment.created'
                """
            ),
            {"workspace_id": workspace_id, "commitment_id": new_commitment_id},
        ).scalar_one()
    assert commitment["summary"] == "Deliver the Q3 report"
    assert commitment["direction"] == "made_by_me"
    assert commitment["owner_id"] == user_id
    assert audit_count == 1


def test_create_risk_recommendation_end_to_end(
    recommendation_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = recommendation_context
    generated = _generate_create(
        client,
        token,
        "risk",
        {"description": "Vendor may miss the renewal deadline", "probability": 3, "impact": 4},
    ).json()
    recommendation_id = generated["id"]
    client.post(
        f"/api/v1/recommendations/{recommendation_id}/publish",
        headers=_headers(token),
        json={"expected_version": 1},
    )
    confirmed = client.post(
        f"/api/v1/recommendations/{recommendation_id}/confirm",
        headers=_headers(token),
        json={"expected_version": 2, "target_expected_version": None},
    )
    assert confirmed.status_code == 200
    new_risk_id = UUID(confirmed.json()["execution_result"]["target_id"])
    with engine.connect() as connection:
        risk = (
            connection.execute(
                text("SELECT description, probability, impact, owner_id FROM risks WHERE id=:id"),
                {"id": new_risk_id},
            )
            .mappings()
            .one()
        )
        audit_count = connection.execute(
            text(
                """
                SELECT count(*) FROM audit_events
                WHERE workspace_id=:workspace_id AND aggregate_type='risk'
                  AND aggregate_id=:risk_id AND event_type='risk.created'
                """
            ),
            {"workspace_id": workspace_id, "risk_id": new_risk_id},
        ).scalar_one()
    assert risk["description"] == "Vendor may miss the renewal deadline"
    assert risk["probability"] == 3
    assert risk["impact"] == 4
    assert risk["owner_id"] == user_id
    assert audit_count == 1


def test_confirm_create_recommendation_rejects_a_target_expected_version(
    recommendation_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = recommendation_context
    generated = _generate_create(client, token, "task", {"title": "Follow up"}).json()
    recommendation_id = generated["id"]
    client.post(
        f"/api/v1/recommendations/{recommendation_id}/publish",
        headers=_headers(token),
        json={"expected_version": 1},
    )
    rejected = client.post(
        f"/api/v1/recommendations/{recommendation_id}/confirm",
        headers=_headers(token),
        json={"expected_version": 2, "target_expected_version": 1},
    )
    assert rejected.status_code == 422


def test_confirm_non_create_recommendation_requires_a_target_expected_version(
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
    rejected = client.post(
        f"/api/v1/recommendations/{recommendation_id}/confirm",
        headers=_headers(token),
        json={"expected_version": 2},
    )
    assert rejected.status_code == 422


def test_two_pending_create_recommendations_of_the_same_type_do_not_supersede_each_other(
    recommendation_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """Unlike update-type recommendations (`test_supersession_and_stored_
    target_version_enforcement`), a second `operation="create"` recommendation
    for the same `target_type` deliberately does not supersede the first --
    there is no shared `target_id` to key superseding on (`target_id` is
    always `NULL` for every create-type row, and SQL's `NULL = NULL` is never
    true), and two AI-detected action items from different email threads are
    legitimately independent proposals, not competing edits of the same
    thing.
    """
    client, _workspace_id, _user_id, token = recommendation_context
    first = _generate_create(client, token, "task", {"title": "First detected action"}).json()
    second = _generate_create(client, token, "task", {"title": "Second detected action"}).json()

    first_loaded = client.get(f"/api/v1/recommendations/{first['id']}")
    assert first_loaded.status_code == 200
    assert first_loaded.json()["status"] == "proposed"
    second_loaded = client.get(f"/api/v1/recommendations/{second['id']}")
    assert second_loaded.status_code == 200
    assert second_loaded.json()["status"] == "proposed"


def test_confirm_set_status_recommendation_routes_through_the_real_state_machine(
    recommendation_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """Architecture-review finding: `execute_target`'s `set_status` branch
    used to run a raw `UPDATE commitments SET status=...`, bypassing
    `commitments.py`'s own `_lifecycle`/`lifecycle_write` guards entirely --
    a recommendation could silently un-terminal an already-`cancelled`
    commitment back to `active`, with no audit trail at all. Confirms the
    fix: the same transition attempted here must be rejected with the
    identical `INVALID_COMMITMENT_TRANSITION` a manual `POST .../confirm`
    would return, the commitment's status must be untouched, and the whole
    confirm attempt must roll back cleanly (no partial recommendation-state
    mutation left behind by the rejected execution).
    """
    client, workspace_id, user_id, token = recommendation_context
    commitment_id = _commitment(workspace_id, user_id, status="cancelled")

    generated = client.post(
        "/api/v1/recommendations",
        headers=_headers(token),
        json={
            "recommendation_type": "commitment_status",
            "target_type": "commitment",
            "target_id": str(commitment_id),
            "proposed_action": {"operation": "set_status", "value": "active"},
            "expected_version": 1,
            "rationale": "New activity suggests this commitment is back on.",
            "confidence": 0.8,
            "evidence_ids": [],
            "expires_at": None,
            "source": "rule",
        },
    ).json()
    recommendation_id = generated["id"]
    client.post(
        f"/api/v1/recommendations/{recommendation_id}/publish",
        headers=_headers(token),
        json={"expected_version": 1},
    )

    confirmed = client.post(
        f"/api/v1/recommendations/{recommendation_id}/confirm",
        headers=_headers(token),
        json={"expected_version": 2, "target_expected_version": 1},
    )
    assert confirmed.status_code == 409
    assert confirmed.json()["error"]["code"] == "INVALID_COMMITMENT_TRANSITION"

    with engine.connect() as connection:
        commitment = (
            connection.execute(
                text("SELECT status, version FROM commitments WHERE id=:id"),
                {"id": commitment_id},
            )
            .mappings()
            .one()
        )
        recommendation_status = connection.execute(
            text("SELECT status FROM recommendations WHERE id=:id"),
            {"id": UUID(str(recommendation_id))},
        ).scalar_one()
    assert commitment["status"] == "cancelled"
    assert commitment["version"] == 1
    assert recommendation_status == "pending_confirmation"


def test_confirm_set_status_recommendation_writes_audit_trail_and_supports_break(
    recommendation_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """A *valid* recommendation-driven status transition -- unlike the
    rejected one above -- must both succeed and leave the exact audit trail
    a manual `POST .../break` would (the missing-audit-trail half of the
    same architecture-review finding). Also exercises the new `broken`
    status/`POST /commitments/{id}/break` endpoint this fix added, since
    nothing could reach that status at all before it.
    """
    client, workspace_id, user_id, token = recommendation_context
    commitment_id = _commitment(workspace_id, user_id, status="active")

    generated = client.post(
        "/api/v1/recommendations",
        headers=_headers(token),
        json={
            "recommendation_type": "commitment_status",
            "target_type": "commitment",
            "target_id": str(commitment_id),
            "proposed_action": {"operation": "set_status", "value": "broken"},
            "expected_version": 1,
            "rationale": "Counterparty confirmed this will not be delivered.",
            "confidence": 0.85,
            "evidence_ids": [],
            "expires_at": None,
            "source": "rule",
        },
    ).json()
    recommendation_id = generated["id"]
    client.post(
        f"/api/v1/recommendations/{recommendation_id}/publish",
        headers=_headers(token),
        json={"expected_version": 1},
    )

    confirmed = client.post(
        f"/api/v1/recommendations/{recommendation_id}/confirm",
        headers=_headers(token),
        json={"expected_version": 2, "target_expected_version": 1},
    )
    assert confirmed.status_code == 200
    assert confirmed.json()["status"] == "executed"

    with engine.connect() as connection:
        commitment = (
            connection.execute(
                text("SELECT status, version FROM commitments WHERE id=:id"),
                {"id": commitment_id},
            )
            .mappings()
            .one()
        )
        audit_count = connection.execute(
            text(
                """
                SELECT count(*) FROM audit_events
                WHERE workspace_id=:workspace_id AND aggregate_type='commitment'
                  AND aggregate_id=:commitment_id AND event_type='commitment.broken'
                """
            ),
            {"workspace_id": workspace_id, "commitment_id": commitment_id},
        ).scalar_one()
    assert commitment["status"] == "broken"
    assert commitment["version"] == 2
    assert audit_count == 1


def test_confirm_field_patch_recommendation_denies_a_private_target(
    recommendation_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """Regression test for a real authz gap in `execute_target`'s generic
    column-patch branch (`set_priority`/`set_pinned`/`set_importance`/
    `set_probability`/`set_impact`): unlike `set_status`, which routes
    through `lifecycle_write`/`lifecycle_task_write`/`set_risk_status_
    write` -- each of which `authz.authorize()`s the target before
    mutating -- the column-patch branch went straight to a raw `UPDATE`
    scoped only by `workspace_id`/`version`/`archived_at`, never the
    target's own visibility. A workspace member holding the generic
    write-role action could confirm a `set_pinned` recommendation against
    another member's `visibility='private'` task and silently flip
    `pinned`, even though their own direct `PATCH /api/v1/tasks/{id}`
    would 404 for that same task.
    """
    client, workspace_id, owner_id, owner_token = recommendation_context
    task_id = _task(workspace_id, owner_id)
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE tasks SET visibility = 'private' "
                "WHERE id = :id AND workspace_id = :workspace_id"
            ),
            {"id": task_id, "workspace_id": workspace_id},
        )

    generated = client.post(
        "/api/v1/recommendations",
        headers=_headers(owner_token),
        json={
            "recommendation_type": "task_priority",
            "target_type": "task",
            "target_id": str(task_id),
            "proposed_action": {"operation": "set_pinned", "value": True},
            "expected_version": 1,
            "rationale": "Task should be pinned for visibility.",
            "confidence": 0.9,
            "evidence_ids": [],
            "expires_at": None,
            "source": "rule",
        },
    )
    assert generated.status_code == 201, generated.text
    recommendation_id = generated.json()["id"]
    published = client.post(
        f"/api/v1/recommendations/{recommendation_id}/publish",
        headers=_headers(owner_token),
        json={"expected_version": 1},
    )
    assert published.status_code == 200, published.text

    # A second, non-owner member of the same workspace -- an ordinary
    # "member" role, holding the generic write-role action but no
    # ownership of, or explicit grant to, the private task above.
    member_id = uuid4()
    member_token = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        create_identity(
            connection,
            workspace_id=workspace_id,
            user_id=member_id,
            email=f"{member_id}@example.test",
            now=now,
            role="member",
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
                "user_id": member_id,
                "token_hash": sha256(member_token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=2),
                "last_seen_at": now,
            },
        )
    member_client = TestClient(app)
    member_client.cookies.set("ecc_session", member_token)
    try:
        confirmed = member_client.post(
            f"/api/v1/recommendations/{recommendation_id}/confirm",
            headers=_headers(member_token),
            json={"expected_version": 2, "target_expected_version": 1},
        )
        assert confirmed.status_code == 404, confirmed.text
    finally:
        member_client.close()

    with engine.connect() as connection:
        task = (
            connection.execute(
                text("SELECT pinned, version FROM tasks WHERE id=:id"),
                {"id": task_id},
            )
            .mappings()
            .one()
        )
    assert task["pinned"] is False
    assert task["version"] == 1
