"""
tests/test_idempotency.py

Comprehensive test suite for Phase 5 idempotency system.

Test sections:
  1. Unit tests for idempotency_service functions (pure logic, all mocked)
  2. Integration tests for IdempotentRoute middleware behavior
  3. Double-charge prevention scenarios

Run with:
    pytest tests/test_idempotency.py -v

Prerequisites:
    pip install pytest pytest-asyncio httpx
    pytest.ini or pyproject.toml must set asyncio_mode = "auto"
"""

import asyncio
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.routing import APIRouter
from httpx import ASGITransport, AsyncClient

# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

SAMPLE_USER_ID = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
SAMPLE_ENDPOINT = "/api/v1/wallet/transfer"
SAMPLE_BODY = b'{"receiver_email":"bob@example.com","amount":"100.00"}'
SAMPLE_KEY = "test-idempotency-key-001"


def make_mock_redis(*, set_returns: Any = True, get_returns: Any = None) -> AsyncMock:
    """Build a mock Redis client for common test scenarios."""
    redis = AsyncMock()
    redis.set = AsyncMock(return_value=set_returns)
    redis.get = AsyncMock(return_value=get_returns)
    redis.delete = AsyncMock(return_value=1)
    return redis


def make_mock_db_session(*, existing_record: Any = None) -> AsyncMock:
    """Build a mock AsyncSession that returns `existing_record` on execute."""
    db = AsyncMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = existing_record
    db.execute = AsyncMock(return_value=result)
    db.add = MagicMock()
    db.commit = AsyncMock()
    return db


# ═════════════════════════════════════════════════════════════════════════════
# Section 1: Unit tests for idempotency_service.py
# ═════════════════════════════════════════════════════════════════════════════


class TestComputeRequestHash:
    """
    compute_request_hash is a pure function — no mocks needed.

    It must satisfy:
      - Deterministic: same inputs → same output.
      - Sensitive: any input change → completely different output.
      - Fixed length: always 64 hex chars (SHA256).
    """

    def test_same_inputs_produce_identical_hash(self):
        from app.services.idempotency_service import compute_request_hash

        h1 = compute_request_hash(SAMPLE_BODY, SAMPLE_USER_ID, SAMPLE_ENDPOINT)
        h2 = compute_request_hash(SAMPLE_BODY, SAMPLE_USER_ID, SAMPLE_ENDPOINT)
        assert h1 == h2

    def test_different_user_produces_different_hash(self):
        from app.services.idempotency_service import compute_request_hash

        other_user = uuid.UUID("ffffffff-bbbb-cccc-dddd-eeeeeeeeeeee")
        h1 = compute_request_hash(SAMPLE_BODY, SAMPLE_USER_ID, SAMPLE_ENDPOINT)
        h2 = compute_request_hash(SAMPLE_BODY, other_user, SAMPLE_ENDPOINT)
        assert h1 != h2, "Different users must produce different hashes"

    def test_different_endpoint_produces_different_hash(self):
        from app.services.idempotency_service import compute_request_hash

        deposit_endpoint = "/api/v1/wallet/deposit"
        h1 = compute_request_hash(SAMPLE_BODY, SAMPLE_USER_ID, SAMPLE_ENDPOINT)
        h2 = compute_request_hash(SAMPLE_BODY, SAMPLE_USER_ID, deposit_endpoint)
        assert h1 != h2, "Different endpoints must produce different hashes"

    def test_different_body_produces_different_hash(self):
        from app.services.idempotency_service import compute_request_hash

        other_body = b'{"receiver_email":"bob@example.com","amount":"200.00"}'
        h1 = compute_request_hash(SAMPLE_BODY, SAMPLE_USER_ID, SAMPLE_ENDPOINT)
        h2 = compute_request_hash(other_body, SAMPLE_USER_ID, SAMPLE_ENDPOINT)
        assert h1 != h2, "Different body must produce different hash"

    def test_output_is_64_char_lowercase_hex(self):
        from app.services.idempotency_service import compute_request_hash

        h = compute_request_hash(SAMPLE_BODY, SAMPLE_USER_ID, SAMPLE_ENDPOINT)
        assert len(h) == 64, "SHA256 hex digest must be 64 characters"
        assert h == h.lower(), "Must be lowercase hex"
        assert all(c in "0123456789abcdef" for c in h), "Must be valid hex"

    def test_matches_manual_sha256(self):
        """Verify the hash matches a manually computed SHA256."""
        from app.services.idempotency_service import compute_request_hash

        # Manual computation: same logic as the function
        expected = hashlib.sha256()
        expected.update(str(SAMPLE_USER_ID).encode("utf-8"))
        expected.update(SAMPLE_ENDPOINT.encode("utf-8"))
        expected.update(SAMPLE_BODY)

        result = compute_request_hash(SAMPLE_BODY, SAMPLE_USER_ID, SAMPLE_ENDPOINT)
        assert result == expected.hexdigest()


@pytest.mark.asyncio
class TestAcquireLock:
    """
    acquire_lock wraps Redis SET NX EX.

    Key behaviors:
      - Returns True when Redis returns True (lock acquired).
      - Returns False when Redis returns None (key already exists — locked).
      - Lock key includes user_id, endpoint, and idempotency_key.
      - Uses NX=True (set only if not exists) and EX=30 (auto-expire).
    """

    async def test_returns_true_when_lock_available(self):
        from app.services.idempotency_service import acquire_lock

        redis = make_mock_redis(set_returns=True)
        result = await acquire_lock(redis, SAMPLE_USER_ID, SAMPLE_ENDPOINT, SAMPLE_KEY)

        assert result is True
        redis.set.assert_awaited_once()

    async def test_returns_false_when_lock_already_held(self):
        from app.services.idempotency_service import acquire_lock

        # Redis SET NX returns None when key already exists
        redis = make_mock_redis(set_returns=None)
        result = await acquire_lock(redis, SAMPLE_USER_ID, SAMPLE_ENDPOINT, SAMPLE_KEY)

        assert result is False

    async def test_lock_key_contains_user_id(self):
        from app.services.idempotency_service import acquire_lock

        redis = make_mock_redis(set_returns=True)
        await acquire_lock(redis, SAMPLE_USER_ID, SAMPLE_ENDPOINT, SAMPLE_KEY)

        lock_key = redis.set.call_args[0][0]
        assert str(SAMPLE_USER_ID) in lock_key

    async def test_lock_key_contains_idempotency_key(self):
        from app.services.idempotency_service import acquire_lock

        redis = make_mock_redis(set_returns=True)
        await acquire_lock(redis, SAMPLE_USER_ID, SAMPLE_ENDPOINT, SAMPLE_KEY)

        lock_key = redis.set.call_args[0][0]
        assert SAMPLE_KEY in lock_key

    async def test_uses_nx_true(self):
        """NX=True ensures SET is atomic: set ONLY if not exists."""
        from app.services.idempotency_service import acquire_lock

        redis = make_mock_redis(set_returns=True)
        await acquire_lock(redis, SAMPLE_USER_ID, SAMPLE_ENDPOINT, SAMPLE_KEY)

        kwargs = redis.set.call_args[1]
        assert kwargs.get("nx") is True, "Must use NX=True for atomic conditional set"

    async def test_uses_expire_30_seconds(self):
        """EX=30 ensures the lock auto-expires if the server crashes."""
        from app.services.idempotency_service import LOCK_EXPIRE_SECONDS, acquire_lock

        redis = make_mock_redis(set_returns=True)
        await acquire_lock(redis, SAMPLE_USER_ID, SAMPLE_ENDPOINT, SAMPLE_KEY)

        kwargs = redis.set.call_args[1]
        assert kwargs.get("ex") == LOCK_EXPIRE_SECONDS

    async def test_two_different_users_get_different_lock_keys(self):
        from app.services.idempotency_service import acquire_lock

        user_a = uuid.uuid4()
        user_b = uuid.uuid4()

        redis_a = make_mock_redis(set_returns=True)
        redis_b = make_mock_redis(set_returns=True)

        await acquire_lock(redis_a, user_a, SAMPLE_ENDPOINT, SAMPLE_KEY)
        await acquire_lock(redis_b, user_b, SAMPLE_ENDPOINT, SAMPLE_KEY)

        lock_key_a = redis_a.set.call_args[0][0]
        lock_key_b = redis_b.set.call_args[0][0]
        assert lock_key_a != lock_key_b, "Different users must use different lock keys"


@pytest.mark.asyncio
class TestReleaseLock:
    """
    release_lock wraps Redis DEL.

    Key behaviors:
      - Calls redis.delete with the correct lock key.
      - Does NOT raise if the key doesn't exist (idempotent release).
      - Does NOT raise if Redis is unavailable (swallows exception, logs warning).
    """

    async def test_deletes_lock_key(self):
        from app.services.idempotency_service import release_lock

        redis = AsyncMock()
        redis.delete = AsyncMock(return_value=1)

        await release_lock(redis, SAMPLE_USER_ID, SAMPLE_ENDPOINT, SAMPLE_KEY)

        redis.delete.assert_awaited_once()
        lock_key = redis.delete.call_args[0][0]
        assert SAMPLE_KEY in lock_key
        assert str(SAMPLE_USER_ID) in lock_key

    async def test_does_not_raise_when_key_missing(self):
        """Redis DEL on a non-existent key returns 0 — should not raise."""
        from app.services.idempotency_service import release_lock

        redis = AsyncMock()
        redis.delete = AsyncMock(return_value=0)  # key didn't exist

        # Must not raise
        await release_lock(redis, SAMPLE_USER_ID, SAMPLE_ENDPOINT, SAMPLE_KEY)

    async def test_swallows_redis_exception(self):
        """If Redis is down during release, log and continue — never raise."""
        from app.services.idempotency_service import release_lock

        redis = AsyncMock()
        redis.delete = AsyncMock(side_effect=ConnectionError("Redis down"))

        # Must not raise — the lock will auto-expire via TTL
        await release_lock(redis, SAMPLE_USER_ID, SAMPLE_ENDPOINT, SAMPLE_KEY)


@pytest.mark.asyncio
class TestCheckExistingKey:
    """
    check_existing_key queries the DB for a non-expired idempotency record.

    Key behaviors:
      - Returns None when no record exists.
      - Returns the record when found with future expires_at.
      - Filters by user_id, endpoint, AND key (never cross-user replay).
    """

    async def test_returns_none_when_not_found(self):
        from app.services.idempotency_service import check_existing_key

        db = make_mock_db_session(existing_record=None)
        result = await check_existing_key(db, SAMPLE_USER_ID, SAMPLE_ENDPOINT, SAMPLE_KEY)

        assert result is None

    async def test_returns_record_when_found(self):
        from app.services.idempotency_service import check_existing_key

        mock_record = MagicMock()
        mock_record.key = SAMPLE_KEY
        mock_record.expires_at = datetime.now(tz=timezone.utc) + timedelta(hours=23)

        db = make_mock_db_session(existing_record=mock_record)
        result = await check_existing_key(db, SAMPLE_USER_ID, SAMPLE_ENDPOINT, SAMPLE_KEY)

        assert result is mock_record

    async def test_executes_query_against_db(self):
        from app.services.idempotency_service import check_existing_key

        db = make_mock_db_session(existing_record=None)
        await check_existing_key(db, SAMPLE_USER_ID, SAMPLE_ENDPOINT, SAMPLE_KEY)

        db.execute.assert_awaited_once()


@pytest.mark.asyncio
class TestStoreIdempotencyResponse:
    """
    store_idempotency_response inserts a new idempotency record.

    Key behaviors:
      - Creates an IdempotencyKey instance and adds it to the DB.
      - Commits the session.
      - Sets expires_at to 24 hours from now.
      - Parses the response body JSON and stores it as a dict (for JSONB).
      - Sets status=COMPLETED.
    """

    async def test_adds_record_and_commits(self):
        from app.services.idempotency_service import store_idempotency_response

        db = AsyncMock()
        db.add = MagicMock()
        db.commit = AsyncMock()

        await store_idempotency_response(
            db=db,
            user_id=SAMPLE_USER_ID,
            endpoint=SAMPLE_ENDPOINT,
            key=SAMPLE_KEY,
            request_hash="a" * 64,
            response_body=b'{"transfer_reference_id":"some-uuid","amount":"100.00"}',
            http_status_code=200,
        )

        db.add.assert_called_once()
        db.commit.assert_awaited_once()

    async def test_parses_response_body_as_dict(self):
        """response_body must be a dict (stored as JSONB), not a string."""
        from app.services.idempotency_service import store_idempotency_response
        from app.models.idempotency_key import IdempotencyKey

        stored_records = []

        db = AsyncMock()
        db.add = MagicMock(side_effect=lambda r: stored_records.append(r))
        db.commit = AsyncMock()

        await store_idempotency_response(
            db=db,
            user_id=SAMPLE_USER_ID,
            endpoint=SAMPLE_ENDPOINT,
            key=SAMPLE_KEY,
            request_hash="a" * 64,
            response_body=b'{"amount":"100.00","sender_new_balance":"400.00"}',
            http_status_code=200,
        )

        record = stored_records[0]
        assert isinstance(record.response_body, dict)
        assert record.response_body["amount"] == "100.00"

    async def test_sets_correct_http_status_code(self):
        from app.services.idempotency_service import store_idempotency_response

        stored_records = []
        db = AsyncMock()
        db.add = MagicMock(side_effect=lambda r: stored_records.append(r))
        db.commit = AsyncMock()

        await store_idempotency_response(
            db=db,
            user_id=SAMPLE_USER_ID,
            endpoint="/api/v1/wallet/deposit",
            key="deposit-key-001",
            request_hash="b" * 64,
            response_body=b'{"new_balance":"500.00"}',
            http_status_code=201,
        )

        record = stored_records[0]
        assert record.http_status_code == 201

    async def test_sets_expires_at_24_hours_from_now(self):
        from app.services.idempotency_service import KEY_TTL_SECONDS, store_idempotency_response

        stored_records = []
        db = AsyncMock()
        db.add = MagicMock(side_effect=lambda r: stored_records.append(r))
        db.commit = AsyncMock()

        before = datetime.now(tz=timezone.utc)
        await store_idempotency_response(
            db=db,
            user_id=SAMPLE_USER_ID,
            endpoint=SAMPLE_ENDPOINT,
            key=SAMPLE_KEY,
            request_hash="c" * 64,
            response_body=b'{"result":"ok"}',
            http_status_code=200,
        )
        after = datetime.now(tz=timezone.utc)

        record = stored_records[0]
        expected_min = before + timedelta(seconds=KEY_TTL_SECONDS)
        expected_max = after + timedelta(seconds=KEY_TTL_SECONDS)
        assert expected_min <= record.expires_at <= expected_max

    async def test_sets_status_completed(self):
        from app.services.idempotency_service import store_idempotency_response
        from app.models.idempotency_key import IdempotencyStatus

        stored_records = []
        db = AsyncMock()
        db.add = MagicMock(side_effect=lambda r: stored_records.append(r))
        db.commit = AsyncMock()

        await store_idempotency_response(
            db=db,
            user_id=SAMPLE_USER_ID,
            endpoint=SAMPLE_ENDPOINT,
            key=SAMPLE_KEY,
            request_hash="d" * 64,
            response_body=b'{"result":"ok"}',
            http_status_code=200,
        )

        assert stored_records[0].status == IdempotencyStatus.COMPLETED


# ═════════════════════════════════════════════════════════════════════════════
# Section 2: Middleware integration tests
# ═════════════════════════════════════════════════════════════════════════════
#
# These tests create a minimal FastAPI app with IdempotentRoute applied to a
# /test/pay endpoint. All external dependencies (Redis, DB) are mocked via
# unittest.mock.patch.
#
# Why a minimal test app instead of the full PyWallet app?
#   - Isolates idempotency behavior from auth, DB schema, Celery, etc.
#   - No Docker required — tests run entirely in-process.
#   - Fast: no network I/O.
#   - Each test controls exactly what the mock DB/Redis returns.
# ═════════════════════════════════════════════════════════════════════════════


def _build_test_jwt(user_id: uuid.UUID) -> str:
    """
    Create a minimal JWT containing only the 'sub' claim.
    Used to simulate a logged-in user in integration tests.
    Signs with the real JWT_SECRET_KEY from settings so _extract_user_id_from_request
    can decode it.
    """
    import jwt

    from app.core.config import settings

    payload = {"sub": str(user_id), "type": "access"}
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def _make_idempotency_record(
    *,
    request_hash: str,
    response_body: dict,
    http_status_code: int = 200,
) -> MagicMock:
    """Build a mock IdempotencyKey ORM object for replay tests."""
    record = MagicMock()
    record.request_hash = request_hash
    record.response_body = response_body
    record.http_status_code = http_status_code
    return record


@pytest.fixture
def minimal_app() -> FastAPI:
    """
    A minimal FastAPI app with an IdempotentRoute /pay endpoint.
    The endpoint always returns {"result": "payment_ok"} with HTTP 200.
    """
    from app.middleware.idempotency import IdempotentRoute

    app = FastAPI()
    payment_router = APIRouter(route_class=IdempotentRoute)

    @payment_router.post("/pay")
    async def pay():
        return {"result": "payment_ok"}

    app.include_router(payment_router)
    return app


@pytest.mark.asyncio
class TestIdempotencyMiddleware:
    """Integration tests for the IdempotentRoute middleware."""

    # ── Helper to post with a JWT and optional idempotency key ──────────────
    async def _post(
        self,
        app: FastAPI,
        *,
        user_id: uuid.UUID,
        idempotency_key: str | None,
        body: dict | None = None,
    ):
        token = _build_test_jwt(user_id)
        headers = {"Authorization": f"Bearer {token}"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            return await client.post("/pay", json=body or {}, headers=headers)

    # ── Test: missing Idempotency-Key → 400 ─────────────────────────────────

    async def test_missing_key_returns_400(self, minimal_app):
        response = await self._post(minimal_app, user_id=SAMPLE_USER_ID, idempotency_key=None)
        assert response.status_code == 400
        assert "Idempotency-Key" in response.json()["detail"]

    # ── Test: first request processed normally → 200 ─────────────────────────

    async def test_first_request_processes_normally(self, minimal_app):
        idem_key = str(uuid.uuid4())

        with (
            patch("app.services.idempotency_service.acquire_lock", AsyncMock(return_value=True)),
            patch("app.services.idempotency_service.release_lock", AsyncMock()),
            patch("app.services.idempotency_service.check_existing_key", AsyncMock(return_value=None)),
            patch("app.services.idempotency_service.store_idempotency_response", AsyncMock()),
            # Patch AsyncSessionLocal so no real DB connection is needed
            patch("app.middleware.idempotency.AsyncSessionLocal") as mock_session_cls,
        ):
            # Configure the mock session context manager
            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
            mock_session.add = MagicMock()
            mock_session.commit = AsyncMock()
            mock_session_cls.return_value = mock_session

            with patch("app.middleware.idempotency.get_redis_client", return_value=AsyncMock()):
                response = await self._post(
                    minimal_app, user_id=SAMPLE_USER_ID, idempotency_key=idem_key
                )

        assert response.status_code == 200
        assert response.json() == {"result": "payment_ok"}
        assert "X-Idempotency-Replayed" not in response.headers

    # ── Test: duplicate request replays cached response → 200 + replay header ──

    async def test_duplicate_request_replays_response(self, minimal_app):
        """
        The critical test: a retry with the same key+body must receive the
        exact same response without executing the handler again.
        """
        idem_key = str(uuid.uuid4())
        body = {"receiver_email": "bob@example.com", "amount": "100.00"}
        body_bytes = json.dumps(body).encode()
        request_hash = hashlib.sha256(
            str(SAMPLE_USER_ID).encode()
            + "/pay".encode()
            + body_bytes
        ).hexdigest()

        cached_record = _make_idempotency_record(
            request_hash=request_hash,
            response_body={"result": "payment_ok"},  # stored from first request
            http_status_code=200,
        )

        handler_call_count = 0

        with (
            patch("app.services.idempotency_service.acquire_lock", AsyncMock(return_value=True)),
            patch("app.services.idempotency_service.release_lock", AsyncMock()),
            patch("app.services.idempotency_service.check_existing_key", AsyncMock(return_value=cached_record)),
            patch("app.middleware.idempotency.AsyncSessionLocal") as mock_session_cls,
            patch("app.middleware.idempotency.get_redis_client", return_value=AsyncMock()),
        ):
            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = mock_session

            response = await self._post(
                minimal_app, user_id=SAMPLE_USER_ID, idempotency_key=idem_key, body=body
            )

        assert response.status_code == 200
        assert response.json() == {"result": "payment_ok"}
        # The replay header proves the handler was NOT called — cached response returned
        assert response.headers.get("X-Idempotency-Replayed") == "true"

    # ── Test: same key, different payload → 422 ──────────────────────────────

    async def test_payload_mismatch_returns_422(self, minimal_app):
        """
        Reusing an idempotency key with a different request body is a client bug.
        Must return 422 — not 200 and not a replay.
        """
        idem_key = str(uuid.uuid4())

        # Record stores hash for body {"amount": "100.00"}
        original_hash = "aaaa" + "bb" * 30  # arbitrary 64-char hex
        cached_record = _make_idempotency_record(
            request_hash=original_hash,   # hash of the ORIGINAL payload
            response_body={"result": "payment_ok"},
        )

        # The incoming request has body {"amount": "200.00"} → different hash
        with (
            patch("app.services.idempotency_service.acquire_lock", AsyncMock(return_value=True)),
            patch("app.services.idempotency_service.release_lock", AsyncMock()),
            patch("app.services.idempotency_service.check_existing_key", AsyncMock(return_value=cached_record)),
            patch("app.middleware.idempotency.AsyncSessionLocal") as mock_session_cls,
            patch("app.middleware.idempotency.get_redis_client", return_value=AsyncMock()),
        ):
            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = mock_session

            # Body that produces a DIFFERENT hash than original_hash
            response = await self._post(
                minimal_app,
                user_id=SAMPLE_USER_ID,
                idempotency_key=idem_key,
                body={"amount": "200.00"},  # different body → different computed hash
            )

        assert response.status_code == 422
        assert "Payload mismatch" in response.json()["detail"] or "different" in response.json()["detail"].lower()

    # ── Test: concurrent request with same key → 409 ─────────────────────────

    async def test_concurrent_request_returns_409(self, minimal_app):
        """
        If Redis lock is already held (acquire_lock returns False),
        return 409 to tell the client: "wait, your request is in flight."
        """
        idem_key = str(uuid.uuid4())

        with (
            # lock_acquired=False means another request is holding it
            patch("app.services.idempotency_service.acquire_lock", AsyncMock(return_value=False)),
            patch("app.middleware.idempotency.get_redis_client", return_value=AsyncMock()),
        ):
            response = await self._post(
                minimal_app, user_id=SAMPLE_USER_ID, idempotency_key=idem_key
            )

        assert response.status_code == 409
        assert "already being processed" in response.json()["detail"].lower()

    # ── Test: Redis unavailable → degrade gracefully ─────────────────────────

    async def test_redis_unavailable_degrades_gracefully(self, minimal_app):
        """
        If Redis is down, we skip the distributed lock but still process
        the request (degraded mode). The DB UNIQUE constraint is the safety net.
        """
        idem_key = str(uuid.uuid4())

        with (
            # Simulating Redis down: acquire_lock raises an exception
            patch(
                "app.services.idempotency_service.acquire_lock",
                AsyncMock(side_effect=ConnectionError("Redis connection refused")),
            ),
            patch("app.services.idempotency_service.release_lock", AsyncMock()),
            patch("app.services.idempotency_service.check_existing_key", AsyncMock(return_value=None)),
            patch("app.services.idempotency_service.store_idempotency_response", AsyncMock()),
            patch("app.middleware.idempotency.AsyncSessionLocal") as mock_session_cls,
            patch("app.middleware.idempotency.get_redis_client", return_value=AsyncMock()),
        ):
            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = mock_session

            response = await self._post(
                minimal_app, user_id=SAMPLE_USER_ID, idempotency_key=idem_key
            )

        # Request is processed despite Redis being down
        assert response.status_code == 200

    # ── Test: lock released even when handler fails ───────────────────────────

    async def test_lock_released_on_handler_failure(self):
        """
        If the payment handler raises an exception (business logic error),
        the Redis lock MUST still be released. Without `finally`, a failed
        payment would block all retries for 30 seconds.
        """
        from fastapi import HTTPException
        from app.middleware.idempotency import IdempotentRoute

        app = FastAPI()
        payment_router = APIRouter(route_class=IdempotentRoute)

        @payment_router.post("/fail")
        async def always_fails():
            raise HTTPException(status_code=400, detail="Insufficient funds")

        app.include_router(payment_router)

        idem_key = str(uuid.uuid4())
        token = _build_test_jwt(SAMPLE_USER_ID)

        release_mock = AsyncMock()

        with (
            patch("app.services.idempotency_service.acquire_lock", AsyncMock(return_value=True)),
            patch("app.services.idempotency_service.release_lock", release_mock),
            patch("app.services.idempotency_service.check_existing_key", AsyncMock(return_value=None)),
            patch("app.middleware.idempotency.AsyncSessionLocal") as mock_session_cls,
            patch("app.middleware.idempotency.get_redis_client", return_value=AsyncMock()),
        ):
            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = mock_session

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/fail",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Idempotency-Key": idem_key,
                    },
                )

        # The handler failed (400)
        assert response.status_code == 400
        # But the lock was STILL released
        release_mock.assert_awaited_once()

    # ── Test: failed payment is NOT stored ────────────────────────────────────

    async def test_failed_payment_not_stored(self):
        """
        A payment that fails (400 Insufficient Funds) must NOT be stored
        in the idempotency_keys table. The client must be able to retry
        after topping up their balance.
        """
        from fastapi import HTTPException
        from app.middleware.idempotency import IdempotentRoute

        app = FastAPI()
        payment_router = APIRouter(route_class=IdempotentRoute)

        @payment_router.post("/fail")
        async def always_fails():
            raise HTTPException(status_code=400, detail="Insufficient funds")

        app.include_router(payment_router)

        idem_key = str(uuid.uuid4())
        token = _build_test_jwt(SAMPLE_USER_ID)
        store_mock = AsyncMock()

        with (
            patch("app.services.idempotency_service.acquire_lock", AsyncMock(return_value=True)),
            patch("app.services.idempotency_service.release_lock", AsyncMock()),
            patch("app.services.idempotency_service.check_existing_key", AsyncMock(return_value=None)),
            patch("app.services.idempotency_service.store_idempotency_response", store_mock),
            patch("app.middleware.idempotency.AsyncSessionLocal") as mock_session_cls,
            patch("app.middleware.idempotency.get_redis_client", return_value=AsyncMock()),
        ):
            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = mock_session

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post(
                    "/fail",
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Idempotency-Key": idem_key,
                    },
                )

        assert response.status_code == 400
        # store was NOT called — failed responses are never cached
        store_mock.assert_not_awaited()


# ═════════════════════════════════════════════════════════════════════════════
# Section 3: Double-charge prevention
# ═════════════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestDoubleChargePrevention:
    """
    These tests verify the core financial guarantee:
    A transfer retried N times deducts money exactly once.

    The test simulates a common real-world scenario:
      1. Client sends transfer request.
      2. Server processes it (payment succeeds, 200 returned).
      3. Network drops before client receives the 200.
      4. Client retries (same Idempotency-Key, same body).
      5. Server must return the cached 200 WITHOUT re-executing the transfer.

    We verify this by checking that the transfer handler is called exactly once
    even when the HTTP call is made multiple times.
    """

    async def test_handler_called_exactly_once_on_retry(self):
        """
        Simulates: first request processes, second request replays.
        The transfer handler must be called ONCE total across both HTTP calls.
        """
        from app.middleware.idempotency import IdempotentRoute

        handler_call_count = 0
        stored_response = {}

        app = FastAPI()
        payment_router = APIRouter(route_class=IdempotentRoute)

        @payment_router.post("/transfer")
        async def fake_transfer():
            nonlocal handler_call_count
            handler_call_count += 1
            return JSONResponse({"transfer_reference_id": "ref-001", "amount": "100.00"})

        app.include_router(payment_router)

        idem_key = str(uuid.uuid4())
        token = _build_test_jwt(SAMPLE_USER_ID)
        headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": idem_key}

        async def simulate_acquire(redis, uid, endpoint, key) -> bool:
            return True

        async def simulate_release(redis, uid, endpoint, key) -> None:
            pass

        async def simulate_check_first_call(db, uid, endpoint, key):
            return None  # No existing record on first call

        async def simulate_check_second_call(db, uid, endpoint, key):
            # Second call: return the stored response (simulates DB replay)
            if stored_response:
                record = MagicMock()
                record.request_hash = stored_response["hash"]
                record.response_body = stored_response["body"]
                record.http_status_code = 200
                return record
            return None

        check_call_count = 0

        async def simulate_check(db, uid, endpoint, key):
            nonlocal check_call_count
            check_call_count += 1
            if check_call_count == 1:
                return await simulate_check_first_call(db, uid, endpoint, key)
            else:
                return await simulate_check_second_call(db, uid, endpoint, key)

        async def simulate_store(db, user_id, endpoint, key, request_hash, response_body, http_status_code):
            stored_response["hash"] = request_hash
            stored_response["body"] = json.loads(response_body.decode("utf-8"))

        with (
            patch("app.services.idempotency_service.acquire_lock", AsyncMock(side_effect=simulate_acquire)),
            patch("app.services.idempotency_service.release_lock", AsyncMock(side_effect=simulate_release)),
            patch("app.services.idempotency_service.check_existing_key", AsyncMock(side_effect=simulate_check)),
            patch("app.services.idempotency_service.store_idempotency_response", AsyncMock(side_effect=simulate_store)),
            patch("app.middleware.idempotency.AsyncSessionLocal") as mock_session_cls,
            patch("app.middleware.idempotency.get_redis_client", return_value=AsyncMock()),
        ):
            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = mock_session

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                # First request — should process and store
                r1 = await client.post("/transfer", json={}, headers=headers)
                # Second request — should replay from cache
                r2 = await client.post("/transfer", json={}, headers=headers)

        # Handler called exactly once
        assert handler_call_count == 1, (
            f"Transfer handler was called {handler_call_count} times! "
            "Idempotency failed to prevent the duplicate execution."
        )

        # Both requests return 200
        assert r1.status_code == 200
        assert r2.status_code == 200

        # Replay header present on second request only
        assert "X-Idempotency-Replayed" not in r1.headers
        assert r2.headers.get("X-Idempotency-Replayed") == "true"

        # Both return the same response body
        assert r1.json() == r2.json()

    async def test_different_keys_process_independently(self):
        """
        Two requests with DIFFERENT Idempotency-Keys are independent transfers.
        Both should call the handler — they are not retries of each other.
        """
        from app.middleware.idempotency import IdempotentRoute

        handler_call_count = 0

        app = FastAPI()
        payment_router = APIRouter(route_class=IdempotentRoute)

        @payment_router.post("/transfer")
        async def fake_transfer():
            nonlocal handler_call_count
            handler_call_count += 1
            return {"result": f"call_{handler_call_count}"}

        app.include_router(payment_router)

        token = _build_test_jwt(SAMPLE_USER_ID)

        with (
            patch("app.services.idempotency_service.acquire_lock", AsyncMock(return_value=True)),
            patch("app.services.idempotency_service.release_lock", AsyncMock()),
            patch("app.services.idempotency_service.check_existing_key", AsyncMock(return_value=None)),
            patch("app.services.idempotency_service.store_idempotency_response", AsyncMock()),
            patch("app.middleware.idempotency.AsyncSessionLocal") as mock_session_cls,
            patch("app.middleware.idempotency.get_redis_client", return_value=AsyncMock()),
        ):
            mock_session = AsyncMock()
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = mock_session

            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                r1 = await client.post(
                    "/transfer", json={},
                    headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
                )
                r2 = await client.post(
                    "/transfer", json={},
                    headers={"Authorization": f"Bearer {token}", "Idempotency-Key": str(uuid.uuid4())},
                )

        # Both requests should process independently
        assert handler_call_count == 2, (
            "Two different Idempotency-Keys should result in two independent handler calls"
        )
        assert r1.status_code == 200
        assert r2.status_code == 200


# ═════════════════════════════════════════════════════════════════════════════
# Phase 8: End-to-end integration tests (real DB + Redis via conftest.py)
#
# These tests use the `client` fixture from conftest.py which wires up:
#   - Real test PostgreSQL (pywallet_test DB)
#   - Real test Redis (DB 9)
#   - Full FastAPI app with all middleware
#
# They prove the same guarantees as Section 3 above, but against real infra.
# ═════════════════════════════════════════════════════════════════════════════


def _ikey() -> str:
    return str(uuid.uuid4())


class TestE2EIdempotencyNoDoubleCharge:
    async def test_same_key_deposit_charges_only_once(
        self, client: AsyncClient, auth_headers: dict
    ):
        key = _ikey()
        payload = {"amount": "500.00"}

        for _ in range(3):
            await client.post(
                "/api/v1/wallet/deposit", json=payload,
                headers={**auth_headers, "Idempotency-Key": key},
            )

        balance_resp = await client.get("/api/v1/wallet/balance", headers=auth_headers)
        assert Decimal(balance_resp.json()["balance"]) == Decimal("500.00")

    async def test_same_key_transfer_debits_only_once(
        self, client: AsyncClient, funded_user: dict, second_user: dict, auth_headers: dict
    ):
        key = _ikey()
        payload = {"receiver_email": second_user["email"], "amount": "300.00"}

        for _ in range(3):
            await client.post(
                "/api/v1/wallet/transfer", json=payload,
                headers={**auth_headers, "Idempotency-Key": key},
            )

        balance_resp = await client.get("/api/v1/wallet/balance", headers=auth_headers)
        assert Decimal(balance_resp.json()["balance"]) == Decimal("700.00")

    async def test_replay_does_not_create_extra_transactions(
        self, client: AsyncClient, auth_headers: dict
    ):
        key = _ikey()
        for _ in range(5):
            await client.post(
                "/api/v1/wallet/deposit",
                json={"amount": "100.00"},
                headers={**auth_headers, "Idempotency-Key": key},
            )
        txns_resp = await client.get("/api/v1/wallet/transactions", headers=auth_headers)
        assert txns_resp.json()["total"] == 1

    async def test_replay_returns_identical_transaction_id(
        self, client: AsyncClient, auth_headers: dict
    ):
        key = _ikey()
        payload = {"amount": "200.00"}
        r1 = await client.post(
            "/api/v1/wallet/deposit", json=payload,
            headers={**auth_headers, "Idempotency-Key": key},
        )
        r2 = await client.post(
            "/api/v1/wallet/deposit", json=payload,
            headers={**auth_headers, "Idempotency-Key": key},
        )
        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r1.json()["transaction"]["id"] == r2.json()["transaction"]["id"]


class TestE2EPayloadMismatch:
    async def test_deposit_payload_mismatch_returns_422(
        self, client: AsyncClient, auth_headers: dict
    ):
        key = _ikey()
        await client.post(
            "/api/v1/wallet/deposit",
            json={"amount": "100.00"},
            headers={**auth_headers, "Idempotency-Key": key},
        )
        resp = await client.post(
            "/api/v1/wallet/deposit",
            json={"amount": "999.00"},
            headers={**auth_headers, "Idempotency-Key": key},
        )
        assert resp.status_code == 422

    async def test_payload_mismatch_balance_unchanged(
        self, client: AsyncClient, auth_headers: dict
    ):
        key = _ikey()
        await client.post(
            "/api/v1/wallet/deposit",
            json={"amount": "100.00"},
            headers={**auth_headers, "Idempotency-Key": key},
        )
        await client.post(
            "/api/v1/wallet/deposit",
            json={"amount": "999.00"},
            headers={**auth_headers, "Idempotency-Key": key},
        )
        balance_resp = await client.get("/api/v1/wallet/balance", headers=auth_headers)
        assert Decimal(balance_resp.json()["balance"]) == Decimal("100.00")


@pytest.mark.slow
class TestE2EConcurrentSameKey:
    async def test_concurrent_same_key_no_double_charge(
        self, client: AsyncClient, auth_headers: dict
    ):
        key = _ikey()
        tasks = [
            client.post(
                "/api/v1/wallet/deposit",
                json={"amount": "500.00"},
                headers={**auth_headers, "Idempotency-Key": key},
            )
            for _ in range(4)
        ]
        responses = await asyncio.gather(*tasks)
        status_codes = [r.status_code for r in responses]
        assert all(c in (201, 409) for c in status_codes)

        balance_resp = await client.get("/api/v1/wallet/balance", headers=auth_headers)
        assert Decimal(balance_resp.json()["balance"]) == Decimal("500.00")
