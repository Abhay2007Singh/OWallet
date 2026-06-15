# OWallet

A production-grade fintech wallet application with a React frontend and FastAPI backend. Demonstrates real financial system patterns: ACID atomicity, idempotent payments, double-entry bookkeeping, JWT auth with refresh token rotation, rate limiting, and async background tasks.

---

## Table of Contents

- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Setup & Running](#setup--running)
- [Environment Variables](#environment-variables)
- [Database Migrations](#database-migrations)
- [API Endpoints](#api-endpoints)
- [Key Design Concepts](#key-design-concepts)
- [Testing](#testing)

---

## Tech Stack

### Frontend

| Layer | Technology |
|---|---|
| Framework | React 18 + TypeScript + Vite |
| Styling | Tailwind CSS v4 |
| Routing | React Router v6 |
| Server state | TanStack Query v5 |
| Forms | React Hook Form + Zod v4 |
| Auth state | Zustand (persisted to localStorage) |
| HTTP client | Axios (JWT interceptor + auto-refresh) |

### Backend

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
├── frontend/                        # React + TypeScript frontend
│   ├── public/
│   │   └── favicon.svg              # App favicon
│   ├── src/
│   │   ├── api/
│   │   │   ├── axios.ts             # Axios instance, JWT interceptor, auto-refresh
│   │   │   ├── auth.ts              # Auth API calls (register, login, refresh, logout, me)
│   │   │   └── wallet.ts            # Wallet API calls (balance, transactions, deposit, transfer)
│   │   ├── components/
│   │   │   ├── layout/
│   │   │   │   ├── AppLayout.tsx    # Sidebar + main content wrapper
│   │   │   │   ├── AuthLayout.tsx   # Centered card layout for login/register
│   │   │   │   ├── Navbar.tsx       # Top nav with logo, links, user avatar
│   │   │   │   └── ProtectedRoute.tsx
│   │   │   └── ui/                  # Reusable components: Button, Card, Input, Alert, Badge, Skeleton
│   │   ├── pages/
│   │   │   ├── auth/                # LoginPage, RegisterPage
│   │   │   ├── dashboard/           # DashboardPage (balance, recent transactions, quick actions)
│   │   │   ├── wallet/              # WalletPage (balance card + deposit form)
│   │   │   ├── transfer/            # TransferPage (transfer form + receipt)
│   │   │   ├── transactions/        # TransactionsPage (paginated, filterable)
│   │   │   ├── profile/             # ProfilePage (user info from /auth/me)
│   │   │   └── settings/            # SettingsPage (account info + sign out)
│   │   ├── store/
│   │   │   └── authStore.ts         # Zustand store with localStorage persistence
│   │   ├── types/
│   │   │   ├── auth.ts              # TypeScript types mirroring backend Pydantic schemas
│   │   │   └── wallet.ts            # Transaction, Wallet, Deposit, Transfer types
│   │   ├── utils/
│   │   │   └── formatters.ts        # formatCurrency, formatDate, extractApiError
│   │   ├── App.tsx                  # Route tree (nested layouts + ProtectedRoute)
│   │   └── main.tsx
│   ├── index.html
│   ├── vite.config.ts
│   ├── tsconfig.app.json
│   └── .env.example
│
├── app/                             # FastAPI backend
│   ├── main.py                      # App factory, middleware stack, router registration
│   ├── core/
│   │   ├── config.py                # Pydantic Settings — all env vars in one place
│   │   ├── database.py              # Async SQLAlchemy engine + get_db dependency
│   │   ├── redis.py                 # Redis connection pool + get_redis dependency
│   │   ├── security.py              # bcrypt hashing, JWT creation/validation, token storage
│   │   ├── dependencies.py          # get_current_user — JWT → User ORM object
│   │   └── logging_config.py        # structlog configuration (JSON output)
│   ├── models/
│   │   ├── base.py                  # UUIDPrimaryKeyMixin, TimestampMixin
│   │   ├── user.py                  # User model (email, hashed_password, role)
│   │   ├── wallet.py                # Wallet model (balance NUMERIC, currency enum)
│   │   ├── transaction.py           # Transaction model (double-entry ledger)
│   │   └── idempotency_key.py       # IdempotencyKey model (request dedup store)
│   ├── schemas/
│   │   ├── auth.py                  # Pydantic request/response schemas for auth
│   │   └── wallet.py                # Pydantic request/response schemas for wallet
│   ├── routes/
│   │   ├── auth.py                  # POST /auth/register, /login, /refresh, /logout, GET /me
│   │   ├── wallet.py                # GET /wallet/balance, transactions; POST /deposit, /transfer
│   │   └── health.py                # GET /health, /health/detailed
│   ├── services/
│   │   ├── auth_service.py          # register_user, login_user, refresh_tokens, logout
│   │   ├── wallet_service.py        # get_wallet_balance, deposit, transfer_money, get_transactions
│   │   └── idempotency_service.py   # Idempotency key lookup, storage, and replay logic
│   ├── middleware/
│   │   ├── idempotency.py           # IdempotentRoute — wraps payment endpoints
│   │   ├── rate_limiter.py          # SlowAPI limiter instance + transfer rate limit dep
│   │   ├── request_logging.py       # Logs every request/response with X-Request-ID
│   │   └── security_headers.py      # Adds security headers + generates X-Request-ID
│   └── workers/
│       ├── celery_app.py            # Celery app instance, config, beat schedule
│       ├── deposit_tasks.py         # simulate_bank_webhook — marks deposit COMPLETED
│       ├── notification_tasks.py    # send_transfer_notification — simulated push/email
│       ├── webhook_tasks.py         # Outbound webhook delivery simulation
│       └── cleanup_tasks.py         # Periodic cleanup of stale PENDING transactions
├── migrations/
│   ├── env.py                       # Alembic env — connects to async DB
│   └── versions/
│       ├── 001_initial_schema.py
│       ├── 002_phase4_transfer_fields.py
│       └── 003_idempotency_phase5.py
├── tests/
│   ├── conftest.py
│   ├── test_auth.py
│   ├── test_wallet.py
│   ├── test_transfer.py
│   ├── test_idempotency.py
│   └── test_rate_limits.py
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
# 1. Clone the repo
git clone https://github.com/Abhay2007Singh/OWallet.git
cd OWallet

# 2. Create backend .env
cp .env.example .env
# Defaults work for local Docker — no edits needed

# 3. Start all backend services
docker compose up --build -d

# 4. Run database migrations (first time only)
docker compose exec api alembic upgrade head

# 5. Start the frontend
cd frontend
npm install
npm run dev
```

| Service | URL |
|---|---|
| Frontend | http://localhost:5173 |
| FastAPI API | http://localhost:8000 |
| Swagger UI | http://localhost:8000/docs |
| ReDoc | http://localhost:8000/redoc |
| Flower (Celery monitor) | http://localhost:5555 |
| PostgreSQL | localhost:5432 |
| Redis | localhost:6379 |

### Frontend environment

Create `frontend/.env` (already in `.env.example`):

```env
VITE_API_BASE_URL=http://localhost:8000/api/v1
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

# Celery
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
docker compose exec api alembic upgrade head

# Roll back all migrations
docker compose exec api alembic downgrade base

# Create a new migration after changing models
docker compose exec api alembic revision --autogenerate -m "describe_change"
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

### Wallet — `/api/v1/wallet`

| Method | Path | Auth | Description |
|---|---|---|---|
| GET | `/wallet/balance` | Bearer | Current balance (Redis-cached, 30s TTL). |
| GET | `/wallet/transactions` | Bearer | Paginated transaction history. Filterable by status, date_from, date_to. |
| GET | `/wallet/transactions/{id}` | Bearer | Single transaction detail. |
| POST | `/wallet/deposit` | Bearer + `Idempotency-Key` | Deposit funds. Creates PENDING transaction; Celery marks COMPLETED. |
| POST | `/wallet/transfer` | Bearer + `Idempotency-Key` | P2P transfer. Atomic DEBIT + CREDIT. Rate limited: 5/minute/user. |

### Health — `/api/v1/health`

| Method | Path | Description |
|---|---|---|
| GET | `/health` | Shallow check — process alive. |
| GET | `/health/detailed` | Deep check — verifies PostgreSQL + Redis. |

---

## Key Design Concepts

**Double-entry bookkeeping** — every transfer creates two transaction rows: a DEBIT on the sender's wallet and a CREDIT on the receiver's wallet. Both rows share a `transfer_reference_id`. The ledger always balances: `SUM(DEBIT) == SUM(CREDIT)`.

**Idempotency** — payment endpoints require an `Idempotency-Key` header (UUID v4). The key is scoped to `user_id + endpoint + SHA256(body)`. Retrying with the same key replays the original response. Concurrent requests with the same key get a 409. This prevents double charges on network retries. The frontend auto-generates a new UUID per request.

**SELECT FOR UPDATE** — deposits and transfers lock the wallet row(s) before reading the balance, preventing race conditions where two concurrent requests both read the same balance and write an incorrect result.

**Deadlock-safe locking** — transfers always acquire locks on the two wallets in ascending UUID order. If Alice→Bob and Bob→Alice happen simultaneously, both acquire the lower UUID lock first, preventing circular wait.

**Immutable ledger** — transaction rows are never deleted or updated (except `status`). Mistakes are corrected with reversal transactions, satisfying audit and regulatory requirements.

**JWT + refresh token rotation** — access tokens expire in 15 minutes. Refresh tokens rotate on every use: the old token is invalidated immediately. Reusing an old refresh token (replay attack) terminates all sessions for that user. The frontend Axios interceptor handles 401s by auto-refreshing the token and retrying the original request.

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

| File | Covers |
|---|---|
| `test_auth.py` | Register, login, refresh, logout, /me |
| `test_wallet.py` | Balance, transaction history, single transaction |
| `test_transfer.py` | P2P transfer, insufficient funds, self-transfer, frozen wallet |
| `test_idempotency.py` | Duplicate key replay, payload mismatch, concurrent lock |
| `test_rate_limits.py` | Auth rate limits, transfer rate limits |
