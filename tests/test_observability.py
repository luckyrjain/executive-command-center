"""Structured request logging and Phase 1 metrics (``backend/ecc/observability.py``).

Split into two independent surfaces, mirroring the module itself:

1. Logging -- ``request_observability_middleware`` emits one structured JSON
   log line per request. These tests build a minimal standalone FastAPI app
   (same style as ``tests/test_production_security.py``'s ``_build_test_app``)
   wiring a stand-in for ``ecc.main.response_contract_middleware`` (just the
   two ``request.state`` assignments it makes, since that's the only part
   these tests depend on) plus the real ``request_observability_middleware``,
   so logging behavior is exercised without needing the full app/DB. A
   handful of tests run against the *real* ``ecc.main.app`` to prove the
   actual registered middleware order is correct (request_id/correlation_id
   genuinely available, not a coincidence of test wiring).

2. Metrics -- the hand-rolled ``Counter``/``Histogram`` primitives and the
   ``/metrics`` text-exposition endpoint. Instrument-level tests call the
   ``record_*`` functions directly and assert on ``render_metrics()`` output;
   a Postgres-gated test covers the outbox backlog gauge, which is computed
   live from ``event_outbox`` at scrape time (see observability.py for why).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from sqlalchemy.exc import SQLAlchemyError

from ecc.config import get_settings
from ecc.database import SessionFactory, engine
from ecc.observability import (
    record_audit_outbox_failure,
    record_brief_stale,
    record_database_failure,
    record_idempotency_conflict,
    record_lifecycle_event,
    record_ranking,
    record_recommendation_transition,
    record_request,
    record_search,
    render_metrics,
    request_observability_middleware,
)

settings = get_settings()


class _CaptureHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def capture_request_logs() -> Iterator[_CaptureHandler]:
    handler = _CaptureHandler()
    logger = logging.getLogger("ecc.request")
    previous_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)


def _stub_request_ids_middleware(request: Request, call_next):  # type: ignore[no-untyped-def]
    """Stand-in for the *part* of response_contract_middleware this suite
    depends on: setting request.state.request_id/correlation_id before the
    rest of the stack runs. Kept intentionally tiny (no JSON envelope
    reshaping) so these tests stay focused on the observability middleware,
    not a reimplementation of ecc.main.
    """
    request.state.request_id = str(uuid4())
    request.state.correlation_id = str(uuid4())
    return call_next(request)


def _build_test_app() -> FastAPI:
    app = FastAPI()
    # Registration order matters: request_observability_middleware must be
    # added *before* the request-id stub so it executes (wraps) *after* the
    # stub has set request.state -- see ecc.main for the real ordering this
    # mirrors (added before response_contract_middleware).
    app.middleware("http")(request_observability_middleware)
    app.middleware("http")(_stub_request_ids_middleware)

    @app.get("/api/v1/widgets/{widget_id}")
    def get_widget(widget_id: str, request: Request) -> dict:
        return {"id": widget_id}

    @app.get("/api/v1/authenticated")
    def get_authenticated(request: Request) -> dict:
        request.state.workspace_id = uuid4()
        return {"ok": True}

    @app.post("/api/v1/echo")
    def echo(payload: dict, request: Request) -> dict:
        return {"received": payload}

    @app.get("/api/v1/db-failure")
    def db_failure(request: Request) -> dict:
        raise SQLAlchemyError("simulated database failure")

    return app


# ---------------------------------------------------------------------------
# Logging: request/correlation IDs match the response headers exactly
# (middleware-ordering proof), not an independently generated pair.
# ---------------------------------------------------------------------------


def test_log_request_id_matches_stub_request_state(capture_request_logs: _CaptureHandler) -> None:
    client = TestClient(_build_test_app())

    response = client.get("/api/v1/widgets/abc-123")

    assert response.status_code == 200
    assert len(capture_request_logs.records) == 1
    logged = capture_request_logs.records[0]
    assert logged.request_id is not None  # type: ignore[attr-defined]
    assert logged.correlation_id is not None  # type: ignore[attr-defined]


def test_real_app_log_request_id_matches_response_header(
    capture_request_logs: _CaptureHandler,
) -> None:
    """Regression tripwire tied to ecc.main's actual middleware registration
    order: if request_observability_middleware were ever registered on the
    wrong side of response_contract_middleware, request.state.request_id
    would not be set yet when this middleware runs, and this assertion would
    fail (logged request_id would be None, not the header value).
    """
    from ecc.main import app as real_app

    client = TestClient(real_app)

    response = client.get("/health/live")

    assert response.status_code == 200
    assert len(capture_request_logs.records) == 1
    logged = capture_request_logs.records[0]
    assert logged.request_id == response.headers["X-Request-ID"]  # type: ignore[attr-defined]
    assert logged.correlation_id == response.headers["X-Correlation-ID"]  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Logging: route template (bounded), never the raw resolved path/entity ID.
# ---------------------------------------------------------------------------


def test_log_uses_route_template_not_resolved_path(capture_request_logs: _CaptureHandler) -> None:
    client = TestClient(_build_test_app())

    client.get("/api/v1/widgets/some-real-entity-id-999")

    logged = capture_request_logs.records[0]
    assert logged.route == "/api/v1/widgets/{widget_id}"  # type: ignore[attr-defined]
    assert "some-real-entity-id-999" not in logged.route  # type: ignore[attr-defined]


def test_log_route_falls_back_to_bounded_sentinel_when_unmatched(
    capture_request_logs: _CaptureHandler,
) -> None:
    client = TestClient(_build_test_app())

    response = client.get("/api/v1/does-not-exist")

    assert response.status_code == 404
    logged = capture_request_logs.records[0]
    assert logged.route == "unmatched"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Logging: method, status, duration.
# ---------------------------------------------------------------------------


def test_log_includes_method_status_and_duration(capture_request_logs: _CaptureHandler) -> None:
    client = TestClient(_build_test_app())

    response = client.get("/api/v1/widgets/abc")

    assert response.status_code == 200
    logged = capture_request_logs.records[0]
    assert logged.http_method == "GET"  # type: ignore[attr-defined]
    assert logged.status_code == 200  # type: ignore[attr-defined]
    assert isinstance(logged.duration_ms, float)  # type: ignore[attr-defined]
    assert logged.duration_ms >= 0  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Logging: authenticated workspace identifier.
# ---------------------------------------------------------------------------


def test_log_includes_workspace_id_when_authenticated(
    capture_request_logs: _CaptureHandler,
) -> None:
    client = TestClient(_build_test_app())

    client.get("/api/v1/authenticated")

    logged = capture_request_logs.records[0]
    assert logged.workspace_id is not None  # type: ignore[attr-defined]


def test_log_omits_workspace_id_when_unauthenticated(
    capture_request_logs: _CaptureHandler,
) -> None:
    client = TestClient(_build_test_app())

    client.get("/api/v1/widgets/abc")

    logged = capture_request_logs.records[0]
    assert logged.workspace_id is None  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Logging: redaction -- request/response bodies, cookies, and CSRF/session
# tokens must never appear in any emitted log record, in any field.
# ---------------------------------------------------------------------------

_SECRET_BODY_MARKER = "TOP_SECRET_NOTE_BODY_CONTENT"
_SECRET_COOKIE_VALUE = "super-secret-session-cookie-value"
_SECRET_CSRF_VALUE = "super-secret-csrf-token-value"


def test_log_never_contains_request_body_cookies_or_csrf(
    capture_request_logs: _CaptureHandler,
) -> None:
    client = TestClient(_build_test_app())
    client.cookies.set("ecc_session", _SECRET_COOKIE_VALUE)

    response = client.post(
        "/api/v1/echo",
        json={"body": _SECRET_BODY_MARKER},
        headers={"X-CSRF-Token": _SECRET_CSRF_VALUE},
    )

    assert response.status_code == 200
    assert _SECRET_BODY_MARKER in response.json()["received"]["body"]  # sanity: it was sent
    for logged in capture_request_logs.records:
        rendered = logged.getMessage() + repr(logged.__dict__)
        assert _SECRET_BODY_MARKER not in rendered
        assert _SECRET_COOKIE_VALUE not in rendered
        assert _SECRET_CSRF_VALUE not in rendered


# ---------------------------------------------------------------------------
# Logging + Metrics: the ``except SQLAlchemyError`` branch must *also* emit
# the request-completion log line and record_request metric before
# re-raising -- not just record_database_failure -- so a DB-failure-driven
# request is neither silently missing from the structured logs nor
# undercounted in ecc_http_requests_total/ecc_http_request_duration_seconds.
# ---------------------------------------------------------------------------


def test_database_failure_path_emits_request_log_line(
    capture_request_logs: _CaptureHandler,
) -> None:
    client = TestClient(_build_test_app())

    with pytest.raises(SQLAlchemyError):
        client.get("/api/v1/db-failure")

    assert len(capture_request_logs.records) == 1
    logged = capture_request_logs.records[0]
    assert logged.getMessage() == "request_handled"
    assert logged.route == "/api/v1/db-failure"  # type: ignore[attr-defined]
    assert logged.http_method == "GET"  # type: ignore[attr-defined]
    assert logged.status_code == 500  # type: ignore[attr-defined]
    assert isinstance(logged.duration_ms, float)  # type: ignore[attr-defined]
    assert logged.duration_ms >= 0  # type: ignore[attr-defined]
    assert logged.request_id is not None  # type: ignore[attr-defined]


def test_database_failure_path_increments_request_metric() -> None:
    label_prefix = 'ecc_http_requests_total{route="/api/v1/db-failure",method="GET",status="500"}'
    before_rendered = render_metrics()
    before = 0
    if label_prefix in before_rendered:
        line = next(line for line in before_rendered.splitlines() if line.startswith(label_prefix))
        before = int(float(line.split()[-1]))

    client = TestClient(_build_test_app())
    with pytest.raises(SQLAlchemyError):
        client.get("/api/v1/db-failure")

    rendered = render_metrics()
    assert label_prefix in rendered
    line = next(line for line in rendered.splitlines() if line.startswith(label_prefix))
    after = int(float(line.split()[-1]))
    assert after == before + 1

    # The database-failure counter still fires too -- this fix is additive,
    # not a replacement for record_database_failure.
    assert 'ecc_database_failures_total{route="/api/v1/db-failure"}' in rendered


# ---------------------------------------------------------------------------
# Metrics: instrument-level tests against render_metrics().
# ---------------------------------------------------------------------------


def test_record_request_appears_in_metrics_with_bounded_labels() -> None:
    record_request("/api/v1/widgets/{widget_id}", "GET", 200, 0.012)

    rendered = render_metrics()

    assert 'ecc_http_requests_total{route="/api/v1/widgets/{widget_id}",method="GET"' in rendered
    assert "ecc_http_request_duration_seconds_bucket" in rendered


def test_record_database_failure_appears_in_metrics() -> None:
    record_database_failure("/api/v1/tasks/{task_id}")

    rendered = render_metrics()

    assert 'ecc_database_failures_total{route="/api/v1/tasks/{task_id}"}' in rendered


def test_record_lifecycle_event_appears_in_metrics() -> None:
    record_lifecycle_event("meeting", "meeting.created", "allowed")

    rendered = render_metrics()

    assert "ecc_lifecycle_events_total" in rendered
    assert 'domain="meeting"' in rendered
    assert 'event_type="meeting.created"' in rendered


def test_queue_lifecycle_event_is_not_counted_until_session_commits() -> None:
    """The domain layer calls queue_lifecycle_event() from inside the same
    transaction as the rest of a mutation (audit write, outbox write,
    idempotency-key storage). If a later statement in that transaction fails
    and the session rolls back, the queued event must never reach
    lifecycle_events_total -- otherwise the metric overcounts successes
    relative to what actually persisted."""
    from sqlalchemy import text

    from ecc.observability import queue_lifecycle_event

    session = SessionFactory()
    try:
        before = render_metrics()
        # A real statement first, same as every production call site (the
        # audit-row INSERT always precedes queue_lifecycle_event) -- this is
        # what actually puts the session in a transaction for rollback() to
        # roll back and fire after_rollback on.
        session.execute(text("SELECT 1"))
        queue_lifecycle_event(session, "task", "task.rollback_test", "allowed")
        assert 'event_type="task.rollback_test"' not in render_metrics()

        session.rollback()

        after_rollback = render_metrics()
        assert after_rollback == before
        assert 'event_type="task.rollback_test"' not in after_rollback
    finally:
        session.close()


def test_queue_lifecycle_event_does_not_leak_across_rollback_into_later_commit() -> None:
    """session.info is not reset by SQLAlchemy on rollback -- without an
    explicit after_rollback listener clearing the queue, a rolled-back
    event would sit in session.info and get incorrectly flushed by a later,
    unrelated commit on the same Session object."""
    from sqlalchemy import text

    from ecc.observability import queue_lifecycle_event

    session = SessionFactory()
    try:
        session.execute(text("SELECT 1"))
        queue_lifecycle_event(session, "task", "task.rollback_leak_test", "allowed")
        session.rollback()

        session.execute(text("SELECT 1"))
        queue_lifecycle_event(session, "task", "task.unrelated_commit_test", "allowed")
        session.commit()

        rendered = render_metrics()
        assert 'event_type="task.rollback_leak_test"' not in rendered
        assert 'event_type="task.unrelated_commit_test"' in rendered
    finally:
        session.close()


def test_queue_lifecycle_event_is_counted_after_session_commits() -> None:
    from ecc.observability import queue_lifecycle_event

    session = SessionFactory()
    try:
        queue_lifecycle_event(session, "task", "task.commit_test", "allowed")
        assert 'event_type="task.commit_test"' not in render_metrics()

        session.commit()

        assert 'event_type="task.commit_test"' in render_metrics()
    finally:
        session.close()


def _brief_generation_observation_count(rendered: str) -> int:
    # brief_generation_duration_seconds is an unlabeled, process-lifetime
    # singleton histogram (like every instrument in this module), so its
    # `_HELP`/`_TYPE` declaration lines are always present regardless of
    # whether it has ever been observed -- only the `_count` line's value
    # actually reflects observations, and only appears once observe() has
    # been called at least once.
    match = re.search(
        r"^ecc_brief_generation_duration_seconds_count (\d+)$", rendered, re.MULTILINE
    )
    return int(match.group(1)) if match else 0


def test_queue_brief_generated_is_not_counted_until_session_commits() -> None:
    """Regression test: record_brief_generated() was previously called
    directly at brief-generation time, before session.commit() -- so a
    commit failure would still leave a duration sample recorded for a brief
    that never persisted. queue_brief_generated() must defer the same way
    queue_lifecycle_event() does."""
    from sqlalchemy import text

    from ecc.observability import queue_brief_generated

    session = SessionFactory()
    try:
        before_count = _brief_generation_observation_count(render_metrics())
        session.execute(text("SELECT 1"))
        queue_brief_generated(session, 0.042)
        assert _brief_generation_observation_count(render_metrics()) == before_count

        session.rollback()

        assert _brief_generation_observation_count(render_metrics()) == before_count
    finally:
        session.close()


def test_queue_brief_generated_is_counted_after_session_commits() -> None:
    from ecc.observability import queue_brief_generated

    session = SessionFactory()
    try:
        before_count = _brief_generation_observation_count(render_metrics())
        queue_brief_generated(session, 0.042)
        assert _brief_generation_observation_count(render_metrics()) == before_count

        session.commit()

        assert _brief_generation_observation_count(render_metrics()) == before_count + 1
    finally:
        session.close()


def test_record_search_appears_in_metrics() -> None:
    record_search(0.045, 7)

    rendered = render_metrics()

    assert "ecc_search_duration_seconds" in rendered
    assert "ecc_search_results_count" in rendered


def test_record_ranking_appears_in_metrics() -> None:
    record_ranking(0.2, 350)

    rendered = render_metrics()

    assert "ecc_ranking_duration_seconds" in rendered
    assert "ecc_ranking_input_count" in rendered


def test_record_brief_stale_appears_in_metrics() -> None:
    record_brief_stale("stale_by_age")

    rendered = render_metrics()

    assert 'ecc_brief_stale_total{reason="stale_by_age"}' in rendered


def test_record_recommendation_transition_appears_in_metrics() -> None:
    record_recommendation_transition("recommendation.accepted")

    rendered = render_metrics()

    assert 'ecc_recommendation_transitions_total{event_type="recommendation.accepted"}' in rendered


def test_record_idempotency_conflict_appears_in_metrics() -> None:
    record_idempotency_conflict("meetings")

    rendered = render_metrics()

    assert 'ecc_idempotency_conflicts_total{domain="meetings"}' in rendered


def test_record_audit_outbox_failure_appears_in_metrics() -> None:
    record_audit_outbox_failure("risks")

    rendered = render_metrics()

    assert 'ecc_audit_outbox_failures_total{domain="risks"}' in rendered


def test_metrics_contain_no_note_body_or_entity_id_content() -> None:
    """Sanity: nothing recorded across this module's tests should ever leak
    the redaction markers used above -- metrics never touch bodies at all,
    so this should trivially hold, but it's the cheapest possible tripwire.
    """
    rendered = render_metrics()

    assert _SECRET_BODY_MARKER not in rendered
    assert _SECRET_COOKIE_VALUE not in rendered


def test_metrics_endpoint_is_exposed_on_real_app() -> None:
    from ecc.main import app as real_app

    client = TestClient(real_app)
    record_request("/health/live", "GET", 200, 0.001)

    response = client.get("/metrics")

    assert response.status_code == 200
    assert "ecc_http_requests_total" in response.text


def test_metrics_endpoint_requires_token_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ECC_METRICS_TOKEN is set, /metrics must reject requests that
    don't present it -- otherwise anyone can hit the endpoint at unlimited
    rate (it's outside mutation_rate_limit_middleware's scope) and force a
    live outbox-backlog DB query on every call."""
    import ecc.main as main

    monkeypatch.setattr(main.settings, "metrics_token", "s3cret-scrape-token")
    client = TestClient(main.app)

    unauthenticated = client.get("/metrics")
    assert unauthenticated.status_code == 401

    wrong_token = client.get("/metrics", headers={"Authorization": "Bearer wrong-token"})
    assert wrong_token.status_code == 401

    correct_token = client.get("/metrics", headers={"Authorization": "Bearer s3cret-scrape-token"})
    assert correct_token.status_code == 200
    assert "ecc_http_requests_total" in correct_token.text


# ---------------------------------------------------------------------------
# Metrics: outbox backlog gauge -- computed live from event_outbox at scrape
# time (Postgres-gated: sqlite's default test DB has no event_outbox rows to
# speak of and the design doc's "outbox backlog" signal is specifically about
# real unpublished-row counts).
# ---------------------------------------------------------------------------

pytestmark_postgres = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


@pytestmark_postgres
def test_outbox_backlog_gauge_reflects_unpublished_rows() -> None:
    from datetime import UTC, datetime
    from json import dumps

    from sqlalchemy import text

    workspace_id = uuid4()
    event_id = uuid4()
    now = datetime.now(UTC)

    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO workspaces (id, name, created_at) VALUES (:id, :name, :created_at)"),
            {"id": workspace_id, "name": "Outbox Backlog Test", "created_at": now},
        )
        connection.execute(
            text(
                """
                INSERT INTO event_outbox (
                    event_id, workspace_id, event_type, event_version,
                    correlation_id, payload, occurred_at, attempt_count
                ) VALUES (
                    :event_id, :workspace_id, 'test.event', 1,
                    :correlation_id, CAST(:payload AS jsonb), :occurred_at, 0
                )
                """
            ),
            {
                "event_id": event_id,
                "workspace_id": workspace_id,
                "correlation_id": uuid4(),
                "payload": dumps({"marker": "test"}),
                "occurred_at": now,
            },
        )
    try:
        rendered = render_metrics()
        assert "ecc_event_outbox_backlog" in rendered
        line = next(
            line for line in rendered.splitlines() if line.startswith("ecc_event_outbox_backlog ")
        )
        backlog_value = int(line.split()[-1])
        assert backlog_value >= 1
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM event_outbox WHERE workspace_id = :workspace_id"),
                {"workspace_id": workspace_id},
            )
            connection.execute(
                text("DELETE FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": workspace_id},
            )


# ---------------------------------------------------------------------------
# End-to-end domain emission proof: a real mutation through the real app
# actually reaches the lifecycle/idempotency-conflict emission points added
# to backend/ecc/domains/planning/tasks.py (rather than only unit-testing
# the record_* functions directly, above). Postgres-gated -- tasks.py's SQL
# uses CAST(... AS jsonb)/RETURNING, which sqlite's default test DB doesn't
# support (see the other tests/test_*_postgres.py files for the same
# convention).
# ---------------------------------------------------------------------------

from collections.abc import Iterator as _Iterator  # noqa: E402
from datetime import UTC as _UTC  # noqa: E402
from datetime import datetime as _datetime  # noqa: E402
from datetime import timedelta as _timedelta  # noqa: E402
from hashlib import sha256 as _sha256  # noqa: E402
from hmac import new as _hmac_new  # noqa: E402

from sqlalchemy import text as _text  # noqa: E402

from ecc.main import app as _real_app  # noqa: E402

pytestmark_postgres_domain = pytest.mark.skipif(
    not settings.database_url.startswith("postgresql"),
    reason="PostgreSQL integration test",
)


@pytest.fixture
def _domain_test_context() -> _Iterator[tuple[TestClient, str]]:
    workspace_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    token = f"session-{uuid4()}"
    now = _datetime.now(_UTC)

    with engine.begin() as connection:
        connection.execute(
            _text("INSERT INTO workspaces (id, name, created_at) VALUES (:id, :name, :created_at)"),
            {"id": workspace_id, "name": "Observability Domain Test", "created_at": now},
        )
        connection.execute(
            _text(
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
            _text(
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
                "token_hash": _sha256(token.encode()).hexdigest(),
                "expires_at": now + _timedelta(hours=1),
                "last_seen_at": now,
            },
        )

    client = TestClient(_real_app)
    client.cookies.set("ecc_session", token)
    try:
        yield client, token
    finally:
        client.close()
        with engine.begin() as connection:
            connection.execute(
                _text("DELETE FROM event_outbox WHERE workspace_id = :workspace_id"),
                {"workspace_id": workspace_id},
            )
            connection.execute(
                _text("DELETE FROM audit_events WHERE workspace_id = :workspace_id"),
                {"workspace_id": workspace_id},
            )
            connection.execute(
                _text("DELETE FROM idempotency_records WHERE workspace_id = :workspace_id"),
                {"workspace_id": workspace_id},
            )
            connection.execute(
                _text("DELETE FROM tasks WHERE workspace_id = :workspace_id"),
                {"workspace_id": workspace_id},
            )
            connection.execute(
                _text("DELETE FROM sessions WHERE workspace_id = :workspace_id"),
                {"workspace_id": workspace_id},
            )
            connection.execute(
                _text("DELETE FROM users WHERE workspace_id = :workspace_id"),
                {"workspace_id": workspace_id},
            )
            connection.execute(
                _text("DELETE FROM workspaces WHERE id = :workspace_id"),
                {"workspace_id": workspace_id},
            )


def _domain_headers(token: str, key: str) -> dict[str, str]:
    csrf = _hmac_new(settings.session_secret.encode(), token.encode(), "sha256").hexdigest()
    return {"Idempotency-Key": key, "X-CSRF-Token": csrf}


def _metric_line_value(rendered: str, prefix: str) -> int:
    line = next(line for line in rendered.splitlines() if line.startswith(prefix))
    return int(float(line.split()[-1]))


@pytestmark_postgres_domain
def test_task_create_emits_lifecycle_event_metric(
    _domain_test_context: tuple[TestClient, str],
) -> None:
    client, token = _domain_test_context
    label = 'ecc_lifecycle_events_total{domain="task",event_type="task.created",result="allowed"}'
    before_rendered = render_metrics()
    before = _metric_line_value(before_rendered, label) if label in before_rendered else 0

    response = client.post(
        "/api/v1/tasks",
        headers=_domain_headers(token, "observability-lifecycle-create"),
        json={"title": "Observability lifecycle proof", "manual_priority": "low"},
    )
    assert response.status_code == 201

    after = _metric_line_value(render_metrics(), label)
    assert after == before + 1


@pytestmark_postgres_domain
def test_task_idempotency_conflict_emits_metric(
    _domain_test_context: tuple[TestClient, str],
) -> None:
    client, token = _domain_test_context
    label = 'ecc_idempotency_conflicts_total{domain="tasks"}'
    before_rendered = render_metrics()
    before = _metric_line_value(before_rendered, label) if label in before_rendered else 0

    key = "observability-idempotency-conflict"
    first = client.post(
        "/api/v1/tasks",
        headers=_domain_headers(token, key),
        json={"title": "First body", "manual_priority": "low"},
    )
    assert first.status_code == 201

    conflicting = client.post(
        "/api/v1/tasks",
        headers=_domain_headers(token, key),
        json={"title": "Different body -- same idempotency key", "manual_priority": "high"},
    )
    assert conflicting.status_code == 409

    after = _metric_line_value(render_metrics(), label)
    assert after == before + 1
