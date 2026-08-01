"""Phase 7 Personal Intelligence Task 2: the `learning` domain
(`docs/superpowers/specs/2026-07-31-phase-7-personal-intelligence-design.md`,
`docs/phases/phase-007/DATA-MODEL.md`'s "Task 2 status" section).

**No new backend code exists for `learning` -- this file exists to prove
that, not to exercise a new module.** `learning` is `standard`-classified,
identical to `habits`; Task 1's domain enablement and generic
`domain_records` CRUD (`ecc.domains.personal.domains`) already accept any
closed-enum `domain_key`, not a `habits`-specific code path. This is the
concrete confirmation of the design doc's own "why `habits` first"
reasoning (Decision 1): a second `standard` domain onboards with zero new
backend code.

Covers: enable/list/disable for `learning` specifically (not merely
re-running `habits`' own tests with a different string -- `_classification_
for` and the `DomainKey` enum are the two places a typo could silently
degrade to the wrong behavior); the `course`/`resource` `record_type`
conventions `DATA-MODEL.md`'s Task 2 section documents (create/list/get/
patch, including a `course`'s `progress_pct` update); whole-domain export/
delete; and that `learning`'s own enablement is independent of `habits`'
(enabling one does not implicitly enable or leak into the other, per
`DOMAIN-PRIVACY-CONTRACT.md`'s "Enabling one does not grant another
access").
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
def learning_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Learning Test', 'UTC', :now)"
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


def test_enable_learning_domain(
    learning_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = learning_test_context
    body = _enable(client, token, "learning")
    assert body["domain_key"] == "learning"
    assert body["classification"] == "standard"
    assert body["enabled"] is True


def test_enable_learning_does_not_enable_habits(
    learning_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = learning_test_context
    _enable(client, token, "learning")
    domains = client.get("/api/v1/personal/domains", headers=_headers(token)).json()["domains"]
    assert [d["domain_key"] for d in domains] == ["learning"]

    resp = client.post(
        "/api/v1/personal/records",
        json={"domain_key": "habits", "record_type": "note", "payload": {}},
        headers=_headers(token, str(uuid4())),
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "DOMAIN_NOT_ENABLED"


def test_course_record_create_list_get_patch(
    learning_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = learning_test_context
    _enable(client, token, "learning")

    created = client.post(
        "/api/v1/personal/records",
        json={
            "domain_key": "learning",
            "record_type": "course",
            "payload": {"title": "Intro to Statistics", "provider": "Coursera"},
        },
        headers=_headers(token, str(uuid4())),
    )
    assert created.status_code == 201
    record = created.json()
    assert record["record_type"] == "course"

    listed = client.get(
        "/api/v1/personal/records?domain_key=learning", headers=_headers(token)
    ).json()["records"]
    assert [r["id"] for r in listed] == [record["id"]]

    fetched = client.get(f"/api/v1/personal/records/{record['id']}", headers=_headers(token))
    assert fetched.json()["payload"]["title"] == "Intro to Statistics"

    patched = client.patch(
        f"/api/v1/personal/records/{record['id']}",
        json={
            "expected_version": 1,
            "payload": {"title": "Intro to Statistics", "provider": "Coursera", "progress_pct": 40},
        },
        headers=_headers(token, str(uuid4())),
    )
    assert patched.status_code == 200
    assert patched.json()["payload"]["progress_pct"] == 40
    assert patched.json()["version"] == 2


def test_resource_record_create(
    learning_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = learning_test_context
    _enable(client, token, "learning")
    resp = client.post(
        "/api/v1/personal/records",
        json={
            "domain_key": "learning",
            "record_type": "resource",
            "payload": {"title": "Deep Learning Book", "resource_type": "book"},
        },
        headers=_headers(token, str(uuid4())),
    )
    assert resp.status_code == 201
    assert resp.json()["payload"]["resource_type"] == "book"


def test_export_and_delete_learning_domain(
    learning_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, _user_id, token = learning_test_context
    _enable(client, token, "learning")
    client.post(
        "/api/v1/personal/records",
        json={
            "domain_key": "learning",
            "record_type": "course",
            "payload": {"title": "Intro to Statistics"},
        },
        headers=_headers(token, str(uuid4())),
    )

    export = client.post(
        "/api/v1/personal/domains/learning/export", headers=_headers(token, str(uuid4()))
    )
    assert export.status_code == 200
    assert len(export.json()["domain_records"]) == 1

    delete = client.post(
        "/api/v1/personal/domains/learning/delete", headers=_headers(token, str(uuid4()))
    )
    assert delete.status_code == 200
    assert delete.json()["status"] == "completed"

    records = client.get(
        "/api/v1/personal/records?domain_key=learning", headers=_headers(token)
    ).json()["records"]
    assert records == []

    with engine.begin() as connection:
        jobs = (
            connection.execute(
                text(
                    "SELECT status FROM deletion_jobs "
                    "WHERE workspace_id = :workspace_id AND domain_key = 'learning'"
                ),
                {"workspace_id": workspace_id},
            )
            .mappings()
            .all()
        )
    assert len(jobs) == 1
    assert jobs[0]["status"] == "completed"
