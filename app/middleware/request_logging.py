"""
app/middleware/request_logging.py

RequestLoggingMiddleware — emits one structured log line per HTTP request.

Logged fields:
  event        — "http_request" (structlog event key)
  method       — HTTP verb: GET, POST, etc.
  path         — URL path WITHOUT query string (avoids logging tokens in params)
  status_code  — HTTP response status code
  duration_ms  — total wall-clock time for the request, in milliseconds
  ip           — client IP address (from request.client.host)
  request_id   — X-Request-ID set by SecurityHeadersMiddleware via request.state
  user_agent   — User-Agent header for client identification

Intentionally excluded from logs:
  - Query parameters: may contain auth tokens, search terms, PII
  - Request body: contains passwords, account numbers, amounts
  - Authorization header: never log credentials
  - Response body: may contain balances, transaction details

Execution position:
  SlowAPI → SecurityHeaders (sets request_id) → RequestLogging → CORS → App
  SecurityHeaders runs before this middleware, so request.state.request_id
  is always populated by the time dispatch() runs.
"""

import time

import structlog
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

logger = structlog.get_logger(__name__)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        start = time.monotonic()
        request_id = getattr(request.state, "request_id", "unknown")
        ip = request.client.host if request.client else "unknown"

        structlog.contextvars.bind_contextvars(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            ip=ip,
        )

        try:
            response = await call_next(request)
        except Exception:
            structlog.contextvars.clear_contextvars()
            raise

        duration_ms = round((time.monotonic() - start) * 1000, 2)

        logger.info(
            "http_request",
            status_code=response.status_code,
            duration_ms=duration_ms,
            user_agent=request.headers.get("User-Agent", ""),
        )

        structlog.contextvars.clear_contextvars()
        return response
