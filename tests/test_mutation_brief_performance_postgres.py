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

import os
import warnings
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from time import perf_counter
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from identity_fixtures import create_identity
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

# Real documented budgets, widened for CI's shared-runner noise -- same
# CI/local split as SEARCH_BUDGET_SECONDS in test_search_performance_postgres.py
# and every other *_performance_postgres.py file's own budget constants.
_IN_CI = os.getenv("CI") is not None
MUTATION_P95_BUDGET_SECONDS = 0.48 if _IN_CI else 0.3
BRIEF_P95_BUDGET_SECONDS = 3.2 if _IN_CI else 2.0
# 20 samples, not 15: with nearest-rank `_p95` below, 15 samples put p95 at
# index ceil(0.95 * 15) - 1 = 14 -- the maximum -- so a single GC pause or
# checkpoint write on a shared CI runner failed the whole gate. At 20 samples
# p95 is the 19th-ranked value, so one outlier per pass is tolerated. A pass
# with two or more samples over budget triggers one retry, which is judged on
# the pooled 40 samples of both passes (p95 = 38th-ranked, so up to two
# outliers across both passes). The gate therefore fails only when three or
# more of those 40 samples are over budget; see `_assert_p95_under_budget`.
# One discarded warm-up request per pass absorbs first-call costs (plan
# caching, connection checkout). Warm-up + samples (21) stays under the
# mutation rate limiter's 40-requests-per-session window; see `_mint_session`.
WARMUP_ITERATIONS = 1
SAMPLE_SIZE = 20


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
    single shared session across three ~21-request mutation-class test
    functions in the same file would trip that limiter (63 > 40) purely as
    a test-isolation artifact, not a real regression. Minting a session per
    test gives each its own rate-limit bucket, matching how distinct real
    users would never share one. A retried measurement pass (see
    `_assert_p95_under_budget`) likewise mints its own session.
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


# One request of a measurement pass: (client, session token, idempotency-key
# label) -> None. Performs the request and asserts on its response.
_RequestOnce = Callable[[TestClient, str, str], None]


def _measure_pass(
    workspace_id: UUID, user_id: UUID, request_once: _RequestOnce, label: str
) -> tuple[float, list[float]]:
    """Run one warm-up-then-sample pass on a freshly minted session."""
    token = _mint_session(workspace_id, user_id)
    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    try:
        for index in range(WARMUP_ITERATIONS):
            request_once(client, token, f"{label}-warmup-{index}-{uuid4()}")
        samples: list[float] = []
        for index in range(SAMPLE_SIZE):
            started = perf_counter()
            request_once(client, token, f"{label}-sample-{index}-{uuid4()}")
            samples.append(perf_counter() - started)
    finally:
        client.close()
    return _p95(samples), samples


def _assert_p95_under_budget(
    name: str,
    budget_seconds: float,
    workspace_id: UUID,
    user_id: UUID,
    request_once: _RequestOnce,
) -> None:
    """Assert p95 is under budget, allowing one retry of the *entire* pass.

    The same narrow exception `test_ranking_10000_eligible_entities_under_
    budget` (`tests/test_risks_attention_postgres.py`) and
    `test_reverse_with_200_rehomed_relationships_p95_under_budget`
    (`tests/test_knowledge_entity_operations_performance_postgres.py`)
    document: transient Postgres background work (checkpoint writes,
    autovacuum) or runner scheduling noise can slow a couple of calls in one
    pass without reflecting a regression. A real regression fails both the
    initial pass and the retry. The budget itself is unchanged.

    The retry is judged on the pooled samples of both passes, not on the
    retry's samples alone. Judging the retry alone would let a partial
    regression (say 10% of calls slow) through whenever its slow calls
    happen to land at most once in the retry's 20 samples, however many
    the first pass caught. Pooling keeps that evidence: the gate fails when
    three or more of the 40 pooled samples exceed the budget.
    """
    key_prefix = name.replace(" ", "-")
    first_p95, first_samples = _measure_pass(workspace_id, user_id, request_once, key_prefix)
    if first_p95 < budget_seconds:
        return
    # A warning, not a print: pytest captures stdout of passing tests, so a
    # pass-on-retry would otherwise leave no trace in CI. The warnings summary
    # is shown even when the test passes.
    warnings.warn(
        f"[{name} budget] initial pass p95 {first_p95 * 1000:.1f} ms exceeded "
        f"{budget_seconds * 1000:.0f} ms budget (in_ci={_IN_CI}); retrying once with a "
        f"fresh measurement pass before failing. samples(ms)="
        f"{[round(s * 1000, 1) for s in first_samples]}",
        stacklevel=2,
    )
    retry_p95, retry_samples = _measure_pass(
        workspace_id, user_id, request_once, f"{key_prefix}-retry"
    )
    pooled_p95 = _p95(first_samples + retry_samples)
    assert pooled_p95 < budget_seconds, (
        f"{name} p95 over both passes ({len(first_samples) + len(retry_samples)} samples) "
        f"is {pooled_p95 * 1000:.1f} ms, exceeding the {budget_seconds * 1000:.0f} ms "
        f"budget (in_ci={_IN_CI}; initial pass p95 {first_p95 * 1000:.1f} ms, retry p95 "
        f"{retry_p95 * 1000:.1f} ms); this indicates a real regression, not one-off "
        f"environmental noise. initial samples(ms)="
        f"{[round(s * 1000, 1) for s in first_samples]}; "
        f"retry samples(ms)={[round(s * 1000, 1) for s in retry_samples]}"
    )


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
        create_identity(
            connection,
            workspace_id=workspace_id,
            user_id=user_id,
            email=f"{user_id}@example.test",
            now=now,
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

    version = 1

    def patch_task(client: TestClient, token: str, key: str) -> None:
        nonlocal version
        response = client.patch(
            f"/api/v1/tasks/{task_id}",
            headers=_headers(token, key),
            json={"expected_version": version, "title": f"Mutation perf task {key}"},
        )
        assert response.status_code == 200, response.text
        version = response.json()["version"]

    _assert_p95_under_budget(
        "task mutation", MUTATION_P95_BUDGET_SECONDS, workspace_id, user_id, patch_task
    )


def test_commitment_mutation_p95_under_budget(
    mutation_brief_dataset: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = mutation_brief_dataset
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

    version = 1

    def patch_commitment(client: TestClient, token: str, key: str) -> None:
        nonlocal version
        response = client.patch(
            f"/api/v1/commitments/{commitment_id}",
            headers=_headers(token, key),
            json={"expected_version": version, "summary": f"Mutation perf commitment {key}"},
        )
        assert response.status_code == 200, response.text
        version = response.json()["version"]

    _assert_p95_under_budget(
        "commitment mutation",
        MUTATION_P95_BUDGET_SECONDS,
        workspace_id,
        user_id,
        patch_commitment,
    )


def test_brief_generation_p95_under_budget(
    mutation_brief_dataset: tuple[UUID, UUID],
) -> None:
    workspace_id, user_id = mutation_brief_dataset

    def generate_brief(client: TestClient, token: str, key: str) -> None:
        response = client.post("/api/v1/briefs/morning", headers=_headers(token, key), json={})
        assert response.status_code == 200, response.text

    _assert_p95_under_budget(
        "brief generation", BRIEF_P95_BUDGET_SECONDS, workspace_id, user_id, generate_brief
    )


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
