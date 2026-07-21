"""Production-hardening tests: settings validation and HTTP-layer protections.

Two independent surfaces are covered here:

1. ``validate_production_settings`` (backend/ecc/config.py) -- a pure function
   that fails startup fast when ``Settings`` would be unsafe outside local
   development. Exercised directly against constructed ``Settings`` instances,
   no app/HTTP involved.
2. The ASGI middleware in ``backend/ecc/http_security.py`` -- security
   headers, request body size limiting, and bounded mutation rate limiting.
   Exercised against a minimal standalone FastAPI app (not the real
   ``ecc.main`` app) so these tests stay independent of domain/DB concerns;
   plus a couple of tests against the real app to prove the dev-bootstrap
   router is not registered outside development.
"""

from __future__ import annotations

import importlib
import os
import secrets
import shutil
import socket
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import httpx
import pytest
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from ecc.config import ConfigurationError, Settings, validate_production_settings
from ecc.http_security import (
    MAX_REQUEST_BODY_BYTES,
    MaxBodySizeMiddleware,
    mutation_rate_limit_middleware,
    security_headers_middleware,
)

# Generated at import time rather than a literal string constant: it's just a
# stand-in for "a real, non-placeholder secret" in these tests, but a
# hardcoded literal here trips static-analysis "hardcoded secret" scanners
# (CWE-547) even though nothing here is an actual credential.
VALID_PROD_SECRET = secrets.token_urlsafe(40)


def _settings(**overrides: object) -> Settings:
    """Build a ``Settings`` instance for a given field state directly.

    Uses ``model_construct`` (bypasses field validators and the env/dotenv
    sources entirely) rather than the normal constructor. Two reasons:

    1. ``Settings`` fields use ``validation_alias`` (e.g. ``ECC_ENV``), so
       calling ``Settings(environment=...)`` by field name silently falls
       through to ambient env vars / the repo's real .env file instead of
       raising or applying the override -- a test-only footgun this avoids.
    2. Pydantic v2 validates *explicitly provided* values against field
       constraints (unlike unset defaults), so a normal constructor call
       cannot even reach the "short/placeholder secret slipped past
       validation" scenario this module exists to guard against --
       ``Settings(session_secret="short")`` would itself raise a
       ``pydantic.ValidationError`` before ``validate_production_settings``
       ever runs. ``model_construct`` reproduces the real-world gap: a
       field value that arrived without going through validation at all
       (today, that's the empty-string default; ``model_construct`` lets
       these tests exercise the same code path for any value).
    """
    base: dict[str, object] = {
        "environment": "production",
        "database_url": "postgresql+psycopg://ecc:ecc@localhost:5432/ecc",
        "session_secret": VALID_PROD_SECRET,
        "cors_origins": "https://app.example.com",
    }
    base.update(overrides)
    return Settings.model_construct(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# validate_production_settings: placeholder/short secrets
# ---------------------------------------------------------------------------


def test_rejects_short_session_secret_in_production() -> None:
    settings = _settings(session_secret="short-secret")

    with pytest.raises(ConfigurationError):
        validate_production_settings(settings)


def test_rejects_empty_session_secret_in_production() -> None:
    settings = _settings(session_secret="")

    with pytest.raises(ConfigurationError):
        validate_production_settings(settings)


def test_rejects_known_placeholder_session_secret_in_production() -> None:
    settings = _settings(session_secret="development-only-secret-change-before-real-data")

    with pytest.raises(ConfigurationError):
        validate_production_settings(settings)


def test_rejects_env_example_placeholder_secret_in_production() -> None:
    settings = _settings(session_secret="replace-with-a-random-secret-at-least-32-characters-long")

    with pytest.raises(ConfigurationError):
        validate_production_settings(settings)


# ---------------------------------------------------------------------------
# validate_production_settings: permissive CORS origins
# ---------------------------------------------------------------------------


def test_rejects_wildcard_cors_origin_in_production() -> None:
    settings = _settings(cors_origins="*")

    with pytest.raises(ConfigurationError):
        validate_production_settings(settings)


def test_rejects_http_scheme_cors_origin_in_production() -> None:
    settings = _settings(cors_origins="http://app.example.com")

    with pytest.raises(ConfigurationError):
        validate_production_settings(settings)


def test_rejects_empty_cors_origins_in_production() -> None:
    settings = _settings(cors_origins="")

    with pytest.raises(ConfigurationError):
        validate_production_settings(settings)


def test_rejects_mixed_wildcard_and_valid_cors_origins_in_production() -> None:
    settings = _settings(cors_origins="https://app.example.com,*")

    with pytest.raises(ConfigurationError):
        validate_production_settings(settings)


# ---------------------------------------------------------------------------
# validate_production_settings: missing/unrecognized environment classification
# ---------------------------------------------------------------------------


def test_rejects_empty_environment_classification() -> None:
    settings = _settings(environment="")

    with pytest.raises(ConfigurationError):
        validate_production_settings(settings)


def test_rejects_unrecognized_environment_classification() -> None:
    settings = _settings(environment="prod")  # typo of "production"

    with pytest.raises(ConfigurationError):
        validate_production_settings(settings)


# ---------------------------------------------------------------------------
# validate_production_settings: permissive development defaults preserved
# ---------------------------------------------------------------------------


def test_allows_todays_development_defaults() -> None:
    settings = Settings.model_construct(
        environment="development",
        database_url="postgresql+psycopg://ecc:ecc@localhost:5432/ecc",
        session_secret="",
        cors_origins="http://localhost:5173",
    )

    validate_production_settings(settings)


def test_allows_valid_production_settings() -> None:
    settings = _settings()

    validate_production_settings(settings)


def test_allows_staging_with_valid_settings() -> None:
    settings = _settings(environment="staging")

    validate_production_settings(settings)


def test_rejects_short_secret_in_staging_too() -> None:
    settings = _settings(environment="staging", session_secret="short")

    with pytest.raises(ConfigurationError):
        validate_production_settings(settings)


# ---------------------------------------------------------------------------
# Development bootstrap reachability / insecure production cookies:
# the dev-bootstrap router must not be registered at all outside development,
# so its cookie-issuing routes are not merely 404'd at runtime but entirely
# absent from the route table. See backend/ecc/main.py and the module reload
# helper below.
# ---------------------------------------------------------------------------


def _reload_main(monkeypatch: pytest.MonkeyPatch, environment: str | None) -> ModuleType:
    import ecc.config as config_module
    import ecc.main as main_module

    if environment is None:
        monkeypatch.delenv("ECC_ENV", raising=False)
    else:
        monkeypatch.setenv("ECC_ENV", environment)
    monkeypatch.setenv("ECC_SESSION_SECRET", VALID_PROD_SECRET)
    monkeypatch.setenv("ECC_CORS_ORIGINS", "https://app.example.com")
    config_module.get_settings.cache_clear()
    return importlib.reload(main_module)


@pytest.fixture
def restore_main_module() -> Iterator[None]:
    # Snapshot the actual pre-test environment (including "unset") so
    # teardown can restore exactly what was there before -- not a hardcoded
    # literal. In CI, ECC_SESSION_SECRET is a real environment variable set
    # before pytest even starts (see .github/workflows/ci.yml), so
    # overwriting it with tests/conftest.py's dev-default literal instead of
    # restoring it would silently mutate the process env for every test that
    # runs afterward.
    restore_vars = ("ECC_ENV", "ECC_CORS_ORIGINS", "ECC_SESSION_SECRET")
    prior_values = {name: os.environ.get(name) for name in restore_vars}

    yield

    # Reload back to whatever settings were actually in effect before this
    # test ran, so later-imported test modules (already collected with
    # `from ecc.main import app`, so unaffected either way, but future
    # fixtures/tests running in-process still see a consistent app) observe
    # the real prior state rather than a guessed default.
    import ecc.config as config_module
    import ecc.main as main_module

    for name, value in prior_values.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    config_module.get_settings.cache_clear()
    importlib.reload(main_module)


def _registered_router_prefixes(app: FastAPI) -> set[str]:
    prefixes: set[str] = set()
    for route in app.routes:
        original_router = getattr(route, "original_router", None)
        prefix = getattr(original_router, "prefix", None)
        if prefix:
            prefixes.add(prefix)
    return prefixes


def test_dev_bootstrap_router_not_registered_in_production(
    monkeypatch: pytest.MonkeyPatch,
    restore_main_module: None,
) -> None:
    reloaded = _reload_main(monkeypatch, "production")

    prefixes = _registered_router_prefixes(reloaded.app)

    assert "/dev/bootstrap" not in prefixes


def test_dev_bootstrap_malformed_request_is_generic_404_in_production(
    monkeypatch: pytest.MonkeyPatch,
    restore_main_module: None,
) -> None:
    reloaded = _reload_main(monkeypatch, "production")
    client = TestClient(reloaded.app)

    # A malformed body would normally 422 (Pydantic validation, which runs
    # before the endpoint's own _require_development() check) if the router
    # were registered -- that 422-vs-404 distinction is exactly the
    # discoverability leak this closes.
    response = client.post("/dev/bootstrap/session", json={"not_code": "x"})

    assert response.status_code == 404


def test_dev_bootstrap_router_still_registered_in_development(
    monkeypatch: pytest.MonkeyPatch,
    restore_main_module: None,
) -> None:
    reloaded = _reload_main(monkeypatch, "development")

    prefixes = _registered_router_prefixes(reloaded.app)

    assert "/dev/bootstrap" in prefixes


# ---------------------------------------------------------------------------
# HTTP middleware: security headers
# ---------------------------------------------------------------------------


def _build_test_app(
    *,
    max_body_bytes: int = MAX_REQUEST_BODY_BYTES,
    include_cors: bool = False,
    cors_origins: list[str] | None = None,
) -> FastAPI:
    app = FastAPI()
    app.add_middleware(MaxBodySizeMiddleware, max_bytes=max_body_bytes)
    app.middleware("http")(mutation_rate_limit_middleware)
    app.middleware("http")(security_headers_middleware)
    if include_cors:
        # Registered *last* -- mirroring the corrected order in ecc.main --
        # so CORSMiddleware is the outermost layer and wraps every response,
        # including the ones MaxBodySizeMiddleware/mutation_rate_limit_middleware
        # short-circuit before ever calling call_next()/self._app(). See the
        # "CORS composition" tests below.
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins or ["https://frontend.example.com"],
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=[
                "Content-Type",
                "X-CSRF-Token",
                "X-Correlation-ID",
                "Idempotency-Key",
            ],
        )

    @app.post("/api/v1/widgets")
    def create_widget(payload: dict) -> dict:
        return {"ok": True, "received": payload}

    @app.get("/api/v1/widgets")
    def list_widgets() -> dict:
        return {"items": []}

    @app.get("/health/live")
    def live() -> dict:
        return {"status": "ok"}

    return app


def test_required_security_headers_present_on_normal_response() -> None:
    client = TestClient(_build_test_app())

    response = client.get("/api/v1/widgets")

    assert response.status_code == 200
    assert response.headers["X-Content-Type-Options"] == "nosniff"
    assert response.headers["X-Frame-Options"] == "DENY"
    assert response.headers["Referrer-Policy"] == "no-referrer"
    assert "default-src 'none'" in response.headers["Content-Security-Policy"]
    assert "max-age=" in response.headers["Strict-Transport-Security"]


def test_security_headers_present_on_health_check() -> None:
    client = TestClient(_build_test_app())

    response = client.get("/health/live")

    assert response.status_code == 200
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_security_headers_do_not_clobber_route_specific_headers() -> None:
    app = FastAPI()
    app.middleware("http")(security_headers_middleware)

    @app.get("/custom")
    def custom() -> Response:
        return Response(
            content="ok",
            headers={"Content-Security-Policy": "default-src 'self'"},
        )

    client = TestClient(app)
    response = client.get("/custom")

    assert response.headers["Content-Security-Policy"] == "default-src 'self'"


# ---------------------------------------------------------------------------
# HTTP middleware: oversized request body -> 413
# ---------------------------------------------------------------------------


def test_oversized_request_body_is_rejected_with_413() -> None:
    client = TestClient(_build_test_app(max_body_bytes=16))

    response = client.post("/api/v1/widgets", json={"padding": "x" * 200})

    assert response.status_code == 413


def test_body_within_limit_is_accepted() -> None:
    client = TestClient(_build_test_app(max_body_bytes=16))

    response = client.post("/api/v1/widgets", json={})

    assert response.status_code == 200


def test_health_check_unaffected_by_body_size_limit() -> None:
    client = TestClient(_build_test_app(max_body_bytes=1))

    response = client.get("/health/live")

    assert response.status_code == 200


# ---------------------------------------------------------------------------
# HTTP middleware: bounded mutation rate limiting -> 429 + Retry-After
# ---------------------------------------------------------------------------


def test_mutation_rate_limit_returns_429_with_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ecc.http_security as http_security

    limiter = http_security._MutationRateLimiter(window_seconds=60.0, max_requests=2)
    monkeypatch.setattr(http_security, "_mutation_rate_limiter", limiter)
    app = FastAPI()
    app.middleware("http")(http_security.mutation_rate_limit_middleware)

    @app.post("/api/v1/widgets")
    def create_widget() -> dict:
        return {"ok": True}

    client = TestClient(app)
    client.cookies.set("ecc_session", "same-session-token")

    first = client.post("/api/v1/widgets")
    second = client.post("/api/v1/widgets")
    third = client.post("/api/v1/widgets")

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 429
    assert int(third.headers["Retry-After"]) >= 1


def test_mutation_rate_limit_keys_by_session_not_shared_globally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ecc.http_security as http_security

    limiter = http_security._MutationRateLimiter(window_seconds=60.0, max_requests=1)
    monkeypatch.setattr(http_security, "_mutation_rate_limiter", limiter)
    app = FastAPI()
    app.middleware("http")(http_security.mutation_rate_limit_middleware)

    @app.post("/api/v1/widgets")
    def create_widget() -> dict:
        return {"ok": True}

    client = TestClient(app)

    client.cookies.set("ecc_session", "session-a")
    first = client.post("/api/v1/widgets")
    client.cookies.set("ecc_session", "session-b")
    second = client.post("/api/v1/widgets")

    assert first.status_code == 200
    assert second.status_code == 200


def test_mutation_rate_limit_not_bypassable_by_rotating_cookie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A client cannot evade the limit by sending a fresh, unvalidated
    session cookie value on every request -- the bucket key must always
    incorporate client IP, since the cookie hasn't been authenticated at
    this point in the middleware chain."""
    import ecc.http_security as http_security

    # Session bucket generous (isolates this test from the per-session
    # ceiling); IP bucket tight -- this is the one that must catch the
    # bypass since each forged cookie value gets its own session bucket.
    monkeypatch.setattr(
        http_security,
        "_mutation_rate_limiter",
        http_security._MutationRateLimiter(window_seconds=60.0, max_requests=100),
    )
    monkeypatch.setattr(
        http_security,
        "_mutation_ip_rate_limiter",
        http_security._MutationRateLimiter(window_seconds=60.0, max_requests=2),
    )
    app = FastAPI()
    app.middleware("http")(http_security.mutation_rate_limit_middleware)

    @app.post("/api/v1/widgets")
    def create_widget() -> dict:
        return {"ok": True}

    client = TestClient(app)

    responses = []
    for i in range(3):
        client.cookies.set("ecc_session", f"forged-token-{i}")
        responses.append(client.post("/api/v1/widgets"))

    assert [r.status_code for r in responses] == [200, 200, 429]


def test_read_routes_are_not_mutation_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ecc.http_security as http_security

    limiter = http_security._MutationRateLimiter(window_seconds=60.0, max_requests=1)
    monkeypatch.setattr(http_security, "_mutation_rate_limiter", limiter)
    app = FastAPI()
    app.middleware("http")(http_security.mutation_rate_limit_middleware)

    @app.get("/api/v1/widgets")
    def list_widgets() -> dict:
        return {"items": []}

    client = TestClient(app)
    client.cookies.set("ecc_session", "same-session-token")

    for _ in range(5):
        response = client.get("/api/v1/widgets")
        assert response.status_code == 200


def test_health_check_unaffected_by_mutation_rate_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ecc.http_security as http_security

    limiter = http_security._MutationRateLimiter(window_seconds=60.0, max_requests=1)
    monkeypatch.setattr(http_security, "_mutation_rate_limiter", limiter)
    app = FastAPI()
    app.middleware("http")(http_security.mutation_rate_limit_middleware)

    @app.post("/api/v1/widgets")
    def create_widget() -> dict:
        return {"ok": True}

    @app.get("/health/live")
    def live() -> dict:
        return {"status": "ok"}

    client = TestClient(app)
    client.cookies.set("ecc_session", "same-session-token")

    client.post("/api/v1/widgets")
    client.post("/api/v1/widgets")  # exhausts the limit

    response = client.get("/health/live")

    assert response.status_code == 200


def test_rate_limit_uses_monotonic_clock_and_resets_after_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ecc.http_security as http_security

    fake_now = 1000.0

    def fake_monotonic() -> float:
        return fake_now

    monkeypatch.setattr(http_security.time, "monotonic", fake_monotonic)
    limiter = http_security._MutationRateLimiter(window_seconds=10.0, max_requests=1)

    assert limiter.check("k") is None  # 1st request allowed
    retry_after = limiter.check("k")  # 2nd request, still within window
    assert retry_after is not None
    assert retry_after == pytest.approx(10.0, abs=0.01)

    fake_now += 10.5  # advance past the window using the fake monotonic clock
    assert limiter.check("k") is None  # window reset, allowed again


def test_rate_limit_buckets_are_bounded_in_memory() -> None:
    import ecc.http_security as http_security

    limiter = http_security._MutationRateLimiter(
        window_seconds=60.0, max_requests=100, max_buckets=50
    )

    for i in range(500):
        limiter.check(f"key-{i}")

    assert len(limiter._buckets) <= 50


def test_rate_limit_bucket_reset_is_not_immediately_evicted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test: a bucket whose window just expired -- and which this
    very call just made the most-recently-used -- must not still be sitting
    at the eviction front from its original insertion position. Plain
    ``dict``/``OrderedDict`` item assignment on an *existing* key does not
    move it to the end on its own; only a brand-new key lands there
    automatically."""
    import ecc.http_security as http_security

    fake_now = 1000.0

    def fake_monotonic() -> float:
        return fake_now

    monkeypatch.setattr(http_security.time, "monotonic", fake_monotonic)
    limiter = http_security._MutationRateLimiter(
        window_seconds=10.0, max_requests=100, max_buckets=2
    )

    limiter.check("stale-but-active")  # inserted first -> oldest by position
    limiter.check("other")

    fake_now += 10.5  # "stale-but-active"'s window has now expired...
    limiter.check("stale-but-active")  # ...but this call just reused it
    limiter.check("newcomer")  # pushes the bucket table over max_buckets=2

    assert "stale-but-active" in limiter._buckets
    assert "other" not in limiter._buckets


# ---------------------------------------------------------------------------
# Client IP resolution: the raw ASGI socket peer by default, X-Forwarded-For
# only when the deployment explicitly configures a trusted proxy hop count.
# Exercises the fix for the rate limiter collapsing every real client behind
# an unconfigured reverse proxy into one shared bucket.
# ---------------------------------------------------------------------------


def test_client_ip_from_forwarded_for_trusts_only_the_configured_hop_count() -> None:
    import ecc.http_security as http_security

    # One trusted proxy: the rightmost hop is what it appended -- the real
    # client -- regardless of what an attacker prepended further left.
    assert (
        http_security._client_ip_from_forwarded_for("203.0.113.7", trusted_proxy_count=1)
        == "203.0.113.7"
    )
    assert (
        http_security._client_ip_from_forwarded_for(
            "attacker-forged, 203.0.113.7", trusted_proxy_count=1
        )
        == "203.0.113.7"
    )
    # Two trusted proxies: the real client is two hops in from the right.
    assert (
        http_security._client_ip_from_forwarded_for(
            "attacker-forged, 203.0.113.7, 10.0.0.5", trusted_proxy_count=2
        )
        == "203.0.113.7"
    )


def test_client_ip_from_forwarded_for_falls_back_when_header_has_too_few_hops() -> None:
    import ecc.http_security as http_security

    assert http_security._client_ip_from_forwarded_for("203.0.113.7", trusted_proxy_count=2) is None


def test_client_ip_from_forwarded_for_rejects_zero_trusted_proxy_count() -> None:
    """Defense-in-depth regression test: `list[-0]` is Python for `list[0]`
    -- the leftmost, attacker-controlled hop -- so trusted_proxy_count=0
    must never fall through to the `hops[-trusted_proxy_count]` indexing,
    even though the sole caller (_client_host) already special-cases this
    before calling in."""
    import ecc.http_security as http_security

    assert (
        http_security._client_ip_from_forwarded_for(
            "attacker-forged, 203.0.113.7", trusted_proxy_count=0
        )
        is None
    )


def _forwarded_request(*, forwarded_for: str, client_host: str) -> Request:
    return Request(
        {
            "type": "http",
            "headers": [(b"x-forwarded-for", forwarded_for.encode("utf-8"))],
            "client": (client_host, 12345),
            "method": "GET",
            "path": "/",
        }
    )


def test_client_host_ignores_forwarded_for_when_trusted_proxy_count_is_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default posture: an unconfigured deployment must never trust a
    client-supplied X-Forwarded-For header, since that would let any client
    spoof its rate-limit identity for free."""
    import ecc.config as config_module
    import ecc.http_security as http_security

    monkeypatch.delenv("ECC_TRUSTED_PROXY_COUNT", raising=False)
    config_module.get_settings.cache_clear()

    request = _forwarded_request(forwarded_for="203.0.113.7", client_host="10.0.0.9")

    assert http_security._client_host(request) == "10.0.0.9"


def test_client_host_reads_forwarded_for_when_trusted_proxy_count_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ecc.config as config_module
    import ecc.http_security as http_security

    monkeypatch.setenv("ECC_TRUSTED_PROXY_COUNT", "1")
    config_module.get_settings.cache_clear()
    try:
        # The socket peer is the trusted reverse proxy's own address, not the
        # real client -- exactly the value that otherwise collapses every
        # client behind it into one shared rate-limit bucket.
        request = _forwarded_request(forwarded_for="203.0.113.7", client_host="10.0.0.9")

        assert http_security._client_host(request) == "203.0.113.7"
    finally:
        monkeypatch.delenv("ECC_TRUSTED_PROXY_COUNT", raising=False)
        config_module.get_settings.cache_clear()


def test_mutation_rate_limit_distinguishes_clients_behind_a_trusted_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression test for the collapsed-bucket bug: with a trusted proxy
    configured, two distinct real clients arriving through the same proxy
    socket address must not share an IP rate-limit bucket."""
    import ecc.config as config_module
    import ecc.http_security as http_security

    monkeypatch.setenv("ECC_TRUSTED_PROXY_COUNT", "1")
    config_module.get_settings.cache_clear()
    monkeypatch.setattr(
        http_security,
        "_mutation_rate_limiter",
        http_security._MutationRateLimiter(window_seconds=60.0, max_requests=100),
    )
    monkeypatch.setattr(
        http_security,
        "_mutation_ip_rate_limiter",
        http_security._MutationRateLimiter(window_seconds=60.0, max_requests=1),
    )
    app = FastAPI()
    app.middleware("http")(http_security.mutation_rate_limit_middleware)

    @app.post("/api/v1/widgets")
    def create_widget() -> dict:
        return {"ok": True}

    try:
        client = TestClient(app)  # every request shares one fixed socket peer

        first = client.post("/api/v1/widgets", headers={"X-Forwarded-For": "203.0.113.7"})
        second = client.post("/api/v1/widgets", headers={"X-Forwarded-For": "203.0.113.8"})

        assert first.status_code == 200
        assert second.status_code == 200
    finally:
        monkeypatch.delenv("ECC_TRUSTED_PROXY_COUNT", raising=False)
        config_module.get_settings.cache_clear()


# ---------------------------------------------------------------------------
# CORS composition: short-circuited 413/429 responses must still carry CORS
# headers for a real cross-origin browser client.
#
# ecc.main's actual deployment has the frontend on one origin and the
# backend on another, with CORSMiddleware configured allow_credentials=True.
# CORSMiddleware only annotates responses that pass through the `send`
# channel it wraps. If it is registered as an *inner* layer (Starlette wraps
# the most-recently-added middleware outermost -- see the ordering comments
# in ecc.main), any middleware that short-circuits *before* calling
# call_next()/self._app() -- as MaxBodySizeMiddleware's fast Content-Length
# path and mutation_rate_limit_middleware's 429 branch both do -- produces a
# response CORSMiddleware never gets a chance to see. A real browser then
# reports an opaque network/CORS error instead of a readable 429/413 the
# frontend could read `Retry-After` from and act on.
#
# `_build_test_app(include_cors=True)` registers CORSMiddleware *last*,
# mirroring the corrected registration order in ecc.main (CORSMiddleware
# added after every other middleware, so it ends up outermost). These tests
# were confirmed to fail (no Access-Control-Allow-Origin header on the
# 429/413 response) when CORSMiddleware was instead registered *first* --
# reproducing ecc.main's pre-fix order -- and pass with it registered last.
# ---------------------------------------------------------------------------

_CROSS_ORIGIN = "https://frontend.example.com"


def test_cors_headers_present_on_rate_limited_cross_origin_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ecc.http_security as http_security

    limiter = http_security._MutationRateLimiter(window_seconds=60.0, max_requests=1)
    monkeypatch.setattr(http_security, "_mutation_rate_limiter", limiter)

    app = _build_test_app(include_cors=True, cors_origins=[_CROSS_ORIGIN])
    client = TestClient(app)
    client.cookies.set("ecc_session", "same-session-token")
    origin_headers = {"Origin": _CROSS_ORIGIN}

    first = client.post("/api/v1/widgets", json={}, headers=origin_headers)
    second = client.post("/api/v1/widgets", json={}, headers=origin_headers)

    assert first.status_code == 200
    assert second.status_code == 429
    assert int(second.headers["Retry-After"]) >= 1
    # These are exactly what a real browser needs in order to read a
    # cross-origin response's status/body at all, rather than surfacing an
    # opaque CORS/network error to the frontend.
    assert second.headers["access-control-allow-origin"] == _CROSS_ORIGIN
    assert second.headers["access-control-allow-credentials"] == "true"
    assert "origin" in second.headers.get("vary", "").lower()


def test_cors_headers_present_on_oversized_body_cross_origin_response() -> None:
    app = _build_test_app(max_body_bytes=16, include_cors=True, cors_origins=[_CROSS_ORIGIN])
    client = TestClient(app)
    origin_headers = {"Origin": _CROSS_ORIGIN}

    response = client.post("/api/v1/widgets", json={"padding": "x" * 200}, headers=origin_headers)

    assert response.status_code == 413
    assert response.headers["access-control-allow-origin"] == _CROSS_ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"
    assert "origin" in response.headers.get("vary", "").lower()


def test_cors_headers_present_on_real_app_oversized_body_cross_origin_response(
    monkeypatch: pytest.MonkeyPatch,
    restore_main_module: None,
) -> None:
    """Same regression as the two tests above, but against the *real*
    ``ecc.main.app`` object -- not ``_build_test_app``'s hand-copied mirror --
    so this test is a tripwire tied to main.py's actual middleware
    registration order: if a future edit to main.py reintroduced
    CORSMiddleware-first (without anyone touching this test file), this test
    would fail even though the two tests above (which reconstruct the
    ordering by hand) would keep passing unchanged.

    Uses ``_reload_main`` (already used by the dev-bootstrap tests above) to
    build the real app with production-classified settings, so this also
    exercises the real, un-overridden ``MAX_REQUEST_BODY_BYTES`` (1 MiB) body
    cap -- MaxBodySizeMiddleware's fast Content-Length path rejects the
    request before routing/handlers ever run, so no DB/domain setup is
    needed. A real 429 would require 41 rapid mutation requests against the
    real 40-req/60s limiter (see RATE_LIMIT_MAX_REQUESTS in
    http_security.py) sharing one session/IP key; that's impractical as a
    fast, deterministic unit test, so only the 413 path is covered here. The
    429 case remains covered by test_cors_headers_present_on_rate_limited_
    cross_origin_response above, using a small max_requests for
    determinism.
    """
    cross_origin = "https://app.example.com"  # matches _reload_main's ECC_CORS_ORIGINS
    reloaded = _reload_main(monkeypatch, "production")
    client = TestClient(reloaded.app)

    oversized_body = b"x" * (MAX_REQUEST_BODY_BYTES + 1024)

    response = client.post(
        "/api/v1/tasks",
        content=oversized_body,
        headers={"Origin": cross_origin, "Content-Type": "application/octet-stream"},
    )

    assert response.status_code == 413
    assert response.headers["access-control-allow-origin"] == cross_origin
    assert response.headers["access-control-allow-credentials"] == "true"
    assert "origin" in response.headers.get("vary", "").lower()


def test_cors_headers_present_on_normal_response_with_cors_enabled() -> None:
    """Sanity check: enabling CORS in the test app doesn't break the plain
    (non-short-circuited) response path that the other tests in this file
    exercise without CORS."""
    app = _build_test_app(include_cors=True, cors_origins=[_CROSS_ORIGIN])
    client = TestClient(app)

    response = client.get("/api/v1/widgets", headers={"Origin": _CROSS_ORIGIN})

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == _CROSS_ORIGIN


# ---------------------------------------------------------------------------
# Container-level assertion: the production frontend image actually serves
# the nginx security header policy (frontend/nginx.conf.template), not merely that
# the config file contains the right text.
#
# This is opt-in (skipped unless ECC_RUN_CONTAINER_SECURITY_TEST=1), unlike
# everything else in this file: it shells out to `docker build`/`docker
# run`, which needs Docker, network access to pull node:22-alpine and
# nginx:1.27-alpine, and takes tens of seconds even warm from cache -- not
# appropriate to run on every default `pytest`/`uv run pytest` invocation
# alongside the rest of this fast, in-process suite. It is still fully
# automated and re-runnable on demand:
#
#     ECC_RUN_CONTAINER_SECURITY_TEST=1 \
#         uv run pytest tests/test_production_security.py -k container -q
#
# It was also run manually once during this task (same build/run/curl
# sequence, see the task report for the captured header output) to prove
# the assertion actually passes against a real image before relying on it.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[1]


def _free_tcp_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available")
@pytest.mark.skipif(
    os.environ.get("ECC_RUN_CONTAINER_SECURITY_TEST") != "1",
    reason=(
        "opt-in container build/run test; set ECC_RUN_CONTAINER_SECURITY_TEST=1 "
        "to exercise it (see comment above)"
    ),
)
def test_production_container_serves_security_headers() -> None:
    image_tag = f"ecc-frontend-prod-test-{uuid.uuid4().hex[:12]}"
    container_name = f"ecc-frontend-prod-check-{uuid.uuid4().hex[:12]}"
    port = _free_tcp_port()
    backend_origin = "https://api.example.test"

    subprocess.run(
        [
            "docker",
            "build",
            "-f",
            str(_REPO_ROOT / "frontend" / "Dockerfile"),
            "--target",
            "production",
            "-t",
            image_tag,
            str(_REPO_ROOT),
        ],
        check=True,
        capture_output=True,
    )
    try:
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container_name,
                "-p",
                f"127.0.0.1:{port}:80",
                # Mirrors the deployment runbook: the CSP connect-src is
                # rendered from this at container start (nginx.conf.template),
                # independent of whatever VITE_API_BASE_URL the JS bundle was
                # built with -- this test only exercises the run-time half.
                "-e",
                f"VITE_API_BASE_URL={backend_origin}",
                image_tag,
            ],
            check=True,
            capture_output=True,
        )

        response: httpx.Response | None = None
        last_error: Exception | None = None
        for _ in range(30):
            try:
                response = httpx.get(f"http://127.0.0.1:{port}/", timeout=1.0)
                break
            except httpx.TransportError as error:
                last_error = error
                time.sleep(0.5)
        if response is None:
            raise AssertionError(f"production container never became reachable: {last_error}")

        assert response.status_code == 200
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["referrer-policy"] == "no-referrer"
        csp = response.headers["content-security-policy"]
        assert "default-src 'self'" in csp
        # The regression this guards: connect-src used to be hardcoded to
        # 'self' only, which silently blocks the app's own API calls in the
        # documented cross-origin deployment shape (see nginx.conf.template's
        # header comment and docs/runbooks/PHASE-1-DEPLOYMENT.md).
        assert f"connect-src 'self' {backend_origin}" in csp
        assert "max-age=" in response.headers["strict-transport-security"]
    finally:
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
        subprocess.run(["docker", "rmi", "-f", image_tag], capture_output=True)


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker CLI not available")
@pytest.mark.skipif(
    os.environ.get("ECC_RUN_CONTAINER_SECURITY_TEST") != "1",
    reason=(
        "opt-in container build/run test; set ECC_RUN_CONTAINER_SECURITY_TEST=1 "
        "to exercise it (see comment above)"
    ),
)
def test_production_container_csp_falls_back_safely_without_backend_origin() -> None:
    """If a deployer forgets -e VITE_API_BASE_URL at `docker run` time, the
    container must still start and serve a valid (if same-origin-only) CSP,
    not a malformed header or a crashed nginx."""
    image_tag = f"ecc-frontend-prod-test-{uuid.uuid4().hex[:12]}"
    container_name = f"ecc-frontend-prod-check-{uuid.uuid4().hex[:12]}"
    port = _free_tcp_port()

    subprocess.run(
        [
            "docker",
            "build",
            "-f",
            str(_REPO_ROOT / "frontend" / "Dockerfile"),
            "--target",
            "production",
            "-t",
            image_tag,
            str(_REPO_ROOT),
        ],
        check=True,
        capture_output=True,
    )
    try:
        subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container_name,
                "-p",
                f"127.0.0.1:{port}:80",
                image_tag,
            ],
            check=True,
            capture_output=True,
        )

        response: httpx.Response | None = None
        last_error: Exception | None = None
        for _ in range(30):
            try:
                response = httpx.get(f"http://127.0.0.1:{port}/", timeout=1.0)
                break
            except httpx.TransportError as error:
                last_error = error
                time.sleep(0.5)
        if response is None:
            raise AssertionError(f"production container never became reachable: {last_error}")

        assert response.status_code == 200
        assert "connect-src 'self'" in response.headers["content-security-policy"]
    finally:
        subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)
        subprocess.run(["docker", "rmi", "-f", image_tag], capture_output=True)
