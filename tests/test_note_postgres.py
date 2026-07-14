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
def note_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
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
                "name": "Note Test",
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
                    id, workspace_id, user_id, token_hash,
                    expires_at, last_seen_at
                ) VALUES (
                    :id, :workspace_id, :user_id, :token_hash,
                    :expires_at, :last_seen_at
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
    csrf = new(
        settings.session_secret.encode(),
        token.encode(),
        "sha256",
    ).hexdigest()
    return {
        "Idempotency-Key": key,
        "X-CSRF-Token": csrf,
        "X-Correlation-ID": str(uuid4()),
    }


def test_note_lifecycle_autosave_search_and_redacted_audit(
    note_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = note_test_context
    body = "Board preparation with revenue risk and hiring actions"

    create = client.post(
        "/api/v1/notes",
        headers=_headers(token, "create-note"),
        json={
            "title": "Board preparation",
            "body": body,
            "note_type": "general",
        },
    )
    assert create.status_code == 201
    created = create.json()
    note_id = created["id"]
    assert created["owner_id"] == str(user_id)
    assert created["version"] == 1
    assert created["request_id"]
    assert created["correlation_id"]

    replay = client.post(
        "/api/v1/notes",
        headers=_headers(token, "create-note"),
        json={
            "title": "Board preparation",
            "body": body,
            "note_type": "general",
        },
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == note_id

    update = client.patch(
        f"/api/v1/notes/{note_id}",
        headers=_headers(token, "autosave-note"),
        json={
            "expected_version": 1,
            "body": body + " and liquidity review",
        },
    )
    assert update.status_code == 200
    assert update.json()["version"] == 2

    conflict = client.patch(
        f"/api/v1/notes/{note_id}",
        headers=_headers(token, "stale-autosave"),
        json={"expected_version": 1, "body": "stale body"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "VERSION_CONFLICT"
    assert conflict.json()["error"]["details"]["current_version"] == 2

    search = client.get("/api/v1/notes", params={"q": "liquidity"})
    assert search.status_code == 200
    assert [item["id"] for item in search.json()["items"]] == [note_id]

    archive = client.post(
        f"/api/v1/notes/{note_id}/archive",
        headers=_headers(token, "archive-note"),
        json={"expected_version": 2},
    )
    assert archive.status_code == 200
    assert archive.json()["archived_at"] is not None

    hidden = client.get("/api/v1/notes")
    assert all(item["id"] != note_id for item in hidden.json()["items"])

    restore = client.post(
        f"/api/v1/notes/{note_id}/restore",
        headers=_headers(token, "restore-note"),
        json={"expected_version": 3},
    )
    assert restore.status_code == 200
    assert restore.json()["archived_at"] is None
    assert restore.json()["version"] == 4

    with engine.connect() as connection:
        audit_rows = (
            connection.execute(
                text(
                    """
                    SELECT event_type, before, after, metadata
                    FROM audit_events
                    WHERE workspace_id = :workspace_id
                      AND aggregate_id = :note_id
                    ORDER BY occurred_at
                    """
                ),
                {"workspace_id": workspace_id, "note_id": note_id},
            )
            .mappings()
            .all()
        )
        outbox_rows = (
            connection.execute(
                text(
                    """
                    SELECT event_type, payload
                    FROM event_outbox
                    WHERE workspace_id = :workspace_id
                    ORDER BY occurred_at
                    """
                ),
                {"workspace_id": workspace_id},
            )
            .mappings()
            .all()
        )

    assert [row["event_type"] for row in audit_rows] == [
        "note.created",
        "note.updated",
        "note.archived",
        "note.restored",
    ]
    serialized_audit = str(audit_rows)
    assert body not in serialized_audit
    assert "body_checksum" in serialized_audit
    assert all(row["metadata"]["body_redacted"] is True for row in audit_rows)
    assert [row["event_type"] for row in outbox_rows] == [
        "note.created.v1",
        "note.updated.v1",
        "note.archived.v1",
        "note.restored.v1",
    ]
    assert (
        outbox_rows[1]["payload"]["body_checksum"]
        == sha256((body + " and liquidity review").encode()).hexdigest()
    )


def test_note_cursor_restore_guard_and_workspace_isolation(
    note_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _, _, token = note_test_context
    ids: list[str] = []
    for index in range(3):
        response = client.post(
            "/api/v1/notes",
            headers=_headers(token, f"page-note-{index}"),
            json={"title": f"Note {index}", "body": f"Body {index}"},
        )
        assert response.status_code == 201
        ids.append(response.json()["id"])

    first = client.get("/api/v1/notes", params={"limit": 2})
    assert first.status_code == 200
    assert len(first.json()["items"]) == 2
    assert first.json()["next_cursor"] is not None

    second = client.get(
        "/api/v1/notes",
        params={"limit": 2, "cursor": first.json()["next_cursor"]},
    )
    assert second.status_code == 200
    assert len(second.json()["items"]) == 1

    malformed = client.get("/api/v1/notes", params={"cursor": "not-signed"})
    assert malformed.status_code == 400
    assert malformed.json()["error"]["code"] == "MALFORMED_CURSOR"

    restore = client.post(
        f"/api/v1/notes/{ids[0]}/restore",
        headers=_headers(token, "restore-never-archived"),
        json={"expected_version": 1},
    )
    assert restore.status_code == 409
    assert restore.json()["error"]["code"] == "NOTE_NOT_ARCHIVED"

    missing = client.get(f"/api/v1/notes/{uuid4()}")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "NOTE_NOT_FOUND"


def test_note_meeting_reference_is_non_disclosing_until_meetings_exist(
    note_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _, _, token = note_test_context
    response = client.post(
        "/api/v1/notes",
        headers=_headers(token, "missing-meeting"),
        json={
            "body": "Meeting note",
            "note_type": "meeting",
            "meeting_id": str(uuid4()),
            "source_type": "meeting",
        },
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "MEETING_NOT_FOUND"


def test_note_patch_rejects_null_required_fields(
    note_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _, _, token = note_test_context
    created = client.post(
        "/api/v1/notes",
        headers=_headers(token, "null-fields-create"),
        json={"title": "Required fields", "body": "Body"},
    )
    assert created.status_code == 201
    note = created.json()

    for field in ("body", "note_type", "source_type"):
        response = client.patch(
            f"/api/v1/notes/{note['id']}",
            headers=_headers(token, f"null-{field}"),
            json={"expected_version": note["version"], field: None},
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "VALIDATION_ERROR"
