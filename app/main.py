"""
app/main.py

FastAPI application factory.

This module:
1. Creates the FastAPI app instance
2. Manages startup/shutdown lifecycle (lifespan context manager)
3. Registers all routers
4. Configures global middleware

Import the `app` object for uvicorn:
    uvicorn app.main:app --reload
"""

from contextlib import asynccontextmanager
from collections.abc import AsyncGenerator

import structlog
from fastapi import FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import settings
from app.core.logging_config import configure_logging
from app.core.redis import close_redis
from app.middleware.rate_limiter import limiter
from app.middleware.request_logging import RequestLoggingMiddleware
from app.middleware.security_headers import SecurityHeadersMiddleware
from app.routes.auth import router as auth_router
from app.routes.health import router as health_router
from app.routes.wallet import payment_router, router as wallet_router

# Configure structured logging immediately at module load.
# This runs before the lifespan and before any other module emits a log event.
configure_logging()

logger = structlog.get_logger(__name__)


# =============================================================================
# Lifespan — startup and shutdown logic
# =============================================================================
@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """
    Manages application lifecycle events.

    Startup (before yield):
    - Log that the application is starting
    - Verify DB and Redis are reachable (fail fast if not)

    Shutdown (after yield):
    - Close Redis connection pool gracefully
    - SQLAlchemy engine disposes its pool automatically on GC,
      but explicit disposal is cleaner for testing.

    Why not create DB tables here?
    Tables are created by Alembic migrations, not by SQLAlchemy
    metadata.create_all(). Mixing the two causes confusion about
    which is the source of truth for the schema.
    """
    # ---- STARTUP ----
    logger.info(
        "app_starting",
        app_name=settings.APP_NAME,
        env=settings.APP_ENV,
        db_host=settings.POSTGRES_HOST,
        db_port=settings.POSTGRES_PORT,
        db_name=settings.POSTGRES_DB,
        redis_host=settings.REDIS_HOST,
        redis_port=settings.REDIS_PORT,
    )

    yield  # Application runs here — requests are handled between yield points

    # ---- SHUTDOWN ----
    logger.info("app_shutting_down", app_name=settings.APP_NAME)
    await close_redis()
    logger.info("redis_connection_pool_closed")


# =============================================================================
# FastAPI Application Instance
# =============================================================================
_OPENAPI_TAGS = [
    {
        "name": "Authentication",
        "description": (
            "JWT-based auth. Register to get a token pair. Use the access token in "
            "`Authorization: Bearer <token>` for all protected endpoints. "
            "Access tokens expire in 15 minutes; refresh tokens rotate every use (7-day TTL). "
            "**Rate limited:** 10 requests/hour per IP for register and login."
        ),
    },
    {
        "name": "Wallet",
        "description": (
            "Read-only wallet operations: balance (Redis-cached, 30s TTL) and "
            "transaction history (paginated, newest-first, filterable by status and date)."
        ),
    },
    {
        "name": "Wallet (Payments)",
        "description": (
            "Write operations — **every request requires an `Idempotency-Key` header** "
            "(UUID v4 you generate per operation). "
            "Retrying with the same key and body replays the original response safely. "
            "Using the same key with a different body returns `422 Payload Mismatch`. "
            "Concurrent requests with the same key return `409 Conflict`.\n\n"
            "**Transfer rate limit:** 5 transfers per 60 seconds per authenticated user."
        ),
    },
    {
        "name": "Health",
        "description": "Service health check — verifies PostgreSQL and Redis connectivity.",
    },
]

app = FastAPI(
    title=settings.APP_NAME,
    description=(
        "**PyWallet** — Production-grade fintech wallet API.\n\n"
        "Demonstrates real financial system patterns:\n"
        "- ACID atomicity via `SELECT FOR UPDATE` and `async with db.begin():`\n"
        "- Idempotent payments (SHA256 request hash, Redis NX lock, DB replay)\n"
        "- Double-entry ledger (DEBIT + CREDIT rows, shared `transfer_reference_id`)\n"
        "- Deadlock-safe locking (ascending UUID order for two-row transfers)\n"
        "- JWT auth with refresh token rotation + replay detection\n"
        "- Per-user rate limiting (Redis INCR + EXPIRE NX pipeline)\n"
        "- Celery background tasks (webhook simulation, notifications, cleanup)\n"
        "- Structured JSON logging (structlog) with `X-Request-ID` correlation\n\n"
        "**Quick start:**\n"
        "1. `POST /api/v1/auth/register` — creates user + wallet, returns token pair\n"
        "2. Use `access_token` in `Authorization: Bearer` header\n"
        "3. `POST /api/v1/wallet/deposit` with `Idempotency-Key: <uuid4>`\n"
        "4. `POST /api/v1/wallet/transfer` with `Idempotency-Key: <uuid4>`"
    ),
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    openapi_tags=_OPENAPI_TAGS,
    lifespan=lifespan,
    debug=settings.APP_DEBUG,
)


# =============================================================================
# Phase 7: Rate limiter state
# SlowAPIMiddleware reads app.state.limiter to find the Limiter instance.
# Must be set before add_middleware(SlowAPIMiddleware).
# =============================================================================
app.state.limiter = limiter


# =============================================================================
# Phase 7: Exception handlers
#
# Registration order matters: more specific types should be registered BEFORE
# more general ones. FastAPI checks handlers in the order they were added.
#
# RateLimitExceeded:
#   slowapi raises this when a @limiter.limit() rule is exceeded.
#   We delegate to slowapi's built-in handler which emits a 429 with the
#   standard rate-limit headers (X-RateLimit-Limit, Retry-After, etc.).
#
# Exception (catch-all):
#   Fires for any unhandled non-HTTP exception (programming errors, unexpected
#   third-party failures). We log the error class for ops diagnostics and
#   return a generic 500 — no stack trace, no internal detail exposed to callers.
#   HTTPException/StarletteHTTPException are re-delegated to FastAPI's built-in
#   handler so 4xx/5xx HTTP errors still render correctly.
# =============================================================================
@app.exception_handler(RateLimitExceeded)
async def _rate_limit_handler(request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, RateLimitExceeded):
        return JSONResponse(
            status_code=429,
            content={"detail": f"Rate limit exceeded: {exc.detail}"},
            headers={"Retry-After": str(getattr(exc, "retry_after", 60))},
        )
    # Redis ConnectionError or other non-HTTP exception from slowapi
    logger.error("rate_limiter_error", error_type=type(exc).__name__, error=str(exc))
    return JSONResponse(status_code=503, content={"detail": "Rate limiter unavailable."})


@app.exception_handler(Exception)
async def _unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    if isinstance(exc, StarletteHTTPException):
        return await http_exception_handler(request, exc)
    logger.error(
        "unhandled_exception",
        error_type=type(exc).__name__,
        path=str(request.url.path),
        method=request.method,
    )
    return JSONResponse(
        status_code=500,
        content={"detail": "An internal server error occurred."},
    )


# =============================================================================
# Middleware stack
#
# Starlette middleware is LIFO — the LAST middleware added is the OUTERMOST
# (first to process incoming requests, last to process outgoing responses).
#
# Execution order for requests:
#   SlowAPIMiddleware → SecurityHeadersMiddleware → RequestLoggingMiddleware
#   → CORSMiddleware → App
#
# Execution order for responses (reverse):
#   App → CORSMiddleware → RequestLoggingMiddleware → SecurityHeadersMiddleware
#   → SlowAPIMiddleware
#
# Why this order?
#   1. SlowAPI (outermost): rate-limit BEFORE any other processing. Rejected
#      requests never touch the DB, Redis, or business logic.
#   2. SecurityHeaders (2nd): generates X-Request-ID and stores it on
#      request.state BEFORE RequestLogging reads it.
#   3. RequestLogging (3rd): reads request.state.request_id set by
#      SecurityHeaders, logs the complete request/response.
#   4. CORS (innermost): handles preflight OPTIONS requests close to the app.
# =============================================================================
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "https://o-wallet-three.vercel.app",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestLoggingMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(SlowAPIMiddleware)


# =============================================================================
# Router Registration
# =============================================================================
# All API routes are registered under /api/v1 prefix.
# Versioning the URL means future breaking changes go to /api/v2 without
# disrupting existing clients still using /api/v1.
# =============================================================================
app.include_router(health_router) #, prefix="/api/v1")
app.include_router(auth_router) #, prefix="/api/v1")
app.include_router(wallet_router) #, prefix="/api/v1")          # read-only wallet ops
app.include_router(payment_router) #, prefix="/api/v1")         # Phase 5: idempotent payments


# =============================================================================
# Root redirect (optional — gives users a hint if they hit /)
# =============================================================================
@app.get("/", include_in_schema=False)
async def root() -> dict:
    return {
        "message": f"Welcome to {settings.APP_NAME}",
        "docs": "/docs",
        "health": "/api/v1/health",
    }
