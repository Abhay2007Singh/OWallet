"""
app/middleware/idempotency.py

IdempotentRoute — a custom FastAPI route class that wraps payment endpoints
with a full idempotency lifecycle.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DESIGN CHOICE: Custom APIRoute class, not Starlette ASGI middleware
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Why not @app.middleware("http") (Starlette ASGI middleware)?

  1. No FastAPI DI access. DB sessions and Redis clients come from
     Depends() — which only resolves during endpoint execution, not at
     the middleware layer.

  2. user_id requires a decoded JWT. The JWT is validated by get_current_user()
     inside the route handler (DI context). ASGI middleware runs BEFORE that.

  3. Reading + storing the response body is fragile in ASGI streaming middleware:
     you must buffer the entire byte stream, re-emit it, and hope nothing
     breaks. FastAPI's Response object gives us .body directly.

Why custom APIRoute.get_route_handler()?

  This is FastAPI's documented pattern for wrapping route execution:
  https://fastapi.tiangolo.com/how-to/custom-request-and-route/

  The returned callable receives (request: Request) → Response.
  The Response object is fully rendered with .body populated.
  request.body() is cached by Starlette after the first read — safe
  to call multiple times.

  Applied selectively:
    payment_router = APIRouter(route_class=IdempotentRoute)
  Only payment endpoints use this class. Read-only endpoints are unaffected.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
IDEMPOTENCY LIFECYCLE (one request)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  1. Extract Idempotency-Key header → 400 if missing
  2. Decode JWT for user_id (no full auth — just key scoping)
  3. Compute SHA256(user_id + endpoint + body)
  4. Acquire Redis NX lock → 409 if another request is in flight
  5. Check DB:
       Found + hash matches  → replay cached response (200/201)
       Found + hash differs  → 422 Payload Mismatch
       Not found             → process request
  6. Execute payment handler
  7. On 2xx: store response in DB for future replay
  8. ALWAYS release Redis lock (finally block)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
FAILURE SCENARIOS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Server crashes AFTER commit, BEFORE storing idempotency record:
  → Lock auto-expires (EX 30 seconds).
  → Client retry finds no record → reprocesses. This is the "at-least-once"
    edge case. Fully exact-once requires a transactional outbox (Phase 6).

Redis unavailable:
  → We degrade gracefully: skip lock, proceed without distributed protection.
  → DB UNIQUE constraint on (key, user_id, endpoint) still prevents
    duplicate inserts when two requests race to completion.

DB unavailable during check_existing_key:
  → Exception → 503 to client. Lock released in finally. Safe to retry.

Failed payment (4xx):
  → handler raises HTTPException → propagates out of original_handler().
  → finally releases lock. Record NOT stored. Client can retry freely.

store_idempotency_response fails after payment succeeds:
  → Logged as error. Response is returned to client (payment succeeded).
  → This specific key loses idempotency. Retry will reprocess.
  → Production fix: store the idempotency record in the SAME DB transaction
    as the payment (transactional outbox pattern).
"""

import logging
import uuid
from typing import Callable

import jwt as pyjwt
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute
from starlette.responses import Response

from app.core.config import settings
from app.services.idempotency_service import (
    acquire_lock,
    check_existing_key,
    compute_request_hash,
    release_lock,
    store_idempotency_response,
)

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# JWT user extraction helper
# ─────────────────────────────────────────────────────────────────────────────

def _extract_user_id_from_request(request: Request) -> uuid.UUID | None:
    """
    Decode the Bearer JWT to extract user_id for lock and DB query scoping.

    We do NOT perform full auth validation here (expiry, type checks, DB lookup).
    That is the responsibility of get_current_user() inside the route handler's DI.

    Here we only need the user_id UUID to:
      - Scope the Redis lock key (idempotency_lock:{user_id}:{endpoint}:{key})
      - Scope the DB query (WHERE user_id = ?)

    If the JWT is absent or malformed, we return None and let the original
    handler produce the 401 through its normal authentication dependency.

    options={"verify_exp": False}: We skip expiry verification here because:
      1. Idempotency scoping doesn't require a valid-at-this-moment token.
      2. The real expiry check runs inside get_current_user() milliseconds later.
      3. An expired-but-parseable token still gives us the correct user_id.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None

    token = auth_header.removeprefix("Bearer ").strip()
    try:
        payload = pyjwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            options={"verify_exp": False},
        )
        sub = payload.get("sub")
        if sub is None:
            return None
        return uuid.UUID(str(sub))
    except (pyjwt.InvalidTokenError, ValueError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# IdempotentRoute
# ─────────────────────────────────────────────────────────────────────────────

class IdempotentRoute(APIRoute):
    """
    Custom APIRoute that enforces idempotency on every handler it wraps.

    Usage — apply at the router level:
        payment_router = APIRouter(prefix="/wallet", route_class=IdempotentRoute)

        @payment_router.post("/transfer")
        async def create_transfer(...):
            ...

    The endpoint code is unchanged. IdempotentRoute adds the full
    idempotency lifecycle invisibly around each handler invocation.
    """

    def get_route_handler(self) -> Callable:
        original_handler = super().get_route_handler()

        async def idempotent_handler(request: Request) -> Response:
            # Late imports to avoid circular imports at module load time.
            # Python caches imports after the first execution — no penalty.
            from app.core.database import AsyncSessionLocal
            from app.core.redis import get_redis_client

            # ─────────────────────────────────────────────────────────────
            # STEP 1: Require Idempotency-Key header
            # ─────────────────────────────────────────────────────────────
            idempotency_key = request.headers.get("Idempotency-Key")
            if not idempotency_key:
                return JSONResponse(
                    status_code=400,
                    content={
                        "detail": (
                            "Idempotency-Key header is required for payment operations. "
                            "Generate a UUID v4 per logical operation and include it as: "
                            "Idempotency-Key: <uuid-v4>. "
                            "Reuse the exact same key when retrying a failed or timed-out request."
                        )
                    },
                )

            # ─────────────────────────────────────────────────────────────
            # STEP 2: Extract user_id from JWT
            # ─────────────────────────────────────────────────────────────
            user_id = _extract_user_id_from_request(request)
            if user_id is None:
                # JWT is absent or completely unparseable.
                # Fall through to the handler — get_current_user() will 401.
                return await original_handler(request)

            # ─────────────────────────────────────────────────────────────
            # STEP 3: Compute request hash
            # ─────────────────────────────────────────────────────────────
            # request.body() is safe to call here — Starlette caches body bytes
            # in request._body after the first read. The route handler's Pydantic
            # parsing reads from the same cache. No double-read of the stream.
            # ─────────────────────────────────────────────────────────────
            body_bytes: bytes = await request.body()
            endpoint: str = request.url.path
            request_hash: str = compute_request_hash(body_bytes, user_id, endpoint)

            redis = get_redis_client()

            # ─────────────────────────────────────────────────────────────
            # STEP 4: Acquire Redis distributed lock
            # ─────────────────────────────────────────────────────────────
            # Three outcomes:
            #   lock_acquired=True  → we hold the lock, proceed
            #   lock_acquired=False → another request is in flight → 409
            #   redis_down=True     → Redis unavailable → degrade gracefully
            # ─────────────────────────────────────────────────────────────
            redis_down = False
            lock_acquired = False

            try:
                lock_acquired = await acquire_lock(redis, user_id, endpoint, idempotency_key)
            except Exception as exc:
                redis_down = True
                logger.warning(
                    "Redis unavailable for idempotency lock (degraded mode): %s", exc
                )

            if not redis_down and not lock_acquired:
                # Redis is healthy but the lock key already exists — another
                # in-flight request is processing this key right now.
                return JSONResponse(
                    status_code=409,
                    content={
                        "detail": (
                            "A request with this Idempotency-Key is already being processed. "
                            "Wait for the current request to complete before retrying. "
                            "If the problem persists, wait 30 seconds — the lock auto-expires."
                        )
                    },
                )

            # If redis_down: we skip the lock and proceed without distributed protection.
            # The DB UNIQUE constraint on (key, user_id, endpoint) is the safety net.

            try:
                # ─────────────────────────────────────────────────────────
                # STEP 5: Check DB for an existing idempotency record
                # ─────────────────────────────────────────────────────────
                async with AsyncSessionLocal() as db:
                    existing = await check_existing_key(db, user_id, endpoint, idempotency_key)

                if existing is not None:
                    if existing.request_hash != request_hash:
                        # The client reused a key with a different request body.
                        # This is a client programming error, not a transient failure.
                        return JSONResponse(
                            status_code=422,
                            content={
                                "detail": (
                                    "Idempotency-Key reuse with a different request payload is not allowed. "
                                    "The same Idempotency-Key must always carry the identical request body. "
                                    "To perform a different operation, generate a new Idempotency-Key."
                                )
                            },
                        )

                    # Hash matches — this is a safe retry. Replay the cached response.
                    logger.info(
                        "[idempotency] REPLAY key=%r user=%s endpoint=%r status=%d",
                        idempotency_key, user_id, endpoint, existing.http_status_code,
                    )
                    return JSONResponse(
                        status_code=existing.http_status_code,
                        content=existing.response_body,   # dict (JSONB from DB)
                        headers={"X-Idempotency-Replayed": "true"},
                    )

                # ─────────────────────────────────────────────────────────
                # STEP 6: Process the request
                # ─────────────────────────────────────────────────────────
                # No existing record → run the payment handler normally.
                #
                # If the handler raises HTTPException (e.g., 400 Insufficient Funds):
                #   - The exception propagates out of original_handler(request).
                #   - Our `finally` block runs → lock is released.
                #   - The exception continues up to FastAPI's exception handlers.
                #   - No idempotency record is stored (failed ops are stateless).
                # ─────────────────────────────────────────────────────────
                response: Response = await original_handler(request)

                # ─────────────────────────────────────────────────────────
                # STEP 7: Store the successful response
                # ─────────────────────────────────────────────────────────
                # Only 2xx responses are stored. A 4xx that somehow doesn't
                # raise an exception (rare FastAPI edge case) is not cached.
                #
                # Storage failure is non-fatal: the payment already succeeded
                # and the client will receive their 200 regardless.
                # ─────────────────────────────────────────────────────────
                if 200 <= response.status_code < 300:
                    try:
                        async with AsyncSessionLocal() as db:
                            await store_idempotency_response(
                                db=db,
                                user_id=user_id,
                                endpoint=endpoint,
                                key=idempotency_key,
                                request_hash=request_hash,
                                response_body=response.body,
                                http_status_code=response.status_code,
                            )
                    except Exception as exc:
                        logger.error(
                            "[idempotency] Storage failed after successful payment "
                            "(key=%r user=%s endpoint=%r): %s — "
                            "this key loses replay protection for future retries.",
                            idempotency_key, user_id, endpoint, exc,
                        )

                return response

            finally:
                # ─────────────────────────────────────────────────────────
                # STEP 8: Release Redis lock — ALWAYS, even on exception
                # ─────────────────────────────────────────────────────────
                # `finally` runs when:
                #   - Handler succeeded (normal return)
                #   - Handler raised HTTPException (4xx business logic error)
                #   - Unexpected exception (DB error, network error)
                #   - even when return statements exit the try block early
                #
                # Without `finally`, any exception leaves the lock held for
                # the full 30-second TTL — every retry during that window
                # gets a 409. With `finally`, the lock is released immediately
                # and the next retry can proceed without waiting.
                # ─────────────────────────────────────────────────────────
                if lock_acquired:
                    await release_lock(redis, user_id, endpoint, idempotency_key)

        return idempotent_handler
