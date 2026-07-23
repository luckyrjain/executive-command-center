from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from hmac import new
from time import perf_counter
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

# TEST-PLAN.md's Performance section names candidate generation as something
# that must be "measured at acceptance dataset size" with p50/p95/p99
# recorded for regressions, but -- unlike entity lookup, lexical/hybrid
# retrieval and the 10,000-entry timeline -- the phase spec never quantifies
# a numeric budget for it. This test measures and records the real
# percentiles as the contract asks, but asserts only a generous sanity
# ceiling rather than inventing an official contractual number the doc never
# committed to; if this ever fires it means a genuine multi-second
# regression, not a missed-by-a-few-ms budget.
SANITY_CEILING_SECONDS = 2.0
_BACKGROUND_ENTITY_COUNT = 10_000
SAMPLE_SIZE = 20


def _p50(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[len(ordered) // 2]


def _p95(samples: list[float]) -> float:
    """Nearest-rank 95th percentile: the smallest value at or above 95% of samples."""
    ordered = sorted(samples)
    index = min(len(ordered) - 1, -(-(95 * len(ordered)) // 100) - 1)
    return ordered[index]


def _p99(samples: list[float]) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, -(-(99 * len(ordered)) // 100) - 1)
    return ordered[index]


def _headers(token: str, key: str) -> dict[str, str]:
    csrf = new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    return {
        "Idempotency-Key": key,
        "X-CSRF-Token": csrf,
        "X-Correlation-ID": str(uuid4()),
    }


@pytest.fixture
def candidate_generation_performance_context() -> Iterator[tuple[TestClient, UUID, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    token = f"session-{uuid4()}"
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO workspaces (id, name, created_at) VALUES (:id, :name, :created_at)"),
            {"id": workspace_id, "name": "Candidate Generation Performance", "created_at": now},
        )
        connection.execute(
            text(
                "INSERT INTO users (id, workspace_id, email, password_hash, created_at) "
                "VALUES (:id, :workspace_id, :email, :password_hash, :created_at)"
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
                "INSERT INTO sessions (id, workspace_id, user_id, token_hash, "
                "expires_at, last_seen_at) VALUES (:id, :workspace_id, :user_id, "
                ":token_hash, :expires_at, :last_seen_at)"
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
        # Background entities representing acceptance dataset scale --
        # candidate generation happens in a workspace already populated at
        # real volume, not an empty one.
        connection.execute(
            text(
                """
                INSERT INTO pkos_nodes (
                    id, workspace_id, node_type, canonical_name, attributes,
                    status, confidence, version, created_at, updated_at
                )
                SELECT
                    gen_random_uuid(), :workspace_id, 'person',
                    'Representative synthetic person ' || series::text,
                    '{}'::jsonb, 'active', 1.00, 1, :now, :now
                FROM generate_series(1, :count) AS series
                """
            ),
            {"workspace_id": workspace_id, "now": now, "count": _BACKGROUND_ENTITY_COUNT},
        )
        # SAMPLE_SIZE + 1 (warm-up) distinct near-duplicate-name pairs,
        # pre-created outside the timed loop -- each pair must be genuinely
        # new so every timed call runs the real fuzzy-scoring path
        # (score_candidate) rather than _existing_candidate's cached-pair
        # short-circuit or _deterministic_alias_match's exact-name
        # fast-path.
        pairs: list[tuple[UUID, UUID]] = []
        for i in range(SAMPLE_SIZE + 1):
            left_id, right_id = uuid4(), uuid4()
            connection.execute(
                text(
                    """
                    INSERT INTO pkos_nodes (
                        id, workspace_id, node_type, canonical_name, attributes,
                        status, confidence, version, created_at, updated_at
                    ) VALUES
                        (:left_id, :workspace_id, 'person', :left_name,
                         '{}'::jsonb, 'active', 1.00, 1, :now, :now),
                        (:right_id, :workspace_id, 'person', :right_name,
                         '{}'::jsonb, 'active', 1.00, 1, :now, :now)
                    """
                ),
                {
                    "left_id": left_id,
                    "right_id": right_id,
                    "workspace_id": workspace_id,
                    "left_name": f"Ada Lovelace {i}",
                    "right_name": f"Ada Lovelase {i}",
                    "now": now,
                },
            )
            pairs.append((left_id, right_id))

    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text("VACUUM (ANALYZE) pkos_nodes"))

    client = TestClient(app)
    client.cookies.set("ecc_session", token)
    try:
        yield client, workspace_id, token, pairs
    finally:
        client.close()
        with engine.begin() as connection:
            # See test_knowledge_retrieval_performance_postgres.py's
            # teardown for why this workspace's 10,000-row DELETE needs a
            # relaxed statement_timeout beyond the 5s application-request
            # budget.
            connection.execute(text("SET LOCAL statement_timeout = '60s'"))
            for table in (
                "event_outbox",
                "audit_events",
                "idempotency_records",
                "resolution_candidates",
                "pkos_nodes",
                "sessions",
                "users",
            ):
                connection.execute(
                    text(f"DELETE FROM {table} WHERE workspace_id = :workspace_id"),  # noqa: S608
                    {"workspace_id": workspace_id},
                )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": workspace_id},
            )


def test_candidate_generation_10000_entity_workspace_records_percentiles(
    candidate_generation_performance_context: tuple[TestClient, UUID, str, list[tuple[UUID, UUID]]],
) -> None:
    client, _workspace_id, token, pairs = candidate_generation_performance_context

    warmup_left, warmup_right = pairs[0]
    warmup = client.post(
        "/api/v1/knowledge/resolution/candidates",
        headers=_headers(token, "candidate-perf-warmup"),
        json={"left_entity_id": str(warmup_left), "right_entity_id": str(warmup_right)},
    )
    assert warmup.status_code == 201

    samples: list[float] = []
    for index, (left_id, right_id) in enumerate(pairs[1:]):
        started = perf_counter()
        response = client.post(
            "/api/v1/knowledge/resolution/candidates",
            headers=_headers(token, f"candidate-perf-{index}"),
            json={"left_entity_id": str(left_id), "right_entity_id": str(right_id)},
        )
        samples.append(perf_counter() - started)
        assert response.status_code == 201

    p50, p95, p99 = _p50(samples), _p95(samples), _p99(samples)
    print(
        f"\n[candidate generation] p50={p50 * 1000:.1f}ms p95={p95 * 1000:.1f}ms "
        f"p99={p99 * 1000:.1f}ms samples(ms)={[round(s * 1000, 1) for s in samples]}"
    )
    assert p99 < SANITY_CEILING_SECONDS, (
        f"candidate generation p99 {p99 * 1000:.1f} ms exceeded the "
        f"{SANITY_CEILING_SECONDS * 1000:.0f} ms sanity ceiling; this indicates a genuine "
        f"regression, not a documented-budget miss (TEST-PLAN.md names this as measured "
        f"but never sets a numeric target)"
    )
