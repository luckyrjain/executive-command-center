import os

import pytest

os.environ.setdefault("ECC_SESSION_SECRET", "test-secret-value-that-is-long-enough")
os.environ.setdefault("ECC_DATABASE_URL", "sqlite+pysqlite:///:memory:")


@pytest.fixture(autouse=True)
def _reset_mutation_rate_limiters() -> None:
    """The mutation rate limiters in ecc.http_security are module-level
    singletons shared by the real `ecc.main.app`. Every test file that calls
    TestClient(app) directly (i.e. doesn't monkeypatch its own limiter
    instance) shares that global bucket state -- including the IP-keyed
    bucket, which is deliberately keyed the same way regardless of session,
    so it accumulates across every test in the process, not just tests using
    the same session. Clear both bucket tables before each test so one test
    file's mutation volume can never push another test toward a spurious 429.
    """
    from ecc.http_security import _mutation_ip_rate_limiter, _mutation_rate_limiter

    _mutation_rate_limiter._buckets.clear()
    _mutation_ip_rate_limiter._buckets.clear()


@pytest.fixture(autouse=True)
def _reset_outbox_backlog_cache() -> None:
    """ecc.observability._outbox_backlog_count caches its result for
    _OUTBOX_BACKLOG_CACHE_TTL_SECONDS behind a module-level singleton, shared
    by every test in the process. Without a reset, a test that calls
    render_metrics() shortly after another test's outbox-mutating test could
    see that other test's stale cached count instead of its own real DB
    state. Clear it before each test so every test starts with a real query.
    """
    import ecc.observability as observability

    observability._outbox_backlog_cache = None
