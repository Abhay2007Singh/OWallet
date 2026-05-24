"""
app/core/security.py

Cryptographic utilities for PyWallet authentication.

Responsibilities:
  - Password hashing and verification (bcrypt)
  - JWT creation: access tokens and refresh tokens
  - JWT decoding and validation
  - Redis refresh token lifecycle (store / fetch / delete)

All functions here are unit-testable in isolation — no FastAPI, no routes.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: Why bcrypt is used for passwords
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Problem: You cannot store plaintext passwords. If your DB is stolen,
every user's password is immediately exposed — including passwords they
reuse on Gmail, banking, etc.

Problem with fast hashes (MD5, SHA-256): A GPU can compute 10 BILLION
SHA-256 hashes per second. An attacker with a stolen hash DB can crack
most passwords in minutes.

bcrypt solution:
  1. Intentionally slow: 12 rounds ≈ 250ms per hash. 250ms × 10B = ~79 years
     per password guessed. Hardware speed-up → increase rounds.
  2. Random salt per hash: bcrypt generates 22 random bytes of salt and
     embeds them in the output string. Identical passwords produce different
     hashes. This destroys rainbow table attacks.
  3. Self-describing: The output string "$2b$12$<salt><hash>" contains the
     algorithm, rounds, and salt — no separate storage needed.
  4. One-way: bcrypt is a key derivation function, not encryption. There is
     no "decrypt" — you can only re-hash the candidate and compare.

Why not SHA-256 + salt? Because SHA-256 is designed to be FAST (for
network checksums). Fast = bad for passwords. bcrypt is designed to be slow.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: JWT structure
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

A JWT is three base64url-encoded JSON objects joined by dots:

  eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9         ← Header
  .eyJzdWIiOiJ1c2VyLXV1aWQiLCJ0eXBlIjoiYWNjZXNzIn0  ← Payload
  .SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c  ← Signature

Header: {"alg": "HS256", "typ": "JWT"}
Payload (our claims):
  sub  → subject: the user_id this token represents
  type → "access" or "refresh" (our custom claim — prevents token confusion)
  jti  → JWT ID: unique string per token (for audit trails)
  iat  → issued at: Unix timestamp of creation
  exp  → expiration: Unix timestamp after which token is invalid
Signature: HMAC_SHA256(base64(header) + "." + base64(payload), SECRET_KEY)

Why is this secure?
  - The signature is computed using SECRET_KEY, which only the server knows.
  - Anyone can decode the payload (base64 is not encryption).
  - But nobody can FORGE a valid signature without SECRET_KEY.
  - The server validates the signature on every request — no DB lookup needed.
  - This is "stateless authentication": the token IS the session.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: Access token vs Refresh token
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Why have two tokens?

Access token (15 minutes):
  + Sent with EVERY API request (Authorization: Bearer <token>)
  + Stateless: validated by signature alone — no Redis, no DB
  + Short-lived: if stolen, attacker has 15-minute window
  - Cannot be revoked mid-flight (stateless = no central registry)

Refresh token (7 days):
  + Used ONLY to obtain a new access token (POST /auth/refresh)
  + Stored in Redis: can be revoked instantly (logout deletes it)
  + Never sent with regular API calls — lower exposure surface
  - Stateful: requires a Redis lookup to validate

If you only had access tokens with 7-day expiry:
  A stolen token gives 7 days of access with no way to revoke it.

If you only had refresh tokens:
  Every API call hits Redis — defeats the scalability benefit of JWT.

The two-token system gives you both: scalability (stateless access)
and revocability (stateful refresh).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: Refresh token rotation and replay attack prevention
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Without rotation:
  1. User logs in → gets refresh_token_A (valid 7 days)
  2. Attacker steals refresh_token_A (e.g., from a compromised device)
  3. User refreshes at day 3 → gets new access token (refresh_token_A still valid)
  4. Attacker uses refresh_token_A at day 5 → also gets a valid access token
  → Both user and attacker have valid sessions indefinitely!

With rotation:
  1. User logs in → gets refresh_token_A; Redis stores: refresh:{uid}=A
  2. User refreshes → server deletes A, stores B; returns refresh_token_B
  3. If attacker tries refresh_token_A → Redis has B, A≠B → rejected
  4. If attacker refreshes BEFORE user → Redis has null (or B), A≠stored → rejected
     AND: we detect the stale token usage and immediately delete ALL sessions
     for this user, forcing re-login. This is a theft detection signal.

Replay attack: using a token that was already consumed.
  Server deletes the old refresh token BEFORE generating the new one.
  If the delete-then-generate sequence is interrupted (e.g., server crash),
  the user simply logs in again. No security risk, just minor UX friction.
"""

import uuid
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import HTTPException, status
from redis.asyncio import Redis

from app.core.config import settings


# =============================================================================
# Constants
# =============================================================================

# bcrypt cost factor — the number of hashing rounds as a power of 2.
# rounds=12 → 2^12 = 4096 internal iterations → ~250ms on modern hardware.
# rounds=13 → ~500ms. Increase as hardware gets faster (every 2-3 years).
BCRYPT_ROUNDS: int = 12

# Redis key prefix for refresh tokens.
# Key format: "refresh:{user_id}" → refresh token string
# Using a namespace prefix prevents collisions if the same Redis instance
# is shared with other applications or used for caching.
REFRESH_TOKEN_PREFIX: str = "refresh"

# Pre-computed bcrypt hash used for timing-attack-safe login.
# When an email doesn't exist, we still run bcrypt.checkpw() against this
# dummy hash to consume the same ~250ms as a real verification attempt.
# Without this, an attacker times responses: fast=email not found,
# slow=email found with wrong password — leaking user enumeration data.
_DUMMY_HASH: str = bcrypt.hashpw(
    b"dummy-sentinel-password-never-used",
    bcrypt.gensalt(rounds=BCRYPT_ROUNDS),
).decode("utf-8")


# =============================================================================
# Password Hashing
# =============================================================================

def hash_password(plain_password: str) -> str:
    """
    Hash a plaintext password using bcrypt.

    The output is a 60-character string that encodes the algorithm version,
    cost factor, salt, and hash together: "$2b$12$<22-char-salt><31-char-hash>"

    This self-describing format means no separate salt storage is needed.
    The same string is passed back to verify_password() for comparison.

    Args:
        plain_password: The raw password string from the user.

    Returns:
        A bcrypt hash string safe to store in the database.
    """
    password_bytes = plain_password.encode("utf-8")
    salt = bcrypt.gensalt(rounds=BCRYPT_ROUNDS)
    hashed_bytes = bcrypt.hashpw(password_bytes, salt)
    return hashed_bytes.decode("utf-8")


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """
    Verify a plaintext password against a stored bcrypt hash.

    bcrypt.checkpw() internally:
    1. Extracts the salt from the hashed_password string.
    2. Re-hashes plain_password with that salt.
    3. Compares the result using a constant-time comparison.

    The constant-time comparison (hmac.compare_digest internally) prevents
    timing attacks where an attacker infers partial matches by measuring
    how long the comparison takes.

    Args:
        plain_password: The raw password from the login request.
        hashed_password: The bcrypt hash stored in the database.

    Returns:
        True if the password matches, False otherwise.
    """
    try:
        return bcrypt.checkpw(
            plain_password.encode("utf-8"),
            hashed_password.encode("utf-8"),
        )
    except Exception:
        # bcrypt raises exceptions for malformed hash strings.
        # Return False instead of propagating — caller decides the response.
        return False


def verify_password_timing_safe(
    plain_password: str,
    hashed_password: str | None,
) -> bool:
    """
    Timing-attack-safe password verification.

    If hashed_password is None (user not found), run bcrypt against a
    dummy hash to consume the same time as a real verification.
    This prevents user enumeration via response timing differences.

    Args:
        plain_password: The raw password from the login request.
        hashed_password: The stored hash, or None if the user was not found.

    Returns:
        True only if hashed_password is not None AND matches the password.
    """
    if hashed_password is None:
        # User not found — still run bcrypt to normalize response time.
        verify_password(plain_password, _DUMMY_HASH)
        return False
    return verify_password(plain_password, hashed_password)


# =============================================================================
# JWT Token Creation
# =============================================================================

def _build_jwt_payload(
    user_id: uuid.UUID,
    token_type: str,
    expires_delta: timedelta,
) -> dict:
    """
    Build the standard JWT payload dict.

    Claims:
        sub  (Subject)    — user_id as string; who this token identifies
        type (Custom)     — "access" or "refresh"; prevents token confusion attacks
        jti  (JWT ID)     — unique UUID per token; enables audit trails
        iat  (Issued At)  — UTC timestamp of creation
        exp  (Expiration) — UTC timestamp after which PyJWT rejects the token
    """
    now = datetime.now(timezone.utc)
    return {
        "sub": str(user_id),
        "type": token_type,
        "jti": str(uuid.uuid4()),  # unique per token — not reused across rotations
        "iat": now,
        "exp": now + expires_delta,
    }


def create_access_token(user_id: uuid.UUID) -> str:
    """
    Create a short-lived JWT access token (15 minutes).

    This token is sent with every authenticated API request in the
    Authorization header: "Bearer <access_token>"

    It is STATELESS — the server validates only the signature and expiry.
    No database or Redis lookup on every request. This is the core
    scalability benefit of JWT-based authentication.

    Args:
        user_id: The UUID of the authenticated user.

    Returns:
        A signed JWT string.
    """
    payload = _build_jwt_payload(
        user_id=user_id,
        token_type="access",
        expires_delta=timedelta(minutes=settings.JWT_ACCESS_TOKEN_EXPIRE_MINUTES),
    )
    return jwt.encode(
        payload,
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


def create_refresh_token(user_id: uuid.UUID) -> str:
    """
    Create a long-lived JWT refresh token (7 days).

    This token is used ONLY at POST /auth/refresh to obtain a new
    access token. It is NEVER sent with regular API calls.

    Unlike access tokens, refresh tokens are STATEFUL — the server
    stores the current valid refresh token in Redis. Even if the JWT
    signature is valid, a token rejected by Redis is invalid.

    Args:
        user_id: The UUID of the authenticated user.

    Returns:
        A signed JWT string.
    """
    payload = _build_jwt_payload(
        user_id=user_id,
        token_type="refresh",
        expires_delta=timedelta(days=settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS),
    )
    return jwt.encode(
        payload,
        settings.JWT_SECRET_KEY,
        algorithm=settings.JWT_ALGORITHM,
    )


# =============================================================================
# JWT Decoding and Validation
# =============================================================================

def decode_token(token: str, expected_type: str) -> dict:
    """
    Decode and validate a JWT. Raises HTTPException on any failure.

    Validation steps (in order):
    1. Signature verification — HMAC-SHA256 with JWT_SECRET_KEY
    2. Expiration check — PyJWT raises ExpiredSignatureError if exp < now
    3. Type claim check — prevents using a refresh token as an access token
    4. Subject claim presence — ensures the token has a user_id

    Why separate ExpiredSignatureError?
    We give a specific "Token has expired" message for UX — the client
    needs to know to refresh rather than re-login. All other JWT errors
    get the generic "Could not validate credentials" to avoid leaking
    implementation details to attackers.

    Args:
        token: The raw JWT string from the Authorization header.
        expected_type: "access" or "refresh" — the type claim we require.

    Returns:
        The decoded payload dict with all claims.

    Raises:
        HTTPException 401 on any validation failure.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )

    try:
        payload: dict = jwt.decode(
            token,
            settings.JWT_SECRET_KEY,
            algorithms=[settings.JWT_ALGORITHM],
        )
    except jwt.ExpiredSignatureError:
        # Specific message: client should call /auth/refresh, not re-login
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token has expired",
            headers={"WWW-Authenticate": "Bearer"},
        )
    except jwt.InvalidTokenError:
        # Covers: malformed token, wrong signature, invalid claims structure
        raise credentials_exception

    # --- Custom claim validation ---

    # Type check: reject refresh tokens on access-protected routes and vice versa.
    # Without this, an attacker who obtains a refresh token (7-day lifetime)
    # could use it as an access token and bypass short-expiry protection.
    if payload.get("type") != expected_type:
        raise credentials_exception

    # Subject must be present — it's the user_id we act on behalf of.
    if not payload.get("sub"):
        raise credentials_exception

    return payload


# =============================================================================
# Redis Refresh Token Management
# =============================================================================

def _refresh_token_key(user_id: str | uuid.UUID) -> str:
    """
    Build the Redis key for a user's active refresh token.

    Design decision: One key per user (not one key per token).
    This means each user has exactly ONE active session. Logging in from
    a second device invalidates the first device's session.

    Pro: Simple; instant single-user logout; easy to audit.
    Con: No multi-device concurrent sessions (Phase 3 can address this
    by storing a Redis Set of tokens per user).

    Key format: "refresh:{user_id}"
    Example:    "refresh:550e8400-e29b-41d4-a716-446655440000"
    """
    return f"{REFRESH_TOKEN_PREFIX}:{user_id}"


async def store_refresh_token(
    redis: Redis,
    user_id: uuid.UUID,
    refresh_token: str,
) -> None:
    """
    Store a refresh token in Redis with a 7-day TTL.

    Why Redis for refresh tokens?
    ┌──────────────────────────────────────────────────────────────────┐
    │  Property          │  Redis           │  PostgreSQL               │
    │──────────────────────────────────────────────────────────────────│
    │  Revocation speed  │  O(1), in-memory │  O(log n), disk read      │
    │  TTL management    │  Native (EXPIRE) │  Cron job needed          │
    │  Read latency      │  <1ms            │  5-20ms                   │
    │  Write amplification│ None             │ WAL, index update         │
    │  Scale             │  ~1M ops/sec     │  ~10K ops/sec             │
    └──────────────────────────────────────────────────────────────────┘

    Redis SET with EX (TTL) is atomic — the value and expiry are set
    in one command. There is no window where the token exists without
    an expiry (which would cause keys to accumulate forever).

    Args:
        redis: The async Redis client.
        user_id: UUID of the user this token belongs to.
        refresh_token: The full JWT refresh token string.
    """
    key = _refresh_token_key(user_id)
    ttl_seconds = settings.JWT_REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60  # 604800
    await redis.set(key, refresh_token, ex=ttl_seconds)


async def get_refresh_token(
    redis: Redis,
    user_id: uuid.UUID,
) -> str | None:
    """
    Retrieve the currently stored refresh token for a user.

    Returns None if:
    - The user has never logged in (key doesn't exist)
    - The user has logged out (key was deleted)
    - The token expired (Redis TTL elapsed)
    - The user logged in from another device (key was overwritten)

    Args:
        redis: The async Redis client.
        user_id: UUID of the user.

    Returns:
        The stored refresh token string, or None.
    """
    key = _refresh_token_key(user_id)
    return await redis.get(key)


async def delete_refresh_token(
    redis: Redis,
    user_id: uuid.UUID,
) -> None:
    """
    Delete the refresh token for a user (logout or rotation).

    DEL is idempotent — deleting a non-existent key is not an error.
    This is called in two scenarios:
    1. Logout: user explicitly ends their session
    2. Token rotation: old token deleted before new token is stored

    The rotation sequence (delete THEN store new) is critical:
    If we crash between delete and store-new, the user must re-login.
    If we crash between store-new and delete, the old token still works
    for one more rotation — acceptable, because the next rotation will
    detect the stale token and revoke all sessions.

    Args:
        redis: The async Redis client.
        user_id: UUID of the user.
    """
    key = _refresh_token_key(user_id)
    await redis.delete(key)
