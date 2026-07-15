from collections.abc import Awaitable, Callable
from json import JSONDecodeError, loads
from uuid import UUID, uuid4

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ecc.audit import rejected_mutation_audit_middleware
from ecc.config import get_settings
from ecc.database import engine
from ecc.domains.calendar.events import router as calendar_events_router
from ecc.domains.communication.commitments import router as commitments_router
from ecc.domains.governance.attention import router as attention_router
from ecc.domains.governance.recommendation_mutations import router as recommendation_mutations_router
from ecc.domains.governance.recommendation_queries import router as recommendation_queries_router
from ecc.domains.governance.risk_mutations import router as risk_mutations_router
from ecc.domains.governance.risks import router as risks_router
from ecc.domains.knowledge.notes import router as notes_router
from ecc.domains.planning.tasks import router as tasks_router
from ecc.domains.platform.audit_queries import router as audit_queries_router
from ecc.domains.platform.dashboard_briefs import router as dashboard_briefs_router
from ecc.domains.scheduling.meetings import router as meetings_router
from ecc.logging import configure_logging
from ecc.search import router as search_router

configure_logging()
settings = get_settings()
app = FastAPI(title="Executive Command Center", version="0.2.0")
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
app.include_router(search_router)
app.include_router(dashboard_briefs_router)
app.middleware("http")(rejected_mutation_audit_middleware)


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
