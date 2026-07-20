"""Structured request logging and hand-rolled Phase 1 operational metrics.

Two independent concerns live in this one module (by design -- see the task
brief), kept in clearly separated sections so neither tangles with the other:

1. **Logging** -- ``request_observability_middleware`` emits one structured
   JSON log line per request via the ``ecc.request`` logger, carrying route
   template, method, status, duration, and the *same* request/correlation
   IDs ``ecc.main.response_contract_middleware`` already generated (never a
   second, independently generated pair -- see the middleware-ordering note
   below), plus the authenticated workspace identifier when present. It
   never touches request/response bodies, cookies, or auth/CSRF headers.

2. **Metrics** -- small hand-rolled ``_Counter``/``_Histogram`` primitives
   (this project has no ``prometheus_client`` dependency; these follow the
   same minimal-custom-implementation style as
   ``ecc.http_security._MutationRateLimiter``) plus a Prometheus
   text-exposition renderer mounted at ``GET /metrics`` in ``ecc.main``.

Label cardinality: every label used anywhere in this module is drawn from a
small, fixed, code-defined set -- route *templates* from the route registry
(never a resolved path/entity ID), HTTP methods/status codes, and literal
domain/event_type/reason strings hardcoded at each call site (e.g.
``"meeting.archived"``, ``"stale_by_age"``). None of these are influenced by
request content, entity IDs, or workspace IDs, so -- unlike
``_MutationRateLimiter``'s externally-keyed bucket table -- no bounded
eviction policy is needed here: the key space is inherently small and fixed
by this codebase, not by callers.

Middleware ordering (why this file's middleware is registered where it is
in ``ecc.main``): ``response_contract_middleware`` sets
``request.state.request_id``/``correlation_id`` *before* calling
``call_next``, i.e. before delegating to whatever is registered "more
inward" than it. Starlette's ``add_middleware``/``@app.middleware("http")``
prepends to the middleware list, so a middleware registered *earlier in the
file* ends up wrapped *more inward* (closer to the router) than one
registered later. Registering ``request_observability_middleware`` earlier
in ``ecc.main`` than ``response_contract_middleware`` therefore places it
inward of it: by the time this middleware's own dispatch body starts
running, ``response_contract_middleware`` has already set request.state.
This module's own ``call_next`` call then wraps everything further inward
(the mutation rate limiter, the body-size guard, the rejected-mutation audit
middleware, and the router itself) so, symmetrically, the matched route
template (``request.scope["route"]``, set by the router) and any
authenticated workspace identifier (set by ``ecc.auth.require_auth_context``
via the same ``request.state`` mechanism) are already available by the time
this middleware's ``call_next`` call returns. ``tests/test_observability.py``
has a regression tripwire against the real registered app proving this.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from threading import Lock
from typing import Final

from fastapi import Request, Response
from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ecc.database import engine

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

_request_logger = logging.getLogger("ecc.request")
_observability_logger = logging.getLogger("ecc.observability")

_UNMATCHED_ROUTE: Final = "unmatched"


def _route_template(request: Request) -> str:
    """The registered route *template* Starlette matched, e.g.
    ``/api/v1/tasks/{task_id}`` -- never the resolved path with a real ID
    substituted in, so this stays a bounded log field / metric label across
    the fixed set of registered routes.

    ``request.scope["route"]`` is populated by the router only once it
    actually dispatches to a handler. If an earlier middleware short-circuited
    the request before routing ever ran (e.g. a 413 from the body-size guard,
    or a 429 from the mutation rate limiter), no route ever matched --
    ``_UNMATCHED_ROUTE`` is the bounded fallback for that case, chosen over
    the raw request path specifically because the raw path could contain an
    unbounded/PII-bearing segment for an unregistered route.
    """
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return path if isinstance(path, str) else _UNMATCHED_ROUTE


async def request_observability_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Emit one structured JSON log line and record request metrics per
    request. See the module docstring for why this is registered where it
    is in ``ecc.main`` relative to ``response_contract_middleware``.

    "Database failures" are recorded here too: ``call_next`` wraps the full
    downstream stack including the router/endpoint, and Starlette's
    ``BaseHTTPMiddleware.call_next`` re-raises any exception the downstream
    app raised (it runs it in a task and propagates the result), so a
    ``SQLAlchemyError`` raised by domain code is visible here before it
    reaches Starlette's outer ``ServerErrorMiddleware`` (which still produces
    the same 500 response as today -- this only *observes* the failure, it
    is re-raised unchanged so response/status behavior is untouched).

    That observation includes the *same* request-completion log line and
    ``record_request`` metric call the success path below emits -- not just
    ``record_database_failure`` -- using status ``500`` (the response
    Starlette's ``ServerErrorMiddleware`` will actually send once this
    re-raise reaches it), so a DB-failure-driven request still produces
    exactly one structured log line and is still counted in
    ``ecc_http_requests_total``/``ecc_http_request_duration_seconds``.
    """
    start = time.monotonic()
    try:
        response = await call_next(request)
    except SQLAlchemyError:
        route = _route_template(request)
        record_database_failure(route)
        duration_seconds = time.monotonic() - start
        _record_and_log_request(request, route, duration_seconds, status_code=500)
        raise

    duration_seconds = time.monotonic() - start
    route = _route_template(request)
    status_code = response.status_code
    _record_and_log_request(request, route, duration_seconds, status_code=status_code)
    return response


def _record_and_log_request(
    request: Request, route: str, duration_seconds: float, *, status_code: int
) -> None:
    method = request.method
    request_id = getattr(request.state, "request_id", None)
    correlation_id = getattr(request.state, "correlation_id", None)
    workspace_id = getattr(request.state, "workspace_id", None)

    record_request(route, method, status_code, duration_seconds)

    _request_logger.info(
        "request_handled",
        extra={
            "request_id": request_id,
            "correlation_id": correlation_id,
            "route": route,
            "http_method": method,
            "status_code": status_code,
            "duration_ms": round(duration_seconds * 1000.0, 3),
            "workspace_id": str(workspace_id) if workspace_id else None,
        },
    )


# ---------------------------------------------------------------------------
# Metrics primitives (hand-rolled; no prometheus_client dependency)
# ---------------------------------------------------------------------------


def _format_labels(names: tuple[str, ...], values: tuple[str, ...]) -> str:
    if not names:
        return ""
    pairs = ",".join(f'{name}="{value}"' for name, value in zip(names, values, strict=True))
    return "{" + pairs + "}"


class _Counter:
    """A monotonic counter, optionally labeled. Every label combination this
    module actually uses is drawn from a small fixed set (see module
    docstring), so the label-value dict below cannot grow unbounded.
    """

    def __init__(self, name: str, help_text: str, label_names: tuple[str, ...] = ()) -> None:
        self.name = name
        self.help_text = help_text
        self.label_names = label_names
        self._values: dict[tuple[str, ...], float] = {}
        self._lock = Lock()

    def inc(self, *label_values: str, amount: float = 1.0) -> None:
        if len(label_values) != len(self.label_names):
            raise ValueError(f"{self.name}: expected {len(self.label_names)} label values")
        with self._lock:
            self._values[label_values] = self._values.get(label_values, 0.0) + amount

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} counter"]
        with self._lock:
            snapshot = list(self._values.items())
        for label_values, value in snapshot:
            lines.append(f"{self.name}{_format_labels(self.label_names, label_values)} {value}")
        return lines


_DEFAULT_DURATION_BUCKETS: Final[tuple[float, ...]] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)


class _Histogram:
    """A cumulative-bucket histogram in the standard Prometheus text-format
    shape (``_bucket{le="..."}``, ``_sum``, ``_count``), hand-rolled to match
    this module's no-new-dependency constraint.
    """

    def __init__(
        self,
        name: str,
        help_text: str,
        label_names: tuple[str, ...] = (),
        buckets: tuple[float, ...] = _DEFAULT_DURATION_BUCKETS,
    ) -> None:
        self.name = name
        self.help_text = help_text
        self.label_names = label_names
        self.buckets: tuple[float, ...] = tuple(sorted(buckets)) + (float("inf"),)
        self._bucket_counts: dict[tuple[str, ...], list[int]] = {}
        self._sums: dict[tuple[str, ...], float] = {}
        self._counts: dict[tuple[str, ...], int] = {}
        self._lock = Lock()

    def observe(self, *label_values: str, value: float) -> None:
        if len(label_values) != len(self.label_names):
            raise ValueError(f"{self.name}: expected {len(self.label_names)} label values")
        with self._lock:
            counts = self._bucket_counts.setdefault(label_values, [0] * len(self.buckets))
            for index, bound in enumerate(self.buckets):
                if value <= bound:
                    counts[index] += 1
            self._sums[label_values] = self._sums.get(label_values, 0.0) + value
            self._counts[label_values] = self._counts.get(label_values, 0) + 1

    def render(self) -> list[str]:
        lines = [f"# HELP {self.name} {self.help_text}", f"# TYPE {self.name} histogram"]
        with self._lock:
            snapshot = {
                key: (list(counts), self._sums[key], self._counts[key])
                for key, counts in self._bucket_counts.items()
            }
        for label_values, (counts, total, count) in snapshot.items():
            cumulative = 0
            for bound, bucket_count in zip(self.buckets, counts, strict=True):
                cumulative += bucket_count
                bound_label = "+Inf" if bound == float("inf") else str(bound)
                names = (*self.label_names, "le")
                values = (*label_values, bound_label)
                lines.append(f"{self.name}_bucket{_format_labels(names, values)} {cumulative}")
            suffix_labels = _format_labels(self.label_names, label_values)
            lines.append(f"{self.name}_sum{suffix_labels} {total}")
            lines.append(f"{self.name}_count{suffix_labels} {count}")
        return lines


# ---------------------------------------------------------------------------
# Concrete instruments -- one per design-doc signal.
# ---------------------------------------------------------------------------

http_requests_total = _Counter(
    "ecc_http_requests_total",
    "Total HTTP requests, by route template, method, and status code.",
    ("route", "method", "status"),
)
http_request_duration_seconds = _Histogram(
    "ecc_http_request_duration_seconds",
    "HTTP request duration in seconds, by route template and method.",
    ("route", "method"),
)
database_failures_total = _Counter(
    "ecc_database_failures_total",
    "Requests that failed with a database error, by route template.",
    ("route",),
)
lifecycle_events_total = _Counter(
    "ecc_lifecycle_events_total",
    "Domain lifecycle events recorded to audit_events, by domain, event "
    "type, and authorization result.",
    ("domain", "event_type", "result"),
)
search_duration_seconds = _Histogram(
    "ecc_search_duration_seconds",
    "Search query duration in seconds.",
)
search_results_total = _Histogram(
    "ecc_search_results_count",
    "Number of results returned per search request.",
    buckets=(0, 1, 5, 10, 20, 50, 100),
)
ranking_duration_seconds = _Histogram(
    "ecc_ranking_duration_seconds",
    "Attention/ranking regeneration duration in seconds.",
)
ranking_input_total = _Histogram(
    "ecc_ranking_input_count",
    "Number of eligible entities scored per ranking regeneration.",
    buckets=(0, 10, 50, 100, 500, 1000, 5000, 10000),
)
brief_generation_duration_seconds = _Histogram(
    "ecc_brief_generation_duration_seconds",
    "Morning brief generation duration in seconds.",
)
brief_stale_total = _Counter(
    "ecc_brief_stale_total",
    "Morning briefs found stale on read, by reason.",
    ("reason",),
)
recommendation_transitions_total = _Counter(
    "ecc_recommendation_transitions_total",
    "Recommendation lifecycle transitions, by event type.",
    ("event_type",),
)
idempotency_conflicts_total = _Counter(
    "ecc_idempotency_conflicts_total",
    "Idempotency-Key request-hash conflicts (409s), by domain.",
    ("domain",),
)
audit_outbox_failures_total = _Counter(
    "ecc_audit_outbox_failures_total",
    "Failures writing audit_events/event_outbox rows, by domain.",
    ("domain",),
)

_COUNTERS: Final[tuple[_Counter, ...]] = (
    http_requests_total,
    database_failures_total,
    lifecycle_events_total,
    brief_stale_total,
    recommendation_transitions_total,
    idempotency_conflicts_total,
    audit_outbox_failures_total,
)
_HISTOGRAMS: Final[tuple[_Histogram, ...]] = (
    http_request_duration_seconds,
    search_duration_seconds,
    search_results_total,
    ranking_duration_seconds,
    ranking_input_total,
    brief_generation_duration_seconds,
)


# ---------------------------------------------------------------------------
# record_* helpers -- the single call domain modules make at each emission
# point. Each wraps exactly one instrument update so call sites stay a
# single line.
# ---------------------------------------------------------------------------


def record_request(route: str, method: str, status_code: int, duration_seconds: float) -> None:
    http_requests_total.inc(route, method, str(status_code))
    http_request_duration_seconds.observe(route, method, value=duration_seconds)


def record_database_failure(route: str) -> None:
    database_failures_total.inc(route)


def record_lifecycle_event(domain: str, event_type: str, result: str = "allowed") -> None:
    lifecycle_events_total.inc(domain, event_type, result)


# ---------------------------------------------------------------------------
# Lifecycle events: deferred until commit. Every domain mutation writes its
# audit row and calls queue_lifecycle_event() *inside* the same transaction
# as the rest of the mutation (outbox write, idempotency-key storage, etc).
# If a later statement in that same transaction fails, the whole thing rolls
# back -- but a direct record_lifecycle_event() call at audit-write time
# would already have incremented lifecycle_events_total, silently counting a
# mutation that never actually persisted. Queuing on session.info and
# flushing from an `after_commit` event means the metric only ever reflects
# committed state, regardless of which of the (several, near-identical)
# domain call sites queued it, and regardless of whether that route commits
# via `with session.begin():` or a manual `session.commit()` call.
# ---------------------------------------------------------------------------


def queue_lifecycle_event(
    session: Session, domain: str, event_type: str, result: str = "allowed"
) -> None:
    session.info.setdefault("_pending_lifecycle_events", []).append((domain, event_type, result))


@event.listens_for(Session, "after_commit")
def _flush_lifecycle_events(session: Session) -> None:
    pending = session.info.pop("_pending_lifecycle_events", None)
    if not pending:
        return
    for domain, event_type, result in pending:
        record_lifecycle_event(domain, event_type, result)


@event.listens_for(Session, "after_rollback")
def _discard_lifecycle_events_on_rollback(session: Session) -> None:
    # session.info is a plain dict that outlives any one transaction (it is
    # NOT reset by SQLAlchemy on rollback/commit), so without this a rolled
    # back transaction's queued events would sit in session.info and get
    # incorrectly flushed by a *later*, unrelated commit on the same Session
    # object. Each request gets its own fresh Session today (see
    # ecc.database.get_session), so that reuse doesn't currently happen in
    # practice -- but nothing about queue_lifecycle_event's contract should
    # depend on that.
    session.info.pop("_pending_lifecycle_events", None)


def record_search(duration_seconds: float, result_count: int) -> None:
    search_duration_seconds.observe(value=duration_seconds)
    search_results_total.observe(value=float(result_count))


def record_ranking(duration_seconds: float, input_count: int) -> None:
    ranking_duration_seconds.observe(value=duration_seconds)
    ranking_input_total.observe(value=float(input_count))


def record_brief_generated(duration_seconds: float) -> None:
    brief_generation_duration_seconds.observe(value=duration_seconds)


def record_brief_stale(reason: str) -> None:
    brief_stale_total.inc(reason)


def record_recommendation_transition(event_type: str) -> None:
    recommendation_transitions_total.inc(event_type)


def record_idempotency_conflict(domain: str) -> None:
    idempotency_conflicts_total.inc(domain)


def record_audit_outbox_failure(domain: str) -> None:
    audit_outbox_failures_total.inc(domain)


# ---------------------------------------------------------------------------
# Outbox backlog: unlike every other signal above, this one has no natural
# per-mutation emission point -- it's a property of the *table*, not of any
# single mutation (a mutation that successfully inserts an outbox row tells
# you nothing about how many older rows are still unpublished). It's
# computed live, from a direct count query, each time /metrics is scraped --
# the same "read a real signal at scrape time" shape as a Prometheus gauge
# with a collector callback, just hand-rolled. Judgment call recorded in the
# task report.
# ---------------------------------------------------------------------------


# Cache TTL for the backlog query below: bounds how often a live COUNT(*)
# against event_outbox can be forced, independent of whatever auth gate (or
# lack of one, see ECC_METRICS_TOKEN in ecc.main) sits in front of /metrics.
# Short enough that a real scrape interval (typically 15-60s) always sees a
# fresh value; long enough that even unthrottled/unauthenticated repeated
# requests can't turn /metrics into a query-amplification vector.
_OUTBOX_BACKLOG_CACHE_TTL_SECONDS = 5.0
_outbox_backlog_cache: tuple[float, int | None] | None = None
_outbox_backlog_cache_lock = Lock()


def _outbox_backlog_count() -> int | None:
    global _outbox_backlog_cache
    now = time.monotonic()
    with _outbox_backlog_cache_lock:
        if _outbox_backlog_cache is not None:
            cached_at, cached_value = _outbox_backlog_cache
            if now - cached_at < _OUTBOX_BACKLOG_CACHE_TTL_SECONDS:
                return cached_value

    try:
        with engine.connect() as connection:
            result = connection.execute(
                text("SELECT count(*) FROM event_outbox WHERE published_at IS NULL")
            ).scalar_one()
            value = int(result)
    except SQLAlchemyError:
        _observability_logger.warning("outbox_backlog_query_failed", exc_info=True)
        value = None

    with _outbox_backlog_cache_lock:
        _outbox_backlog_cache = (now, value)
    return value


def render_metrics() -> str:
    lines: list[str] = []
    for counter in _COUNTERS:
        lines.extend(counter.render())
    for histogram in _HISTOGRAMS:
        lines.extend(histogram.render())

    backlog = _outbox_backlog_count()
    if backlog is not None:
        lines.append(
            "# HELP ecc_event_outbox_backlog Unpublished event_outbox rows at scrape time."
        )
        lines.append("# TYPE ecc_event_outbox_backlog gauge")
        lines.append(f"ecc_event_outbox_backlog {backlog}")

    return "\n".join(lines) + "\n"
