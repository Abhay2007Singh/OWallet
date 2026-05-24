"""
app/core/dependencies.py

FastAPI dependency functions shared across multiple routes.

Dependencies in FastAPI:
  A dependency is any callable that FastAPI can call automatically before
  your route handler runs. Declare them with `Depends(...)`.

  The dependency injection tree:
    route handler
      └── get_current_user(token, db)
            ├── OAuth2PasswordBearer  → extracts Bearer token from header
            ├── get_db               → provides AsyncSession
            └── (internally) decode_token + DB query

Why centralize in dependencies.py?
  get_current_user() will be used by EVERY protected route (wallets,
  transactions, profile, etc.). Defining it once here means:
  - One place to update auth logic
  - One place to write tests for it
  - Route files stay clean: just `Depends(get_current_user)`

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: Stateless Authentication
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Traditional session-based auth:
  1. Login → server creates a session in the DB → returns session_id cookie
  2. Each request → server looks up session_id in DB → finds user
  Problem: Every request hits the DB. At 10,000 req/s, that's 10,000 DB reads/s.

JWT stateless auth:
  1. Login → server creates JWT with user_id embedded → returns token
  2. Each request → server validates JWT signature → reads user_id from payload
  No DB lookup for authentication. The token IS the session.
  Problem: Tokens cannot be revoked (stateless = no registry).
  Solution: Short expiry (15min). If stolen, damage window is 15 minutes.

How get_current_user() works:
  1. OAuth2PasswordBearer extracts the Bearer token from the Authorization header.
     Authorization: Bearer eyJhbGci...
  2. decode_token() validates: signature, expiry, type claim.
  3. We query the DB only once — to get the full User object.
     (JWT proves identity; DB gives us the current state: is_active, role, etc.)
  4. Return the User ORM object, which the route handler receives.

Why query the DB if it's "stateless"?
  JWTs only tell us the user_id at the time of login. We still need to
  check is_active (account may have been disabled after login). This single
  DB read per request is acceptable — it's not a session lookup, it's a
  user state check.

  Optimization (Phase 3): Cache user objects in Redis with a 1-minute TTL.
  Reduces DB reads from 10,000/s to ~1,000/s under high load.
"""

import uuid
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.security import decode_token
from app.models.user import User

# =============================================================================
# OAuth2PasswordBearer
# =============================================================================
# This is NOT a login mechanism — it's a token extractor.
# It tells FastAPI:
#   1. Look for an Authorization header with "Bearer <token>"
#   2. Extract <token> and pass it to the dependency function
#   3. If the header is missing, return 401 automatically
#   4. In Swagger UI, show a lock icon and "Authorize" button
#      that sends the token to tokenUrl for the user to authenticate
#
# tokenUrl is shown in the OpenAPI spec (helps Swagger UI).
# It does NOT affect how the dependency works at runtime.
# =============================================================================
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login")


# =============================================================================
# Reusable 401 exception
# =============================================================================
# Defined once so every place that raises it sends the same structure.
# WWW-Authenticate: Bearer is the RFC 6750 standard header for Bearer token auth.
# Without it, browsers and API clients don't know what auth scheme to use.
CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


async def get_current_user(
    token: Annotated[str, Depends(oauth2_scheme)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """
    FastAPI dependency that authenticates and returns the current user.

    Used on every protected route:
        @router.get("/wallets")
        async def list_wallets(
            current_user: Annotated[User, Depends(get_current_user)],
        ): ...

    Flow:
      1. oauth2_scheme extracts "Bearer <token>" from the header automatically
      2. decode_token() validates signature, expiry, and type="access"
      3. Extract user_id UUID from the "sub" claim
      4. Query the DB for the current user state
      5. Verify the user still exists and is active
      6. Return the User ORM object to the route handler

    FastAPI's dependency injection ensures this runs before EVERY route
    that declares it — routes never need to repeat auth logic themselves.

    Args:
        token: JWT string extracted from Authorization header by oauth2_scheme.
        db: Async SQLAlchemy session from get_db dependency.

    Returns:
        The authenticated User ORM object.

    Raises:
        HTTPException 401: Missing/invalid/expired token, or user not found.
        HTTPException 403: Account exists but is disabled.
    """

    # -------------------------------------------------------------------------
    # Decode and validate the JWT
    # decode_token raises HTTPException 401 if the token is invalid or expired
    # -------------------------------------------------------------------------
    payload = decode_token(token, expected_type="access")

    # -------------------------------------------------------------------------
    # Extract and validate user_id from the "sub" claim
    # -------------------------------------------------------------------------
    user_id_str: str | None = payload.get("sub")
    if not user_id_str:
        raise CREDENTIALS_EXCEPTION

    try:
        user_id = uuid.UUID(user_id_str)
    except ValueError:
        # sub claim exists but is not a valid UUID — malformed token
        raise CREDENTIALS_EXCEPTION

    # -------------------------------------------------------------------------
    # Fetch the user from the database
    # db.get() is a primary-key lookup — O(1), uses PK index.
    # This is the ONE DB call we make per authenticated request.
    # -------------------------------------------------------------------------
    user: User | None = await db.get(User, user_id)

    if user is None:
        # Token was valid (correct signature, not expired) but the user was
        # deleted from the DB after the token was issued.
        raise CREDENTIALS_EXCEPTION

    # -------------------------------------------------------------------------
    # Check account status
    # The JWT doesn't carry is_active — we must check DB for current state.
    # This is how we immediately prevent disabled accounts from accessing the API,
    # even if their access token hasn't expired yet.
    # -------------------------------------------------------------------------
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This account has been disabled",
        )

    return user


# =============================================================================
# Role-based access control dependency (used in Phase 3+)
# =============================================================================

async def require_admin(
    current_user: Annotated[User, Depends(get_current_user)],
) -> User:
    """
    Dependency that requires the current user to have ADMIN role.

    Usage:
        @router.get("/admin/users")
        async def list_all_users(
            admin: Annotated[User, Depends(require_admin)],
        ): ...

    By composing dependencies (require_admin depends on get_current_user),
    FastAPI automatically handles the full auth chain without duplication.

    Raises:
        HTTPException 403: User is authenticated but not an admin.
    """
    from app.models.user import UserRole

    if current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator access required",
        )
    return current_user
