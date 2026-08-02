"""Phase 7 Personal Intelligence Task 3: the `travel` domain
(`docs/superpowers/specs/2026-07-31-phase-7-personal-intelligence-design.md`,
`docs/phases/phase-007/DATA-MODEL.md`'s "Task 3 status" section).

Like `learning` (Task 2), `travel` is `standard`-classified and onboards
through the same generic domain enablement / `domain_records` CRUD
(`ecc.domains.personal.domains`) with no domain-specific code path -- not
re-proven here beyond the enable/independent-domain checks every prior
task's own test file already carries.

What Task 3 actually adds: `travel` is the first domain whose records are
meaningfully time-ranged (a `trip`'s own `effective_at` is its start date,
matching `DATA-MODEL.md`'s Task 3 `record_type` convention), so this file
also exercises the new `effective_from`/`effective_until` query parameters
on `GET /personal/records` -- generic on the endpoint, not `travel`-
specific, but `travel` is the first domain with a real reason to use them.

Covers: enable/independent-domain isolation for `travel`; the `trip`
`record_type` convention (create/list/get/patch); `effective_from`/
`effective_until` range filtering (inclusive boundaries, combined with
`domain_key`, and the empty-result case for a range with no matches);
whole-domain export/delete.
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

from ecc.config import get_settings
from ecc.database import engine
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


@pytest.fixture
def travel_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Travel Test', 'UTC', :now)"
            ),
            {"id": workspace_id, "now": now},
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
                "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                "expires_at, last_seen_at) "
                "VALUES (:id, :workspace_id, :user_id, :token_hash, :expires_at, :now)"
            ),
            {
                "id": uuid4(),
                "workspace_id": workspace_id,
                "user_id": user_id,
                "token_hash": sha256(token.encode()).hexdigest(),
                "expires_at": now + timedelta(hours=1),
                "now": now,
            },
        )

    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    try:
        yield client, workspace_id, user_id, token
    finally:
        client.close()
        _cleanup_workspace(workspace_id)


def _cleanup_workspace(workspace_id: UUID) -> None:
    with engine.begin() as connection:
        for table in (
            "personal_insights",
            "check_ins",
            "routines",
            "goals",
            "domain_records",
            "domain_sources",
            "deletion_jobs",
            "domain_consents",
            "personal_domains",
            "event_outbox",
            "audit_events",
            "idempotency_records",
            "sessions",
            "users",
        ):
            connection.execute(
                text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                {"workspace_id": workspace_id},
            )
        connection.execute(
            text("DELETE FROM workspaces WHERE id = :workspace_id"), {"workspace_id": workspace_id}
        )


def _headers(token: str, key: str | None = None) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    headers = {"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _enable(client: TestClient, token: str, domain_key: str) -> dict[str, Any]:
    resp = client.post(
        "/api/v1/personal/domains",
        json={"domain_key": domain_key},
        headers=_headers(token, str(uuid4())),
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def _create_trip(
    client: TestClient, token: str, *, destination: str, start_date: datetime
) -> dict[str, Any]:
    resp = client.post(
        "/api/v1/personal/records",
        json={
            "domain_key": "travel",
            "record_type": "trip",
            "payload": {"destination": destination, "start_date": start_date.date().isoformat()},
            "effective_at": start_date.isoformat(),
        },
        headers=_headers(token, str(uuid4())),
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def test_enable_travel_domain(
    travel_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = travel_test_context
    body = _enable(client, token, "travel")
    assert body["domain_key"] == "travel"
    assert body["classification"] == "standard"
    assert body["enabled"] is True


def test_enable_travel_does_not_enable_habits(
    travel_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = travel_test_context
    _enable(client, token, "travel")
    domains = client.get("/api/v1/personal/domains", headers=_headers(token)).json()["domains"]
    assert [d["domain_key"] for d in domains] == ["travel"]

    resp = client.post(
        "/api/v1/personal/records",
        json={"domain_key": "habits", "record_type": "note", "payload": {}},
        headers=_headers(token, str(uuid4())),
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "DOMAIN_NOT_ENABLED"


def test_trip_record_create_list_get_patch(
    travel_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = travel_test_context
    _enable(client, token, "travel")

    start = datetime(2026, 9, 1, tzinfo=UTC)
    created = _create_trip(client, token, destination="Lisbon", start_date=start)
    assert created["record_type"] == "trip"
    assert created["payload"]["destination"] == "Lisbon"

    listed = client.get(
        "/api/v1/personal/records?domain_key=travel", headers=_headers(token)
    ).json()["records"]
    assert [r["id"] for r in listed] == [created["id"]]

    fetched = client.get(f"/api/v1/personal/records/{created['id']}", headers=_headers(token))
    assert fetched.json()["payload"]["destination"] == "Lisbon"

    patched = client.patch(
        f"/api/v1/personal/records/{created['id']}",
        json={
            "expected_version": 1,
            "payload": {
                "destination": "Lisbon",
                "start_date": "2026-09-01",
                "end_date": "2026-09-08",
            },
        },
        headers=_headers(token, str(uuid4())),
    )
    assert patched.status_code == 200
    assert patched.json()["payload"]["end_date"] == "2026-09-08"
    assert patched.json()["version"] == 2


def test_records_filtered_by_effective_date_range(
    travel_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = travel_test_context
    _enable(client, token, "travel")

    jan = _create_trip(
        client, token, destination="Tokyo", start_date=datetime(2026, 1, 10, tzinfo=UTC)
    )
    jun = _create_trip(
        client, token, destination="Oslo", start_date=datetime(2026, 6, 15, tzinfo=UTC)
    )
    dec = _create_trip(
        client, token, destination="Cape Town", start_date=datetime(2026, 12, 20, tzinfo=UTC)
    )

    # A range covering only the June trip.
    mid_year = client.get(
        "/api/v1/personal/records"
        "?domain_key=travel&effective_from=2026-05-01T00:00:00Z&effective_until=2026-07-01T00:00:00Z",
        headers=_headers(token),
    ).json()["records"]
    assert [r["id"] for r in mid_year] == [jun["id"]]

    # Boundary is inclusive on both ends -- an exact-instant range around
    # the January trip's own effective_at must still match it.
    exact_boundary = client.get(
        "/api/v1/personal/records"
        "?domain_key=travel&effective_from=2026-01-10T00:00:00Z&effective_until=2026-01-10T00:00:00Z",
        headers=_headers(token),
    ).json()["records"]
    assert [r["id"] for r in exact_boundary] == [jan["id"]]

    # Only a lower bound -- everything from June onward.
    from_only = client.get(
        "/api/v1/personal/records?domain_key=travel&effective_from=2026-06-01T00:00:00Z",
        headers=_headers(token),
    ).json()["records"]
    assert {r["id"] for r in from_only} == {jun["id"], dec["id"]}

    # Only an upper bound -- everything up to and including June.
    until_only = client.get(
        "/api/v1/personal/records?domain_key=travel&effective_until=2026-07-01T00:00:00Z",
        headers=_headers(token),
    ).json()["records"]
    assert {r["id"] for r in until_only} == {jan["id"], jun["id"]}

    # A range with no matches returns an empty list, not an error.
    no_matches = client.get(
        "/api/v1/personal/records"
        "?domain_key=travel&effective_from=2027-01-01T00:00:00Z&effective_until=2027-02-01T00:00:00Z",
        headers=_headers(token),
    )
    assert no_matches.status_code == 200
    assert no_matches.json()["records"] == []


def test_export_and_delete_travel_domain(
    travel_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, _user_id, token = travel_test_context
    _enable(client, token, "travel")
    _create_trip(client, token, destination="Lisbon", start_date=datetime(2026, 9, 1, tzinfo=UTC))

    export = client.post(
        "/api/v1/personal/domains/travel/export", headers=_headers(token, str(uuid4()))
    )
    assert export.status_code == 200
    assert len(export.json()["domain_records"]) == 1

    delete = client.post(
        "/api/v1/personal/domains/travel/delete", headers=_headers(token, str(uuid4()))
    )
    assert delete.status_code == 200
    assert delete.json()["status"] == "completed"

    records = client.get(
        "/api/v1/personal/records?domain_key=travel", headers=_headers(token)
    ).json()["records"]
    assert records == []

    with engine.begin() as connection:
        jobs = (
            connection.execute(
                text(
                    "SELECT status FROM deletion_jobs "
                    "WHERE workspace_id = :workspace_id AND domain_key = 'travel'"
                ),
                {"workspace_id": workspace_id},
            )
            .mappings()
            .all()
        )
    assert len(jobs) == 1
    assert jobs[0]["status"] == "completed"
