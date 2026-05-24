"""
app/routes/health.py

Health check endpoint — GET /health

Purpose:
- Docker Compose, Kubernetes, and load balancers call this to know if the
  service is alive and ready to accept traffic.
- A shallow ping (/health) just checks the process is running.
- A deep check (/health/detailed) verifies DB and Redis connectivity.

Two levels:
  GET /health          → shallow (process alive, always fast)
  GET /health/detailed → deep (DB + Redis connection verified)
"""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.redis import get_redis

router = APIRouter(prefix="/health", tags=["Health"])


@router.get(
    "",
    summary="Shallow health check",
    response_description="Service is alive",
)
async def health_check() -> dict:
    """
    Returns 200 OK immediately if the FastAPI process is running.
    No DB or Redis calls — this should never fail unless the process is dead.
    Used by load balancers for liveness probes.
    """
    return {
        "status": "ok",
        "service": "PyWallet API",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get(
    "/detailed",
    summary="Deep health check (DB + Redis)",
    response_description="Connectivity status for all dependencies",
)
async def detailed_health_check(
    db: AsyncSession = Depends(get_db),
    redis: Redis = Depends(get_redis),
) -> dict:
    """
    Verifies the API can connect to both PostgreSQL and Redis.
    Used by readiness probes — if this fails, the service should not
    receive traffic, because it can't process requests correctly.

    Returns 503 if any dependency is unreachable.
    """
    health: dict = {
        "status": "ok",
        "service": "PyWallet API",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "dependencies": {},
    }

    # -------------------------------------------------------------------------
    # Check PostgreSQL connectivity
    # text("SELECT 1") is the canonical lightweight DB ping.
    # We wrap in try/except so a DB failure returns 503 instead of 500.
    # -------------------------------------------------------------------------
    try:
        await db.execute(text("SELECT 1"))
        health["dependencies"]["postgres"] = {"status": "ok"}
    except Exception as exc:
        health["status"] = "degraded"
        health["dependencies"]["postgres"] = {
            "status": "error",
            "detail": str(exc),
        }

    # -------------------------------------------------------------------------
    # Check Redis connectivity
    # redis.ping() sends PING and expects PONG — the Redis heartbeat command.
    # -------------------------------------------------------------------------
    try:
        pong = await redis.ping()
        health["dependencies"]["redis"] = {
            "status": "ok" if pong else "error",
        }
    except Exception as exc:
        health["status"] = "degraded"
        health["dependencies"]["redis"] = {
            "status": "error",
            "detail": str(exc),
        }

    # -------------------------------------------------------------------------
    # Return 503 if any dependency failed — not 200.
    # A 200 with "status: degraded" in the body would fool health check systems.
    # -------------------------------------------------------------------------
    if health["status"] != "ok":
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=health,
        )

    return health
