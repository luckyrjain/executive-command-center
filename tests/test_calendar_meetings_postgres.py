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
def calendar_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO workspaces (id, name, timezone, created_at)
                VALUES (:id, :name, :timezone, :created_at)
                """
            ),
            {
                "id": workspace_id,
                "name": "Calendar Test",
                "timezone": "Asia/Kolkata",
                "created_at": now,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO users (id, workspace_id, email, password_hash, created_at)
                VALUES (:id, :workspace_id, :email, :password_hash, :created_at)
                """
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
                """
                INSERT INTO sessions (
                    id, workspace_id, user_id, token_hash, expires_at, last_seen_at
                ) VALUES (
                    :id, :workspace_id, :user_id, :token_hash, :expires_at, :last_seen_at
                )
                """
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
                "notes",
                "meetings",
                "calendar_events",
                "sessions",
                "users",
            ):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),
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


def test_calendar_and_linked_meeting_lifecycle(
    calendar_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, _, token = calendar_test_context
    starts_at = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)
    ends_at = starts_at + timedelta(hours=1)
    event_payload = {
        "title": "Operating review",
        "starts_at": starts_at.isoformat(),
        "ends_at": ends_at.isoformat(),
        "timezone": "Asia/Kolkata",
    }

    create_event = client.post(
        "/api/v1/calendar/events",
        headers=_headers(token, "create-event"),
        json=event_payload,
    )
    assert create_event.status_code == 201
    event = create_event.json()
    event_id = event["id"]
    assert event["version"] == 1

    replay = client.post(
        "/api/v1/calendar/events",
        headers=_headers(token, "create-event"),
        json=event_payload,
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == event_id

    create_meeting = client.post(
        "/api/v1/meetings",
        headers=_headers(token, "create-meeting"),
        json={"calendar_event_id": event_id, "title": "Operating review"},
    )
    assert create_meeting.status_code == 201
    meeting = create_meeting.json()
    meeting_id = meeting["id"]
    assert meeting["starts_at"] == event["starts_at"]
    assert meeting["ends_at"] == event["ends_at"]
    assert meeting["timezone"] == "Asia/Kolkata"

    duplicate = client.post(
        "/api/v1/meetings",
        headers=_headers(token, "duplicate-meeting"),
        json={"calendar_event_id": event_id, "title": "Duplicate"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "CALENDAR_EVENT_ALREADY_LINKED"

    update_event = client.patch(
        f"/api/v1/calendar/events/{event_id}",
        headers=_headers(token, "update-event"),
        json={
            "expected_version": 1,
            "starts_at": (starts_at + timedelta(hours=1)).isoformat(),
            "ends_at": (ends_at + timedelta(hours=1)).isoformat(),
        },
    )
    assert update_event.status_code == 200

    projected = client.get(f"/api/v1/meetings/{meeting_id}")
    assert projected.status_code == 200
    assert projected.json()["starts_at"] == update_event.json()["starts_at"]

    conflict = client.patch(
        f"/api/v1/meetings/{meeting_id}",
        headers=_headers(token, "stale-meeting"),
        json={"expected_version": 99, "agenda": "stale"},
    )
    assert conflict.status_code == 409

    archive = client.post(
        f"/api/v1/meetings/{meeting_id}/archive",
        headers=_headers(token, "archive-meeting"),
        json={"expected_version": 1},
    )
    assert archive.status_code == 200
    assert archive.json()["archived_at"] is not None

    restore = client.post(
        f"/api/v1/meetings/{meeting_id}/restore",
        headers=_headers(token, "restore-meeting"),
        json={"expected_version": 2},
    )
    assert restore.status_code == 200
    assert restore.json()["archived_at"] is None

    malformed = client.get("/api/v1/meetings?cursor=not-a-valid-cursor")
    assert malformed.status_code == 400

    with engine.connect() as connection:
        audit_types = {
            row[0]
            for row in connection.execute(
                text(
                    """
                    SELECT event_type FROM audit_events
                    WHERE workspace_id = :workspace_id
                    """
                ),
                {"workspace_id": workspace_id},
            )
        }
        outbox_types = {
            row[0]
            for row in connection.execute(
                text(
                    """
                    SELECT event_type FROM event_outbox
                    WHERE workspace_id = :workspace_id
                    """
                ),
                {"workspace_id": workspace_id},
            )
        }

    assert "calendar_event.created" in audit_types
    assert "calendar_event.updated" in audit_types
    assert "meeting.created" in audit_types
    assert "meeting.archived" in audit_types
    assert "meeting.restored" in audit_types
    assert "calendar_event.created.v1" in outbox_types
    assert "meeting.created.v1" in outbox_types


def test_linked_meeting_rejects_cross_workspace_event(
    calendar_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _, _, token = calendar_test_context
    response = client.post(
        "/api/v1/meetings",
        headers=_headers(token, "cross-workspace-meeting"),
        json={"calendar_event_id": str(uuid4()), "title": "Hidden event"},
    )
    assert response.status_code == 404


def test_standalone_meeting_reschedule_and_linked_timing_rejection(
    calendar_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, _, token = calendar_test_context
    starts_at = datetime(2026, 7, 20, 9, tzinfo=UTC)
    standalone = client.post(
        "/api/v1/meetings",
        headers=_headers(token, "create-standalone-reschedule"),
        json={
            "title": "Standalone planning",
            "starts_at": starts_at.isoformat(),
            "ends_at": (starts_at + timedelta(hours=1)).isoformat(),
            "timezone": "UTC",
        },
    )
    assert standalone.status_code == 201
    meeting = standalone.json()

    patch_payload = {
        "expected_version": 1,
        "starts_at": (starts_at + timedelta(hours=2)).isoformat(),
        "ends_at": (starts_at + timedelta(hours=3)).isoformat(),
        "timezone": "Asia/Kolkata",
    }
    rescheduled = client.patch(
        f"/api/v1/meetings/{meeting['id']}",
        headers=_headers(token, "reschedule-standalone"),
        json=patch_payload,
    )
    assert rescheduled.status_code == 200
    assert rescheduled.json()["version"] == 2
    assert rescheduled.json()["starts_at"] == patch_payload["starts_at"]
    assert rescheduled.json()["timezone"] == "Asia/Kolkata"

    replay = client.patch(
        f"/api/v1/meetings/{meeting['id']}",
        headers=_headers(token, "reschedule-standalone"),
        json=patch_payload,
    )
    assert replay.status_code == 200
    assert replay.json() == rescheduled.json()

    with engine.connect() as connection:
        stored = connection.execute(
            text(
                """
                SELECT standalone_starts_at, standalone_ends_at, standalone_timezone
                FROM meetings WHERE workspace_id = :workspace_id AND id = :meeting_id
                """
            ),
            {"workspace_id": workspace_id, "meeting_id": meeting["id"]},
        ).one()
        audit = connection.execute(
            text(
                """
                SELECT changed_fields FROM audit_events
                WHERE workspace_id = :workspace_id AND aggregate_id = :meeting_id
                  AND event_type = 'meeting.updated'
                """
            ),
            {"workspace_id": workspace_id, "meeting_id": meeting["id"]},
        ).one()
    assert stored.standalone_starts_at == starts_at + timedelta(hours=2)
    assert stored.standalone_ends_at == starts_at + timedelta(hours=3)
    assert stored.standalone_timezone == "Asia/Kolkata"
    assert set(audit.changed_fields) == {"starts_at", "ends_at", "timezone"}

    event = client.post(
        "/api/v1/calendar/events",
        headers=_headers(token, "linked-rejection-event"),
        json={
            "title": "Linked event",
            "starts_at": starts_at.isoformat(),
            "ends_at": (starts_at + timedelta(hours=1)).isoformat(),
            "timezone": "UTC",
        },
    )
    linked = client.post(
        "/api/v1/meetings",
        headers=_headers(token, "linked-rejection-meeting"),
        json={"calendar_event_id": event.json()["id"], "title": "Linked meeting"},
    )
    rejection = client.patch(
        f"/api/v1/meetings/{linked.json()['id']}",
        headers=_headers(token, "linked-rejection-patch"),
        json=patch_payload,
    )
    assert rejection.status_code == 422
    assert rejection.json()["error"]["code"] == "LINKED_MEETING_TIMING_READ_ONLY"
