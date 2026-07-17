"""Mutation, brief-generation, and statement-timeout performance gates.

Proves, against the documented Phase 1 representative scale (10,000 tasks,
commitments, risks, and calendar events; 50,000 notes; 100,000 audit rows):

* task and commitment mutation p95 below 300 ms, measured through the real
  ``PATCH /api/v1/tasks/{id}`` and ``PATCH /api/v1/commitments/{id}``
  endpoints (`backend/ecc/domains/planning/tasks.py:583`,
  `backend/ecc/domains/communication/commitments.py:605`) with real CSRF and
  per-attempt idempotency-key headers -- not bypassed for convenience;
* deterministic brief generation p95 below 2 seconds, measured through the
  real ``POST /api/v1/briefs/morning`` endpoint
  (`backend/ecc/domains/platform/dashboard_briefs.py:645`);
* no query above the approved 5-second statement timeout configured in
  `backend/ecc/database.py`.

See ``docs/superpowers/specs/2026-07-16-phase-1-completion-design.md:178``
and ``docs/phases/phase-001/TEST-PLAN.md:57`` for the exact budgets.
"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from time import perf_counter
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from phase1_dataset import seed_phase1_dataset
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from ecc.config import get_settings
from ecc.database import STATEMENT_TIMEOUT_MS, engine
from ecc.main import app

settings = get_settings()
pytestmark = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)

# Real documented budgets.
MUTATION_P95_BUDGET_SECONDS = 0.3
BRIEF_P95_BUDGET_SECONDS = 2.0
SAMPLE_SIZE = 15


def _p95(samples: list[float]) -> float:
    """Nearest-rank 95th percentile: the smallest value at or above 95% of samples."""
    ordered = sorted(samples)
    index = min(len(ordered) - 1, -(-(95 * len(ordered)) // 100) - 1)
    return ordered[index]


def _headers(token: str, key: str) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    return {
        "X-CSRF-Token": csrf,
        "X-Correlation-ID": str(uuid4()),
        "Idempotency-Key": key,
    }


def _mint_session(workspace_id: UUID, user_id: UUID) -> str:
    """Create a fresh session row and return its bearer token.

    Each performance test below mints its own session rather than sharing
    one across the module. `_mutation_rate_limiter` in
    `backend/ecc/http_security.py` is a real, process-lifetime, per-session
    fixed-window limiter (40 mutation-class requests per 60 seconds) --
    genuine production abuse protection that this task must not bypass. A
    single shared session across three ~15-request mutation-class test
    functions in the same file would trip that limiter (45 > 40) purely as
    a test-isolation artifact, not a real regression. Minting a session per
    test gives each its own rate-limit bucket, matching how distinct real
    users would never share one.
    """
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)
    with engine.begin() as connection:
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
    return token


@pytest.fixture(scope="module")
def mutation_brief_dataset() -> Iterator[tuple[UUID, UUID]]:
    workspace_id = uuid4()
    user_id = uuid4()
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO workspaces (id, name, timezone, created_at)
                VALUES (:id, 'Mutation Brief Performance', 'Asia/Kolkata', :created_at)
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
        seed_phase1_dataset(connection, workspace_id=workspace_id, owner_id=user_id)

    try:
        yield workspace_id, user_id
    finally:
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


def test_task_mutation_p95_under_budget(
    mutation_brief_dataset: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = mutation_brief_dataset
    token = _mint_session(workspace_id, user_id)
    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    now = datetime.now(UTC)
    task_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO tasks (
                    id, workspace_id, owner_id, title, status, manual_priority,
                    pinned, source_type, created_by, updated_by,
                    created_at, updated_at, version
                ) VALUES (
                    :id, :workspace_id, :owner_id, 'Mutation perf task', 'planned', 'medium',
                    false, 'local', :actor, :actor, :now, :now, 1
                )
                """
            ),
            {
                "id": task_id,
                "workspace_id": workspace_id,
                "owner_id": user_id,
                "actor": user_id,
                "now": now,
            },
        )

    samples: list[float] = []
    version = 1
    for index in range(SAMPLE_SIZE):
        started = perf_counter()
        response = client.patch(
            f"/api/v1/tasks/{task_id}",
            headers=_headers(token, f"task-mutation-perf-{index}-{uuid4()}"),
            json={"expected_version": version, "title": f"Mutation perf task {index}"},
        )
        samples.append(perf_counter() - started)
        assert response.status_code == 200, response.text
        version = response.json()["version"]

    p95 = _p95(samples)
    assert p95 < MUTATION_P95_BUDGET_SECONDS, (
        f"task mutation p95 {p95 * 1000:.1f} ms exceeds "
        f"{MUTATION_P95_BUDGET_SECONDS * 1000:.0f} ms budget; samples(ms)="
        f"{[round(s * 1000, 1) for s in samples]}"
    )
    client.close()


def test_commitment_mutation_p95_under_budget(
    mutation_brief_dataset: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = mutation_brief_dataset
    token = _mint_session(workspace_id, user_id)
    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    now = datetime.now(UTC)
    commitment_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO commitments (
                    id, workspace_id, owner_id, summary, direction, status,
                    importance, confidence, pinned, created_by, updated_by,
                    created_at, updated_at, version
                ) VALUES (
                    :id, :workspace_id, :owner_id, 'Mutation perf commitment',
                    'made_to_me', 'active', 'medium', 0.5, false,
                    :actor, :actor, :now, :now, 1
                )
                """
            ),
            {
                "id": commitment_id,
                "workspace_id": workspace_id,
                "owner_id": user_id,
                "actor": user_id,
                "now": now,
            },
        )

    samples: list[float] = []
    version = 1
    for index in range(SAMPLE_SIZE):
        started = perf_counter()
        response = client.patch(
            f"/api/v1/commitments/{commitment_id}",
            headers=_headers(token, f"commitment-mutation-perf-{index}-{uuid4()}"),
            json={"expected_version": version, "summary": f"Mutation perf commitment {index}"},
        )
        samples.append(perf_counter() - started)
        assert response.status_code == 200, response.text
        version = response.json()["version"]

    p95 = _p95(samples)
    assert p95 < MUTATION_P95_BUDGET_SECONDS, (
        f"commitment mutation p95 {p95 * 1000:.1f} ms exceeds "
        f"{MUTATION_P95_BUDGET_SECONDS * 1000:.0f} ms budget; samples(ms)="
        f"{[round(s * 1000, 1) for s in samples]}"
    )
    client.close()


def test_brief_generation_p95_under_budget(
    mutation_brief_dataset: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = mutation_brief_dataset
    token = _mint_session(workspace_id, user_id)
    client = TestClient(app)
    client.cookies.set("ecc_session", token)

    samples: list[float] = []
    for index in range(SAMPLE_SIZE):
        started = perf_counter()
        response = client.post(
            "/api/v1/briefs/morning",
            headers=_headers(token, f"brief-perf-{index}-{uuid4()}"),
            json={},
        )
        samples.append(perf_counter() - started)
        assert response.status_code == 200, response.text

    p95 = _p95(samples)
    assert p95 < BRIEF_P95_BUDGET_SECONDS, (
        f"brief generation p95 {p95 * 1000:.1f} ms exceeds "
        f"{BRIEF_P95_BUDGET_SECONDS * 1000:.0f} ms budget; samples(ms)="
        f"{[round(s * 1000, 1) for s in samples]}"
    )
    client.close()


def test_statement_timeout_is_configured_at_approved_value() -> None:
    with engine.connect() as connection:
        raw = connection.execute(text("SHOW statement_timeout")).scalar_one()
    # PostgreSQL normalizes a millisecond GUC value to the largest whole unit
    # it divides evenly into, so 5000ms round-trips through SHOW as "5s".
    assert raw == "5s", raw


def test_statement_timeout_cancels_a_query_that_exceeds_the_budget() -> None:
    """A genuinely slow query is actually cancelled by the server, not just configured.

    This is the difference between "statement_timeout is set" and
    "statement_timeout is enforced": `pg_sleep` beyond the configured budget
    must raise, proving the setting actually applies to the connection
    executing it (not merely readable via SHOW on a separate session).
    """
    sleep_seconds = (STATEMENT_TIMEOUT_MS / 1000) + 1
    with pytest.raises(DBAPIError, match="statement timeout"):
        with engine.connect() as connection:
            connection.execute(text("SELECT pg_sleep(:seconds)"), {"seconds": sleep_seconds})
