"""
app/core/redis.py

Async Redis client setup using the redis-py library.

Architecture:
  - A single connection pool is created at module import time.
  - get_redis() returns a client connected to that pool.
  - close_redis() is called during application shutdown to release resources.
"""

import redis.asyncio as aioredis
from redis.asyncio import Redis

from app.core.config import settings

# =============================================================================
# Redis Connection Pool
# =============================================================================
# A connection pool maintains multiple persistent TCP connections to Redis.
# Without a pool, every Redis call would open a new TCP connection —
# expensive and slow. The pool reuses connections across requests.
#
# decode_responses=True → Redis returns str instead of bytes.
#   This is almost always what you want in Python applications.
#   Without it, you'd need to .decode("utf-8") every Redis response.
# =============================================================================
redis_pool = aioredis.ConnectionPool.from_url(
    settings.REDIS_URL,
    decode_responses=True,
    max_connections=20,
)


def get_redis() -> Redis:
    """
    Returns an async Redis client connected to the shared pool.

    This is a synchronous factory (not async) — creating a client from
    a pool is instantaneous, no I/O needed.

    Usage as a FastAPI dependency:
        @router.get("/example")
        async def example(redis: Redis = Depends(get_redis)):
            await redis.set("key", "value", ex=60)
            value = await redis.get("key")
    """
    return aioredis.Redis(connection_pool=redis_pool)


def get_redis_client() -> Redis:
    """
    Return a Redis client from the shared pool for use OUTSIDE FastAPI DI.

    Identical to get_redis() — the distinction is naming convention only.
    Use this in:
      - Celery tasks (no FastAPI DI context)
      - Custom APIRoute handlers (get_route_handler pattern)
      - Any code that runs before or outside the request lifecycle

    Creating a Redis client from a pool is instantaneous (no I/O).
    Connections are acquired lazily from the pool on the first Redis command
    and returned automatically when the command completes.
    """
    return aioredis.Redis(connection_pool=redis_pool)


async def close_redis() -> None:
    """
    Gracefully close all Redis connections in the pool.
    Called in the FastAPI lifespan shutdown handler.
    """
    await redis_pool.disconnect()
