"""
app/routes/wallet.py

Wallet HTTP route handlers.

Security principles applied everywhere:
  - user_id always comes from current_user.id (JWT) — never from the request body.
  - Delegate all business logic to service modules.
  - Return typed Pydantic response models.

Two routers are defined:

  router (APIRouter)
    Read-only endpoints: balance, transaction history, single transaction.
    No idempotency required — reads are naturally idempotent.

  payment_router (APIRouter with route_class=IdempotentRoute)
    Write endpoints: deposit, transfer.
    Every request MUST carry an Idempotency-Key header.
    Duplicate requests with the same key replay the cached response.
    Concurrent requests with the same key receive 409.

Why split instead of applying idempotency to all routes?
  Idempotency adds overhead (Redis lock + DB check) to every request.
  GET /wallet/balance called 10,000 times/minute shouldn't incur that cost.
  Reads are safe to re-execute freely; writes are not.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.redis import get_redis
from app.middleware.idempotency import IdempotentRoute
from app.middleware.rate_limiter import _transfer_rate_limit
from app.models.user import User
from app.schemas.wallet import (
    DepositRequest,
    DepositResponse,
    PaginatedTransactionResponse,
    TransactionFilters,
    TransactionResponse,
    TransferRequest,
    TransferResponse,
    WalletBalanceResponse,
)
from app.services.wallet_service import (
    deposit,
    get_transaction_by_id,
    get_transactions,
    get_wallet_balance,
    transfer_money,
)

# ─────────────────────────────────────────────────────────────────────────────
# Read-only router — no idempotency overhead
# ─────────────────────────────────────────────────────────────────────────────
router = APIRouter(prefix="/wallet", tags=["Wallet"])

# ─────────────────────────────────────────────────────────────────────────────
# Payment router — every route is wrapped by IdempotentRoute
# ─────────────────────────────────────────────────────────────────────────────
payment_router = APIRouter(
    prefix="/wallet",
    tags=["Wallet (Payments)"],
    route_class=IdempotentRoute,
)


# =============================================================================
# GET /wallet/balance
# =============================================================================

@router.get(
    "/balance",
    response_model=WalletBalanceResponse,
    status_code=status.HTTP_200_OK,
    summary="Get current wallet balance",
    description=(
        "Returns the current balance of the authenticated user's primary wallet. "
        "Balance may be served from Redis cache (up to 30 seconds stale). "
        "The `cached` field indicates whether Redis or PostgreSQL was the source."
    ),
    responses={
        200: {"description": "Balance returned successfully"},
        401: {"description": "Missing or invalid access token"},
        404: {"description": "No active wallet found for this account"},
    },
)
async def get_balance(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> WalletBalanceResponse:
    return await get_wallet_balance(db, redis, current_user.id)


# =============================================================================
# POST /wallet/deposit   (idempotent — requires Idempotency-Key header)
# =============================================================================

@payment_router.post(
    "/deposit",
    response_model=DepositResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Deposit funds into wallet",
    description=(
        "Credits the specified amount to the user's primary wallet. "
        "**Requires `Idempotency-Key` header** — a UUID v4 you generate per deposit. "
        "Retrying with the same key and same body replays the original response safely. "
        "The balance is updated immediately (SELECT FOR UPDATE). "
        "A Transaction record is created with status=PENDING; "
        "a Celery worker updates it to COMPLETED after simulating bank processing."
    ),
    responses={
        201: {"description": "Deposit initiated. Transaction created in PENDING state."},
        400: {"description": "Missing Idempotency-Key, invalid amount"},
        401: {"description": "Missing or invalid access token"},
        403: {"description": "Wallet is frozen"},
        404: {"description": "No active wallet found"},
        409: {"description": "Concurrent request with same Idempotency-Key in flight"},
        422: {"description": "Payload mismatch — Idempotency-Key reused with different body"},
    },
)
async def create_deposit(
    body: DepositRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
) -> DepositResponse:
    """
    Process a deposit with idempotency protection.

    The Idempotency-Key header (enforced by IdempotentRoute) guarantees:
      - First call: processes deposit, returns 201.
      - Retry with same key + body: returns the original 201 from cache.
      - Retry with same key + different body: returns 422 (payload mismatch).
      - Concurrent call with same key: returns 409 (request in flight).
    """
    new_balance, transaction = await deposit(
        db=db,
        redis=redis,
        user_id=current_user.id,
        amount=body.amount,
        description=body.description,
    )

    return DepositResponse(
        message=(
            "Deposit of ${:.2f} received. Processing in background. "
            "Check status at GET /api/v1/wallet/transactions/{}.".format(
                body.amount, transaction.id
            )
        ),
        transaction=TransactionResponse.model_validate(transaction),
        new_balance=new_balance,
    )


# =============================================================================
# POST /wallet/transfer   (idempotent — requires Idempotency-Key header)
# =============================================================================

@payment_router.post(
    "/transfer",
    response_model=TransferResponse,
    status_code=status.HTTP_200_OK,
    summary="Transfer funds to another user",
    description=(
        "Atomically transfers the specified amount from the authenticated user's wallet "
        "to the recipient's wallet. "
        "**Requires `Idempotency-Key` header** — a UUID v4 you generate per transfer. "
        "Retrying with the same key and same body replays the original response safely — "
        "no second deduction occurs. "
        "Both wallets are updated in a single DB transaction "
        "with deadlock-safe ascending UUID lock ordering. "
        "Two immutable ledger rows are created: one DEBIT (sender) and one CREDIT (receiver)."
    ),
    responses={
        200: {"description": "Transfer completed. Both wallets updated atomically."},
        400: {
            "description": "Self-transfer, insufficient funds, or receiver has no wallet"
        },
        401: {"description": "Missing or invalid access token"},
        403: {"description": "Your wallet is frozen"},
        404: {"description": "Recipient email not found, or your wallet not found"},
        409: {"description": "Concurrent request with same Idempotency-Key in flight"},
        422: {"description": "Payload mismatch — Idempotency-Key reused with different body"},
        429: {"description": "Transfer rate limit exceeded (5 per minute per user)"},
    },
)
async def create_transfer(
    body: TransferRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    redis: Annotated[Redis, Depends(get_redis)],
    _rate_limit: None = Depends(_transfer_rate_limit),
) -> TransferResponse:
    """
    Execute a P2P wallet transfer with idempotency protection.

    Rate limited to 5 transfers per 60 seconds per authenticated user.
    The limit is enforced by _transfer_rate_limit before any DB access.

    Idempotency flow (handled by IdempotentRoute before this function runs):
      1. Redis lock acquired (NX EX 30).
      2. DB checked: no existing record → proceed.
      3. transfer_money() executes atomically.
      4. Response stored in idempotency_keys table.
      5. Redis lock released.

    On retry with same Idempotency-Key + same body:
      - Steps 1-2 run, existing record FOUND, original 200 replayed.
      - transfer_money() is never called again.
      - No second DEBIT row, no second balance deduction.
    """
    return await transfer_money(
        db=db,
        redis=redis,
        sender_user_id=current_user.id,
        receiver_email=str(body.receiver_email),
        amount=body.amount,
        description=body.description,
    )


# =============================================================================
# GET /wallet/transactions
# =============================================================================

@router.get(
    "/transactions",
    response_model=PaginatedTransactionResponse,
    status_code=status.HTTP_200_OK,
    summary="List transaction history",
    description=(
        "Returns a paginated list of the authenticated user's transactions. "
        "Results are ordered newest-first. "
        "Optional filters: status, date_from, date_to."
    ),
    responses={
        200: {"description": "Transaction list returned"},
        401: {"description": "Missing or invalid access token"},
    },
)
async def list_transactions(
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
    filters: Annotated[TransactionFilters, Depends()],
) -> PaginatedTransactionResponse:
    return await get_transactions(db, current_user.id, filters)


# =============================================================================
# GET /wallet/transactions/{transaction_id}
# =============================================================================

@router.get(
    "/transactions/{transaction_id}",
    response_model=TransactionResponse,
    status_code=status.HTTP_200_OK,
    summary="Get a specific transaction",
    description=(
        "Returns the full detail of a single transaction. "
        "Returns 404 if the transaction does not exist OR belongs to another user "
        "(IDOR prevention)."
    ),
    responses={
        200: {"description": "Transaction returned"},
        401: {"description": "Missing or invalid access token"},
        404: {"description": "Transaction not found or not owned by this user"},
    },
)
async def get_transaction(
    transaction_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> TransactionResponse:
    transaction = await get_transaction_by_id(db, current_user.id, transaction_id)
    return TransactionResponse.model_validate(transaction)
