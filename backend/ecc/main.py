from collections.abc import Awaitable, Callable
from hmac import compare_digest
from json import JSONDecodeError, loads
from uuid import UUID, uuid4

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ecc.audit import rejected_mutation_audit_middleware
from ecc.config import get_settings, validate_production_settings
from ecc.database import engine
from ecc.dev_bootstrap import router as dev_bootstrap_router
from ecc.domains.calendar.events import router as calendar_events_router
from ecc.domains.communication.commitments import router as commitments_router
from ecc.domains.governance.attention import router as attention_router
from ecc.domains.governance.recommendation_mutations import (
    router as recommendation_mutations_router,
)
from ecc.domains.governance.recommendation_queries import router as recommendation_queries_router
from ecc.domains.governance.risk_mutations import router as risk_mutations_router
from ecc.domains.governance.risks import router as risks_router
from ecc.domains.identity.person_organizations import router as identity_router
from ecc.domains.knowledge.claims import router as knowledge_claims_router
from ecc.domains.knowledge.entities import router as knowledge_entities_router
from ecc.domains.knowledge.entities_mutations import (
    router as knowledge_entities_mutations_router,
)
from ecc.domains.knowledge.evidence import router as evidence_router
from ecc.domains.knowledge.notes import router as notes_router
from ecc.domains.planning.tasks import router as tasks_router
from ecc.domains.platform.audit_queries import router as audit_queries_router
from ecc.domains.platform.dashboard_briefs import router as dashboard_briefs_router
from ecc.domains.scheduling.meetings import router as meetings_router
from ecc.http_security import (
    MaxBodySizeMiddleware,
    mutation_rate_limit_middleware,
    security_headers_middleware,
)
from ecc.logging import configure_logging
from ecc.observability import render_metrics, request_observability_middleware
from ecc.search import router as search_router

configure_logging()
settings = get_settings()
validate_production_settings(settings)
app = FastAPI(title="Executive Command Center", version="0.2.0")
# The dev-bootstrap router is only ever functional in development (each of
# its routes calls _require_development() and 404s otherwise) -- but
# registering it unconditionally still makes it discoverable outside
# development: a malformed request to its POST route would 422 (Pydantic
# body validation, which runs before the handler's own environment check)
# instead of 404, leaking that the path exists. Not registering it at all
# outside development closes that gap and is also the concrete fix behind
# "insecure production cookies", since dev_bootstrap.py is the only
# cookie-issuing code in the app. See tests/test_production_security.py.
if settings.environment.casefold() == "development":
    app.include_router(dev_bootstrap_router)
app.include_router(tasks_router)
app.include_router(commitments_router)
app.include_router(notes_router)
app.include_router(calendar_events_router)
app.include_router(meetings_router)
app.include_router(risks_router)
app.include_router(risk_mutations_router)
app.include_router(attention_router)
app.include_router(recommendation_queries_router)
app.include_router(recommendation_mutations_router)
app.include_router(audit_queries_router)
app.include_router(evidence_router)
app.include_router(knowledge_entities_router)
app.include_router(knowledge_entities_mutations_router)
app.include_router(knowledge_claims_router)
app.include_router(identity_router)
app.include_router(search_router)
app.include_router(dashboard_briefs_router)
app.middleware("http")(rejected_mutation_audit_middleware)
# Pure-ASGI body size guard: registered via add_middleware (not the
# "http" dispatch helper) so it can intercept the raw receive() channel and
# reject an oversized body without ever buffering it. See http_security.py.
app.add_middleware(MaxBodySizeMiddleware)
app.middleware("http")(mutation_rate_limit_middleware)
# Registered here -- after mutation_rate_limit_middleware, before
# response_contract_middleware's own registration below -- so it ends up
# wrapped *inward* of response_contract_middleware (registered earlier in
# the file = wrapped more inward; see ecc.http_security's ordering comments
# and ecc.observability's module docstring for the full derivation).
# response_contract_middleware sets request.state.request_id/correlation_id
# *before* calling call_next, i.e. before delegating to this middleware, so
# by the time request_observability_middleware's own dispatch body runs,
# those IDs are already set -- it reuses them rather than generating a
# second, independent pair. Its own call_next in turn wraps
# mutation_rate_limit_middleware, MaxBodySizeMiddleware,
# rejected_mutation_audit_middleware, and the router itself, so the matched
# route template and any authenticated workspace identifier (set deep
# inside route handling) are available once that call_next returns too.
# See tests/test_observability.py's real-app regression test for the proof.
app.middleware("http")(request_observability_middleware)


def _request_uuid(raw: str | None) -> str:
    try:
        return str(UUID(raw)) if raw else str(uuid4())
    except ValueError:
        return str(uuid4())


def _error_payload(detail: object, request_id: str, correlation_id: str) -> dict[str, object]:
    if isinstance(detail, str):
        code = detail
        message = detail.replace("_", " ").title()
        details: object = {}
    elif isinstance(detail, dict):
        code = str(detail.get("code", "REQUEST_FAILED"))
        message = str(detail.get("message", code.replace("_", " ").title()))
        details = {key: value for key, value in detail.items() if key not in {"code", "message"}}
    else:
        code = "VALIDATION_ERROR"
        message = "Request validation failed"
        details = {"violations": detail}
    return {
        "error": {
            "code": code,
            "message": message,
            "request_id": request_id,
            "details": details,
        },
        "correlation_id": correlation_id,
    }


@app.middleware("http")
async def response_contract_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    request_id = str(uuid4())
    correlation_id = _request_uuid(request.headers.get("X-Correlation-ID"))
    request.state.request_id = request_id
    request.state.correlation_id = correlation_id
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = correlation_id
    response.headers["X-Request-ID"] = request_id

    if not request.url.path.startswith("/api/v1"):
        return response
    if "application/json" not in response.headers.get("content-type", ""):
        return response

    body_iterator = getattr(response, "body_iterator", None)
    if body_iterator is None:
        return response
    body = b"".join([chunk async for chunk in body_iterator])
    try:
        payload = loads(body)
    except (JSONDecodeError, UnicodeDecodeError):
        return Response(
            content=body,
            status_code=response.status_code,
            headers=dict(response.headers),
            media_type=response.media_type,
        )

    if response.status_code >= 400:
        detail = payload.get("detail") if isinstance(payload, dict) else payload
        payload = _error_payload(detail, request_id, correlation_id)
    elif isinstance(payload, dict):
        payload["request_id"] = request_id
        payload["correlation_id"] = correlation_id

    headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in {"content-length", "content-type"}
    }
    return JSONResponse(
        content=payload,
        status_code=response.status_code,
        headers=headers,
        background=response.background,
    )


# Registered before CORSMiddleware (below) so it is the second-outermost
# middleware layer (Starlette wraps most-recently-added first): every
# response -- including one produced by any other middleware above, e.g. a
# 413/429 -- gets the baseline security headers via response.headers.setdefault.
app.middleware("http")(security_headers_middleware)

# Registered *last*, so CORSMiddleware ends up outermost of all -- wrapping
# every response, including the ones the security middleware above
# short-circuits before ever calling `call_next` (MaxBodySizeMiddleware's
# fast-path 413, mutation_rate_limit_middleware's 429). Those responses are
# built by writing directly to the ASGI `send` channel / returning a fresh
# JSONResponse rather than going through `self._app`/`call_next`, so they
# never reach an inner CORSMiddleware's wrapped `send` -- the browser would
# see a response with no Access-Control-Allow-Origin/Vary headers and treat
# it as an opaque network/CORS failure instead of a readable 413/429. With
# CORSMiddleware outermost, it wraps `send` before delegating to the rest of
# the stack, so it sees and annotates every response regardless of which
# layer produced it. See tests/test_production_security.py's
# `_build_test_app(include_cors=True)` cases for the regression test.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=[
        "Content-Type",
        "X-CSRF-Token",
        "X-Correlation-ID",
        "Idempotency-Key",
    ],
)


@app.get("/health/live")
def live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/health/ready")
def ready() -> JSONResponse:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        return JSONResponse({"status": "ready"})
    except Exception:
        return JSONResponse({"status": "not_ready"}, status_code=503)


@app.get("/version")
def version() -> dict[str, str]:
    return {"service": "ecc-backend", "version": app.version}


@app.get("/metrics")
def metrics(request: Request) -> Response:
    # Internal Phase 1 operational endpoint (see ecc.observability): a
    # hand-rolled Prometheus text exposition, no request/response body,
    # cookie, CSRF, token, note, or evidence content -- counters/histograms
    # with bounded labels only. Not covered by mutation_rate_limit_middleware
    # (GET, and intentionally scrape-friendly), so it is protected instead by
    # an optional shared-secret token: if ECC_METRICS_TOKEN is configured,
    # every request must present it via `Authorization: Bearer <token>` or
    # get a 401 -- both to stop unauthenticated internet-wide scraping and,
    # since _outbox_backlog_count() below runs a live DB query per call
    # (bounded further by render_metrics' own short-lived cache), to bound
    # how cheaply that can be triggered. If ECC_METRICS_TOKEN is left unset,
    # the endpoint stays open (today's behavior) -- see
    # docs/runbooks/PHASE-1-DEPLOYMENT.md for why an operator must then
    # firewall this route from the public internet themselves.
    expected_token = settings.metrics_token
    if expected_token:
        authorization = request.headers.get("authorization", "")
        provided_token = (
            authorization.removeprefix("Bearer ") if authorization.startswith("Bearer ") else ""
        )
        if not provided_token or not compare_digest(provided_token, expected_token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return Response(content=render_metrics(), media_type="text/plain; version=0.0.4")
