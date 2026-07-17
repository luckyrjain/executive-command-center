import json
import logging
from datetime import UTC, datetime


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "service": "ecc-backend",
            "domain": getattr(record, "domain", "platform"),
            "correlation_id": getattr(record, "correlation_id", None),
            "request_id": getattr(record, "request_id", None),
            # Populated only on request-lifecycle log lines (see
            # ecc.observability.request_observability_middleware); None on
            # every other log line, same defaulting already used above.
            # Never a request/response body, cookie, CSRF value, token, note
            # body, or evidence payload -- route is a bounded route
            # *template*, not a resolved path.
            "route": getattr(record, "route", None),
            "http_method": getattr(record, "http_method", None),
            "status_code": getattr(record, "status_code", None),
            "duration_ms": getattr(record, "duration_ms", None),
            "workspace_id": getattr(record, "workspace_id", None),
            "message": record.getMessage(),
        }
        return json.dumps(payload, separators=(",", ":"))


def configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
