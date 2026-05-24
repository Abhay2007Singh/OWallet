# OWallet (PyWallet)

A production-grade fintech wallet API built with FastAPI. Demonstrates real financial system patterns: ACID atomicity, idempotent payments, double-entry bookkeeping, JWT auth with refresh token rotation, rate limiting, and async background tasks.

---

## Table of Contents

- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Setup & Running](#setup--running)
- [Environment Variables](#environment-variables)
- [Database Migrations](#database-migrations)
- [API Endpoints](#api-endpoints)
- [How to Use](#how-to-use)
- [Data Flow](#data-flow)
- [Key Design Concepts](#key-design-concepts)
- [Testing](#testing)

---

## Tech Stack

| Layer | Technology |
|---|---|
| Web framework | FastAPI + Uvicorn |
| Database | PostgreSQL 15 (asyncpg driver) |
| ORM / Migrations | SQLAlchemy 2.0 (async) + Alembic |
| Cache / Broker | Redis 7 |
| Background tasks | Celery (worker + beat) + Flower |
| Auth | PyJWT (HS256) + bcrypt |
| Rate limiting | SlowAPI |
| Logging | structlog (structured JSON) |
| Containerization | Docker + Docker Compose |

---

## Project Structure

```
OWallet/
├── app/
│   ├── main.py                  # App factory, middleware stack, router registration
│   ├── core/
│   │   ├── config.py            # Pydantic Settings — all env vars in one place
│   │   ├── database.py          # Async SQLAlchemy engine + get_db dependency
│   │   ├── redis.py             # Redis connection pool + get_redis dependency
│   │   ├── security.py          # bcrypt hashing, JWT creation/validation, token storage
│   │   ├── dependencies.py      # get_current_user — JWT → User ORM object
│   │   └── logging_config.py    # structlog configuration (JSON output)
│   ├── models/
│   │   ├── base.py              # UUIDPrimaryKeyMixin, TimestampMixin
│   │   ├── user.py              # User model (email, hashed_password, role)
│   │   ├── wallet.py            # Wallet model (balance NUMERIC, currency enum)
│   │   ├── transaction.py       # Transaction model (double-entry ledger)
│   │   └── idempotency_key.py   # IdempotencyKey model (request dedup store)
│   ├── schemas/
│   │   ├── auth.py              # Pydantic request/response schemas for auth
│   │   └── wallet.py            # Pydantic request/response schemas for wallet
│   ├── routes/
│   │   ├── auth.py              # POST /auth/register, /login, /refresh, /logout, GET /me
│   │   ├── wallet.py            # GET /wallet/balance, transactions; POST /deposit, /transfer
│   │   └── health.py            # GET /health, /health/detailed
│   ├── services/
│   │   ├── auth_service.py      # register_user, login_user, refresh_tokens, logout
│   │   ├── wallet_service.py    # get_wallet_balance, deposit, transfer_money, get_transactions
│   │   └── idempotency_service.py # Idempotency key lookup, storage, and replay logic
│   ├── middleware/
│   │   ├── idempotency.py       # IdempotentRoute — wraps payment endpoints
│   │   ├── rate_limiter.py      # SlowAPI limiter instance + transfer rate limit dep
│   │   ├── request_logging.py   # Logs every request/response with X-Request-ID
│   │   └── security_headers.py  # Adds security headers + generates X-Request-ID
│   └── workers/
│       ├── celery_app.py        # Celery app instance, config, beat schedule
│       ├── deposit_tasks.py     # simulate_bank_webhook — marks deposit COMPLETED
│       ├── notification_tasks.py# send_transfer_notification — simulated push/email
│       ├── webhook_tasks.py     # Outbound webhook delivery simulation
│       └── cleanup_tasks.py     # Periodic cleanup of stale PENDING transactions
├── migrations/
│   ├── env.py                   # Alembic env — connects to async DB
│   └── versions/
│       ├── 001_initial_schema.py
│       ├── 002_phase4_transfer_fields.py
│       └── 003_idempotency_phase5.py
├── tests/
│   ├── conftest.py              # Shared fixtures (test DB, test client, users)
│   ├── test_auth.py
│   ├── test_wallet.py
│   ├── test_transfer.py
│   ├── test_idempotency.py
│   ├── test_rate_limits.py
│   └── test_phase6.py / test_phase7.py
├── docker-compose.yml
├── Dockerfile
├── alembic.ini
├── requirements.txt
└── .env.example
```

---

## Setup & Running

### With Docker (recommended)

```bash
# 1. Clone and enter the project
cd OWallet

# 2. Create your .env file
cp .env.example .env
# Edit .env with your values (defaults work for local Docker)

# 3. Start all services
docker compose up --build

# 4. Run migrations (first time only)
docker compose exec api alembic upgrade head
```

Services started:

| Service | URL |
|---|---|
| FastAPI API | http://localhost:8000 |
| Swagger UI | http://localhost:8000/docs |
| ReDoc | http://localhost:8000/redoc |
| Flower (Celery monitor) | http://localhost:5555 |
| PostgreSQL | localhost:5432 |
| Redis | localhost:6379 |

### Without Docker (local dev)

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Start PostgreSQL and Redis locally, then set .env with localhost URLs

# 4. Run migrations
alembic upgrade head

# 5. Start the API
uvicorn app.main:app --reload

# 6. Start a Celery worker (separate terminal)
celery -A app.workers.celery_app worker --loglevel=info
```

---

## Environment Variables

Copy `.env.example` to `.env`. All variables are required unless noted.

```env
# Application
APP_NAME=PyWallet
APP_ENV=development          # development | staging | production
APP_DEBUG=true
SECRET_KEY=<64-char random string>

# PostgreSQL
POSTGRES_USER=pywallet_user
POSTGRES_PASSWORD=pywallet_pass
POSTGRES_DB=pywallet_db
POSTGRES_HOST=postgres        # use 'localhost' outside Docker
POSTGRES_PORT=5432
DATABASE_URL=postgresql+asyncpg://pywallet_user:pywallet_pass@postgres:5432/pywallet_db

# Redis
REDIS_HOST=redis              # use 'localhost' outside Docker
REDIS_PORT=6379
REDIS_DB=0
REDIS_URL=redis://redis:6379/0

# Celery (uses separate Redis DBs to avoid key collisions)
CELERY_BROKER_URL=redis://redis:6379/1
CELERY_RESULT_BACKEND=redis://redis:6379/2

# JWT
JWT_SECRET_KEY=<64-char random string, separate from SECRET_KEY>
JWT_ALGORITHM=HS256
JWT_ACCESS_TOKEN_EXPIRE_MINUTES=15
JWT_REFRESH_TOKEN_EXPIRE_DAYS=7
```

---

## Database Migrations

```bash
# Apply all migrations
alembic upgrade head

# Create a new migration after changing models
alembic revision --autogenerate -m "describe_change"

# Roll back one step
alembic downgrade -1
```

---

## API Endpoints

### Authentication — `/api/v1/auth`

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/auth/register` | No | Register user + create USD wallet. Returns token pair. Rate limited: 10/hour/IP. |
| POST | `/auth/login` | No | Login with email + password. Returns token pair. Rate limited: 10/hour/IP. |
| POST | `/auth/refresh` | No | Exchange refresh token for new token pair (rotation). |
| POST | `/auth/logout` | Bearer | Invalidate refresh token. |
| GET | `/auth/me` | Bearer | Get current user's profile. |

### Wallet (read-only) — `/api/v1/wallet`

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/wallet/balance` | Bearer | Current balance (Redis-cached, 30s TTL). |
| GET | `/wallet/transactions` | Bearer | Paginated transaction history. Filterable by status, date_from, date_to. |
| GET | `/wallet/transactions/{id}` | Bearer | Single transaction detail. |

### Wallet (payments) — `/api/v1/wallet` — requires `Idempotency-Key` header

| Method | Path | Auth | Description |
|---|---|---|---|
| POST | `/wallet/deposit` | Bearer | Deposit funds. Creates PENDING transaction; Celery marks it COMPLETED. |
| POST | `/wallet/transfer` | Bearer | P2P transfer. Atomic DEBIT + CREDIT. Rate limited: 5/minute/user. |

### Health — `/api/v1/health`

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Shallow check — process alive. |
| GET | `/health/detailed` | Deep check — verifies PostgreSQL + Redis. |

---

## How to Use

### 1. Register

```bash
curl -X POST http://localhost:8000/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"email": "alice@example.com", "password": "Str0ng!Pass", "full_name": "Alice"}'
```

Response includes `access_token` and `refresh_token`. Save both.

### 2. Login (if already registered)

```bash
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email": "alice@example.com", "password": "Str0ng!Pass"}'
```

### 3. Check balance

```bash
curl http://localhost:8000/api/v1/wallet/balance \
  -H "Authorization: Bearer <access_token>"
```

### 4. Deposit funds

Generate a UUID v4 for the `Idempotency-Key` (use a new one per deposit):

```bash
curl -X POST http://localhost:8000/api/v1/wallet/deposit \
  -H "Authorization: Bearer <access_token>" \
  -H "Idempotency-Key: 550e8400-e29b-41d4-a716-446655440000" \
  -H "Content-Type: application/json" \
  -d '{"amount": 100.00, "description": "Initial top-up"}'
```

Retrying with the **same** `Idempotency-Key` and body replays the original response — no double deposit.

### 5. Transfer to another user

```bash
curl -X POST http://localhost:8000/api/v1/wallet/transfer \
  -H "Authorization: Bearer <access_token>" \
  -H "Idempotency-Key: 660e8400-e29b-41d4-a716-446655440001" \
  -H "Content-Type: application/json" \
  -d '{"receiver_email": "bob@example.com", "amount": 25.00, "description": "Lunch"}'
```

### 6. View transaction history

```bash
curl "http://localhost:8000/api/v1/wallet/transactions?page=1&page_size=20" \
  -H "Authorization: Bearer <access_token>"
```

Optional query params: `status=completed`, `date_from=2025-01-01`, `date_to=2025-12-31`.

### 7. Refresh tokens (when access token expires)

```bash
curl -X POST http://localhost:8000/api/v1/auth/refresh \
  -H "Content-Type: application/json" \
  -d '{"refresh_token": "<refresh_token>"}'
```

Replace both stored tokens with the new pair. The old refresh token is immediately invalidated.

### 8. Logout

```bash
curl -X POST http://localhost:8000/api/v1/auth/logout \
  -H "Authorization: Bearer <access_token>"
```

---

## Data Flow

### Registration flow

```
Client
  │
  ▼
POST /auth/register
  │
  ▼
Rate limiter (10/hour/IP) ──── exceeded ──▶ 429
  │
  ▼
RegisterRequest validated (Pydantic)
  │
  ▼
auth_service.register_user()
  ├── Check email uniqueness ──── duplicate ──▶ 409
  ├── Hash password (bcrypt, 12 rounds)
  ├── BEGIN transaction
  │     ├── INSERT User row
  │     └── INSERT Wallet row (USD, balance=0)
  └── COMMIT
  │
  ▼
create_access_token() + create_refresh_token()
  └── Store refresh token in Redis (key: refresh:{user_id}, TTL: 7 days)
  │
  ▼
201 { user, tokens: { access_token, refresh_token } }
```

### Login flow

```
POST /auth/login
  │
  ▼
auth_service.login_user()
  ├── SELECT user WHERE email = ? ──── not found ──▶ 401 (generic message)
  ├── verify_password_timing_safe() ── wrong pass ──▶ 401 (same generic message)
  ├── Check is_active ──────────────── inactive ───▶ 403
  └── Issue token pair → store refresh token in Redis
  │
  ▼
200 { user, tokens }
```

### Deposit flow

```
POST /wallet/deposit
  │
  ▼
SlowAPIMiddleware → SecurityHeadersMiddleware → RequestLoggingMiddleware
  │
  ▼
JWT validated → current_user resolved
  │
  ▼
IdempotentRoute (wraps handler)
  ├── Extract Idempotency-Key header ──── missing ──▶ 400
  ├── Decode JWT for user_id (key scoping)
  ├── Compute SHA256(user_id + endpoint + body)
  ├── SET Redis NX lock (EX 30s) ──────── exists ───▶ 409 (concurrent request)
  ├── Query idempotency_keys table
  │     ├── Found + hash matches ──────────────────▶ replay cached response
  │     └── Found + hash differs ──────────────────▶ 422 Payload Mismatch
  └── Not found → proceed
  │
  ▼
wallet_service.deposit()
  ├── BEGIN transaction
  │     ├── SELECT wallet FOR UPDATE (row-level lock)
  │     ├── Check wallet is_active ──── frozen ──▶ 403
  │     ├── wallet.balance += amount
  │     ├── INSERT Transaction (type=DEPOSIT, status=PENDING, balance_before, balance_after)
  │     └── COMMIT
  ├── Invalidate Redis balance cache
  └── Enqueue Celery task: simulate_bank_webhook(transaction_id)
  │
  ▼
IdempotentRoute stores response in idempotency_keys table
  └── Release Redis lock
  │
  ▼
201 { transaction, new_balance }

  [async, ~5s later]
  Celery worker: simulate_bank_webhook()
    └── UPDATE transaction SET status=COMPLETED
```

### Transfer flow

```
POST /wallet/transfer
  │
  ▼
_transfer_rate_limit (5/minute/user via Redis INCR) ── exceeded ──▶ 429
  │
  ▼
IdempotentRoute (same lifecycle as deposit)
  │
  ▼
wallet_service.transfer_money()
  ├── Resolve receiver by email ──── not found ──▶ 404
  ├── Check sender != receiver ────── self ──────▶ 400
  ├── BEGIN transaction
  │     ├── Lock both wallets in ascending UUID order (deadlock prevention)
  │     │     SELECT ... FOR UPDATE on min(sender_id, receiver_id) first
  │     ├── Check sender wallet is_active ──── frozen ──▶ 403
  │     ├── Check receiver wallet is_active ── frozen ──▶ 400
  │     ├── Check sender balance >= amount ─── insufficient ──▶ 400
  │     ├── sender.balance -= amount
  │     ├── receiver.balance += amount
  │     ├── INSERT Transaction (type=DEBIT,  wallet=sender,   status=COMPLETED)
  │     ├── INSERT Transaction (type=CREDIT, wallet=receiver, status=COMPLETED)
  │     │     └── Both rows share the same transfer_reference_id (UUID)
  │     └── COMMIT
  ├── Invalidate Redis balance cache for both wallets
  └── Enqueue Celery task: send_transfer_notification(sender_id, receiver_id, amount)
  │
  ▼
200 { sender_transaction, receiver_transaction, new_balance }
```

### Token refresh flow

```
POST /auth/refresh  { refresh_token }
  │
  ▼
auth_service.refresh_tokens()
  ├── Decode JWT → extract user_id + jti (token ID)
  ├── GET Redis key refresh:{user_id} → stored_jti
  ├── stored_jti != submitted jti ──▶ REPLAY ATTACK
  │     └── DELETE all sessions for user (Redis) ──▶ 401
  ├── DELETE old refresh token from Redis
  ├── Issue new access_token + new refresh_token
  └── Store new refresh_token in Redis
  │
  ▼
200 { tokens: { access_token, refresh_token } }
```

### Request middleware order

Every request passes through this stack (outermost → innermost):

```
SlowAPIMiddleware          — rate limit before any processing
  └── SecurityHeadersMiddleware  — generate X-Request-ID, add security headers
        └── RequestLoggingMiddleware  — log request + response with request_id
              └── CORSMiddleware  — handle preflight OPTIONS
                    └── FastAPI app  — route matching, DI, handler
```

---

## Key Design Concepts

**Double-entry bookkeeping** — every transfer creates two transaction rows: a DEBIT on the sender's wallet and a CREDIT on the receiver's wallet. Both rows share a `transfer_reference_id`. The ledger always balances: `SUM(DEBIT) == SUM(CREDIT)`.

**Idempotency** — payment endpoints require an `Idempotency-Key` header (UUID v4). The key is scoped to `user_id + endpoint + SHA256(body)`. Retrying with the same key replays the original response from the DB. Concurrent requests with the same key get a 409. This prevents double charges on network retries.

**SELECT FOR UPDATE** — deposits and transfers lock the wallet row(s) before reading the balance. This prevents race conditions where two concurrent requests both read the same balance and both write an incorrect result.

**Deadlock-safe locking** — transfers always acquire locks on the two wallets in ascending UUID order. If Alice→Bob and Bob→Alice happen simultaneously, both acquire the lower UUID lock first, preventing circular wait (deadlock).

**Immutable ledger** — transaction rows are never deleted or updated (except `status`). Mistakes are corrected with reversal transactions. This satisfies audit and regulatory requirements.

**JWT + refresh token rotation** — access tokens expire in 15 minutes (short window limits stolen-token damage). Refresh tokens rotate on every use: the old token is invalidated immediately. Reusing an old refresh token (replay attack) terminates all sessions for that user.

**Celery background tasks** — deposits are created with `status=PENDING` and a Celery task simulates async bank confirmation, updating the status to `COMPLETED`. This decouples the HTTP response from slow external calls.

---

## Testing

```bash
# Run all tests
pytest

# With coverage report
pytest --cov=app --cov-report=term-missing

# Run a specific test file
pytest tests/test_transfer.py -v

# Run tests matching a name pattern
pytest -k "idempotency" -v
```

Test files:

| File | Covers |
|---|---|
| `test_auth.py` | Register, login, refresh, logout, /me |
| `test_wallet.py` | Balance, transaction history, single transaction |
| `test_transfer.py` | P2P transfer, insufficient funds, self-transfer, frozen wallet |
| `test_idempotency.py` | Duplicate key replay, payload mismatch, concurrent lock |
| `test_rate_limits.py` | Auth rate limits, transfer rate limits |
| `test_phase6.py` | Celery task behavior |
| `test_phase7.py` | Structured logging, security headers |
