"""Phase 7 Personal Intelligence Task 4: the `relationships` domain
(`docs/superpowers/specs/2026-07-31-phase-7-personal-intelligence-design.md`,
`docs/phases/phase-007/DATA-MODEL.md`'s "Task 4 status" section).

`relationships` is `sensitive`-classified -- the first domain to exercise
real field-level encryption (`ecc.domains.personal.crypto`, deferred until
now since `habits`/`learning`/`travel` are all `standard`). Enable/
independent-domain isolation is not re-proven beyond what every prior
task's own test file already carries; what this file actually covers:

1. The `contact`/`interaction` `record_type` conventions (create/list/get/
   patch), including the structured `contact_name`/`relationship_type`/
   `interaction_type` fields staying plaintext alongside the encrypted
   `notes` field on the same record.
2. `notes` is genuinely encrypted at rest -- verified by querying
   `domain_records.payload` directly via raw SQL, not just trusting the
   API response, since an API-only test could pass even if encryption were
   silently a no-op.
3. List/summary responses redact `notes` to a placeholder; only a single-
   record response (create/get/patch) returns the decrypted value.
4. The idempotency cache (`idempotency_records.response_body`) never
   stores the decrypted `notes` value -- verified the same way, via raw
   SQL against the table, not just the HTTP response shape.
5. Whole-domain export returns `notes` decrypted (a human-readable export
   returning ciphertext would defeat its own purpose) and deletion removes
   the encrypted records the same as any other domain.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from typing import Any
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
def relationships_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Relationships Test', 'UTC', :now)"
            ),
            {"id": workspace_id, "now": now},
        )
        connection.execute(
            text(
                "INSERT INTO users (id, workspace_id, email, password_hash, created_at) "
                "VALUES (:id, :workspace_id, :email, 'test-password-hash', :now)"
            ),
            {
                "id": user_id,
                "workspace_id": workspace_id,
                "email": f"{user_id}@example.test",
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


def _create_contact(
    client: TestClient, token: str, *, name: str, notes: str, key: str | None = None
) -> dict[str, Any]:
    resp = client.post(
        "/api/v1/personal/records",
        json={
            "domain_key": "relationships",
            "record_type": "contact",
            "payload": {
                "contact_name": name,
                "relationship_type": "friend",
                "notes": notes,
            },
        },
        headers=_headers(token, key or str(uuid4())),
    )
    assert resp.status_code == 201, resp.text
    return dict(resp.json())


def test_enable_relationships_domain(
    relationships_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = relationships_test_context
    body = _enable(client, token, "relationships")
    assert body["domain_key"] == "relationships"
    assert body["classification"] == "sensitive"
    assert body["enabled"] is True


def test_enable_relationships_does_not_enable_habits(
    relationships_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = relationships_test_context
    _enable(client, token, "relationships")
    domains = client.get("/api/v1/personal/domains", headers=_headers(token)).json()["domains"]
    assert [d["domain_key"] for d in domains] == ["relationships"]

    resp = client.post(
        "/api/v1/personal/records",
        json={"domain_key": "habits", "record_type": "note", "payload": {}},
        headers=_headers(token, str(uuid4())),
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "DOMAIN_NOT_ENABLED"


def test_contact_record_create_get_returns_decrypted_notes(
    relationships_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = relationships_test_context
    _enable(client, token, "relationships")

    created = _create_contact(client, token, name="Jamie Rivera", notes="Met at the conference")
    assert created["record_type"] == "contact"
    assert created["payload"]["contact_name"] == "Jamie Rivera"
    assert created["payload"]["notes"] == "Met at the conference"

    fetched = client.get(f"/api/v1/personal/records/{created['id']}", headers=_headers(token))
    assert fetched.status_code == 200
    assert fetched.json()["payload"]["notes"] == "Met at the conference"
    assert fetched.json()["payload"]["contact_name"] == "Jamie Rivera"


def test_notes_field_is_actually_encrypted_at_rest(
    relationships_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, _user_id, token = relationships_test_context
    _enable(client, token, "relationships")
    created = _create_contact(client, token, name="Jamie Rivera", notes="Met at the conference")

    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT payload FROM domain_records "
                    "WHERE id = :id AND workspace_id = :workspace_id"
                ),
                {"id": created["id"], "workspace_id": workspace_id},
            )
            .mappings()
            .one()
        )
    stored_payload = row["payload"]
    # Plaintext structured fields are queryable/unmodified at rest.
    assert stored_payload["contact_name"] == "Jamie Rivera"
    assert stored_payload["relationship_type"] == "friend"
    # The encrypted field is neither the plaintext nor human-readable --
    # a real Fernet token, not the API response's decrypted value.
    assert stored_payload["notes"] != "Met at the conference"
    assert "conference" not in stored_payload["notes"]
    assert len(stored_payload["notes"]) > len("Met at the conference")


def test_list_records_redacts_notes_but_get_does_not(
    relationships_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = relationships_test_context
    _enable(client, token, "relationships")
    created = _create_contact(client, token, name="Jamie Rivera", notes="Met at the conference")

    listed = client.get(
        "/api/v1/personal/records?domain_key=relationships", headers=_headers(token)
    ).json()["records"]
    assert len(listed) == 1
    assert listed[0]["payload"]["notes"] == "***encrypted***"
    assert listed[0]["payload"]["contact_name"] == "Jamie Rivera"

    fetched = client.get(f"/api/v1/personal/records/{created['id']}", headers=_headers(token))
    assert fetched.json()["payload"]["notes"] == "Met at the conference"


def test_patch_contact_notes_re_encrypts_and_returns_decrypted(
    relationships_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, _user_id, token = relationships_test_context
    _enable(client, token, "relationships")
    created = _create_contact(client, token, name="Jamie Rivera", notes="Met at the conference")

    patched = client.patch(
        f"/api/v1/personal/records/{created['id']}",
        json={
            "expected_version": 1,
            "payload": {
                "contact_name": "Jamie Rivera",
                "relationship_type": "friend",
                "notes": "Followed up over coffee",
            },
        },
        headers=_headers(token, str(uuid4())),
    )
    assert patched.status_code == 200
    assert patched.json()["payload"]["notes"] == "Followed up over coffee"
    assert patched.json()["version"] == 2

    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT payload FROM domain_records "
                    "WHERE id = :id AND workspace_id = :workspace_id"
                ),
                {"id": created["id"], "workspace_id": workspace_id},
            )
            .mappings()
            .one()
        )
    assert "coffee" not in row["payload"]["notes"]


def test_interaction_record_notes_are_encrypted(
    relationships_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, _user_id, token = relationships_test_context
    _enable(client, token, "relationships")
    contact = _create_contact(client, token, name="Jamie Rivera", notes="")

    resp = client.post(
        "/api/v1/personal/records",
        json={
            "domain_key": "relationships",
            "record_type": "interaction",
            "payload": {
                "contact_record_id": contact["id"],
                "interaction_type": "call",
                "notes": "Discussed the upcoming move",
            },
        },
        headers=_headers(token, str(uuid4())),
    )
    assert resp.status_code == 201
    interaction = resp.json()
    assert interaction["payload"]["notes"] == "Discussed the upcoming move"

    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT payload FROM domain_records "
                    "WHERE id = :id AND workspace_id = :workspace_id"
                ),
                {"id": interaction["id"], "workspace_id": workspace_id},
            )
            .mappings()
            .one()
        )
    assert "move" not in row["payload"]["notes"]
    assert row["payload"]["interaction_type"] == "call"


def test_idempotent_replay_never_persists_decrypted_notes(
    relationships_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """The idempotency cache (`idempotency_records.response_body`) must
    never hold the decrypted `notes` value -- a replayed request should
    still see the correct decrypted content, but the persisted cache row
    itself must stay encrypted, matching `crypto.py`'s "never logged,
    returned by an unrelated response, or written into an audit/outbox
    payload" discipline extended to this table.
    """
    client, workspace_id, _user_id, token = relationships_test_context
    _enable(client, token, "relationships")
    key = str(uuid4())
    created = _create_contact(
        client, token, name="Jamie Rivera", notes="Met at the conference", key=key
    )

    replay = client.post(
        "/api/v1/personal/records",
        json={
            "domain_key": "relationships",
            "record_type": "contact",
            "payload": {
                "contact_name": "Jamie Rivera",
                "relationship_type": "friend",
                "notes": "Met at the conference",
            },
        },
        headers=_headers(token, key),
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == created["id"]
    assert replay.json()["payload"]["notes"] == "Met at the conference"

    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT response_body FROM idempotency_records "
                    "WHERE workspace_id = :workspace_id AND key = :key"
                ),
                {"workspace_id": workspace_id, "key": key},
            )
            .mappings()
            .one()
        )
    cached_payload = row["response_body"]["payload"]
    assert cached_payload["notes"] != "Met at the conference"
    assert "conference" not in cached_payload["notes"]


def test_export_relationships_domain_returns_decrypted_notes(
    relationships_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, _user_id, token = relationships_test_context
    _enable(client, token, "relationships")
    _create_contact(client, token, name="Jamie Rivera", notes="Met at the conference")

    export = client.post(
        "/api/v1/personal/domains/relationships/export", headers=_headers(token, str(uuid4()))
    )
    assert export.status_code == 200
    records = export.json()["domain_records"]
    assert len(records) == 1
    assert records[0]["payload"]["notes"] == "Met at the conference"

    delete = client.post(
        "/api/v1/personal/domains/relationships/delete", headers=_headers(token, str(uuid4()))
    )
    assert delete.status_code == 200
    assert delete.json()["status"] == "completed"

    records_after = client.get(
        "/api/v1/personal/records?domain_key=relationships", headers=_headers(token)
    ).json()["records"]
    assert records_after == []

    with engine.begin() as connection:
        jobs = (
            connection.execute(
                text(
                    "SELECT status FROM deletion_jobs "
                    "WHERE workspace_id = :workspace_id AND domain_key = 'relationships'"
                ),
                {"workspace_id": workspace_id},
            )
            .mappings()
            .all()
        )
    assert len(jobs) == 1
    assert jobs[0]["status"] == "completed"
