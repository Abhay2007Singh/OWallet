"""
app/routes/auth.py

Authentication route handlers.

Routes are intentionally thin — they:
  1. Accept a request (Pydantic validates the body automatically)
  2. Inject dependencies (db, redis, current_user via Depends)
  3. Call the appropriate service function
  4. Return a response schema

No business logic lives here. No direct DB queries. No Redis calls.
All of that belongs in services/auth_service.py.

Why this separation?
  If you later want to expose the same auth logic via a gRPC interface or a
  CLI tool, you reuse the service layer unchanged. Only the transport adapters
  (route handlers) change.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.redis import get_redis
from app.middleware.rate_limiter import limiter
from app.models.user import User
from app.schemas.auth import (
    LoginRequest,
    LoginResponse,
    LogoutResponse,
    MeResponse,
    RefreshRequest,
    RefreshResponse,
    RegisterRequest,
    RegisterResponse,
    TokenResponse,
    UserResponse,
)
from app.services.auth_service import (
    login_user,
    logout,
    refresh_tokens,
    register_user,
)

router = APIRouter(prefix="/auth", tags=["Authentication"])


# =============================================================================
# POST /auth/register
# =============================================================================

@router.post(
    "/register",
    response_model=RegisterResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Register a new user account",
    description=(
        "Creates a new user account and an associated USD wallet. "
        "Registration is atomic — if wallet creation fails, the user is also rolled back. "
        "Returns a JWT token pair on success so the client does not need to call /login separately."
    ),
    responses={
        201: {"description": "User registered successfully"},
        409: {"description": "Email address is already registered"},
        422: {"description": "Validation error (weak password, invalid email, etc.)"},
        429: {"description": "Rate limit exceeded (10 registrations per hour per IP)"},
    },
)
@limiter.limit("10/hour")
async def register(
    request: Request,
    body: RegisterRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> RegisterResponse:
    """
    Register a new user.

    The response includes both the user profile AND a token pair.
    This is a UX convenience: the user is immediately authenticated
    after registration without a separate login call.
    """
    user, access_token, refresh_token = await register_user(db, redis, body)

    return RegisterResponse(
        message="Registration successful. Welcome to PyWallet!",
        user=UserResponse.model_validate(user),
        tokens=TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
        ),
    )


# =============================================================================
# POST /auth/login
# =============================================================================

@router.post(
    "/login",
    response_model=LoginResponse,
    status_code=status.HTTP_200_OK,
    summary="Login with email and password",
    description=(
        "Authenticates a user by email and password. "
        "Returns an access token (15 minutes) and a refresh token (7 days). "
        "The access token must be sent in the Authorization header for protected routes: "
        "Authorization: Bearer <access_token>"
    ),
    responses={
        200: {"description": "Login successful"},
        401: {"description": "Invalid email or password"},
        403: {"description": "Account is disabled"},
        422: {"description": "Validation error"},
        429: {"description": "Rate limit exceeded (10 login attempts per hour per IP)"},
    },
)
@limiter.limit("10/hour")
async def login(
    request: Request,
    body: LoginRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> LoginResponse:
    """
    Authenticate a user and issue a JWT token pair.

    Security note: The error message is intentionally generic ("Invalid email or password")
    for both wrong email and wrong password cases. This prevents user enumeration —
    an attacker cannot determine which emails have accounts by testing error messages.
    """
    user, access_token, refresh_token = await login_user(db, redis, body)

    return LoginResponse(
        message="Login successful",
        user=UserResponse.model_validate(user),
        tokens=TokenResponse(
            access_token=access_token,
            refresh_token=refresh_token,
        ),
    )


# =============================================================================
# POST /auth/refresh
# =============================================================================

@router.post(
    "/refresh",
    response_model=RefreshResponse,
    status_code=status.HTTP_200_OK,
    summary="Rotate token pair using refresh token",
    description=(
        "Exchanges a valid refresh token for a NEW access token and a NEW refresh token. "
        "The old refresh token is immediately invalidated (rotation). "
        "If the submitted refresh token was already used (replay attack), "
        "ALL sessions for this user are terminated as a security measure."
    ),
    responses={
        200: {"description": "New token pair issued"},
        401: {
            "description": (
                "Refresh token is invalid, expired, already used, "
                "or the session was terminated"
            )
        },
    },
)
async def refresh(
    request: RefreshRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> RefreshResponse:
    """
    Rotate the token pair.

    Client workflow:
      1. Client stores both access_token and refresh_token after login.
      2. Client uses access_token for all API calls.
      3. When access_token expires (15 min), client calls POST /auth/refresh
         with the refresh_token.
      4. Client replaces BOTH stored tokens with the new ones from the response.
         (The old refresh_token is now invalid — do not use it again.)
      5. Repeat from step 2.
    """
    user, new_access_token, new_refresh_token = await refresh_tokens(
        db, redis, request.refresh_token
    )

    return RefreshResponse(
        message="Tokens refreshed successfully",
        tokens=TokenResponse(
            access_token=new_access_token,
            refresh_token=new_refresh_token,
        ),
    )


# =============================================================================
# POST /auth/logout
# =============================================================================

@router.post(
    "/logout",
    response_model=LogoutResponse,
    status_code=status.HTTP_200_OK,
    summary="Logout and invalidate refresh token",
    description=(
        "Deletes the user's refresh token from Redis, preventing any further "
        "token refresh operations. The current access token remains technically "
        "valid for up to 15 minutes (stateless — cannot be revoked without a blocklist). "
        "For high-security logout, use the access token blocklist (Phase 3)."
    ),
    responses={
        200: {"description": "Logged out successfully"},
        401: {"description": "Missing or invalid access token"},
    },
)
async def logout_endpoint(
    current_user: Annotated[User, Depends(get_current_user)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> LogoutResponse:
    """
    Logout the currently authenticated user.

    Requires a valid access token in the Authorization header.
    The user_id is extracted from the token — no request body needed.

    Why require auth for logout?
    Without it, anyone could logout any user by guessing their user_id.
    Requiring a valid access token proves the caller is (or was) the account owner.
    """
    await logout(redis, current_user.id)

    return LogoutResponse(message="Logged out successfully")


# =============================================================================
# GET /auth/me
# =============================================================================

@router.get(
    "/me",
    response_model=MeResponse,
    status_code=status.HTTP_200_OK,
    summary="Get current authenticated user's profile",
    description=(
        "Returns the profile of the currently authenticated user. "
        "Requires a valid access token. "
        "Useful for the client to display user information after login."
    ),
    responses={
        200: {"description": "User profile returned"},
        401: {"description": "Missing or invalid access token"},
    },
)
async def get_me(
    current_user: Annotated[User, Depends(get_current_user)],
) -> MeResponse:
    """
    Return the profile of the authenticated user.

    This endpoint demonstrates the Depends(get_current_user) pattern.
    FastAPI calls get_current_user before this function runs, validates the
    JWT, queries the DB, and injects the User object. The route handler
    simply returns it — no auth logic needed here.
    """
    return MeResponse(user=UserResponse.model_validate(current_user))
