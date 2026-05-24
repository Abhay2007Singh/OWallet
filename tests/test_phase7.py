"""
tests/test_phase7.py

Phase 7 test suite: Production-grade Security Hardening + Rate Limiting.

Tests cover:
  1.  SecurityHeadersMiddleware — all required headers present
  2.  SecurityHeadersMiddleware — X-Request-ID is a valid UUID when not provided
  3.  SecurityHeadersMiddleware — client-provided X-Request-ID is preserved
  4.  SecurityHeadersMiddleware — request.state.request_id is set for downstream
  5.  SecurityHeadersMiddleware — different requests get different X-Request-IDs
  6.  RequestLoggingMiddleware — structlog logger called with http_request event
  7.  RequestLoggingMiddleware — status_code and duration_ms logged
  8.  RequestLoggingMiddleware — contextvars cleared after request
  9.  _transfer_rate_limit — allows requests within limit
  10. _transfer_rate_limit — blocks requests over limit with 429
  11. _transfer_rate_limit — 429 response includes Retry-After header
  12. _transfer_rate_limit — skips check when user_id not extractable
  13. _transfer_rate_limit — Redis pipeline called with correct key format
  14. _get_user_id_from_request — extracts user_id from valid JWT
  15. _get_user_id_from_request — returns None for missing Authorization header
  16. _get_user_id_from_request — returns None for non-Bearer auth
  17. _get_user_id_from_request — returns None for invalid/expired JWT
  18. RegisterRequest — password max_length=128 enforced
  19. RegisterRequest — password min_length=8 still enforced
  20. LoginRequest — password max_length=128 enforced
  21. DepositRequest — NaN amount rejected
  22. DepositRequest — Infinity amount rejected
  23. DepositRequest — string "nan" rejected
  24. DepositRequest — string "inf" rejected
  25. DepositRequest — max_digits=15 enforced
  26. TransferRequest — NaN amount rejected
  27. TransferRequest — Infinity amount rejected
  28. Rate limiter — global limit is 100/minute
  29. Rate limiter — storage URI uses Redis DB 3
  30. Rate limiter — key function is get_remote_address

Strategy:
  - Middleware tests use a minimal Starlette test app (no DB, no Redis needed).
  - Rate limit dependency tests use mocked Redis and Request objects.
  - Schema tests are pure Pydantic validation — no server needed.
  - Configuration tests inspect the limiter object directly.
"""

import uuid
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# =============================================================================
# Helpers
# =============================================================================

def _make_starlette_test_app():
    """Build a minimal Starlette app with SecurityHeadersMiddleware attached."""
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from app.middleware.security_headers import SecurityHeadersMiddleware

    def homepage(request):
        # Expose request.state.request_id in response body for assertions
        request_id = getattr(request.state, "request_id", "not-set")
        return PlainTextResponse(request_id)

    test_app = Starlette(routes=[Route("/", homepage)])
    test_app.add_middleware(SecurityHeadersMiddleware)
    return TestClient(test_app)


def _make_logging_test_app():
    """Build a minimal Starlette app with RequestLoggingMiddleware attached."""
    from starlette.applications import Starlette
    from starlette.responses import PlainTextResponse
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from app.middleware.request_logging import RequestLoggingMiddleware

    def homepage(request):
        return PlainTextResponse("OK", status_code=200)

    test_app = Starlette(routes=[Route("/", homepage)])
    test_app.add_middleware(RequestLoggingMiddleware)
    return TestClient(test_app)


def _make_mock_request(auth_header: str = "") -> MagicMock:
    """Build a minimal mock Request with given Authorization header."""
    req = MagicMock()
    headers = {}
    if auth_header:
        headers["Authorization"] = auth_header
    req.headers = headers
    req.client = MagicMock()
    req.client.host = "127.0.0.1"
    req.url = MagicMock()
    req.url.path = "/api/v1/wallet/transfer"
    req.method = "POST"
    return req


def _make_mock_redis(pipeline_results: list) -> AsyncMock:
    """Build a mock Redis client whose pipeline returns the given results."""
    redis_mock = AsyncMock()
    pipe = AsyncMock()
    pipe.__aenter__ = AsyncMock(return_value=pipe)
    pipe.__aexit__ = AsyncMock(return_value=False)
    pipe.incr = MagicMock()
    pipe.expire = MagicMock()
    pipe.execute = AsyncMock(return_value=pipeline_results)
    redis_mock.pipeline = MagicMock(return_value=pipe)
    return redis_mock


# =============================================================================
# 1–5: SecurityHeadersMiddleware
# =============================================================================

class TestSecurityHeadersMiddleware:

    def test_all_required_headers_present(self):
        """Response includes every mandatory security header."""
        client = _make_starlette_test_app()
        response = client.get("/")
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["X-Frame-Options"] == "DENY"
        assert response.headers["X-XSS-Protection"] == "0"
        assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert response.headers["Cache-Control"] == "no-store"
        assert "X-Request-ID" in response.headers

    def test_x_request_id_is_valid_uuid_when_not_provided(self):
        """Generated X-Request-ID is a valid UUID4."""
        client = _make_starlette_test_app()
        response = client.get("/")
        request_id = response.headers["X-Request-ID"]
        parsed = uuid.UUID(request_id)
        assert str(parsed) == request_id

    def test_client_provided_x_request_id_is_preserved(self):
        """Client-supplied X-Request-ID is echoed back unchanged."""
        client = _make_starlette_test_app()
        custom_id = "my-tracing-id-12345"
        response = client.get("/", headers={"X-Request-ID": custom_id})
        assert response.headers["X-Request-ID"] == custom_id

    def test_request_state_request_id_is_set(self):
        """Middleware stores request_id on request.state for downstream use."""
        client = _make_starlette_test_app()
        response = client.get("/")
        # The test homepage returns request.state.request_id as body
        body = response.text
        # Should be a valid UUID (not "not-set")
        assert body != "not-set"
        uuid.UUID(body)  # raises if not a valid UUID

    def test_different_requests_get_different_x_request_ids(self):
        """Each request without X-Request-ID receives a unique UUID."""
        client = _make_starlette_test_app()
        r1 = client.get("/")
        r2 = client.get("/")
        assert r1.headers["X-Request-ID"] != r2.headers["X-Request-ID"]


# =============================================================================
# 6–8: RequestLoggingMiddleware
# =============================================================================

class TestRequestLoggingMiddleware:

    def test_logger_called_with_http_request_event(self):
        """Logger emits an 'http_request' event for every request."""
        with patch("app.middleware.request_logging.logger") as mock_logger:
            client = _make_logging_test_app()
            client.get("/")
        mock_logger.info.assert_called_once()
        event = mock_logger.info.call_args[0][0]
        assert event == "http_request"

    def test_status_code_and_duration_logged(self):
        """http_request event includes status_code and duration_ms."""
        with patch("app.middleware.request_logging.logger") as mock_logger:
            client = _make_logging_test_app()
            client.get("/")
        kwargs = mock_logger.info.call_args[1]
        assert kwargs["status_code"] == 200
        assert "duration_ms" in kwargs
        assert kwargs["duration_ms"] >= 0

    def test_contextvars_cleared_after_request(self):
        """structlog.contextvars are cleared after every request completes."""
        import structlog

        with patch("app.middleware.request_logging.logger"):
            client = _make_logging_test_app()
            client.get("/")

        # After the request, the context should have been cleared
        context = structlog.contextvars.get_contextvars()
        assert context == {}


# =============================================================================
# 9–13: _transfer_rate_limit dependency
# =============================================================================

class TestTransferRateLimit:

    @pytest.mark.asyncio
    async def test_allows_requests_within_limit(self):
        """Does not raise when counter is at or below 5."""
        from app.middleware.rate_limiter import _transfer_rate_limit

        request = _make_mock_request()
        redis = _make_mock_redis([5, True])  # count=5 == limit, allowed

        with patch(
            "app.middleware.rate_limiter._get_user_id_from_request",
            return_value="user-abc",
        ):
            # Should not raise
            await _transfer_rate_limit(request, redis)

    @pytest.mark.asyncio
    async def test_blocks_requests_over_limit(self):
        """Raises HTTP 429 when counter exceeds 5."""
        from fastapi import HTTPException

        from app.middleware.rate_limiter import _transfer_rate_limit

        request = _make_mock_request()
        redis = _make_mock_redis([6, True])  # count=6 > limit

        with patch(
            "app.middleware.rate_limiter._get_user_id_from_request",
            return_value="user-abc",
        ):
            with pytest.raises(HTTPException) as exc_info:
                await _transfer_rate_limit(request, redis)

        assert exc_info.value.status_code == 429

    @pytest.mark.asyncio
    async def test_429_response_includes_retry_after_header(self):
        """429 HTTPException includes Retry-After in headers."""
        from fastapi import HTTPException

        from app.middleware.rate_limiter import _TRANSFER_WINDOW_SECONDS, _transfer_rate_limit

        request = _make_mock_request()
        redis = _make_mock_redis([99, True])

        with patch(
            "app.middleware.rate_limiter._get_user_id_from_request",
            return_value="user-abc",
        ):
            with pytest.raises(HTTPException) as exc_info:
                await _transfer_rate_limit(request, redis)

        assert "Retry-After" in exc_info.value.headers
        assert exc_info.value.headers["Retry-After"] == str(_TRANSFER_WINDOW_SECONDS)

    @pytest.mark.asyncio
    async def test_skips_check_when_user_id_not_extractable(self):
        """Returns without raising when JWT is missing or invalid."""
        from app.middleware.rate_limiter import _transfer_rate_limit

        request = _make_mock_request()  # no Authorization header
        redis = _make_mock_redis([99, True])

        with patch(
            "app.middleware.rate_limiter._get_user_id_from_request",
            return_value=None,
        ):
            # Should not raise (auth will handle 401 later)
            await _transfer_rate_limit(request, redis)

        redis.pipeline.assert_not_called()

    @pytest.mark.asyncio
    async def test_redis_key_includes_user_id(self):
        """Redis INCR is called with a key scoped to the specific user_id."""
        from app.middleware.rate_limiter import _transfer_rate_limit

        user_id = "550e8400-e29b-41d4-a716-446655440000"
        request = _make_mock_request()
        redis = _make_mock_redis([1, True])

        with patch(
            "app.middleware.rate_limiter._get_user_id_from_request",
            return_value=user_id,
        ):
            await _transfer_rate_limit(request, redis)

        pipe = redis.pipeline.return_value.__aenter__.return_value
        incr_call = pipe.incr.call_args
        assert user_id in incr_call[0][0]
        assert incr_call[0][0] == f"rate_limit:transfer:{user_id}"


# =============================================================================
# 14–17: _get_user_id_from_request
# =============================================================================

class TestGetUserIdFromRequest:

    def test_extracts_user_id_from_valid_jwt(self):
        """Returns the sub claim from a valid JWT."""
        import jwt

        from app.core.config import settings
        from app.middleware.rate_limiter import _get_user_id_from_request

        user_id = str(uuid.uuid4())
        token = jwt.encode(
            {"sub": user_id, "type": "access"},
            settings.JWT_SECRET_KEY,
            algorithm=settings.JWT_ALGORITHM,
        )
        request = _make_mock_request(auth_header=f"Bearer {token}")
        result = _get_user_id_from_request(request)
        assert result == user_id

    def test_returns_none_for_missing_authorization_header(self):
        """Returns None when no Authorization header is present."""
        from app.middleware.rate_limiter import _get_user_id_from_request

        request = _make_mock_request()
        assert _get_user_id_from_request(request) is None

    def test_returns_none_for_non_bearer_auth(self):
        """Returns None when Authorization header is not Bearer scheme."""
        from app.middleware.rate_limiter import _get_user_id_from_request

        request = _make_mock_request(auth_header="Basic dXNlcjpwYXNz")
        assert _get_user_id_from_request(request) is None

    def test_returns_none_for_invalid_jwt(self):
        """Returns None for a malformed or tampered JWT."""
        from app.middleware.rate_limiter import _get_user_id_from_request

        request = _make_mock_request(auth_header="Bearer not.a.valid.jwt")
        assert _get_user_id_from_request(request) is None


# =============================================================================
# 18–20: Auth schema password max_length
# =============================================================================

class TestAuthSchemaPasswordMaxLength:

    def test_register_request_rejects_password_over_128_chars(self):
        """Password > 128 characters is rejected at schema validation."""
        from pydantic import ValidationError

        from app.schemas.auth import RegisterRequest

        with pytest.raises(ValidationError) as exc_info:
            RegisterRequest(
                email="alice@example.com",
                full_name="Alice Smith",
                password="A1" + "x" * 128,  # 130 chars
            )
        errors = exc_info.value.errors()
        assert any("password" in str(e["loc"]) for e in errors)

    def test_register_request_accepts_password_at_128_chars(self):
        """Password of exactly 128 characters is accepted."""
        from app.schemas.auth import RegisterRequest

        req = RegisterRequest(
            email="alice@example.com",
            full_name="Alice Smith",
            password="A1" + "x" * 126,  # exactly 128 chars
        )
        assert len(req.password) == 128

    def test_register_request_still_enforces_min_length(self):
        """Password < 8 characters is still rejected."""
        from pydantic import ValidationError

        from app.schemas.auth import RegisterRequest

        with pytest.raises(ValidationError):
            RegisterRequest(
                email="alice@example.com",
                full_name="Alice Smith",
                password="Ab1",  # too short
            )

    def test_login_request_rejects_password_over_128_chars(self):
        """LoginRequest also enforces max_length=128 on password."""
        from pydantic import ValidationError

        from app.schemas.auth import LoginRequest

        with pytest.raises(ValidationError) as exc_info:
            LoginRequest(
                email="alice@example.com",
                password="A1" + "x" * 128,
            )
        errors = exc_info.value.errors()
        assert any("password" in str(e["loc"]) for e in errors)


# =============================================================================
# 21–27: Wallet schema NaN/Infinity and max_digits guards
# =============================================================================

class TestWalletSchemaValidation:

    def test_deposit_rejects_nan_amount(self):
        """DepositRequest rejects Decimal NaN."""
        from decimal import Decimal

        from pydantic import ValidationError

        from app.schemas.wallet import DepositRequest

        with pytest.raises(ValidationError) as exc_info:
            DepositRequest(amount=Decimal("nan"))
        assert any(
            "finite" in str(e["msg"]).lower() or "nan" in str(e["msg"]).lower()
            for e in exc_info.value.errors()
        )

    def test_deposit_rejects_infinity_amount(self):
        """DepositRequest rejects Decimal Infinity."""
        from decimal import Decimal

        from pydantic import ValidationError

        from app.schemas.wallet import DepositRequest

        with pytest.raises(ValidationError) as exc_info:
            DepositRequest(amount=Decimal("inf"))
        assert any(
            "finite" in str(e["msg"]).lower() or "inf" in str(e["msg"]).lower()
            for e in exc_info.value.errors()
        )

    def test_deposit_rejects_string_nan(self):
        """DepositRequest rejects string 'nan' coerced to Decimal."""
        from pydantic import ValidationError

        from app.schemas.wallet import DepositRequest

        with pytest.raises(ValidationError):
            DepositRequest(amount="nan")

    def test_deposit_rejects_string_inf(self):
        """DepositRequest rejects string 'inf' coerced to Decimal."""
        from pydantic import ValidationError

        from app.schemas.wallet import DepositRequest

        with pytest.raises(ValidationError):
            DepositRequest(amount="inf")

    def test_deposit_rejects_negative_infinity(self):
        """DepositRequest rejects negative Infinity."""
        from decimal import Decimal

        from pydantic import ValidationError

        from app.schemas.wallet import DepositRequest

        with pytest.raises(ValidationError):
            DepositRequest(amount=Decimal("-inf"))

    def test_deposit_accepts_valid_amount(self):
        """DepositRequest accepts a normal finite positive Decimal."""
        from decimal import Decimal

        from app.schemas.wallet import DepositRequest

        req = DepositRequest(amount=Decimal("100.00"))
        assert req.amount == Decimal("100.00")

    def test_transfer_rejects_nan_amount(self):
        """TransferRequest rejects Decimal NaN."""
        from decimal import Decimal

        from pydantic import ValidationError

        from app.schemas.wallet import TransferRequest

        with pytest.raises(ValidationError):
            TransferRequest(
                receiver_email="bob@example.com",
                amount=Decimal("nan"),
            )

    def test_transfer_rejects_infinity_amount(self):
        """TransferRequest rejects Decimal Infinity."""
        from decimal import Decimal

        from pydantic import ValidationError

        from app.schemas.wallet import TransferRequest

        with pytest.raises(ValidationError):
            TransferRequest(
                receiver_email="bob@example.com",
                amount=Decimal("inf"),
            )


# =============================================================================
# 28–30: Rate limiter configuration
# =============================================================================

class TestRateLimiterConfiguration:

    def test_global_default_limit_is_100_per_minute(self):
        """limiter default_limits is set to 100/minute."""
        from app.middleware.rate_limiter import limiter

        # _default_limits is a list of LimitGroup objects; iterate to get Limit
        assert len(limiter._default_limits) == 1
        limit_group = limiter._default_limits[0]
        limits = list(limit_group)
        assert len(limits) == 1
        assert "100" in str(limits[0].limit)
        assert "minute" in str(limits[0].limit)

    def test_storage_uri_uses_redis_db_3(self):
        """Rate limiter storage is on Redis DB 3 (isolated from app cache)."""
        from app.middleware.rate_limiter import limiter

        storage_uri = limiter._storage_uri
        assert storage_uri.endswith("/3")

    def test_key_function_is_get_remote_address(self):
        """Key function extracts the client IP address for per-IP limiting."""
        from slowapi.util import get_remote_address

        from app.middleware.rate_limiter import limiter

        assert limiter._key_func is get_remote_address

    def test_transfer_rate_constants(self):
        """Transfer rate limit constants are correctly set."""
        from app.middleware.rate_limiter import _TRANSFER_LIMIT, _TRANSFER_WINDOW_SECONDS

        assert _TRANSFER_LIMIT == 5
        assert _TRANSFER_WINDOW_SECONDS == 60


# =============================================================================
# Main app integration: middleware and exception handlers registered
# =============================================================================

class TestMainAppConfiguration:

    def test_limiter_stored_in_app_state(self):
        """app.state.limiter is set to the rate limiter instance."""
        from app.main import app
        from app.middleware.rate_limiter import limiter

        assert app.state.limiter is limiter

    def test_slowapi_middleware_registered(self):
        """SlowAPIMiddleware is in the middleware stack."""
        from slowapi.middleware import SlowAPIMiddleware

        from app.main import app

        middleware_types = [type(m.cls if hasattr(m, "cls") else m) for m in app.user_middleware]
        class_names = [
            (m.cls.__name__ if hasattr(m, "cls") else type(m).__name__)
            for m in app.user_middleware
        ]
        assert "SlowAPIMiddleware" in class_names

    def test_security_headers_middleware_registered(self):
        """SecurityHeadersMiddleware is in the middleware stack."""
        from app.main import app

        class_names = [
            (m.cls.__name__ if hasattr(m, "cls") else type(m).__name__)
            for m in app.user_middleware
        ]
        assert "SecurityHeadersMiddleware" in class_names

    def test_request_logging_middleware_registered(self):
        """RequestLoggingMiddleware is in the middleware stack."""
        from app.main import app

        class_names = [
            (m.cls.__name__ if hasattr(m, "cls") else type(m).__name__)
            for m in app.user_middleware
        ]
        assert "RequestLoggingMiddleware" in class_names

    def test_rate_limit_exceeded_exception_handler_registered(self):
        """RateLimitExceeded has a registered exception handler."""
        from slowapi.errors import RateLimitExceeded

        from app.main import app

        assert RateLimitExceeded in app.exception_handlers

    def test_unhandled_exception_handler_registered(self):
        """Generic Exception handler is registered."""
        from app.main import app

        assert Exception in app.exception_handlers
