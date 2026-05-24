"""
tests/test_rate_limits.py

Integration tests for the Phase 7 rate limiting layer.

Rate limit rules under test:
  - Transfer: 5 per 60 seconds per authenticated user (per-user, custom Redis INCR)
  - Auth:     10 per hour per IP (slowapi @limiter.limit("10/hour"))
  - Global:   100 per minute per IP (slowapi default_limits=["100/minute"])

Why integration tests instead of unit tests?
  The custom transfer rate limit uses a real Redis INCR+EXPIRE pipeline.
  The slowapi limits use a real LimitMemory/Redis backend.
  Both require a real Redis instance to exercise the actual counting logic.
  Unit tests with mock Redis can't verify the atomic INCR+EXPIRE NX pattern.

Run:
  docker compose exec api pytest tests/test_rate_limits.py -v
"""

import uuid

import pytest
from httpx import AsyncClient


def _ikey() -> str:
    return str(uuid.uuid4())


# =============================================================================
# Transfer rate limit — 5 per 60 seconds per user
# =============================================================================

@pytest.mark.slow
class TestTransferRateLimit:
    async def test_5th_transfer_succeeds(
        self, client: AsyncClient, funded_user: dict, second_user: dict, auth_headers: dict
    ):
        for i in range(5):
            resp = await client.post(
                "/api/v1/wallet/transfer",
                json={"receiver_email": second_user["email"], "amount": "1.00"},
                headers={**auth_headers, "Idempotency-Key": _ikey()},
            )
            # First 5 may succeed or fail on funds — but NOT 429
            assert resp.status_code != 429, (
                f"Request #{i+1} returned 429 — rate limit triggered too early"
            )

    async def test_6th_transfer_returns_429(
        self, client: AsyncClient, funded_user: dict, second_user: dict, auth_headers: dict
    ):
        # Send 5 transfers (within the limit)
        for _ in range(5):
            await client.post(
                "/api/v1/wallet/transfer",
                json={"receiver_email": second_user["email"], "amount": "1.00"},
                headers={**auth_headers, "Idempotency-Key": _ikey()},
            )
        # 6th transfer must be rate-limited
        resp = await client.post(
            "/api/v1/wallet/transfer",
            json={"receiver_email": second_user["email"], "amount": "1.00"},
            headers={**auth_headers, "Idempotency-Key": _ikey()},
        )
        assert resp.status_code == 429

    async def test_429_response_has_retry_after_header(
        self, client: AsyncClient, funded_user: dict, second_user: dict, auth_headers: dict
    ):
        for _ in range(5):
            await client.post(
                "/api/v1/wallet/transfer",
                json={"receiver_email": second_user["email"], "amount": "1.00"},
                headers={**auth_headers, "Idempotency-Key": _ikey()},
            )
        resp = await client.post(
            "/api/v1/wallet/transfer",
            json={"receiver_email": second_user["email"], "amount": "1.00"},
            headers={**auth_headers, "Idempotency-Key": _ikey()},
        )
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers

    async def test_transfer_rate_limit_is_per_user(
        self, client: AsyncClient, funded_user: dict, second_user: dict, auth_headers: dict
    ):
        # Primary user (funded_user) exhausts their rate limit
        for _ in range(6):
            await client.post(
                "/api/v1/wallet/transfer",
                json={"receiver_email": second_user["email"], "amount": "1.00"},
                headers={**auth_headers, "Idempotency-Key": _ikey()},
            )

        # Second user must NOT be rate-limited — limit is per-user
        # Deposit some funds to second user first
        second_headers = {"Authorization": f"Bearer {second_user['access_token']}"}
        await client.post(
            "/api/v1/wallet/deposit",
            json={"amount": "100.00"},
            headers={**second_headers, "Idempotency-Key": _ikey()},
        )
        resp = await client.post(
            "/api/v1/wallet/transfer",
            json={"receiver_email": funded_user["email"], "amount": "1.00"},
            headers={**second_headers, "Idempotency-Key": _ikey()},
        )
        # Second user should not be 429 (they haven't hit their own limit)
        assert resp.status_code != 429

    async def test_deposit_not_rate_limited_by_transfer_rule(
        self, client: AsyncClient, auth_headers: dict
    ):
        # Deposit has its own idempotency key limit, NOT the transfer rate limit
        # Fire 10 deposits — none should return 429 from the transfer rule
        for _ in range(8):
            resp = await client.post(
                "/api/v1/wallet/deposit",
                json={"amount": "1.00"},
                headers={**auth_headers, "Idempotency-Key": _ikey()},
            )
            assert resp.status_code != 429


# =============================================================================
# Auth rate limits — 10 per hour per IP (slowapi)
#
# NOTE: The conftest.py `test_redis` fixture flushes Redis DB 3 (rate limit DB)
# before each test, so each test starts with a fresh counter.
# These tests verify the STRUCTURE of rate limiting (that it returns 429
# after the limit), not the exact timing.
# =============================================================================

class TestAuthRateLimit:
    async def test_login_accepts_up_to_10_attempts(self, client: AsyncClient):
        # The first 10 login attempts (wrong password, right format) should NOT be 429
        for i in range(10):
            resp = await client.post(
                "/api/v1/auth/login",
                json={"email": f"ghost{i}@test.dev", "password": "WrongPass99"},
            )
            assert resp.status_code != 429, (
                f"Login attempt #{i+1} was rate-limited — limit triggered too early"
            )

    async def test_11th_login_returns_429(self, client: AsyncClient):
        for _ in range(10):
            await client.post(
                "/api/v1/auth/login",
                json={"email": "ghost@test.dev", "password": "WrongPass99"},
            )
        resp = await client.post(
            "/api/v1/auth/login",
            json={"email": "ghost@test.dev", "password": "WrongPass99"},
        )
        assert resp.status_code == 429

    async def test_register_accepts_up_to_10_attempts(self, client: AsyncClient):
        for i in range(10):
            resp = await client.post(
                "/api/v1/auth/register",
                json={
                    "email": f"newreg{i}_{uuid.uuid4().hex[:6]}@test.dev",
                    "full_name": "Reg User",
                    "password": "TestPass1",
                },
            )
            # May succeed (201) or fail (409 for email collision) but NOT 429
            assert resp.status_code != 429, (
                f"Register attempt #{i+1} was rate-limited too early"
            )

    async def test_11th_register_returns_429(self, client: AsyncClient):
        for i in range(10):
            await client.post(
                "/api/v1/auth/register",
                json={
                    "email": f"ratelim{i}_{uuid.uuid4().hex[:6]}@test.dev",
                    "full_name": "Reg",
                    "password": "TestPass1",
                },
            )
        resp = await client.post(
            "/api/v1/auth/register",
            json={
                "email": f"ratelim_final_{uuid.uuid4().hex[:6]}@test.dev",
                "full_name": "Final",
                "password": "TestPass1",
            },
        )
        assert resp.status_code == 429


# =============================================================================
# Rate limit isolation — unrelated endpoints not affected
# =============================================================================

class TestRateLimitIsolation:
    async def test_balance_check_not_limited_by_transfer_rule(
        self, client: AsyncClient, auth_headers: dict
    ):
        # GET /wallet/balance is not subject to the transfer rate limit
        for _ in range(15):
            resp = await client.get("/api/v1/wallet/balance", headers=auth_headers)
            assert resp.status_code not in (429,)

    async def test_transactions_endpoint_not_limited_by_transfer_rule(
        self, client: AsyncClient, auth_headers: dict
    ):
        for _ in range(15):
            resp = await client.get("/api/v1/wallet/transactions", headers=auth_headers)
            assert resp.status_code != 429
