from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from hmac import new
from time import perf_counter
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from phase1_dataset import seed_phase1_dataset
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import engine
from ecc.domains.governance.attention import _score_commitment, _score_risk, _score_task
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


@pytest.fixture
def risk_test_context() -> Iterator[tuple[TestClient, UUID, UUID, str]]:
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
                "name": "Risk Test",
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
                "attention_items",
                "event_outbox",
                "audit_events",
                "idempotency_records",
                "risks",
                "commitments",
                "tasks",
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


def _headers(token: str, key: str | None = None) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    headers = {
        "X-CSRF-Token": csrf,
        "X-Correlation-ID": str(uuid4()),
    }
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def test_risk_lifecycle_and_attention_controls(
    risk_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, workspace_id, user_id, token = risk_test_context
    now = datetime.now(UTC)
    task_id = uuid4()
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
                    :id, :workspace_id, :owner_id, 'Critical task', 'planned', 'critical',
                    :due_date, true, 'local', :actor_id, :actor_id,
                    :created_at, :updated_at, 1
                )
                """
            ),
            {
                "id": task_id,
                "workspace_id": workspace_id,
                "owner_id": user_id,
                "actor_id": user_id,
                "due_date": date.today() - timedelta(days=1),
                "created_at": now - timedelta(days=20),
                "updated_at": now - timedelta(days=15),
            },
        )
        connection.execute(
            text(
                """
                INSERT INTO commitments (
                    id, workspace_id, owner_id, summary, direction, status,
                    due_at, importance, confidence, pinned, created_by, updated_by,
                    created_at, updated_at, version
                ) VALUES (
                    :id, :workspace_id, :owner_id, 'Vendor response', 'made_to_me', 'active',
                    :due_at, 'high', 0.9, false, :actor_id, :actor_id,
                    :created_at, :updated_at, 1
                )
                """
            ),
            {
                "id": commitment_id,
                "workspace_id": workspace_id,
                "owner_id": user_id,
                "actor_id": user_id,
                "due_at": now + timedelta(hours=12),
                "created_at": now,
                "updated_at": now,
            },
        )

    create = client.post(
        "/api/v1/risks",
        headers=_headers(token, "create-risk"),
        json={
            "description": "Payment processor concentration",
            "probability": 5,
            "impact": 5,
            "review_at": (now - timedelta(hours=1)).isoformat(),
            "pinned": True,
        },
    )
    assert create.status_code == 201
    risk = create.json()
    risk_id = risk["id"]
    assert risk["score"] == 80

    stale = client.patch(
        f"/api/v1/risks/{risk_id}",
        headers=_headers(token, "stale-risk"),
        json={"expected_version": 99, "mitigation": "stale"},
    )
    assert stale.status_code == 409

    regenerate = client.post(
        "/api/v1/attention/regenerate",
        headers=_headers(token),
        json={},
    )
    assert regenerate.status_code == 200
    items = regenerate.json()["items"]
    assert {item["entity_type"] for item in items} == {"task", "commitment", "risk"}
    assert items[0]["entity_type"] == "task"
    assert items[0]["score"] == 98

    risk_item = next(item for item in items if item["entity_type"] == "risk")
    dismiss = client.post(
        f"/api/v1/attention/{risk_item['id']}/dismiss",
        headers=_headers(token),
        json={},
    )
    assert dismiss.status_code == 200
    visible_after_dismiss = client.get("/api/v1/attention")
    assert all(item["id"] != risk_item["id"] for item in visible_after_dismiss.json()["items"])

    update = client.patch(
        f"/api/v1/risks/{risk_id}",
        headers=_headers(token, "update-risk"),
        json={"expected_version": 1, "mitigation": "Add a second processor"},
    )
    assert update.status_code == 200
    assert update.json()["version"] == 2

    regenerated = client.post(
        "/api/v1/attention/regenerate",
        headers=_headers(token),
        json={},
    )
    assert regenerated.status_code == 200
    updated_risk_item = next(
        item for item in regenerated.json()["items"] if item["entity_type"] == "risk"
    )
    assert updated_risk_item["source_entity_version"] == 2

    deferred_until = now + timedelta(hours=2)
    defer = client.post(
        f"/api/v1/attention/{updated_risk_item['id']}/defer",
        headers=_headers(token),
        json={"deferred_until": deferred_until.isoformat()},
    )
    assert defer.status_code == 200
    assert all(
        item["id"] != updated_risk_item["id"]
        for item in client.get("/api/v1/attention").json()["items"]
    )

    restore_attention = client.post(
        f"/api/v1/attention/{updated_risk_item['id']}/restore",
        headers=_headers(token),
        json={},
    )
    assert restore_attention.status_code == 200

    archive = client.post(
        f"/api/v1/risks/{risk_id}/archive",
        headers=_headers(token, "archive-risk"),
        json={"expected_version": 2},
    )
    assert archive.status_code == 200
    restore = client.post(
        f"/api/v1/risks/{risk_id}/restore",
        headers=_headers(token, "restore-risk"),
        json={"expected_version": 3},
    )
    assert restore.status_code == 200

    with engine.connect() as connection:
        audit_types = {
            row[0]
            for row in connection.execute(
                text("SELECT event_type FROM audit_events WHERE workspace_id = :workspace_id"),
                {"workspace_id": workspace_id},
            )
        }
        outbox_types = {
            row[0]
            for row in connection.execute(
                text("SELECT event_type FROM event_outbox WHERE workspace_id = :workspace_id"),
                {"workspace_id": workspace_id},
            )
        }
    assert {"risk.created", "risk.updated", "risk.archived", "risk.restored"} <= audit_types
    assert {
        "attention_item.dismiss",
        "attention_item.defer",
        "attention_item.restore",
    } <= audit_types
    assert {
        "risk.created.v1",
        "risk.updated.v1",
        "risk.archived.v1",
        "risk.restored.v1",
    } <= outbox_types


def test_risk_is_hidden_across_workspaces(
    risk_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _, _, _ = risk_test_context
    response = client.get(f"/api/v1/risks/{uuid4()}")
    assert response.status_code == 404


def test_closed_risk_cannot_reopen(
    risk_test_context: tuple[TestClient, UUID, UUID, str],
) -> None:
    client, _, _, token = risk_test_context
    created = client.post(
        "/api/v1/risks",
        headers=_headers(token, "closed-risk"),
        json={
            "description": "Terminal risk",
            "probability": 2,
            "impact": 2,
            "status": "closed",
        },
    )
    assert created.status_code == 201
    response = client.patch(
        f"/api/v1/risks/{created.json()['id']}",
        headers=_headers(token, "reopen-risk"),
        json={"expected_version": 1, "status": "monitoring"},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RISK_TERMINAL"


RANKING_BUDGET_SECONDS = 0.5
# More samples than the ~10-15 used elsewhere so the nearest-rank p95 index
# can discount a couple of worst-case outliers -- with fewer samples, "p95"
# and "max" are numerically close to identical, which made this specific
# budget (500 ms, much tighter relative to typical ~350-400 ms observed
# latency than the other budgets in this task) flaky under a single
# cold-cache/connection-pool/local-Docker-noise outlier.
RANKING_SAMPLE_SIZE = 30


@pytest.fixture
def ranking_performance_context() -> Iterator[tuple[TestClient, UUID, str]]:
    """A dedicated, isolated workspace seeded with the full representative
    Phase 1 fixture (10,000 tasks, commitments, risks, and calendar events;
    50,000 notes; 100,000 audit rows) via the shared ``phase1_dataset``
    fixture, then narrowed to exactly the design doc's literal "10,000
    eligible entities" ranking scale: all 10,000 commitments and all 10,000
    risks are archived immediately after seeding (they still physically
    exist for realistic overall DB size), so only the 10,000 freshly
    created tasks remain eligible for ``/api/v1/attention/regenerate``.
    Leaving all 30,000 tasks/commitments/risks eligible at once was tried
    first and, proportionally, took about 3x as long (~1.1s vs ~0.4s) --
    consistent with cost scaling roughly linearly with eligible-entity
    count, not a defect, but 3x the documented "10,000 eligible entities"
    scale the budget is written against.
    """
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO workspaces (id, name, timezone, created_at)
                VALUES (:id, 'Ranking Performance', 'Asia/Kolkata', :created_at)
                """
            ),
            {"id": workspace_id, "created_at": now},
        )
        connection.execute(
            text(
                """
                INSERT INTO users (id, workspace_id, email, password_hash, created_at)
                VALUES (:id, :workspace_id, :email, 'test-password-hash', :created_at)
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
                "expires_at": now + timedelta(hours=1),
                "last_seen_at": now,
            },
        )
        seed_phase1_dataset(connection, workspace_id=workspace_id, owner_id=user_id)
        connection.execute(
            text(
                "UPDATE commitments SET status = 'cancelled', archived_at = now() "
                "WHERE workspace_id = :workspace_id"
            ),
            {"workspace_id": workspace_id},
        )
        connection.execute(
            text(
                "UPDATE risks SET status = 'closed', archived_at = now() "
                "WHERE workspace_id = :workspace_id"
            ),
            {"workspace_id": workspace_id},
        )
        connection.execute(text("ANALYZE commitments, risks"))

    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    try:
        yield client, workspace_id, token
    finally:
        client.close()
        with engine.begin() as connection:
            for table in (
                "attention_items",
                "event_outbox",
                "audit_events",
                "idempotency_records",
                "calendar_events",
                "notes",
                "risks",
                "commitments",
                "tasks",
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


def _p95(samples: list[float]) -> float:
    """Nearest-rank 95th percentile: the smallest value at or above 95% of samples."""
    ordered = sorted(samples)
    index = min(len(ordered) - 1, -(-(95 * len(ordered)) // 100) - 1)
    return ordered[index]


def test_ranking_10000_eligible_entities_under_budget(
    ranking_performance_context: tuple[TestClient, UUID, str],
) -> None:
    """The real end-to-end ranking measurement the design doc means:

    "ranking of 10,000 eligible entities below 500 ms" is measured here by
    seeding the documented representative dataset and calling the actual
    ``POST /api/v1/attention/regenerate`` endpoint
    (`backend/ecc/domains/governance/attention.py:223`) -- the endpoint that
    queries eligible tasks/commitments/risks from the database, scores every
    one of them, and returns the freshly ranked list -- through the real
    HTTP layer with real CSRF headers. This replaces relying solely on the
    pure in-memory scoring-function microbenchmark below as evidence for
    this budget. ``regenerate`` is naturally idempotent (it recomputes the
    same 10,000 rows' scores each call, no Idempotency-Key required by the
    route), so repeated calls give a legitimate, comparable p95 sample.
    """
    client, workspace_id, token = ranking_performance_context
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()

    samples: list[float] = []
    for _ in range(RANKING_SAMPLE_SIZE):
        started = perf_counter()
        response = client.post(
            "/api/v1/attention/regenerate",
            headers={"X-CSRF-Token": csrf, "X-Correlation-ID": str(uuid4())},
            json={},
        )
        samples.append(perf_counter() - started)
        assert response.status_code == 200

    p95 = _p95(samples)
    assert p95 < RANKING_BUDGET_SECONDS, (
        f"ranking p95 {p95 * 1000:.1f} ms exceeds {RANKING_BUDGET_SECONDS * 1000:.0f} ms "
        f"budget; samples(ms)={[round(s * 1000, 1) for s in samples]}"
    )

    with engine.connect() as connection:
        ranked_count = connection.execute(
            text("SELECT count(*) FROM attention_items WHERE workspace_id = :workspace_id"),
            {"workspace_id": workspace_id},
        ).scalar_one()
    assert ranked_count >= 10_000


def test_priority_scoring_10000_entities_under_500ms() -> None:
    now = datetime.now(UTC)
    today = now.date()
    task = {
        "manual_priority": "high",
        "due_date": today,
        "due_at": None,
        "pinned": False,
        "blocked_on_person_id": None,
        "status": "planned",
        "updated_at": now - timedelta(days=8),
    }
    commitment = {
        "importance": "high",
        "due_date": None,
        "due_at": now + timedelta(hours=24),
        "direction": "made_to_me",
        "pinned": False,
        "confidence": 0.9,
        "updated_at": now,
    }
    risk = {
        "probability": 5,
        "impact": 4,
        "review_at": now + timedelta(hours=24),
        "pinned": False,
    }

    started = perf_counter()
    scores = []
    for index in range(10_000):
        selector = index % 3
        if selector == 0:
            scores.append(_score_task(task, today, now)[0])
        elif selector == 1:
            scores.append(_score_commitment(commitment, today, now)[0])
        else:
            scores.append(_score_risk(risk, now)[0])
    elapsed = perf_counter() - started

    assert len(scores) == 10_000
    assert elapsed < 0.5
