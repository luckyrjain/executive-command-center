from collections.abc import Awaitable, Callable
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ecc.audit import rejected_mutation_audit_middleware
from ecc.config import get_settings
from ecc.database import engine
from ecc.domains.planning.tasks import router as tasks_router
from ecc.logging import configure_logging

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
app.middleware("http")(rejected_mutation_audit_middleware)


@app.middleware("http")
async def correlation_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    correlation_id = request.headers.get("X-Correlation-ID", str(uuid4()))
    request.state.correlation_id = correlation_id
    response = await call_next(request)
    response.headers["X-Correlation-ID"] = correlation_id
    return response


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
