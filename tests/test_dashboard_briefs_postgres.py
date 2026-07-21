from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from hmac import new
from time import perf_counter
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
def dashboard_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO workspaces (id, name, timezone, created_at)
                VALUES (:id, 'Dashboard Test', 'Asia/Kolkata', :created_at)
                """
            ),
            {"id": workspace_id, "created_at": now},
        )
        connection.execute(
            text(
                """
                INSERT INTO users (id, workspace_id, email, password_hash, created_at)
                VALUES (:id, :workspace_id, :email, 'hash', :created_at)
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
                    id, workspace_id, user_id, token_hash, expires_at, last_seen_at
                ) VALUES (
                    :id, :workspace_id, :user_id, :token_hash, :expires_at, :last_seen_at
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
                "morning_briefs",
                "attention_items",
                "event_outbox",
                "audit_events",
                "idempotency_records",
                "meetings",
                "calendar_events",
                "risks",
                "commitments",
                "tasks",
                "notes",
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


def _headers(token: str, *, key: str | None = None) -> dict[str, str]:
    return {
        "X-CSRF-Token": new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest(),
        "X-Correlation-ID": str(uuid4()),
        "Idempotency-Key": key or str(uuid4()),
    }


def test_dashboard_and_persisted_brief_lifecycle(
    dashboard_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = dashboard_context
    now = datetime.now(UTC)
    local_day = datetime.now().date()
    task_id = uuid4()
    risk_id = uuid4()
    commitment_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO tasks (
                    id, workspace_id, owner_id, title, status, manual_priority,
                    due_date, pinned, source_type, created_by, updated_by,
                    created_at, updated_at, version
                ) VALUES (
                    :id, :workspace_id, :user_id, 'Priority task', 'planned',
                    'critical', :due_date, true, 'local', :user_id, :user_id,
                    :now, :now, 1
                )
                """
            ),
            {
                "id": task_id,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "due_date": local_day,
                "now": now,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO attention_items (
                    id, workspace_id, entity_type, entity_id, source_entity_version,
                    score, confidence, factors, explanation, generated_at, expires_at,
                    pinned
                ) VALUES (
                    :id, :workspace_id, 'task', :entity_id, 1, 99, 1.0,
                    '{}'::jsonb, 'Critical and due today', :now, :expires_at, true
                )
                """
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "entity_id": task_id,
                "now": now,
                "expires_at": now + timedelta(hours=1),
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO commitments (
                    id, workspace_id, owner_id, summary, direction, status,
                    due_date, importance, confidence, pinned, created_by, updated_by,
                    created_at, updated_at, version
                ) VALUES (
                    :id, :workspace_id, :user_id, 'Vendor response', 'made_to_me',
                    'active', :due_date, 'high', 1.0, false, :user_id, :user_id,
                    :now, :now, 1
                )
                """
            ),
            {
                "id": commitment_id,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "due_date": local_day - timedelta(days=1),
                "now": now,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO risks (
                    id, workspace_id, owner_id, description, probability, impact,
                    status, pinned, created_by, updated_by, created_at, updated_at, version
                ) VALUES (
                    :id, :workspace_id, :user_id, 'Processor concentration', 5, 5,
                    'monitoring', false, :user_id, :user_id, :now, :now, 1
                )
                """
            ),
            {
                "id": risk_id,
                "workspace_id": workspace_id,
                "user_id": user_id,
                "now": now,
            },
        )

    dashboard = client.get("/api/v1/dashboard/today")
    assert dashboard.status_code == 200
    sections = dashboard.json()["sections"]
    assert sections["top_priorities"][0]["entity_id"] == str(task_id)
    assert sections["overdue_commitments"][0]["entity_id"] == str(commitment_id)
    assert sections["risks"][0]["entity_id"] == str(risk_id)

    first = client.get("/api/v1/briefs/morning")
    assert first.status_code == 200
    assert first.json()["generation_version"] == 1
    second = client.get("/api/v1/briefs/morning")
    assert second.status_code == 200
    assert second.json()["id"] == first.json()["id"]

    refresh = client.post("/api/v1/briefs/morning", headers=_headers(token))
    assert refresh.status_code == 200
    assert refresh.json()["generation_version"] == 2

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE tasks SET version=2, updated_at=:now "
                "WHERE workspace_id=:workspace_id AND id=:id"
            ),
            {"now": datetime.now(UTC), "workspace_id": workspace_id, "id": task_id},
        )
    stale = client.get("/api/v1/briefs/morning")
    assert stale.status_code == 200
    assert stale.json()["stale"] is True
    assert stale.json()["stale_reason"] == "source_version_changed"

    with engine.connect() as connection:
        audit_count = connection.execute(
            text(
                "SELECT count(*) FROM audit_events WHERE workspace_id=:workspace_id "
                "AND event_type='morning_brief.generated'"
            ),
            {"workspace_id": workspace_id},
        ).scalar_one()
        outbox_count = connection.execute(
            text(
                "SELECT count(*) FROM event_outbox WHERE workspace_id=:workspace_id "
                "AND event_type='morning_brief.generated'"
            ),
            {"workspace_id": workspace_id},
        ).scalar_one()
    assert audit_count == 2
    assert outbox_count == 2


def test_dashboard_empty_state_and_budget(
    dashboard_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _, _, _ = dashboard_context
    started = perf_counter()
    response = client.get(
        "/api/v1/dashboard/today",
        params={"date": date.today().isoformat()},
    )
    elapsed = perf_counter() - started
    assert response.status_code == 200
    sections = response.json()["sections"]
    assert sections["today_schedule"][0]["empty"] is True
    assert sections["top_priorities"][0]["empty"] is True
    assert elapsed < 2.0


def test_dashboard_review_regressions(
    dashboard_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = dashboard_context
    now = datetime.now(UTC)
    local_day = datetime.now().date()
    future_commitment = uuid4()
    blocked_task = uuid4()
    event_id = uuid4()
    meeting_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO commitments (
                    id, workspace_id, owner_id, summary, direction, status,
                    due_date, importance, confidence, pinned, created_by, updated_by,
                    created_at, updated_at, version
                ) VALUES (
                    :id, :w, :u, 'Future response', 'made_to_me', 'active', :due_date,
                    'medium', 1.0, false, :u, :u, :now, :now, 1
                )
                """
            ),
            {
                "id": future_commitment,
                "w": workspace_id,
                "u": user_id,
                "due_date": local_day + timedelta(days=1),
                "now": now,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO tasks (
                    id, workspace_id, owner_id, title, status, manual_priority,
                    blocked_on_person_id, pinned, source_type, created_by, updated_by,
                    created_at, updated_at, version
                ) VALUES (
                    :id, :w, :u, 'Blocked approval', 'blocked', 'high', :person,
                    false, 'local', :u, :u, :now, :now, 1
                )
                """
            ),
            {
                "id": blocked_task,
                "w": workspace_id,
                "u": user_id,
                "person": uuid4(),
                "now": now,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO calendar_events (
                    id, workspace_id, external_source, title, starts_at, ends_at,
                    all_day, timezone, status, source_authoritative,
                    created_by, updated_by, created_at, updated_at, version
                ) VALUES (
                    :id, :w, 'local', 'Review meeting', :starts, :ends, false,
                    'Asia/Kolkata', 'confirmed', true, :u, :u, :now, :now, 1
                )
                """
            ),
            {
                "id": event_id,
                "w": workspace_id,
                "u": user_id,
                "starts": now,
                "ends": now + timedelta(hours=1),
                "now": now,
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO meetings (
                    id, workspace_id, calendar_event_id, title, status,
                    created_by, updated_by, created_at, updated_at, version
                ) VALUES (
                    :id, :w, :event_id, 'Review meeting', 'planned',
                    :u, :u, :now, :now, 1
                )
                """
            ),
            {
                "id": meeting_id,
                "w": workspace_id,
                "event_id": event_id,
                "u": user_id,
                "now": now,
            },
        )

    dashboard = client.get("/api/v1/dashboard/today")
    assert dashboard.status_code == 200
    sections = dashboard.json()["sections"]
    overdue_ids = {item["entity_id"] for item in sections.get("overdue_commitments", [])}
    waiting_ids = {item["entity_id"] for item in sections.get("waiting_on", [])}
    assert str(future_commitment) not in overdue_ids
    assert str(future_commitment) in waiting_ids
    assert str(blocked_task) in waiting_ids

    key = str(uuid4())
    headers = _headers(token, key=key)
    first = client.post("/api/v1/briefs/morning", headers=headers)
    replay = client.post("/api/v1/briefs/morning", headers=headers)
    assert first.status_code == 200
    assert replay.status_code == 200
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["generation_version"] == first.json()["generation_version"]

    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE calendar_events SET version=2, updated_at=:now "
                "WHERE id=:id AND workspace_id=:w"
            ),
            {"now": datetime.now(UTC), "id": event_id, "w": workspace_id},
        )
    stale = client.get("/api/v1/briefs/morning")
    assert stale.status_code == 200
    assert stale.json()["stale"] is True
    assert stale.json()["stale_reason"] == "source_version_changed"


def test_recently_changed_is_not_crowded_out_by_one_entitys_repeat_edits(
    dashboard_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """Regression test: ``_build_sections``'s ``recently_changed`` query
    used to apply its LIMIT before deduplicating by entity, so one entity
    with many audit rows could crowd every other entity out of the
    candidate window entirely. Uses notes specifically because they don't
    feed any other dashboard section, isolating this bug from the
    unrelated cross-section "already shown elsewhere" filtering."""
    client, _workspace_id, _user_id, token = dashboard_context

    older = client.post(
        "/api/v1/notes",
        headers=_headers(token),
        json={"title": "Older note", "body": "Created first", "note_type": "general"},
    )
    assert older.status_code == 201
    older_note_id = older.json()["id"]

    busy = client.post(
        "/api/v1/notes",
        headers=_headers(token),
        json={"title": "Busy note", "body": "v0", "note_type": "general"},
    )
    assert busy.status_code == 201
    busy_note_id = busy.json()["id"]
    version = busy.json()["version"]

    # 20 edits to the same note produce 20 more audit_events rows, all more
    # recent than the "older" note's single creation event -- enough to
    # crowd it out of a naive `ORDER BY occurred_at DESC LIMIT 20` window
    # entirely, which was the pre-fix query shape.
    for i in range(20):
        patched = client.patch(
            f"/api/v1/notes/{busy_note_id}",
            headers=_headers(token),
            json={"expected_version": version, "body": f"v{i + 1}"},
        )
        assert patched.status_code == 200
        version = patched.json()["version"]

    dashboard = client.get("/api/v1/dashboard/today")
    assert dashboard.status_code == 200
    recently_changed = dashboard.json()["sections"].get("recently_changed", [])
    changed_entity_ids = {item["entity_id"] for item in recently_changed}

    assert older_note_id in changed_entity_ids
    assert busy_note_id in changed_entity_ids
    # The busy note must appear exactly once (its 21 audit rows collapse to
    # its single latest edit), not once per edit.
    assert sum(1 for item in recently_changed if item["entity_id"] == busy_note_id) == 1


def test_concurrent_lazy_brief_generation_creates_exactly_one_brief(
    dashboard_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """Regression test: the lazy-generate GET had no lock around its
    existence check, so concurrent requests for a brief that doesn't exist
    yet could all call _generate() and race into duplicate rows."""
    _client, workspace_id, user_id, token = dashboard_context

    def get_brief_once() -> int:
        worker = TestClient(app)
        worker.cookies.set("ecc_session", token)
        try:
            return worker.get("/api/v1/briefs/morning").status_code
        finally:
            worker.close()

    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(lambda _: get_brief_once(), range(5)))

    assert results == [200] * 5

    with engine.connect() as connection:
        brief_count = connection.execute(
            text("SELECT count(*) FROM morning_briefs WHERE workspace_id=:w AND user_id=:u"),
            {"w": workspace_id, "u": user_id},
        ).scalar_one()
        outbox_count = connection.execute(
            text(
                "SELECT count(*) FROM event_outbox "
                "WHERE workspace_id=:w AND event_type='morning_brief.generated'"
            ),
            {"w": workspace_id},
        ).scalar_one()

    assert brief_count == 1
    assert outbox_count == 1
