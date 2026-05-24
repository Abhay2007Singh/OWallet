"""
app/schemas/auth.py

Pydantic v2 request/response schemas for the authentication system.

Why separate schemas from models?
  SQLAlchemy models define HOW data is stored in PostgreSQL (column types,
  constraints, relationships). Pydantic schemas define WHAT the API accepts
  and returns. They are not the same:
  - The User model has hashed_password → UserResponse schema NEVER does.
  - The User model has is_verified, role → may be hidden from some responses.
  - The User model timestamps are datetime → schema exposes them as ISO strings.

  This separation (Repository Pattern) means changing the DB schema doesn't
  automatically break the API contract, and vice versa.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Why password validation lives in the schema (not the service)?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Input validation should fail as early as possible, before any business
logic or DB access. If the password is too short, we should return 422
immediately — not after querying the DB to check email uniqueness.
Pydantic validators run before the route handler body executes.
"""

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.user import UserRole


# =============================================================================
# Request Schemas — validate incoming data from the client
# =============================================================================

class RegisterRequest(BaseModel):
    """
    Request body for POST /auth/register.

    EmailStr performs RFC 5322 email validation — no custom regex needed.
    The password field_validator enforces strength rules that cannot be
    expressed in Field() constraints alone.
    """

    email: EmailStr = Field(
        examples=["alice@example.com"],
        description="Must be a valid email address. Used as the login identifier.",
    )

    full_name: str = Field(
        min_length=2,
        max_length=100,
        examples=["Alice Smith"],
        description="User's full name. Shown on statements and profile.",
    )

    password: str = Field(
        min_length=8,
        max_length=128,
        examples=["SecurePass123"],
        description=(
            "Minimum 8 characters, maximum 128. "
            "Must contain at least one letter and one digit. "
            "Passwords longer than 72 bytes are silently truncated by bcrypt — "
            "the 128-char cap prevents DoS via intentionally long inputs."
        ),
    )

    phone_number: str | None = Field(
        default=None,
        examples=["+2348012345678"],
        description="Optional. International format recommended.",
    )

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, v: str) -> str:
        """
        Enforce password strength beyond simple length.

        Minimum requirements (production systems typically require more):
        - At least 8 characters (enforced by Field min_length above)
        - At least one alphabetic character (can't be all digits)
        - At least one digit (can't be all letters)

        We deliberately keep requirements minimal — overly complex rules
        cause users to write passwords on sticky notes. The bcrypt cost
        factor does the heavy lifting against brute-force.
        """
        if not re.search(r"[A-Za-z]", v):
            raise ValueError("Password must contain at least one letter")
        if not re.search(r"\d", v):
            raise ValueError("Password must contain at least one digit")
        return v

    @field_validator("full_name")
    @classmethod
    def validate_full_name(cls, v: str) -> str:
        """Strip surrounding whitespace and reject blank strings."""
        stripped = v.strip()
        if not stripped:
            raise ValueError("Full name cannot be empty or whitespace only")
        return stripped

    @field_validator("phone_number")
    @classmethod
    def validate_phone_number(cls, v: str | None) -> str | None:
        """Basic E.164 format check if phone number is provided."""
        if v is None:
            return v
        # Strip whitespace and common formatting characters
        cleaned = re.sub(r"[\s\-\(\)]", "", v)
        if not re.match(r"^\+?\d{7,15}$", cleaned):
            raise ValueError("Phone number must be 7-15 digits, optionally prefixed with +")
        return cleaned


class LoginRequest(BaseModel):
    """
    Request body for POST /auth/login.

    Kept intentionally minimal — no password strength validation here
    because we're verifying against a stored hash, not creating a new one.
    """

    email: EmailStr = Field(examples=["alice@example.com"])
    password: str = Field(
        max_length=128,
        examples=["SecurePass123"],
        description="Maximum 128 characters. Prevents bcrypt DoS via intentionally long inputs.",
    )


class RefreshRequest(BaseModel):
    """
    Request body for POST /auth/refresh.

    The refresh_token is the full JWT string previously issued by login or refresh.
    It is NOT an opaque string — it's a valid JWT that we decode to extract user_id.
    We then compare the full token string against what's stored in Redis.
    """

    refresh_token: str = Field(
        description="The refresh JWT token received from /auth/login or /auth/refresh.",
        examples=["eyJhbGci..."],
    )


# =============================================================================
# Response Schemas — shape data returned to the client
# =============================================================================

class TokenResponse(BaseModel):
    """
    Token pair returned after successful login or token refresh.

    expires_in: Access token lifetime in seconds (900 = 15 minutes).
    The client should start a refresh timer using this value rather than
    parsing the JWT — parsing JWTs client-side creates a dependency on the
    JWT format which should be treated as an opaque implementation detail.
    """

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int = Field(
        default=900,  # 15 * 60 — matches JWT_ACCESS_TOKEN_EXPIRE_MINUTES
        description="Access token lifetime in seconds. Refresh before this elapses.",
    )


class UserResponse(BaseModel):
    """
    Safe user representation returned in API responses.

    model_config with from_attributes=True enables ORM mode:
    Pydantic can read attributes directly from SQLAlchemy model instances
    instead of requiring a dict. Without this, `UserResponse.model_validate(user)`
    would fail because `user` is a SQLAlchemy object, not a dict.

    SECURITY: hashed_password is intentionally absent from this schema.
    Even if a bug in the service layer accidentally includes hashed_password
    in the returned User object, Pydantic will simply not serialize it because
    the field is not declared here. Defense in depth.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    email: str
    full_name: str
    phone_number: str | None
    role: UserRole
    is_active: bool
    is_verified: bool
    created_at: datetime
    updated_at: datetime

    # hashed_password — intentionally excluded. No accidental leakage.


class RegisterResponse(BaseModel):
    """Response for POST /auth/register (HTTP 201)."""

    message: str = "Registration successful"
    user: UserResponse
    tokens: TokenResponse


class LoginResponse(BaseModel):
    """Response for POST /auth/login (HTTP 200)."""

    message: str = "Login successful"
    user: UserResponse
    tokens: TokenResponse


class RefreshResponse(BaseModel):
    """Response for POST /auth/refresh (HTTP 200)."""

    message: str = "Tokens refreshed successfully"
    tokens: TokenResponse


class LogoutResponse(BaseModel):
    """Response for POST /auth/logout (HTTP 200)."""

    message: str = "Logged out successfully"


class MeResponse(BaseModel):
    """Response for GET /auth/me — authenticated user's own profile."""

    user: UserResponse
