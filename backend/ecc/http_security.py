"""HTTP-layer protections: security response headers, a request body size
cap, and a bounded mutation-route rate limit.

This module intentionally does not touch settings validation (see
``ecc.config.validate_production_settings`` for that) -- its one job is
transport-level hardening, applied uniformly regardless of environment.

Three independent pieces, each usable/testable on its own:

1. ``security_headers_middleware`` -- a normal ``@app.middleware("http")``
   dispatcher (same shape as ``ecc.main.response_contract_middleware``) that
   adds baseline security headers to every response via
   ``response.headers.setdefault``, so it never clobbers a more specific
   policy a route already set (e.g. ``ecc.dev_bootstrap.bootstrap_page``'s
   nonce-scoped CSP).

2. ``MaxBodySizeMiddleware`` -- a *pure ASGI* middleware class (registered
   via ``app.add_middleware``, not the ``dispatch``-based helper above)
   because enforcing a body size cap without ever buffering an oversized
   body requires intercepting the raw ``receive()`` channel directly.
   ``@app.middleware("http")``/``BaseHTTPMiddleware`` is the wrong tool here:
   it operates on a already-adapted ``Request``, one layer above the raw
   ASGI messages this needs to inspect as they stream in.

3. ``_MutationRateLimiter`` / ``mutation_rate_limit_middleware`` -- a fixed
   window rate limiter for mutation-class routes only (see
   ``_is_mutation_route`` for the exact grouping and rationale), keyed by
   client IP (refined by session cookie when present, but IP always
   participates in the key since the cookie is unvalidated at this point in
   the chain), backed by a bounded in-memory bucket table so a long-running
   process cannot accumulate unbounded state.
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from hashlib import sha256
from math import ceil

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# ---------------------------------------------------------------------------
# Security headers
# ---------------------------------------------------------------------------

# A focused, defense-in-depth baseline applied to every response. This API is
# JSON-only (no browser-rendered HTML besides ecc.dev_bootstrap's one
# development-only page, which sets its own stricter/nonce-scoped CSP), so a
# maximally restrictive default CSP is safe here. Strict-Transport-Security is
# sent unconditionally: browsers ignore it entirely over plain HTTP (local
# dev), so it is harmless there and required for HTTPS deployments.
_SECURITY_HEADERS: dict[str, str] = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
    "Strict-Transport-Security": "max-age=63072000; includeSubDomains",
}


async def security_headers_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    response = await call_next(request)
    for name, value in _SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


# ---------------------------------------------------------------------------
# Request body size limit
# ---------------------------------------------------------------------------

# The largest single Phase 1 payload field is notes.body at max_length=100000
# characters (backend/ecc/domains/knowledge/notes.py, per
# docs/phases/phase-001/API-SCHEMAS.md). A full note create/update
# (title 500 + body 100000 + source_ref 2000 chars) is ~102500 characters of
# content. 1 MiB gives that payload more than 4x headroom even under
# pessimistic JSON string-escaping, while still bounding worst-case abuse
# (a client cannot force multi-megabyte/gigabyte bodies into memory).
MAX_REQUEST_BODY_BYTES = 1 * 1024 * 1024  # 1 MiB


class _RequestBodyTooLarge(Exception):
    """Internal signal: the body being streamed in exceeded the cap."""


async def _send_body_too_large(send: Send) -> None:
    payload = b'{"detail":"REQUEST_BODY_TOO_LARGE"}'
    await send(
        {
            "type": "http.response.start",
            "status": 413,
            "headers": [(b"content-type", b"application/json")],
        }
    )
    await send({"type": "http.response.body", "body": payload})


def _declared_content_length(scope: Scope) -> int | None:
    for raw_name, raw_value in scope.get("headers", []):
        if raw_name.lower() == b"content-length":
            try:
                return int(raw_value)
            except ValueError:
                return None
    return None


class MaxBodySizeMiddleware:
    """Reject oversized request bodies without ever buffering them.

    Two layers of defense, neither of which accumulates request bytes:

    - Fast path: if the client sent a ``Content-Length`` header declaring a
      size over the cap, respond 413 immediately without calling ``receive``
      at all.
    - Streaming path (no/lying Content-Length, e.g. chunked transfer): wrap
      ``receive`` and keep a running byte *count* as each chunk passes
      through -- never the chunks themselves -- raising once the count
      crosses the cap. Downstream code (Starlette/Pydantic body parsing)
      never sees more than ``max_bytes`` of body before the request is cut
      off.
    """

    def __init__(self, app: ASGIApp, *, max_bytes: int = MAX_REQUEST_BODY_BYTES) -> None:
        self._app = app
        self._max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        declared_length = _declared_content_length(scope)
        if declared_length is not None and declared_length > self._max_bytes:
            await _send_body_too_large(send)
            return

        seen = 0

        async def guarded_receive() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > self._max_bytes:
                    raise _RequestBodyTooLarge
            return message

        try:
            await self._app(scope, guarded_receive, send)
        except _RequestBodyTooLarge:
            await _send_body_too_large(send)


# ---------------------------------------------------------------------------
# Bounded mutation rate limiting
# ---------------------------------------------------------------------------

# Route-class grouping: "mutation" means POST/PUT/PATCH/DELETE under
# /api/v1. This is the same boundary the API contract already draws for CSRF
# and idempotency-key requirements (docs/phases/phase-001/API-SCHEMAS.md),
# so it needs no new taxonomy. Read routes (GET) are intentionally left
# unlimited by this middleware -- the task only calls for a mutation rate
# limit, and rate-limiting reads risks starving the existing
# integration/e2e suites' polling/list traffic for no corresponding security
# benefit (an attacker gains nothing by reading faster; CSRF already gates
# state changes). /dev/bootstrap and /health are excluded: dev-bootstrap is a
# local-only flow (and, outside development, is not even registered -- see
# ecc.main), and health checks must stay unaffected per the task brief.
_MUTATION_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_MUTATION_PATH_PREFIX = "/api/v1"

# Window/threshold: the largest observed sequential mutation-call count in a
# single existing test fixture is 17 (tests/test_calendar_meetings_postgres.py,
# one session token across several event/meeting lifecycle calls). 40
# requests/60s per session/IP leaves that fixture (and any near-term growth
# of it) more than 2x headroom while still meaningfully bounding an
# individual client hammering mutation endpoints.
RATE_LIMIT_WINDOW_SECONDS = 60.0
RATE_LIMIT_MAX_REQUESTS = 40

# A second, coarser ceiling keyed by client IP alone (never by the
# unvalidated session cookie -- see _rate_limit_key). The per-session bucket
# above lets several distinct legitimate sessions behind one shared IP
# (office NAT, etc.) each get their own headroom, but that same design means
# a client can mint a fresh, never-validated ecc_session cookie value on
# every request to get a brand-new per-session bucket each time. This IP-only
# ceiling bounds that: no matter how many cookie values one IP cycles
# through, it cannot exceed RATE_LIMIT_MAX_REQUESTS_PER_IP mutation requests
# per window. Set well above the per-session limit so it only engages against
# genuine hammering/bypass attempts, not normal shared-IP traffic.
RATE_LIMIT_MAX_REQUESTS_PER_IP = RATE_LIMIT_MAX_REQUESTS * 10

# Bucket table cap: bounds worst-case memory for this rate limiter regardless
# of how many distinct sessions/IPs a long-running process observes. Evicts
# least-recently-used once the cap is reached.
RATE_LIMIT_MAX_BUCKETS = 5000


class _RateLimitBucket:
    __slots__ = ("window_start", "count")

    def __init__(self, window_start: float) -> None:
        self.window_start = window_start
        self.count = 0


class _MutationRateLimiter:
    """Fixed-window rate limiter keyed by an arbitrary string.

    Uses ``time.monotonic()`` -- never wall-clock (``time.time()``), which
    can jump backwards or forwards on NTP adjustment or a manual clock
    change, corrupting window math -- and a bounded ``OrderedDict`` so the
    bucket table cannot grow without limit over the life of a process.
    """

    def __init__(
        self,
        *,
        window_seconds: float = RATE_LIMIT_WINDOW_SECONDS,
        max_requests: int = RATE_LIMIT_MAX_REQUESTS,
        max_buckets: int = RATE_LIMIT_MAX_BUCKETS,
    ) -> None:
        self._window_seconds = window_seconds
        self._max_requests = max_requests
        self._max_buckets = max_buckets
        self._buckets: OrderedDict[str, _RateLimitBucket] = OrderedDict()

    def check(self, key: str) -> float | None:
        """Record one request for ``key``.

        Returns ``None`` if the request is allowed, or the number of seconds
        the caller should wait (``Retry-After``) if the limit was exceeded.
        """
        now = time.monotonic()
        bucket = self._buckets.get(key)
        if bucket is None or now - bucket.window_start >= self._window_seconds:
            bucket = _RateLimitBucket(window_start=now)
            self._buckets[key] = bucket
            if len(self._buckets) > self._max_buckets:
                self._buckets.popitem(last=False)
        else:
            self._buckets.move_to_end(key)

        bucket.count += 1
        if bucket.count > self._max_requests:
            return max(self._window_seconds - (now - bucket.window_start), 0.0)
        return None


_mutation_rate_limiter = _MutationRateLimiter()
_mutation_ip_rate_limiter = _MutationRateLimiter(max_requests=RATE_LIMIT_MAX_REQUESTS_PER_IP)


def _client_host(request: Request) -> str:
    client = request.client
    return client.host if client else "unknown"


def _rate_limit_key(request: Request, *, host: str) -> str:
    # The session cookie has not been validated yet at this point in the
    # middleware chain (auth happens later, in the route dependency), so on
    # its own it cannot be trusted as a rate-limit key -- a client could mint
    # a fresh, unvalidated cookie value on every request to get a new bucket
    # each time. That bypass is closed by _mutation_ip_rate_limiter (an
    # IP-only ceiling checked alongside this key in the middleware below),
    # not by this function -- this key still differentiates real sessions
    # sharing one IP so they don't throttle each other.
    session_token = request.cookies.get("ecc_session")
    if session_token:
        return f"ip:{host}:session:" + sha256(session_token.encode("utf-8")).hexdigest()
    return f"ip:{host}"


def _is_mutation_route(request: Request) -> bool:
    if request.method not in _MUTATION_METHODS:
        return False
    return request.url.path.startswith(_MUTATION_PATH_PREFIX)


async def mutation_rate_limit_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    if not _is_mutation_route(request):
        return await call_next(request)

    host = _client_host(request)
    ip_retry_after = _mutation_ip_rate_limiter.check(f"ip:{host}")
    key_retry_after = _mutation_rate_limiter.check(_rate_limit_key(request, host=host))
    candidates = [r for r in (ip_retry_after, key_retry_after) if r is not None]
    retry_after = max(candidates) if candidates else None
    if retry_after is None:
        return await call_next(request)

    retry_after_seconds = max(1, ceil(retry_after))
    return JSONResponse(
        {"detail": "MUTATION_RATE_LIMITED"},
        status_code=429,
        headers={"Retry-After": str(retry_after_seconds)},
    )
