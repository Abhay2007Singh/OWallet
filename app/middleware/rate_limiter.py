"""
app/middleware/rate_limiter.py

Rate limiting configuration for PyWallet.

Two layers of rate limiting:

  1. Global per-IP (SlowAPI limiter)
     default_limits=["100/minute"] — covers ALL endpoints automatically.
     Protects against general API abuse and DDoS amplification.

  2. Per-user transfer limit (_transfer_rate_limit dependency)
     5 transfers per 60 seconds per authenticated user.
     Applied via Depends() on POST /wallet/transfer.

     Why not use @limiter.limit() on the transfer route?
     The payment_router uses route_class=IdempotentRoute, which wraps each
     handler in a closure. When SlowAPIMiddleware inspects scope["endpoint"]
     to find @limiter.limit() decorators, it sees the IdempotentRoute wrapper
     instead of the decorated function — so the decorator is never found.
     Solution: a custom Redis dependency that runs inside the DI system,
     after IdempotentRoute has already set up the route context.

Rate limiting storage uses Redis DB 3 — isolated from:
  DB 0: application cache (wallet balances, idempotency locks)
  DB 1: Celery broker
  DB 2: Celery results
"""

import jwt as pyjwt
from fastapi import Depends, HTTPException, Request, status
from redis.asyncio import Redis
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.config import settings
from app.core.redis import get_redis

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=["100/minute"],
    storage_uri= settings.RATE_LIMIT_REDIS_URL or settings.REDIS_URL,
)

_TRANSFER_LIMIT: int = 5
_TRANSFER_WINDOW_SECONDS: int = 60


def _get_user_id_from_request(request: Request) -> str | None:
    """
    Extract user_id (JWT sub claim) from the Authorization header.
    Returns None on any failure — authentication is handled by get_current_user.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:]
    try:
        payload = pyjwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
        return payload.get("sub")
    except pyjwt.PyJWTError:
        return None


async def _transfer_rate_limit(
    request: Request,
    redis: Redis = Depends(get_redis),
) -> None:
    """
    Per-user transfer rate limit: 5 per 60 seconds.

    Uses a Redis fixed-window counter:
      INCR     — increment (creates key at 1 on first call in the window)
      EXPIRE NX — set TTL=60s only if the key has no TTL yet (first call only)

    The NX flag ensures the window is always exactly 60 seconds starting from
    the first request, not sliding with each subsequent one.

    If user_id cannot be extracted (missing/invalid JWT) this dependency
    returns without raising — get_current_user will handle the 401.
    """
    user_id = _get_user_id_from_request(request)
    if user_id is None:
        return

    key = f"rate_limit:transfer:{user_id}"
    async with redis.pipeline(transaction=True) as pipe:
        pipe.incr(key)
        pipe.expire(key, _TRANSFER_WINDOW_SECONDS, nx=True)
        results = await pipe.execute()

    count = int(results[0])
    if count > _TRANSFER_LIMIT:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"Transfer rate limit exceeded. "
                f"Maximum {_TRANSFER_LIMIT} transfers per {_TRANSFER_WINDOW_SECONDS} seconds."
            ),
            headers={"Retry-After": str(_TRANSFER_WINDOW_SECONDS)},
        )
