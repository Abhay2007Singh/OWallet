"""
app/middleware/security_headers.py

SecurityHeadersMiddleware — adds security headers and X-Request-ID to every
HTTP response. Stores the request_id on request.state for downstream middleware.

Headers set on every response:
  X-Request-ID            — UUID per request for log correlation and support triage.
                            If the client sends this header we preserve it (useful for
                            end-to-end tracing across microservices).
  X-Content-Type-Options  — "nosniff" prevents browsers from MIME-sniffing the response
                            away from the declared Content-Type (stops content-type
                            confusion attacks).
  X-Frame-Options         — "DENY" blocks the page from being embedded in an <iframe>
                            anywhere, preventing clickjacking attacks.
  X-XSS-Protection        — "0" disables the browser's legacy XSS filter. Modern browsers
                            use CSP; the old filter can itself introduce XSS vectors when
                            enabled.
  Referrer-Policy         — "strict-origin-when-cross-origin" sends the full URL as Referer
                            for same-origin requests but only the origin for cross-origin
                            ones, avoiding leaking paths to third parties.
  Cache-Control           — "no-store" prevents any cache (browser, CDN, proxy) from
                            storing API responses that may contain sensitive financial data.

Execution position (middleware is LIFO — last added = outermost):
  SlowAPI (outermost) → SecurityHeaders → RequestLogging → CORS → App (innermost)

SecurityHeaders runs before RequestLogging, so request.state.request_id is set
by the time RequestLoggingMiddleware reads it.
"""

import uuid

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(self, request: Request, call_next) -> Response:
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        request.state.request_id = request_id

        response = await call_next(request)

        response.headers["X-Request-ID"] = request_id
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "0"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Cache-Control"] = "no-store"

        return response
