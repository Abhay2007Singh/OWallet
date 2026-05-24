"""
tests/conftest.py

Shared test infrastructure for PyWallet integration tests.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHY INTEGRATION TESTS FOR A FINANCIAL BACKEND
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Unit tests with mocks pass even when the real system is broken because:

  1. Mock DB sessions don't enforce SELECT FOR UPDATE semantics.
     → Your "concurrent transfer" test passes, but production has a race
       condition that allows negative balances.

  2. Mock Redis doesn't enforce NX (set if not exists) atomicity.
     → Your "idempotency" test passes, but two concurrent retries both
       process the payment because the real Redis NX behaves differently.

  3. Mocked SQLAlchemy doesn't catch real constraint violations.
     → The idempotency UNIQUE constraint doesn't prevent duplicates in
       mocked tests, but two simultaneous requests in production both win.

Financial systems require integration tests that exercise REAL infrastructure.
These tests prove:
  ✓ PostgreSQL ACID atomicity (SELECT FOR UPDATE, commit/rollback)
  ✓ Redis NX lock semantics for idempotency
  ✓ Real FK constraints prevent orphaned records
  ✓ Real balance calculations with Decimal precision
  ✓ End-to-end HTTP → service → DB → response correctness

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
TEST DATABASE ISOLATION STRATEGY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Strategy: TRUNCATE all tables before each test function.

Why TRUNCATE instead of transaction rollback?
  The "rollback" pattern wraps each test in an outer transaction that
  is never committed, isolating test writes. This breaks with our
  architecture because:
  - IdempotentRoute opens its OWN sessions via AsyncSessionLocal()
  - Those sessions are separate connections that CAN'T join the test transaction
  - Commits inside those sessions are visible to other connections
  - The outer rollback can't undo them

  TRUNCATE is explicit, reliable, and works regardless of how many
  sessions/connections the system opens internally.

Running tests:
  docker compose exec api pytest tests/ -v
  docker compose exec api pytest tests/test_transfer.py -v -k concurrent
"""

import uuid
from collections.abc import AsyncGenerator
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config import settings


# =============================================================================
# Test Infrastructure Constants
#
# All integration tests use a dedicated test database to prevent polluting
# the development database with test data. In Docker Compose, the test
# runner should be invoked with:
#
#   docker compose exec api pytest tests/ -v
#
# The DATABASE_URL env var must point to the test database. In the default
# Docker Compose setup, this means creating a 'pywallet_test' database.
# See README.md → Testing section for exact commands.
# =============================================================================

TEST_DATABASE_URL: str = settings.DATABASE_URL.replace("/pywallet_db", "/pywallet_test")

# Test Redis: DB 9 — completely isolated from all application namespaces:
# DB 0 = app cache  DB 1 = Celery broker  DB 2 = Celery results  DB 3 = rate limits
TEST_REDIS_DB: int = 9
TEST_REDIS_URL: str = f"redis://{settings.REDIS_HOST}:{settings.REDIS_PORT}/{TEST_REDIS_DB}"


# =============================================================================
# SESSION-SCOPED FIXTURES
# session-scoped = run ONCE per pytest session (across all test files)
# =============================================================================

@pytest.fixture(scope="session")
async def test_engine():
    """
    Create the test database engine and all tables once per session.

    Why session-scoped?
      Table creation (CREATE TABLE IF NOT EXISTS) is idempotent and slow.
      Running it once per session instead of once per test saves ~30 seconds
      on a full test run.

    Why not use Alembic migrations here?
      Migrations are the source of truth for production. In tests we use
      SQLAlchemy's metadata.create_all() which creates the same schema
      but without migration version tracking. This is faster and avoids
      the dependency on alembic.ini pointing to the right DB.
    """
    import app.models  # noqa: F401 — registers all models with Base.metadata
    from app.core.database import Base

    engine = create_async_engine(TEST_DATABASE_URL, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest.fixture(scope="session")
def test_session_factory(test_engine):
    """
    Session factory pointing at the test database.

    session-scoped: the factory object is reused, but each call to
    test_session_factory() creates a NEW session (not session-scoped sessions).
    """
    return async_sessionmaker(test_engine, expire_on_commit=False)


# =============================================================================
# FUNCTION-SCOPED FIXTURES
# function-scoped (default) = run before and after EACH test function
# =============================================================================

@pytest.fixture
async def truncate_db(test_engine):
    """
    Truncate all tables before each test.

    RESTART IDENTITY CASCADE:
      - RESTART IDENTITY: resets all serial/sequence counters
      - CASCADE: also truncates tables that FK-reference the truncated tables
        (wallets, transactions, idempotency_keys all cascade from users)

    Order: users is the root table — CASCADE handles children automatically.
    """
    async with test_engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE TABLE users RESTART IDENTITY CASCADE"
            )
        )


@pytest.fixture
async def test_redis(truncate_db):
    """
    Redis client pointing at test DB 9. Flushed before AND after each test.

    Also flushes DB 3 (rate limit storage) to prevent test-to-test
    interference when rate limit tests run in sequence.
    """
    import redis.asyncio as aioredis

    # Test application Redis (DB 9)
    client = aioredis.from_url(TEST_REDIS_URL, decode_responses=True)
    await client.flushdb()

    # Rate limit Redis (DB 3) — flush to avoid cross-test interference
    rate_limit_redis = aioredis.from_url(
        f"redis://{settings.REDIS_HOST}:{settings.REDIS_PORT}/3",
        decode_responses=True,
    )
    await rate_limit_redis.flushdb()
    await rate_limit_redis.aclose()

    yield client

    await client.flushdb()
    await client.aclose()


@pytest.fixture
async def client(
    test_session_factory,
    test_redis,
    monkeypatch,
) -> AsyncGenerator[AsyncClient, None]:
    """
    HTTP test client with full dependency injection override.

    What this fixture does:
      1. Overrides get_db() (FastAPI DI) → creates sessions from test_session_factory
      2. Overrides get_redis() (FastAPI DI) → returns test_redis (DB 9)
      3. Patches AsyncSessionLocal (module-level) → used by IdempotentRoute
      4. Patches get_redis_client (module-level) → used by IdempotentRoute
      5. Patches Celery task .delay() → prevents real task dispatch in tests
      6. Creates an HTTPX AsyncClient that calls the app in-process (no real HTTP)

    Why HTTPX AsyncClient instead of TestClient?
      TestClient is synchronous (wraps ASGI in a thread). Our routes are async.
      AsyncClient + ASGITransport tests the real async code path,
      including real asyncio scheduling — which is what matters for
      concurrency tests with asyncio.gather().

    Why patch AsyncSessionLocal and get_redis_client?
      IdempotentRoute uses LATE IMPORTS inside its handler function:
        from app.core.database import AsyncSessionLocal
        from app.core.redis import get_redis_client
      Late imports resolve the module attribute at call time.
      By patching the MODULE attributes before any request runs,
      we ensure IdempotentRoute uses the test DB/Redis too.
    """
    from app.main import app
    from app.core.database import get_db
    from app.core.redis import get_redis
    import app.core.database as db_module
    import app.core.redis as redis_module

    # ─── FastAPI DI overrides ───────────────────────────────────────────────
    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        async with test_session_factory() as session:
            try:
                yield session
            except Exception:
                await session.rollback()
                raise

    async def override_get_redis():
        return test_redis

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_redis] = override_get_redis

    # ─── Module-level patches for IdempotentRoute internals ─────────────────
    monkeypatch.setattr(db_module, "AsyncSessionLocal", test_session_factory)
    monkeypatch.setattr(redis_module, "get_redis_client", lambda: test_redis)

    # ─── Silence Celery task dispatch (no broker in test env) ────────────────
    mock_delay = MagicMock(return_value=MagicMock(id="test-task-id"))
    monkeypatch.setattr(
        "app.workers.webhook_tasks.simulate_bank_webhook.delay", mock_delay
    )
    monkeypatch.setattr(
        "app.workers.notification_tasks.send_transfer_notification.delay", mock_delay
    )

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as ac:
        yield ac

    app.dependency_overrides.clear()


# =============================================================================
# REUSABLE FIXTURES — build common test state
# =============================================================================

@pytest.fixture
async def registered_user(client: AsyncClient) -> dict:
    """
    Register a test user and return their credentials + tokens.

    Returns dict with:
      email, password, full_name,
      access_token, refresh_token, user_id
    """
    email = f"user_{uuid.uuid4().hex[:8]}@test.pywallet.dev"
    password = "TestPass1"

    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "full_name": "Test User", "password": password},
    )
    assert resp.status_code == 201, f"Register failed: {resp.json()}"
    data = resp.json()

    return {
        "email": email,
        "password": password,
        "full_name": "Test User",
        "access_token": data["tokens"]["access_token"],
        "refresh_token": data["tokens"]["refresh_token"],
        "user_id": data["user"]["id"],
    }


@pytest.fixture
def auth_headers(registered_user: dict) -> dict:
    """Authorization headers for the primary test user."""
    return {"Authorization": f"Bearer {registered_user['access_token']}"}


@pytest.fixture
async def second_user(client: AsyncClient) -> dict:
    """
    A second distinct user — the receiver in transfer tests.
    Uses a separate email to prevent collision with the primary user.
    """
    email = f"recv_{uuid.uuid4().hex[:8]}@test.pywallet.dev"
    password = "RecvPass2"

    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": email, "full_name": "Receiver User", "password": password},
    )
    assert resp.status_code == 201, f"Second user register failed: {resp.json()}"
    data = resp.json()

    return {
        "email": email,
        "password": password,
        "access_token": data["tokens"]["access_token"],
        "refresh_token": data["tokens"]["refresh_token"],
        "user_id": data["user"]["id"],
    }


@pytest.fixture
async def funded_user(client: AsyncClient, registered_user: dict, auth_headers: dict) -> dict:
    """
    Test user with $1000.00 in their wallet. Used by transfer tests.
    """
    resp = await client.post(
        "/api/v1/wallet/deposit",
        json={"amount": "1000.00", "description": "Test funding"},
        headers={**auth_headers, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert resp.status_code == 201, f"Fund deposit failed: {resp.json()}"
    return registered_user


def idempotency_key() -> str:
    """Generate a unique UUID v4 Idempotency-Key string."""
    return str(uuid.uuid4())
