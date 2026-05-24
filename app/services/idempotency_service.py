"""
app/services/idempotency_service.py

Idempotency business logic — five pure functions.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: Distributed locking with Redis NX
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The race condition this lock prevents:

  WITHOUT a lock:
  T1: checks DB → key not found → begins processing transfer
  T2: checks DB → key not found → ALSO begins processing transfer (!)
  T1: commits DB, deducts $500
  T2: commits DB, deducts $500 again — DOUBLE CHARGE

  WITH Redis NX lock:
  T1: SET idempotency_lock:X NX EX 30 → succeeds (returns 1)
      → checks DB → not found → processes → stores → releases lock
  T2: SET idempotency_lock:X NX EX 30 → fails (returns None, key exists)
      → returns 409 "Request already processing"

The client that got 409 knows: "my request is in flight, wait a moment."
After T1 completes and releases the lock, a fresh retry by the client
will find the COMPLETED idempotency record and replay the cached response.

Why Redis instead of a DB row lock?
  - Redis NX is atomic at the command level (no separate check-then-set race)
  - Redis SET NX EX is a single atomic command in Redis:
      if key does not exist → set key with value + expiry
      if key exists → return nil (fail)
    There is no window between "check" and "set" where another thread can
    sneak in. This is Redis's single-threaded command execution model.
  - DB row locks require a row to exist first. Creating a "lock row"
    in the DB has its own race condition (two INSERTs for the same key).
  - Redis lock is O(1) and microseconds. DB row lock involves B-tree traversal.

The EX 30 (30-second TTL) is the critical safety net:
  If the server crashes after acquiring the lock but before releasing it,
  the lock auto-expires after 30 seconds. Without EX, the lock is held
  forever — all future requests with that key would return 409 indefinitely.
  This is called a "zombie lock." EX makes it a "soft" distributed lock:
  at-most-once within the TTL window, with automatic recovery.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: Request hashing — detecting payload mismatch
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

SHA256(user_id + endpoint + body_bytes) → 64-char hex string.

Including ALL three components in the hash prevents four attack vectors:

  1. Cross-user replay:
     User A's key "k1" with body B → hash H1 (includes User A's UUID)
     User B uses key "k1" with body B → hash H2 (includes User B's UUID)
     H1 ≠ H2 → 422, even though body and key are identical.
     DB lookup also filters by user_id, providing defense in depth.

  2. Cross-endpoint replay:
     Key "k1" used on /wallet/transfer → hash H1 (includes endpoint)
     Key "k1" attempted on /wallet/deposit → hash H2 (different endpoint)
     H1 ≠ H2 → 422. Plus the DB lookup filters by endpoint.

  3. Same key, different amount:
     Key "k1" with body {"amount":"100.00"} → hash H1
     Retry with body {"amount":"200.00"} → hash H2
     H1 ≠ H2 → 422 "Payload mismatch."
     This is the most common legitimate client bug to detect.

  4. Hash collision (theoretical):
     Two different inputs with the same SHA256 hash.
     Probability ≈ 2^-128 per pair. Computationally negligible.
     SHA256 is cryptographically collision-resistant.

Why SHA256 and not MD5/CRC32?
  MD5/CRC32 are faster but have known collisions (MD5) or poor distribution
  for structured data (CRC32). SHA256 is universally trusted, collision-free
  in practice, and the performance difference is microseconds for request bodies.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: Why FAILED operations must NOT be stored
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Scenario: Alice sends $500 to Bob. She has exactly $500.
  Request 1: transfer attempted → "Insufficient funds" (she had $495)
  Alice tops up her account to $600.
  Request 2: retry with same Idempotency-Key.

  WITHOUT "don't store failures": the 400 error is cached.
  Alice's retry returns "Insufficient funds" even though she now has $600.
  She must generate a new Idempotency-Key to retry. That's bad UX and breaks
  the idempotency contract: clients should be able to retry freely after failure.

  WITH "don't store failures": the failed request leaves no trace.
  Alice's retry with the same key finds nothing → processes fresh.
  The transfer succeeds. Idempotency still protects against double-charge:
  only one successful result is stored, and subsequent retries replay it.

Rule: only COMPLETED (successful) operations are idempotent.
Failed operations are stateless — every attempt is independent.
"""

import hashlib
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.idempotency_key import IdempotencyKey, IdempotencyStatus

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Constants
# ─────────────────────────────────────────────────────────────────────────────

# How long the Redis distributed lock is held.
# Must be longer than the maximum expected request processing time.
# 30 seconds covers even slow DB operations; normal transfers complete in <2s.
LOCK_EXPIRE_SECONDS: int = 30

# How long idempotency records are retained.
# 24 hours matches Stripe, Razorpay, and most payment gateway standards.
KEY_TTL_SECONDS: int = 86_400  # 24 hours


# ─────────────────────────────────────────────────────────────────────────────
# compute_request_hash
# ─────────────────────────────────────────────────────────────────────────────

def compute_request_hash(
    body_bytes: bytes,
    user_id: uuid.UUID,
    endpoint: str,
) -> str:
    """
    Compute SHA256(user_id + endpoint + request_body) → 64-char hex string.

    All three inputs are concatenated before hashing so that changing any
    one of them produces a completely different hash. See module docstring
    for the attack vectors this prevents.

    Args:
        body_bytes: Raw HTTP request body (before Pydantic parsing).
        user_id: UUID of the authenticated user making the request.
        endpoint: HTTP path, e.g. "/api/v1/wallet/transfer".

    Returns:
        64-character lowercase hexadecimal SHA256 digest.
    """
    h = hashlib.sha256()
    h.update(str(user_id).encode("utf-8"))
    h.update(endpoint.encode("utf-8"))
    h.update(body_bytes)
    return h.hexdigest()


# ─────────────────────────────────────────────────────────────────────────────
# Lock key helper
# ─────────────────────────────────────────────────────────────────────────────

def _lock_key(user_id: uuid.UUID, endpoint: str, idempotency_key: str) -> str:
    """
    Build the Redis key for the distributed lock.

    Format: idempotency_lock:{user_id}:{endpoint}:{idempotency_key}

    Including user_id and endpoint prevents lock collisions:
      - Two different users with the same idempotency key get different locks.
      - The same key on two different endpoints gets different locks.

    Example: idempotency_lock:a1b2c3d4::/api/v1/wallet/transfer::abc-123
    """
    return f"idempotency_lock:{user_id}:{endpoint}:{idempotency_key}"


# ─────────────────────────────────────────────────────────────────────────────
# acquire_lock
# ─────────────────────────────────────────────────────────────────────────────

async def acquire_lock(
    redis: Redis,
    user_id: uuid.UUID,
    endpoint: str,
    idempotency_key: str,
) -> bool:
    """
    Attempt to acquire a distributed Redis lock for this (user, endpoint, key) triple.

    Uses Redis SET NX EX — a single atomic command:
      SET {lock_key} "1" NX EX {LOCK_EXPIRE_SECONDS}

    Redis's single-threaded command processing guarantees atomicity:
    there is no gap between "check if exists" and "set if not exists."
    This is the fundamental property that makes Redis NX safe for distributed locking.

    Redis returns:
      True  → lock acquired (SET succeeded, key did not exist)
      None  → lock NOT acquired (key already existed, another request is processing)

    Args:
        redis: Async Redis client.
        user_id: UUID of the authenticated user.
        endpoint: HTTP path of the operation being locked.
        idempotency_key: The Idempotency-Key header value from the client.

    Returns:
        True if this caller acquired the lock (may proceed).
        False if the lock is held by another caller (must wait).
    """
    key = _lock_key(user_id, endpoint, idempotency_key)
    result = await redis.set(key, "1", nx=True, ex=LOCK_EXPIRE_SECONDS)
    # redis.set with nx=True returns True on success, None on failure
    return result is True


# ─────────────────────────────────────────────────────────────────────────────
# release_lock
# ─────────────────────────────────────────────────────────────────────────────

async def release_lock(
    redis: Redis,
    user_id: uuid.UUID,
    endpoint: str,
    idempotency_key: str,
) -> None:
    """
    Release the distributed Redis lock for this (user, endpoint, key) triple.

    DEL is used (not EXPIRE to 0) because DEL is unconditional: it removes
    the key regardless of its current value or state.

    CRITICAL: this must be called in a `finally` block so that the lock is
    released even if the business logic raises an exception. An unreleased
    lock means ALL future retries with this key return 409 until the EX
    TTL expires (up to 30 seconds of unavailability per request).

    Production improvement (Lua script for safer release):
    A simple DEL without validation can accidentally delete a lock you
    don't own (if T1's lock expired and T2 acquired it, T1's DEL removes T2's lock).
    The safe approach uses a Lua script to DEL only if the value matches a
    unique token set during acquire. For Phase 5 (single-server/learning),
    simple DEL is sufficient. Add Lua-based release for production multi-node.

    Args:
        redis: Async Redis client.
        user_id: UUID of the authenticated user.
        endpoint: HTTP path of the operation.
        idempotency_key: The Idempotency-Key header value from the client.
    """
    key = _lock_key(user_id, endpoint, idempotency_key)
    try:
        await redis.delete(key)
    except Exception as exc:
        # Log but don't raise — lock will auto-expire via EX TTL.
        # Raising here would suppress the real exception (business logic error).
        logger.warning(
            "Failed to release idempotency lock %r: %s. "
            "Lock will auto-expire in %d seconds.",
            key, exc, LOCK_EXPIRE_SECONDS,
        )


# ─────────────────────────────────────────────────────────────────────────────
# check_existing_key
# ─────────────────────────────────────────────────────────────────────────────

async def check_existing_key(
    db: AsyncSession,
    user_id: uuid.UUID,
    endpoint: str,
    idempotency_key: str,
) -> IdempotencyKey | None:
    """
    Query the DB for an existing, non-expired idempotency record.

    The triple (key, user_id, endpoint) uniquely identifies a stored result.
    The UNIQUE index on these three columns makes this query an O(log N) index lookup.

    Expiry check: we filter WHERE expires_at > NOW() so that expired keys
    are treated as non-existent without requiring a separate cleanup task.
    The cleanup task (Phase 6) removes expired rows for table hygiene,
    but the expiry check here ensures correctness independently.

    Args:
        db: Async SQLAlchemy session.
        user_id: UUID of the authenticated user (from JWT — never from client).
        endpoint: HTTP path of the operation.
        idempotency_key: The Idempotency-Key header value.

    Returns:
        IdempotencyKey ORM instance if found and not expired. None otherwise.
    """
    now = datetime.now(tz=timezone.utc)
    result = await db.execute(
        select(IdempotencyKey).where(
            IdempotencyKey.key == idempotency_key,
            IdempotencyKey.user_id == user_id,
            IdempotencyKey.endpoint == endpoint,
            IdempotencyKey.expires_at > now,  # skip expired keys
        )
    )
    return result.scalar_one_or_none()


# ─────────────────────────────────────────────────────────────────────────────
# store_idempotency_response
# ─────────────────────────────────────────────────────────────────────────────

async def store_idempotency_response(
    db: AsyncSession,
    user_id: uuid.UUID,
    endpoint: str,
    key: str,
    request_hash: str,
    response_body: bytes,
    http_status_code: int,
) -> None:
    """
    Persist the successful response so future retries can replay it.

    Called ONLY for successful (2xx) responses — see module docstring
    for why failed responses are intentionally NOT stored.

    response_body is stored as JSONB (a Python dict) in PostgreSQL.
    The caller passes raw bytes (from response.body); we decode and parse here.
    PostgreSQL validates the JSON structure on write — no invalid JSON can be stored.

    expires_at is set to NOW + 24 hours. The UNIQUE index on (key, user_id, endpoint)
    prevents duplicate insertions if two concurrent requests somehow both reach
    this function (extremely rare, but the DB constraint is the final safety net).

    Args:
        db: Async SQLAlchemy session.
        user_id: UUID of the authenticated user.
        endpoint: HTTP path of the operation.
        key: The Idempotency-Key header value.
        request_hash: SHA256 digest computed from the original request.
        response_body: Raw bytes of the HTTP response body (valid JSON).
        http_status_code: The HTTP status code of the successful response.

    Raises:
        Exception: If DB write fails. The caller logs and suppresses this
            (a missed idempotency store is non-fatal; the payment already succeeded).
    """
    expires_at = datetime.now(tz=timezone.utc) + timedelta(seconds=KEY_TTL_SECONDS)

    # Decode and parse JSON. If the response body is somehow not valid JSON,
    # fall back to wrapping it in a plain dict to avoid a write failure.
    try:
        parsed_body: dict = json.loads(response_body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning("Could not parse response body as JSON for idempotency storage: %s", exc)
        parsed_body = {"raw": response_body.decode("utf-8", errors="replace")}

    record = IdempotencyKey(
        key=key,
        user_id=user_id,
        endpoint=endpoint,
        request_hash=request_hash,
        status=IdempotencyStatus.COMPLETED,
        response_body=parsed_body,
        http_status_code=http_status_code,
        expires_at=expires_at,
    )
    db.add(record)
    await db.commit()

    logger.info(
        "Stored idempotency record: key=%r user_id=%s endpoint=%r status=%d expires=%s",
        key, user_id, endpoint, http_status_code, expires_at.isoformat(),
    )
