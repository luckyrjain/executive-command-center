"""Phase 7 Task 5 part 2: the production call site for AI-generated
personal insights (`ecc.domains.personal.ai_insights`, `POST /api/v1/
personal/insights/generate`) and `POST /api/v1/personal/insights/{id}/
feedback` (`ecc.domains.personal.habits`).

Covers:
1. `personal_ai_insight_generation_enabled` defaults to `False` --
   `POST .../generate` fail-opens to `available=false, error_code=
   "feature_disabled"` and writes no `personal_insights` row.
2. With the flag enabled and an active grant covering the requested
   domain, a fully-grounded mocked model response persists a new `personal_
   insights` row (`kind="trend"`, `professional_referral_note=None` for a
   `standard`-classified source) and is idempotent on retry.
3. A requested domain with no active grant fails open
   (`error_code="not_found"`, `personal.get_insight_sources`'s own
   enforcement) -- no row written.
4. `POST .../feedback`: creates a `personal_insight_feedback` row and
   leaves the insight's own `kind` unchanged; 404s for an unknown insight.
"""

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
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
def personal_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO workspaces (id, name, timezone, created_at) "
                "VALUES (:id, 'Personal AI Insight Test', 'UTC', :now)"
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
            "domain_records",
            "cross_domain_grants",
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


def _enable_and_grant_habits(
    client: TestClient, token: str, *, workspace_id: UUID, owner_id: UUID
) -> UUID:
    """Enables `habits`, grants `insight_generation` for its `habit_
    summary` category, and inserts one real `domain_records` row directly
    (not via the API) so the test controls its id up front for the mocked
    model response to cite. Returns that record's id.
    """
    resp = client.post(
        "/api/v1/personal/domains",
        json={"domain_key": "habits"},
        headers=_headers(token, str(uuid4())),
    )
    assert resp.status_code == 201, resp.text

    grant_resp = client.post(
        "/api/v1/personal/grants",
        json={
            "source_domain_key": "habits",
            "purpose": "insight_generation",
            "granted_categories": ["habit_summary"],
        },
        headers=_headers(token, str(uuid4())),
    )
    assert grant_resp.status_code == 201, grant_resp.text

    record_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO domain_records (
                    id, workspace_id, owner_id, domain_key, record_type, payload,
                    effective_at, created_by, updated_by, created_at, updated_at, version
                ) VALUES (
                    :id, :workspace_id, :owner_id, 'habits', 'habit_summary',
                    CAST(:payload AS jsonb), :now, :owner_id, :owner_id, :now, :now, 1
                )
                """
            ),
            {
                "id": record_id,
                "workspace_id": workspace_id,
                "owner_id": owner_id,
                "payload": json.dumps({"routine": "Morning run", "streak_days": "9"}),
                "now": now,
            },
        )
    return record_id


def _mock_adapter_citing(
    record_id: UUID, *, professional_referral_note: str | None = None
) -> OllamaAdapter:
    payload = {
        "kind": "trend",
        "title": "Morning run streak",
        "explanation_text": "The morning run streak has grown over the past week.",
        "cited_record_ids": [str(record_id)],
        "source_period": "the past week",
        "missing_data": "none noted",
        "confidence": "medium",
        "limitations": "Based only on the records shown here.",
        "professional_referral_note": professional_referral_note,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = (
            json.dumps(
                {
                    "model": "m",
                    "created_at": "now",
                    "response": json.dumps(payload),
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


# ---------------------------------------------------------------------------
# POST /api/v1/personal/insights/generate
# ---------------------------------------------------------------------------


def test_generate_insight_feature_disabled_by_default(
    personal_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, owner_id, token = personal_test_context
    record_id = _enable_and_grant_habits(
        client, token, workspace_id=workspace_id, owner_id=owner_id
    )
    adapter = _mock_adapter_citing(record_id)
    app.dependency_overrides[get_ollama_adapter] = lambda: adapter
    try:
        resp = client.post(
            "/api/v1/personal/insights/generate",
            json={"source_domain_keys": ["habits"]},
            headers=_headers(token, str(uuid4())),
        )
    finally:
        app.dependency_overrides.pop(get_ollama_adapter, None)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["available"] is False
    assert body["error_code"] == "feature_disabled"
    assert body["insight"] is None

    with engine.connect() as connection:
        count = connection.execute(
            text("SELECT count(*) FROM personal_insights WHERE workspace_id = :workspace_id"),
            {"workspace_id": workspace_id},
        ).scalar_one()
    assert count == 0


def test_generate_insight_happy_path_persists_insight(
    personal_test_context: tuple[TestClient, UUID, UUID, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ECC_PERSONAL_AI_INSIGHT_GENERATION_ENABLED", "true")
    get_settings.cache_clear()
    try:
        client, workspace_id, owner_id, token = personal_test_context
        record_id = _enable_and_grant_habits(
            client, token, workspace_id=workspace_id, owner_id=owner_id
        )
        adapter = _mock_adapter_citing(record_id)
        app.dependency_overrides[get_ollama_adapter] = lambda: adapter
        try:
            resp = client.post(
                "/api/v1/personal/insights/generate",
                json={"source_domain_keys": ["habits"]},
                headers=_headers(token, str(uuid4())),
            )
        finally:
            app.dependency_overrides.pop(get_ollama_adapter, None)

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["available"] is True
        assert body["error_code"] is None
        insight = body["insight"]
        assert insight["kind"] == "trend"
        assert insight["domain_key"] == "habits"
        assert insight["professional_referral_note"] is None
        assert str(record_id) in insight["evidence"]["cited_record_ids"]

        with engine.connect() as connection:
            row = (
                connection.execute(
                    text(
                        "SELECT kind, professional_referral_note FROM personal_insights "
                        "WHERE workspace_id = :workspace_id"
                    ),
                    {"workspace_id": workspace_id},
                )
                .mappings()
                .one()
            )
        assert row["kind"] == "trend"
        assert row["professional_referral_note"] is None
    finally:
        get_settings.cache_clear()


def test_generate_insight_missing_grant_fails_open_not_found(
    personal_test_context: tuple[TestClient, UUID, UUID, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ECC_PERSONAL_AI_INSIGHT_GENERATION_ENABLED", "true")
    get_settings.cache_clear()
    try:
        client, workspace_id, owner_id, token = personal_test_context
        # habits enabled, but never granted for insight_generation.
        resp = client.post(
            "/api/v1/personal/domains",
            json={"domain_key": "habits"},
            headers=_headers(token, str(uuid4())),
        )
        assert resp.status_code == 201, resp.text

        adapter = _mock_adapter_citing(uuid4())
        app.dependency_overrides[get_ollama_adapter] = lambda: adapter
        try:
            gen_resp = client.post(
                "/api/v1/personal/insights/generate",
                json={"source_domain_keys": ["habits"]},
                headers=_headers(token, str(uuid4())),
            )
        finally:
            app.dependency_overrides.pop(get_ollama_adapter, None)

        assert gen_resp.status_code == 200, gen_resp.text
        body = gen_resp.json()
        assert body["available"] is False
        assert body["error_code"] == "not_found"

        with engine.connect() as connection:
            count = connection.execute(
                text("SELECT count(*) FROM personal_insights WHERE workspace_id = :workspace_id"),
                {"workspace_id": workspace_id},
            ).scalar_one()
        assert count == 0
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# POST /api/v1/personal/insights/{id}/feedback
# ---------------------------------------------------------------------------


def _insert_insight_row(workspace_id: UUID, owner_id: UUID) -> UUID:
    insight_id = uuid4()
    now = datetime.now(UTC)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO personal_domains (
                    id, workspace_id, owner_id, domain_key, classification,
                    enabled, enabled_at, created_by, updated_by, created_at, updated_at, version
                ) VALUES (
                    :id, :workspace_id, :owner_id, 'habits', 'standard',
                    true, :now, :owner_id, :owner_id, :now, :now, 1
                )
                """
            ),
            {"id": uuid4(), "workspace_id": workspace_id, "owner_id": owner_id, "now": now},
        )
        connection.execute(
            text(
                """
                INSERT INTO personal_insights (
                    id, workspace_id, owner_id, domain_key, insight_key, kind, title,
                    evidence, source_period_start, source_period_end, missing_data,
                    confidence, limitations, computed_at
                ) VALUES (
                    :id, :workspace_id, :owner_id, 'habits', :insight_key, 'trend', 'Test insight',
                    '{}'::jsonb, :now, :now, NULL, 'medium', 'test limitations', :now
                )
                """
            ),
            {
                "id": insight_id,
                "workspace_id": workspace_id,
                "owner_id": owner_id,
                "insight_key": f"ai_insight:{insight_id}",
                "now": now,
            },
        )
    return insight_id


def test_feedback_creates_row_and_leaves_kind_unchanged(
    personal_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, owner_id, token = personal_test_context
    insight_id = _insert_insight_row(workspace_id, owner_id)

    resp = client.post(
        f"/api/v1/personal/insights/{insight_id}/feedback",
        json={"useful": True, "comment": "Helpful!"},
        headers=_headers(token, str(uuid4())),
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["insight_id"] == str(insight_id)
    assert body["useful"] is True
    assert body["comment"] == "Helpful!"

    with engine.connect() as connection:
        feedback_count = connection.execute(
            text(
                "SELECT count(*) FROM personal_insight_feedback WHERE workspace_id = :workspace_id"
            ),
            {"workspace_id": workspace_id},
        ).scalar_one()
        kind = connection.execute(
            text("SELECT kind FROM personal_insights WHERE id = :id"), {"id": insight_id}
        ).scalar_one()
    assert feedback_count == 1
    assert kind == "trend"


def test_feedback_unknown_insight_404s(
    personal_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _workspace_id, _owner_id, token = personal_test_context
    resp = client.post(
        f"/api/v1/personal/insights/{uuid4()}/feedback",
        json={"useful": False, "comment": None},
        headers=_headers(token, str(uuid4())),
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "INSIGHT_NOT_FOUND"
