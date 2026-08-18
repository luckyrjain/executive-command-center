"""Direct coverage for `backend/ecc/domains/calendar/events.py`'s own
router -- GET/LIST/PATCH/archive/restore, visibility enforcement,
cross-workspace isolation, pagination, and validation error paths.

`tests/test_calendar_meetings_postgres.py` already covers the create+
linked-meeting lifecycle end to end, but never calls GET, LIST, archive,
or restore on this router directly, and never exercises this router's
own visibility/pagination/validation branches (Loop 2 architecture
review's "calendar/events.py test-coverage gap" finding). This file
closes that gap; it intentionally does not re-test what the meetings
file already covers well (create+link, idempotency replay, audit/outbox
event types).
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from identity_fixtures import create_identity
from sqlalchemy import text
from sqlalchemy.engine import Connection

from ecc.config import get_settings
from ecc.database import engine
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


class _Actor:
    def __init__(self, user_id: UUID, token: str, client: TestClient) -> None:
        self.user_id = user_id
        self.token = token
        self.client = client


def _create_session(
    connection: Connection, *, workspace_id: UUID, user_id: UUID, token: str
) -> None:
    now = datetime.now(UTC)
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
            "expires_at": now + timedelta(hours=1),
            "last_seen_at": now,
        },
    )


def _make_actor(connection: Connection, *, workspace_id: UUID, role: str = "owner") -> _Actor:
    user_id = uuid4()
    token = f"session-{uuid4()}"
    create_identity(connection, workspace_id=workspace_id, user_id=user_id, role=role)
    _create_session(connection, workspace_id=workspace_id, user_id=user_id, token=token)
    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    return _Actor(user_id=user_id, token=token, client=client)


@pytest.fixture
def events_context() -> Iterator[dict[str, Any]]:
    workspace_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, :name, :timezone, :created_at)"
            ),
            {"id": workspace_id, "name": "Events Test", "timezone": "UTC", "created_at": now},
        )
        owner = _make_actor(connection, workspace_id=workspace_id, role="owner")
        member = _make_actor(connection, workspace_id=workspace_id, role="member")

    other_workspace_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, :name, :timezone, :created_at)"
            ),
            {
                "id": other_workspace_id,
                "name": "Other Workspace",
                "timezone": "UTC",
                "created_at": now,
            },
        )
        outsider = _make_actor(connection, workspace_id=other_workspace_id, role="owner")

    try:
        yield {
            "workspace_id": workspace_id,
            "owner": owner,
            "member": member,
            "other_workspace_id": other_workspace_id,
            "outsider": outsider,
        }
    finally:
        owner.client.close()
        member.client.close()
        outsider.client.close()
        with engine.begin() as connection:
            for ws_id in (workspace_id, other_workspace_id):
                for table in (
                    "event_outbox",
                    "audit_events",
                    "idempotency_records",
                    "meetings",
                    "calendar_events",
                    "sessions",
                    "users",
                ):
                    connection.execute(
                        text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),
                        {"workspace_id": ws_id},
                    )
                connection.execute(
                    text("DELETE FROM workspaces WHERE id = :workspace_id"),
                    {"workspace_id": ws_id},
                )


def _headers(token: str, key: str) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    return {
        "Idempotency-Key": key,
        "X-CSRF-Token": csrf,
        "X-Correlation-ID": str(uuid4()),
    }


def _create_event(
    client: TestClient,
    token: str,
    key: str,
    *,
    title: str = "Event",
    starts_at: datetime | None = None,
    ends_at: datetime | None = None,
    timezone: str = "UTC",
) -> dict[str, Any]:
    starts_at = starts_at or datetime(2026, 6, 1, 9, tzinfo=UTC)
    ends_at = ends_at or starts_at + timedelta(hours=1)
    response = client.post(
        "/api/v1/calendar/events",
        headers=_headers(token, key),
        json={
            "title": title,
            "starts_at": starts_at.isoformat(),
            "ends_at": ends_at.isoformat(),
            "timezone": timezone,
        },
    )
    assert response.status_code == 201, response.text
    body: dict[str, Any] = response.json()
    return body


# ---------------------------------------------------------------------------
# Visibility: GET and LIST must both hide a private event from a non-owner.
# ---------------------------------------------------------------------------


def test_get_and_list_enforce_visibility_for_a_private_event(
    events_context: dict[str, Any],
) -> None:
    owner = events_context["owner"]
    member = events_context["member"]
    workspace_id = events_context["workspace_id"]

    event = _create_event(owner.client, owner.token, "create-private")
    event_id = event["id"]
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE calendar_events SET visibility = 'private' "
                "WHERE id = :id AND workspace_id = :workspace_id"
            ),
            {"id": event_id, "workspace_id": workspace_id},
        )

    # Owner still sees it via both GET and LIST.
    owner_get = owner.client.get(f"/api/v1/calendar/events/{event_id}")
    assert owner_get.status_code == 200
    owner_list = owner.client.get("/api/v1/calendar/events")
    assert owner_list.status_code == 200
    assert event_id in {item["id"] for item in owner_list.json()["items"]}

    # A different workspace member sees neither.
    member_get = member.client.get(f"/api/v1/calendar/events/{event_id}")
    assert member_get.status_code == 404
    assert member_get.json()["error"]["code"] == "CALENDAR_EVENT_NOT_FOUND"
    member_list = member.client.get("/api/v1/calendar/events")
    assert member_list.status_code == 200
    assert event_id not in {item["id"] for item in member_list.json()["items"]}


# ---------------------------------------------------------------------------
# Cross-workspace isolation.
# ---------------------------------------------------------------------------


def test_cross_workspace_actor_cannot_read_patch_or_archive(events_context: dict[str, Any]) -> None:
    owner = events_context["owner"]
    outsider = events_context["outsider"]

    event = _create_event(owner.client, owner.token, "create-cross-workspace")
    event_id = event["id"]

    get_response = outsider.client.get(f"/api/v1/calendar/events/{event_id}")
    assert get_response.status_code == 404

    patch_response = outsider.client.patch(
        f"/api/v1/calendar/events/{event_id}",
        headers=_headers(outsider.token, "cross-workspace-patch"),
        json={"expected_version": 1, "title": "Hijacked"},
    )
    assert patch_response.status_code == 404

    archive_response = outsider.client.post(
        f"/api/v1/calendar/events/{event_id}/archive",
        headers=_headers(outsider.token, "cross-workspace-archive"),
        json={"expected_version": 1},
    )
    assert archive_response.status_code == 404


# ---------------------------------------------------------------------------
# Pagination: cursor round-trip, tamper rejection, malformed cursor.
# ---------------------------------------------------------------------------


def test_list_pagination_cursor_round_trip_and_tamper_rejection(
    events_context: dict[str, Any],
) -> None:
    owner = events_context["owner"]
    base = datetime(2026, 6, 1, 9, tzinfo=UTC)
    for i in range(3):
        _create_event(
            owner.client,
            owner.token,
            f"paginate-{i}",
            title=f"Event {i}",
            starts_at=base + timedelta(hours=i),
        )

    first_page = owner.client.get("/api/v1/calendar/events?limit=2")
    assert first_page.status_code == 200
    body = first_page.json()
    assert len(body["items"]) == 2
    assert body["next_cursor"] is not None

    second_page = owner.client.get(f"/api/v1/calendar/events?limit=2&cursor={body['next_cursor']}")
    assert second_page.status_code == 200
    second_items = second_page.json()["items"]
    assert len(second_items) == 1
    first_ids = {item["id"] for item in body["items"]}
    assert second_items[0]["id"] not in first_ids

    malformed = owner.client.get("/api/v1/calendar/events?cursor=not-a-valid-cursor")
    assert malformed.status_code == 400
    assert malformed.json()["error"]["code"] == "MALFORMED_CURSOR"

    tampered = body["next_cursor"][:-1] + ("a" if body["next_cursor"][-1] != "a" else "b")
    tampered_response = owner.client.get(f"/api/v1/calendar/events?cursor={tampered}")
    assert tampered_response.status_code == 400
    assert tampered_response.json()["error"]["code"] == "MALFORMED_CURSOR"


# ---------------------------------------------------------------------------
# `day` filter requires `timezone` and correctly narrows the window.
# ---------------------------------------------------------------------------


def test_list_day_filter_requires_timezone_and_narrows_results(
    events_context: dict[str, Any],
) -> None:
    owner = events_context["owner"]
    in_day = _create_event(
        owner.client,
        owner.token,
        "in-day",
        title="In range",
        starts_at=datetime(2026, 6, 10, 9, tzinfo=UTC),
    )
    _create_event(
        owner.client,
        owner.token,
        "out-of-day",
        title="Out of range",
        starts_at=datetime(2026, 6, 12, 9, tzinfo=UTC),
    )

    missing_tz = owner.client.get("/api/v1/calendar/events?day=2026-06-10")
    assert missing_tz.status_code == 422
    assert missing_tz.json()["error"]["code"] == "TIMEZONE_REQUIRED"

    filtered = owner.client.get("/api/v1/calendar/events?day=2026-06-10&timezone=UTC")
    assert filtered.status_code == 200
    ids = {item["id"] for item in filtered.json()["items"]}
    assert ids == {in_day["id"]}


# ---------------------------------------------------------------------------
# Create validation error paths.
# ---------------------------------------------------------------------------


def test_create_validation_rejects_invalid_time_range_and_timezone(
    events_context: dict[str, Any],
) -> None:
    owner = events_context["owner"]
    starts_at = datetime(2026, 6, 1, 9, tzinfo=UTC)

    inverted = owner.client.post(
        "/api/v1/calendar/events",
        headers=_headers(owner.token, "invalid-range"),
        json={
            "title": "Backwards",
            "starts_at": starts_at.isoformat(),
            "ends_at": (starts_at - timedelta(hours=1)).isoformat(),
            "timezone": "UTC",
        },
    )
    assert inverted.status_code == 422

    naive_datetime = owner.client.post(
        "/api/v1/calendar/events",
        headers=_headers(owner.token, "naive-datetime"),
        json={
            "title": "No offset",
            "starts_at": starts_at.replace(tzinfo=None).isoformat(),
            "ends_at": (starts_at + timedelta(hours=1)).replace(tzinfo=None).isoformat(),
            "timezone": "UTC",
        },
    )
    assert naive_datetime.status_code == 422

    bad_timezone = owner.client.post(
        "/api/v1/calendar/events",
        headers=_headers(owner.token, "bad-timezone"),
        json={
            "title": "Bad TZ",
            "starts_at": starts_at.isoformat(),
            "ends_at": (starts_at + timedelta(hours=1)).isoformat(),
            "timezone": "Not/A_Real_Zone",
        },
    )
    assert bad_timezone.status_code == 422

    extra_field = owner.client.post(
        "/api/v1/calendar/events",
        headers=_headers(owner.token, "extra-field"),
        json={
            "title": "Extra",
            "starts_at": starts_at.isoformat(),
            "ends_at": (starts_at + timedelta(hours=1)).isoformat(),
            "timezone": "UTC",
            "unexpected_field": "nope",
        },
    )
    assert extra_field.status_code == 422


# ---------------------------------------------------------------------------
# Patch: version conflict, archived-event rejection, invalid range, no-op.
# ---------------------------------------------------------------------------


def test_patch_version_conflict_archived_rejection_invalid_range_and_noop(
    events_context: dict[str, Any],
) -> None:
    owner = events_context["owner"]
    event = _create_event(owner.client, owner.token, "patch-target")
    event_id = event["id"]

    stale = owner.client.patch(
        f"/api/v1/calendar/events/{event_id}",
        headers=_headers(owner.token, "stale-version"),
        json={"expected_version": 99, "title": "Stale"},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "VERSION_CONFLICT"

    invalid_range = owner.client.patch(
        f"/api/v1/calendar/events/{event_id}",
        headers=_headers(owner.token, "invalid-range-patch"),
        json={
            "expected_version": 1,
            "starts_at": event["ends_at"],
            "ends_at": event["starts_at"],
        },
    )
    assert invalid_range.status_code == 422
    assert invalid_range.json()["error"]["code"] == "INVALID_EVENT_TIME_RANGE"

    noop = owner.client.patch(
        f"/api/v1/calendar/events/{event_id}",
        headers=_headers(owner.token, "noop-patch"),
        json={"expected_version": 1},
    )
    assert noop.status_code == 200
    assert noop.json()["version"] == 1
    assert noop.json()["title"] == event["title"]

    archive = owner.client.post(
        f"/api/v1/calendar/events/{event_id}/archive",
        headers=_headers(owner.token, "archive-before-patch"),
        json={"expected_version": 1},
    )
    assert archive.status_code == 200
    assert archive.json()["version"] == 2

    patch_archived = owner.client.patch(
        f"/api/v1/calendar/events/{event_id}",
        headers=_headers(owner.token, "patch-archived"),
        json={"expected_version": 2, "title": "Should not apply"},
    )
    assert patch_archived.status_code == 409
    assert patch_archived.json()["error"]["code"] == "CALENDAR_EVENT_ARCHIVED"


# ---------------------------------------------------------------------------
# Archive/restore lifecycle edge cases.
# ---------------------------------------------------------------------------


def test_archive_is_idempotent_and_restore_rejects_a_non_archived_event(
    events_context: dict[str, Any],
) -> None:
    owner = events_context["owner"]
    event = _create_event(owner.client, owner.token, "lifecycle-target")
    event_id = event["id"]

    restore_not_archived = owner.client.post(
        f"/api/v1/calendar/events/{event_id}/restore",
        headers=_headers(owner.token, "restore-not-archived"),
        json={"expected_version": 1},
    )
    assert restore_not_archived.status_code == 409
    assert restore_not_archived.json()["error"]["code"] == "CALENDAR_EVENT_NOT_ARCHIVED"

    first_archive = owner.client.post(
        f"/api/v1/calendar/events/{event_id}/archive",
        headers=_headers(owner.token, "first-archive"),
        json={"expected_version": 1},
    )
    assert first_archive.status_code == 200
    assert first_archive.json()["version"] == 2
    assert first_archive.json()["archived_at"] is not None

    # Archiving an already-archived event with the now-current version is a
    # real no-op that still succeeds (idempotent), rather than conflicting.
    second_archive = owner.client.post(
        f"/api/v1/calendar/events/{event_id}/archive",
        headers=_headers(owner.token, "second-archive"),
        json={"expected_version": 2},
    )
    assert second_archive.status_code == 200
    assert second_archive.json()["version"] == 2
    assert second_archive.json()["archived_at"] == first_archive.json()["archived_at"]

    stale_restore = owner.client.post(
        f"/api/v1/calendar/events/{event_id}/restore",
        headers=_headers(owner.token, "stale-restore"),
        json={"expected_version": 1},
    )
    assert stale_restore.status_code == 409
    assert stale_restore.json()["error"]["code"] == "VERSION_CONFLICT"

    restore = owner.client.post(
        f"/api/v1/calendar/events/{event_id}/restore",
        headers=_headers(owner.token, "restore"),
        json={"expected_version": 2},
    )
    assert restore.status_code == 200
    assert restore.json()["version"] == 3
    assert restore.json()["archived_at"] is None
