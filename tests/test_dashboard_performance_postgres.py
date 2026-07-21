"""Dashboard performance acceptance gate.

Proves the ``GET /api/v1/dashboard/today`` endpoint (the real dashboard
route, `backend/ecc/domains/platform/dashboard_briefs.py:577`) stays under
the documented p95 budget (below 2 seconds) at the documented Phase 1
representative scale: 10,000 tasks, commitments, risks, and calendar events;
50,000 notes; and 100,000 audit rows (see
``docs/superpowers/specs/2026-07-16-phase-1-completion-design.md:178`` and
``docs/phases/phase-001/TEST-PLAN.md:57``).

Also carries the dataset-shape tests for ``tests/phase1_dataset.py`` -- the
generator's exact documented counts are asserted here rather than in a
separate file, since this is the first file that depends on the shared
fixture.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from time import perf_counter
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from phase1_dataset import Phase1DatasetCounts, seed_phase1_dataset
from sqlalchemy import text

from ecc.config import get_settings
from ecc.database import engine
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

# Real documented budget: dashboard p95 below 2 seconds.
DASHBOARD_P95_BUDGET_SECONDS = 2.0
SAMPLE_SIZE = 15


def _p95(samples: list[float]) -> float:
    """Nearest-rank 95th percentile: the smallest value at or above 95% of samples."""
    ordered = sorted(samples)
    index = min(len(ordered) - 1, -(-(95 * len(ordered)) // 100) - 1)
    return ordered[index]


@pytest.fixture(scope="module")
def dashboard_performance_dataset() -> Iterator[tuple[TestClient, UUID]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO workspaces (id, name, timezone, created_at)
                VALUES (:id, 'Dashboard Performance', 'Asia/Kolkata', :created_at)
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

    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    try:
        yield client, workspace_id
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
                "notes",
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


def test_phase1_dataset_produces_documented_counts(
    dashboard_performance_dataset: tuple[TestClient, UUID],
) -> None:
    _, workspace_id = dashboard_performance_dataset
    expected = Phase1DatasetCounts()

    with engine.connect() as connection:
        actual = {
            table: connection.execute(
                text(f"SELECT count(*) FROM {table} WHERE workspace_id = :workspace_id"),
                {"workspace_id": workspace_id},
            ).scalar_one()
            for table in (
                "tasks",
                "commitments",
                "risks",
                "calendar_events",
                "notes",
                "audit_events",
            )
        }

    assert actual == {
        "tasks": expected.tasks,
        "commitments": expected.commitments,
        "risks": expected.risks,
        "calendar_events": expected.calendar_events,
        "notes": expected.notes,
        "audit_events": expected.audit_events,
    }


def test_dashboard_today_p95_under_budget(
    dashboard_performance_dataset: tuple[TestClient, UUID],
) -> None:
    client, _ = dashboard_performance_dataset

    samples: list[float] = []
    for _ in range(SAMPLE_SIZE):
        started = perf_counter()
        response = client.get("/api/v1/dashboard/today")
        samples.append(perf_counter() - started)
        assert response.status_code == 200

    p95 = _p95(samples)
    assert p95 < DASHBOARD_P95_BUDGET_SECONDS, (
        f"dashboard p95 {p95 * 1000:.1f} ms exceeds "
        f"{DASHBOARD_P95_BUDGET_SECONDS * 1000:.0f} ms budget; samples(ms)="
        f"{[round(s * 1000, 1) for s in samples]}"
    )
