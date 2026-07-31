"""Phase 7 Personal Intelligence Task 6: the `health` domain
(`docs/superpowers/specs/2026-07-31-phase-7-personal-intelligence-design.md`,
`docs/phases/phase-007/DATA-MODEL.md`'s "Task 6 status" section).

`health` is `high_stakes`-classified -- the first domain the design doc's
retention-acknowledgement requirement and `INSIGHT-CONTRACT.md`'s
`professional_referral_note` requirement both bind against real data
(Task 5 part 2 built the mechanism; `relationships`'/`learning`'s own
grants are `standard`/`sensitive`, never triggering it). This file covers:

1. `vital_reading`/`symptom_log` `record_type` conventions (create/get),
   including `symptom_description`/`notes` staying encrypted alongside
   plaintext structured fields (`metric`, `severity`) on the same record.
2. `POST /personal/records` under a `high_stakes` domain requires
   `retention_acknowledged: true` -- rejected with `422 RETENTION_
   ACKNOWLEDGEMENT_REQUIRED` without it; `retention_acknowledged_at` is
   set to the creation time when supplied, `NULL` otherwise.
3. `symptom_description`/`notes` are genuinely encrypted at rest (direct
   SQL, not just the API response shape) -- the same verification `test_
   personal_relationships_postgres.py` already established for `notes`.
4. Whole-domain export/delete works unmodified.
5. End to end, against a real grant (not the evaluation harness's
   synthetic fixtures): a `health`-sourced AI-generated insight genuinely
   requires a non-empty `professional_referral_note`, proving Task 5 part
   2's `requires_professional_referral` plumbing actually fires for a
   real `high_stakes` domain, not only the evaluation dataset's synthetic
   `health` sources.
"""

import json as json_module
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import engine
from ecc.domains.ai_runtime.ollama_client import OllamaAdapter
from ecc.domains.ai_runtime.runtime import get_ollama_adapter, reset_circuit_breakers
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


@pytest.fixture(autouse=True)
def _reset_breakers() -> Iterator[None]:
    reset_circuit_breakers()
    yield
    reset_circuit_breakers()


@pytest.fixture
def health_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Health Test', 'UTC', :now)"
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
            "generated_artifacts",
            "ai_run_steps",
            "ai_runs",
            "personal_insight_feedback",
            "personal_insights",
            "check_ins",
            "routines",
            "goals",
            "domain_records",
            "domain_sources",
            "deletion_jobs",
            "cross_domain_grants",
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


def _create_vital_reading(
    client: TestClient,
    token: str,
    *,
    notes: str = "Felt fine during the reading",
    retention_acknowledged: bool = True,
    key: str | None = None,
) -> httpx.Response:
    return client.post(
        "/api/v1/personal/records",
        json={
            "domain_key": "health",
            "record_type": "vital_reading",
            "payload": {"metric": "resting_heart_rate", "value": "58", "notes": notes},
            "retention_acknowledged": retention_acknowledged,
        },
        headers=_headers(token, key or str(uuid4())),
    )


def test_enable_health_domain(
    health_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = health_test_context
    body = _enable(client, token, "health")
    assert body["domain_key"] == "health"
    assert body["classification"] == "high_stakes"


def test_create_record_without_retention_acknowledgement_is_rejected(
    health_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _user_id, token = health_test_context
    _enable(client, token, "health")
    resp = _create_vital_reading(client, token, retention_acknowledged=False)
    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["code"] == "RETENTION_ACKNOWLEDGEMENT_REQUIRED"

    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM domain_records WHERE workspace_id = :workspace_id"),
            {"workspace_id": health_test_context[1]},
        ).scalar_one()
    assert count == 0


def test_create_record_with_retention_acknowledgement_sets_timestamp(
    health_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, _user_id, token = health_test_context
    _enable(client, token, "health")
    resp = _create_vital_reading(client, token, retention_acknowledged=True)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["retention_acknowledged_at"] is not None
    assert body["payload"]["notes"] == "Felt fine during the reading"

    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT retention_acknowledged_at FROM domain_records "
                    "WHERE id = :id AND workspace_id = :workspace_id"
                ),
                {"id": body["id"], "workspace_id": workspace_id},
            )
            .mappings()
            .one()
        )
    assert row["retention_acknowledged_at"] is not None


def test_retention_acknowledgement_not_required_for_standard_domain(
    health_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    """`retention_acknowledged` is a `health`-specific (`high_stakes`)
    requirement -- a `standard`-classified domain (`habits`) must still
    accept a record with no acknowledgement at all, unaffected.
    """
    client, _workspace_id, _user_id, token = health_test_context
    _enable(client, token, "habits")
    resp = client.post(
        "/api/v1/personal/records",
        json={"domain_key": "habits", "record_type": "note", "payload": {}},
        headers=_headers(token, str(uuid4())),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["retention_acknowledged_at"] is None


def test_symptom_log_description_is_encrypted_at_rest(
    health_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, _user_id, token = health_test_context
    _enable(client, token, "health")
    resp = client.post(
        "/api/v1/personal/records",
        json={
            "domain_key": "health",
            "record_type": "symptom_log",
            "payload": {"symptom_description": "Mild headache after lunch", "severity": "mild"},
            "retention_acknowledged": True,
        },
        headers=_headers(token, str(uuid4())),
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["payload"]["symptom_description"] == "Mild headache after lunch"
    assert created["payload"]["severity"] == "mild"

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
    assert stored_payload["severity"] == "mild"
    assert stored_payload["symptom_description"] != "Mild headache after lunch"
    assert "headache" not in stored_payload["symptom_description"]

    listed = client.get(
        "/api/v1/personal/records?domain_key=health", headers=_headers(token)
    ).json()["records"]
    assert listed[0]["payload"]["symptom_description"] == "***encrypted***"


def test_export_and_delete_health_domain(
    health_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, _user_id, token = health_test_context
    _enable(client, token, "health")
    created = _create_vital_reading(client, token).json()

    export = client.post(
        "/api/v1/personal/domains/health/export", headers=_headers(token, str(uuid4()))
    )
    assert export.status_code == 200
    records = export.json()["domain_records"]
    assert len(records) == 1
    assert records[0]["payload"]["notes"] == "Felt fine during the reading"

    delete = client.post(
        "/api/v1/personal/domains/health/delete", headers=_headers(token, str(uuid4()))
    )
    assert delete.status_code == 200
    assert delete.json()["status"] == "completed"

    records_after = client.get(
        "/api/v1/personal/records?domain_key=health", headers=_headers(token)
    ).json()["records"]
    assert records_after == []
    assert created["id"]


# ---------------------------------------------------------------------------
# End to end: a real health-sourced insight requires professional_referral_
# note, against a real grant, not the evaluation harness's synthetic data.
# ---------------------------------------------------------------------------


def _mock_adapter_citing(
    record_id: UUID, *, professional_referral_note: str | None
) -> OllamaAdapter:
    payload = {
        "kind": "trend",
        "title": "Resting heart rate trend",
        "explanation_text": "Resting heart rate has stayed within a normal range this week.",
        "cited_record_ids": [str(record_id)],
        "source_period": "the past week",
        "missing_data": "none noted",
        "confidence": "medium",
        "limitations": "Based only on the records shown here.",
        "professional_referral_note": professional_referral_note,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = (
            json_module.dumps(
                {
                    "model": "m",
                    "created_at": "now",
                    "response": json_module.dumps(payload),
                    "done": True,
                    "eval_count": 12,
                    "prompt_eval_count": 40,
                }
            )
            + "\n"
        )
        return httpx.Response(
            200, content=body.encode(), headers={"content-type": "application/x-ndjson"}
        )

    return OllamaAdapter(transport=httpx.MockTransport(handler))


def test_health_sourced_insight_without_referral_note_fails_open(
    health_test_context: tuple[TestClient, UUID, UUID, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real, end-to-end proof that `high_stakes` classification alone
    -- not anything `health`-specific hardcoded into Task 5 part 2's own
    code -- is what triggers the `professional_referral_note` requirement:
    a real `health` domain, a real grant, a real record, and a model
    response that omits the note must fail grounding, exactly like the
    evaluation harness's synthetic `health` examples already prove in the
    abstract.
    """
    monkeypatch.setenv("ECC_PERSONAL_AI_INSIGHT_GENERATION_ENABLED", "true")
    get_settings.cache_clear()
    try:
        client, _workspace_id, _user_id, token = health_test_context
        _enable(client, token, "health")
        created = _create_vital_reading(client, token).json()
        grant_resp = client.post(
            "/api/v1/personal/grants",
            json={
                "source_domain_key": "health",
                "purpose": "insight_generation",
                "granted_categories": ["vital_reading"],
            },
            headers=_headers(token, str(uuid4())),
        )
        assert grant_resp.status_code == 201, grant_resp.text

        adapter = _mock_adapter_citing(UUID(created["id"]), professional_referral_note=None)
        app.dependency_overrides[get_ollama_adapter] = lambda: adapter
        try:
            resp = client.post(
                "/api/v1/personal/insights/generate",
                json={"source_domain_keys": ["health"]},
                headers=_headers(token, str(uuid4())),
            )
        finally:
            app.dependency_overrides.pop(get_ollama_adapter, None)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["available"] is False
        assert body["error_code"] == "grounding_failed"
        assert body["insight"] is None
    finally:
        get_settings.cache_clear()


def test_health_sourced_insight_with_referral_note_succeeds(
    health_test_context: tuple[TestClient, UUID, UUID, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ECC_PERSONAL_AI_INSIGHT_GENERATION_ENABLED", "true")
    get_settings.cache_clear()
    try:
        client, _workspace_id, _user_id, token = health_test_context
        _enable(client, token, "health")
        created = _create_vital_reading(client, token).json()
        grant_resp = client.post(
            "/api/v1/personal/grants",
            json={
                "source_domain_key": "health",
                "purpose": "insight_generation",
                "granted_categories": ["vital_reading"],
            },
            headers=_headers(token, str(uuid4())),
        )
        assert grant_resp.status_code == 201, grant_resp.text

        adapter = _mock_adapter_citing(
            UUID(created["id"]),
            professional_referral_note="Consider discussing this with your doctor.",
        )
        app.dependency_overrides[get_ollama_adapter] = lambda: adapter
        try:
            resp = client.post(
                "/api/v1/personal/insights/generate",
                json={"source_domain_keys": ["health"]},
                headers=_headers(token, str(uuid4())),
            )
        finally:
            app.dependency_overrides.pop(get_ollama_adapter, None)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["available"] is True
        assert body["insight"]["professional_referral_note"] == (
            "Consider discussing this with your doctor."
        )
    finally:
        get_settings.cache_clear()
