"""
app/services/wallet_service.py

Wallet business logic: balance queries, deposits, transaction history, and P2P transfers.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: Wallet architecture
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

A wallet is fundamentally a balance ledger:
  balance = SUM of all completed deposits
           - SUM of all completed withdrawals
           - SUM of outgoing completed transfers
           + SUM of incoming completed transfers

PyWallet maintains two sources of truth:
  1. wallet.balance: a cached running total (fast to read)
  2. transactions table: the immutable audit log (accurate source)

Both must stay in sync. A deposit:
  a) Updates wallet.balance += amount (instant read performance)
  b) Inserts a Transaction record (permanent audit trail)

These two writes happen in ONE database transaction (atomic).
Either both succeed or both fail — no partial state.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: SELECT FOR UPDATE — preventing race conditions
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Race condition scenario (WITHOUT locking):

  T1 starts: SELECT balance FROM wallets WHERE user_id=X → $500
  T2 starts: SELECT balance FROM wallets WHERE user_id=X → $500  ← sees stale value!
  T1: UPDATE wallets SET balance=$600 WHERE user_id=X → COMMIT
  T2: UPDATE wallets SET balance=$600 WHERE user_id=X → COMMIT  ← overwrites T1!

  Result: balance=$600, but TWO deposits of $100 each occurred.
  You have credited the user $100 but the DB shows only $100 total gain.
  This is a $100 loss for the business.

With SELECT FOR UPDATE (pessimistic locking):

  T1: SELECT balance FROM wallets WHERE user_id=X FOR UPDATE → $500 (lock acquired)
  T2: SELECT balance FROM wallets WHERE user_id=X FOR UPDATE → BLOCKS (waiting)
  T1: UPDATE balance=$600 → COMMIT (lock released)
  T2: unblocked → SELECT shows $600 → UPDATE balance=$700 → COMMIT

  Result: balance=$700 ✓ Both deposits correctly credited.

How SELECT FOR UPDATE works:
  PostgreSQL places an exclusive lock on the selected row(s).
  Other transactions that try to SELECT FOR UPDATE the same row will WAIT
  until the first transaction commits or rolls back.
  Read-only SELECT (without FOR UPDATE) still works concurrently — only
  write-intent queries are serialized.

Lock duration: from the SELECT FOR UPDATE until the next COMMIT or ROLLBACK.
Keep the locked section brief — the lock holds a DB connection open.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: Immutable transaction history
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Financial systems treat transaction records like accounting journal entries:
  - Once written, they are NEVER deleted.
  - Once written, the monetary fields (amount, balance_before, balance_after)
    are NEVER updated.
  - Only the status field changes (PENDING → COMPLETED or PENDING → FAILED).

Why never delete?
  1. Regulatory: PCI-DSS, GDPR (in specific contexts), and banking regulations
     require financial history to be retained for 5-10 years.
  2. Audit trails: Support can reconstruct exactly what happened and when.
  3. Balance reconstruction: If wallet.balance becomes corrupted, you can
     replay all COMPLETED transactions to derive the correct balance.
  4. Fraud detection: Patterns over deleted records cannot be analyzed.

What about mistakes? Create a REVERSAL transaction (type=REVERSED) that
references the original. The audit trail shows both the original entry and
the correction — no history is hidden.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: Why transaction snapshots (balance_before/after) matter
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Scenario: A bug introduced at time T caused balances to drift incorrectly.
You need to restore the correct balance for user Alice.

Without snapshots:
  You know Alice had N transactions. You know the amounts. But you don't
  know the SEQUENCE of balances, so you can't tell which transaction
  introduced the drift.

With snapshots (balance_before, balance_after):
  Every transaction is a self-contained ledger entry:
  tx1: balance_before=0,   amount=+$500, balance_after=$500
  tx2: balance_before=$500, amount=+$200, balance_after=$700
  tx3: balance_before=$700, amount=-$50,  balance_after=$650

  If tx3's balance_after doesn't match tx4's balance_before, you found
  the bug. The snapshots are the breadcrumb trail for forensic accounting.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: Redis caching for wallet balance
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Cache key:  wallet:balance:{user_id}
Cache TTL:  30 seconds
Cache type: JSON string {"balance": "...", "currency": "...", "wallet_id": "..."}

Why cache?
  The balance endpoint (GET /wallet/balance) is called very frequently:
  mobile apps poll it every few seconds, dashboards refresh on load.
  Without caching, each call hits PostgreSQL. At 10,000 active users
  polling every 5 seconds = 2,000 DB reads/second for balance alone.
  Redis handles >1M reads/second — caching reduces DB load by ~95%.

Why 30 seconds TTL (not longer)?
  After a deposit, the cache is invalidated immediately.
  But after a TRANSFER (Phase 4), both wallets are affected.
  30s is the maximum a user would see a stale balance before the
  cache auto-expires — an acceptable UX trade-off.

Cache invalidation:
  Any operation that changes wallet.balance must call:
  await redis.delete(f"wallet:balance:{user_id}")
  before or immediately after committing to DB.
"""

import json
import math
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import HTTPException, status
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.transaction import Transaction, TransactionStatus, TransactionType
from app.models.user import User
from app.models.wallet import Wallet
from app.schemas.wallet import (
    PaginatedTransactionResponse,
    PaginationMeta,
    TransactionFilters,
    TransactionResponse,
    TransferResponse,
    WalletBalanceResponse,
)


# =============================================================================
# Cache key helpers
# =============================================================================

def _balance_cache_key(user_id: uuid.UUID) -> str:
    """
    Consistent cache key for wallet balance.
    All wallet operations that read or invalidate the cache use this function.
    A single source of truth for the key format prevents cache key drift bugs.
    """
    return f"wallet:balance:{user_id}"


BALANCE_CACHE_TTL_SECONDS: int = 30


# =============================================================================
# get_wallet_balance
# =============================================================================

async def get_wallet_balance(
    db: AsyncSession,
    redis: Redis,
    user_id: uuid.UUID,
) -> WalletBalanceResponse:
    """
    Return the current balance of the user's primary wallet.

    Cache-first strategy:
      1. Check Redis for a cached balance (O(1), sub-millisecond)
      2. Cache hit → return cached data, no DB query
      3. Cache miss → query PostgreSQL, store in cache with TTL, return

    Args:
        db: Async SQLAlchemy session.
        redis: Async Redis client.
        user_id: UUID of the authenticated user.

    Returns:
        WalletBalanceResponse with balance, currency, and cache indicator.

    Raises:
        HTTPException 404: User has no active wallet.
    """
    cache_key = _balance_cache_key(user_id)

    # -------------------------------------------------------------------------
    # Step 1: Check Redis cache
    # -------------------------------------------------------------------------
    cached_data: str | None = await redis.get(cache_key)

    if cached_data is not None:
        # Cache hit — deserialize and return without touching PostgreSQL
        parsed: dict[str, Any] = json.loads(cached_data)
        return WalletBalanceResponse(
            wallet_id=uuid.UUID(parsed["wallet_id"]),
            balance=Decimal(parsed["balance"]),
            currency=parsed["currency"],
            is_active=parsed["is_active"],
            cached=True,
        )

    # -------------------------------------------------------------------------
    # Step 2: Cache miss — query PostgreSQL
    # No FOR UPDATE here — balance reads are non-locking. Only writes lock.
    # -------------------------------------------------------------------------
    wallet = await _get_primary_wallet(db, user_id)

    # -------------------------------------------------------------------------
    # Step 3: Store in Redis cache for next 30 seconds
    # json.dumps with Decimal: Decimal is not JSON-serializable by default,
    # so we convert to string first. On the way out we convert back with Decimal().
    # -------------------------------------------------------------------------
    cache_payload = json.dumps({
        "wallet_id": str(wallet.id),
        "balance": str(wallet.balance),
        "currency": wallet.currency.value,
        "is_active": wallet.is_active,
    })
    await redis.set(cache_key, cache_payload, ex=BALANCE_CACHE_TTL_SECONDS)

    return WalletBalanceResponse(
        wallet_id=wallet.id,
        balance=Decimal(str(wallet.balance)),
        currency=wallet.currency,
        is_active=wallet.is_active,
        cached=False,
    )


# =============================================================================
# deposit
# =============================================================================

async def deposit(
    db: AsyncSession,
    redis: Redis,
    user_id: uuid.UUID,
    amount: Decimal,
    description: str | None,
) -> tuple[Decimal, Transaction]:
    """
    Credit a deposit to the user's primary wallet.

    This function is the most critical in the wallet service. It must be:
      1. Atomic: balance update + transaction record in ONE DB transaction
      2. Consistent: no race conditions under concurrent deposits (SELECT FOR UPDATE)
      3. Auditable: every cent is recorded with before/after snapshots

    Returns:
        Tuple of (new_balance: Decimal, transaction: Transaction)

    Raises:
        HTTPException 404: Wallet not found.
        HTTPException 403: Wallet is frozen (is_active=False).
        HTTPException 500: DB error during commit (rare, but logged).
    """

    # =========================================================================
    # PHASE A: Acquire exclusive row lock via SELECT FOR UPDATE
    # =========================================================================
    # This is the PESSIMISTIC LOCKING approach.
    # .with_for_update() translates to: SELECT ... FROM wallets WHERE ... FOR UPDATE
    #
    # The lock is held from this SELECT until the session.commit() below.
    # Any other concurrent deposit for the SAME user_id will WAIT at this line
    # until we commit. They then proceed with the UPDATED balance.
    #
    # Why NOT use OPTIMISTIC locking here?
    # Optimistic locking (version numbers) works well for low-conflict writes.
    # For wallets, conflict rate is high — users deposit frequently.
    # With optimistic locking, a conflict causes a RETRY from scratch,
    # which under heavy load causes thundering herd. Pessimistic is correct.
    # =========================================================================
    result = await db.execute(
        select(Wallet)
        .where(Wallet.user_id == user_id)
        .where(Wallet.wallet_tag == "primary")
        .with_for_update()  # ← SELECT ... FOR UPDATE — exclusive row lock
    )
    wallet: Wallet | None = result.scalar_one_or_none()

    # If no "primary" wallet found, fall back to first active wallet for this user
    if wallet is None:
        result = await db.execute(
            select(Wallet)
            .where(Wallet.user_id == user_id)
            .where(Wallet.is_active == True)  # noqa: E712
            .order_by(Wallet.created_at.asc())
            .limit(1)
            .with_for_update()
        )
        wallet = result.scalar_one_or_none()

    if wallet is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active wallet found for this account. Please contact support.",
        )

    if not wallet.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This wallet is currently frozen. Deposits are not accepted.",
        )

    # =========================================================================
    # PHASE B: Compute new balance using Decimal arithmetic
    # =========================================================================
    # wallet.balance is a Python Decimal (because asdecimal=True in Numeric).
    # We explicitly cast to Decimal via str() to avoid any ORM type coercion
    # edge cases — defensive programming for financial calculations.
    balance_before: Decimal = Decimal(str(wallet.balance))
    balance_after: Decimal = balance_before + amount

    # =========================================================================
    # PHASE C: Update wallet balance (within the locked transaction)
    # =========================================================================
    wallet.balance = balance_after
    db.add(wallet)  # marks wallet as dirty — will be UPDATEd on commit

    # =========================================================================
    # PHASE D: Create immutable transaction record
    # =========================================================================
    # This is the JOURNAL ENTRY for this deposit.
    # Once committed, the amount/balance_before/balance_after fields are
    # NEVER changed by application code — they are the financial audit trail.
    # Only `status` changes (PENDING → COMPLETED by the Celery worker).
    # =========================================================================
    transaction = Transaction(
        wallet_id=wallet.id,
        amount=amount,
        balance_before=balance_before,
        balance_after=balance_after,
        transaction_type=TransactionType.DEPOSIT,
        # PENDING: the HTTP handler returns immediately; Celery confirms later.
        # This is the "received and credited" state — money is in the wallet,
        # but bank-side processing confirmation is pending.
        status=TransactionStatus.PENDING,
        description=description,
    )
    db.add(transaction)

    # =========================================================================
    # PHASE E: Atomic commit — balance update + transaction record together
    # =========================================================================
    # COMMIT does two things:
    # 1. Flushes both the wallet UPDATE and transaction INSERT to PostgreSQL
    # 2. Releases the SELECT FOR UPDATE lock — concurrent deposits can now proceed
    #
    # If the commit fails (disk full, DB connection lost), the entire operation
    # rolls back. The wallet.balance is unchanged, no transaction record exists.
    # The user can retry the deposit safely.
    # =========================================================================
    await db.commit()
    await db.refresh(transaction)  # populate server-side defaults (id, created_at, etc.)

    # =========================================================================
    # PHASE F: Invalidate Redis cache AFTER commit
    # =========================================================================
    # The cache now holds a stale balance. Delete it so the next GET /wallet/balance
    # fetches the fresh balance from PostgreSQL.
    #
    # Why AFTER commit (not before)?
    # If the commit fails and we already deleted the cache, the next cache
    # miss would read the DB and cache the WRONG balance (pre-deposit value).
    # Delete after commit guarantees cache consistency.
    # =========================================================================
    await redis.delete(_balance_cache_key(user_id))

    # =========================================================================
    # PHASE G: Queue Celery background task (AFTER commit — critical)
    # =========================================================================
    # WHY after commit?
    # If we queue the task inside the transaction and it then rolls back:
    #   - The Celery message is already in Redis (it was sent)
    #   - But the Transaction row doesn't exist in the DB (rolled back)
    #   - The Celery worker tries to find transaction_id → not found → error
    #
    # Queue after commit ensures the DB record exists when the worker runs.
    #
    # Phase 6: simulate_bank_webhook models the real fintech flow — the bank
    # sends us a confirmation webhook asynchronously after processing our deposit.
    # The task sleeps 3s (bank processing latency) then marks PENDING → COMPLETED.
    # =========================================================================
    from app.workers.webhook_tasks import simulate_bank_webhook  # local import avoids circular dependency
    simulate_bank_webhook.delay(str(transaction.id))

    return balance_after, transaction


# =============================================================================
# get_transactions
# =============================================================================

async def get_transactions(
    db: AsyncSession,
    user_id: uuid.UUID,
    filters: TransactionFilters,
) -> PaginatedTransactionResponse:
    """
    Return a paginated, filtered list of the user's transactions.

    Security: all queries filter through wallet.user_id = current_user.id.
    A user can NEVER see transactions belonging to another user's wallet,
    even if they guess a valid transaction_id.

    This pattern (joining through ownership) is the standard defense against
    IDOR (Insecure Direct Object Reference) vulnerabilities.

    Why JOIN through wallet → user?
    The Transaction table has wallet_id (not user_id). To verify ownership,
    we JOIN: transactions → wallets → users. Only transactions where
    wallets.user_id == current_user.id are returned.

    Pagination:
    OFFSET/LIMIT is simple but has a known issue at scale: on page 500 of
    a million-row table, PostgreSQL must scan 500*page_size rows to skip.
    For Phase 3 this is fine. Cursor-based pagination (using created_at as
    a cursor) is the production solution for large datasets (Phase 5+).
    """

    # -------------------------------------------------------------------------
    # Step 1: Find the user's wallet(s)
    # We get the wallet_id(s) first, then filter transactions by wallet_id.
    # This is more efficient than a JOIN for indexed queries.
    # -------------------------------------------------------------------------
    wallet_result = await db.execute(
        select(Wallet.id).where(Wallet.user_id == user_id)
    )
    wallet_ids = [row[0] for row in wallet_result.fetchall()]

    if not wallet_ids:
        # User has no wallets — return empty paginated response
        return PaginatedTransactionResponse(
            items=[],
            pagination=PaginationMeta(
                total=0, page=filters.page, page_size=filters.page_size,
                pages=0, has_next=False, has_prev=False,
            ),
        )

    # -------------------------------------------------------------------------
    # Step 2: Build base filter conditions
    # Using explicit WHERE clauses rather than .filter(**kwargs) for clarity.
    # -------------------------------------------------------------------------
    base_conditions = [Transaction.wallet_id.in_(wallet_ids)]

    if filters.status is not None:
        base_conditions.append(Transaction.status == filters.status)

    if filters.date_from is not None:
        base_conditions.append(Transaction.created_at >= filters.date_from)

    if filters.date_to is not None:
        base_conditions.append(Transaction.created_at <= filters.date_to)

    # -------------------------------------------------------------------------
    # Step 3: Count total matching records (for pagination metadata)
    # We run the COUNT query first — it doesn't need LIMIT/OFFSET.
    # -------------------------------------------------------------------------
    count_result = await db.execute(
        select(func.count()).select_from(Transaction).where(*base_conditions)
    )
    total: int = count_result.scalar_one()

    # -------------------------------------------------------------------------
    # Step 4: Fetch the requested page
    # ORDER BY created_at DESC: newest transactions first (most useful for users).
    # OFFSET formula: (page - 1) * page_size
    #   Page 1 → OFFSET 0 (first 20 records)
    #   Page 2 → OFFSET 20 (records 21-40)
    # -------------------------------------------------------------------------
    offset = (filters.page - 1) * filters.page_size
    tx_result = await db.execute(
        select(Transaction)
        .where(*base_conditions)
        .order_by(Transaction.created_at.desc())
        .offset(offset)
        .limit(filters.page_size)
    )
    transactions = tx_result.scalars().all()

    # -------------------------------------------------------------------------
    # Step 5: Compute pagination metadata
    # -------------------------------------------------------------------------
    total_pages = math.ceil(total / filters.page_size) if total > 0 else 0

    return PaginatedTransactionResponse(
        items=[TransactionResponse.model_validate(tx) for tx in transactions],
        pagination=PaginationMeta(
            total=total,
            page=filters.page,
            page_size=filters.page_size,
            pages=total_pages,
            has_next=filters.page < total_pages,
            has_prev=filters.page > 1,
        ),
    )


# =============================================================================
# get_transaction_by_id
# =============================================================================

async def get_transaction_by_id(
    db: AsyncSession,
    user_id: uuid.UUID,
    transaction_id: uuid.UUID,
) -> Transaction:
    """
    Fetch a single transaction by ID, enforcing ownership.

    IDOR Prevention:
    ──────────────────────────────────────────────────────────────────
    IDOR (Insecure Direct Object Reference) is an API vulnerability
    where an attacker accesses another user's data by guessing resource IDs.

    Vulnerable query (DO NOT DO THIS):
      SELECT * FROM transactions WHERE id = :transaction_id
      → Returns the transaction regardless of who owns it!

    Secure query (what we do):
      SELECT t.* FROM transactions t
      JOIN wallets w ON t.wallet_id = w.id
      WHERE t.id = :transaction_id AND w.user_id = :current_user_id

    If the transaction exists but belongs to another user → returns None.
    We return 404 (not 403) to avoid revealing that the transaction EXISTS.
    (Returning 403 would tell the attacker: "this ID is valid, just not yours")

    Args:
        db: Async SQLAlchemy session.
        user_id: UUID of the authenticated user (from JWT — never from request body).
        transaction_id: UUID of the transaction to fetch.

    Returns:
        The Transaction ORM object if found and owned by user.

    Raises:
        HTTPException 404: Transaction not found OR not owned by this user.
    """
    result = await db.execute(
        select(Transaction)
        .join(Wallet, Transaction.wallet_id == Wallet.id)  # JOIN to enforce ownership
        .where(Transaction.id == transaction_id)
        .where(Wallet.user_id == user_id)  # OWNERSHIP CHECK — only this user's data
    )
    transaction: Transaction | None = result.scalar_one_or_none()

    if transaction is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Transaction {transaction_id} not found",
            # Intentionally vague: don't reveal whether ID is valid but not theirs
        )

    return transaction


# =============================================================================
# Private helpers
# =============================================================================

async def transfer_money(
    db: AsyncSession,
    redis: Redis,
    sender_user_id: uuid.UUID,
    receiver_email: str,
    amount: Decimal,
    description: str | None,
) -> TransferResponse:
    """
    Execute an atomic P2P wallet-to-wallet transfer.

    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    ATOMICITY — `async with db.begin():`
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    The entire transfer (both wallet balance updates + both transaction rows)
    is wrapped in a single DB transaction via `async with db.begin():`.

    Any exception raised inside the block — validation error, DB error,
    network error — triggers automatic rollback. This guarantees:
      - There is no universe where Alice is debited but Bob is not credited.
      - There is no universe where Bob is credited but Alice is not debited.
      - No partial state can ever reach the DB.

    The difference from manual `await db.commit()`:
    With `async with db.begin():`, an HTTPException raised during validation
    (AFTER one of the wallets was already locked via SELECT FOR UPDATE)
    STILL triggers rollback automatically. With manual commits you would
    need explicit try/except/rollback to achieve the same safety.

    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    DEADLOCK PREVENTION — ascending UUID lock order
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    Deadlock scenario (WITHOUT ordering):
      T1 (Alice→Bob): locks Alice, then waits for Bob lock
      T2 (Bob→Alice): locks Bob, then waits for Alice lock
      → Both wait forever. PostgreSQL detects and kills one (random).

    With ascending UUID ordering:
      T1 always locks the wallet with the lower UUID first.
      T2 also locks the wallet with the lower UUID first.
      → T2 blocks on step 1 (lower UUID). T1 runs to completion.
      → T2 then proceeds. No deadlock possible.

    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    DOUBLE-ENTRY BOOKKEEPING
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    A single P2P transfer of $100 creates TWO transaction rows:
      DEBIT  row: wallet_id=SENDER,   type=DEBIT,  amount=100
      CREDIT row: wallet_id=RECEIVER, type=CREDIT, amount=100
    Both rows share the same `transfer_reference_id` UUID.

    The ledger invariant: SUM(DEBIT) == SUM(CREDIT) across all transfers.
    Money is conserved: it leaves one wallet and enters another.
    No money appears from thin air. No money disappears.

    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    POST-COMMIT SIDE EFFECTS (cache + notifications)
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    Redis cache invalidation and Celery notifications happen AFTER
    `async with db.begin():` exits (i.e., after the DB commits).

    Why? If the DB commit fails:
      - Cache is NOT invalidated → cache still holds the pre-transfer balance (correct)
      - Celery task is NOT queued → no spurious notification for a failed transfer
    If the commit succeeds and Redis.delete() fails: the TTL (30s) auto-expires
    the stale balance. This is an acceptable trade-off (brief staleness vs. atomicity).

    Args:
        db: Async SQLAlchemy session (from get_db() FastAPI dependency).
        redis: Async Redis client.
        sender_user_id: UUID of the authenticated sender (from JWT — never from request).
        receiver_email: Email of the recipient (resolved to User inside this function).
        amount: Positive Decimal, max 2 decimal places (validated by TransferRequest).
        description: Optional memo (stored on both DEBIT and CREDIT rows).

    Returns:
        TransferResponse — sender-facing only. Receiver balance and wallet ID
        are intentionally NOT included. See TransferResponse docstring.

    Raises:
        HTTPException 404: Sender has no wallet, or receiver email not found.
        HTTPException 400: Self-transfer, receiver has no wallet, or insufficient funds.
        HTTPException 403: Sender wallet is frozen.
    """
    # Variables captured inside the transaction for use after commit.
    # Initialized here so Python's type checker knows they will be set.
    transfer_reference_id: uuid.UUID
    debit_tx_id: uuid.UUID
    debit_tx_created_at: datetime
    sender_balance_after_final: Decimal
    receiver_user_id: uuid.UUID
    sender_email_str: str = ""
    receiver_email_str: str = ""

    try:
        # =====================================================================
        # STEP 1: Resolve receiver by email (no lock needed — read-only lookup)
        # Also resolve sender email for post-commit notification tasks.
        # =====================================================================
        receiver_result = await db.execute(
            select(User).where(User.email == receiver_email)
        )
        receiver: User | None = receiver_result.scalar_one_or_none()

        if receiver is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Recipient not found. Verify the email address and try again.",
            )

        # Load sender's email for the notification task.
        # We select only the email column to keep the query lightweight.
        sender_email_row = await db.execute(
            select(User.email).where(User.id == sender_user_id)
        )
        sender_email_str = sender_email_row.scalar_one_or_none() or ""
        receiver_email_str = receiver.email

        # =====================================================================
        # STEP 2: Self-transfer guard
        # =====================================================================
        # Must be checked BEFORE any balance reads to avoid wasting a lock
        # on a transfer that will always fail.
        # =====================================================================
        if receiver.id == sender_user_id:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Cannot transfer funds to your own wallet.",
            )

        receiver_user_id = receiver.id

        # =====================================================================
        # STEP 3: Discover wallet IDs WITHOUT lock (for lock-order computation)
        # =====================================================================
        # We need the wallet IDs before locking so we can sort them ascending.
        # We only read the `.id` column — minimal data, fast indexed lookup.
        # The actual locked Wallet objects come in STEP 4.
        # =====================================================================
        sender_id_result = await db.execute(
            select(Wallet.id)
            .where(Wallet.user_id == sender_user_id)
            .where(Wallet.wallet_tag == "primary")
        )
        sender_wallet_id: uuid.UUID | None = sender_id_result.scalar_one_or_none()

        if sender_wallet_id is None:
            fallback = await db.execute(
                select(Wallet.id)
                .where(Wallet.user_id == sender_user_id)
                .where(Wallet.is_active == True)  # noqa: E712
                .order_by(Wallet.created_at.asc())
                .limit(1)
            )
            sender_wallet_id = fallback.scalar_one_or_none()

        if sender_wallet_id is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="No active wallet found for your account.",
            )

        recv_id_result = await db.execute(
            select(Wallet.id)
            .where(Wallet.user_id == receiver.id)
            .where(Wallet.wallet_tag == "primary")
        )
        receiver_wallet_id: uuid.UUID | None = recv_id_result.scalar_one_or_none()

        if receiver_wallet_id is None:
            fallback = await db.execute(
                select(Wallet.id)
                .where(Wallet.user_id == receiver.id)
                .where(Wallet.is_active == True)  # noqa: E712
                .order_by(Wallet.created_at.asc())
                .limit(1)
            )
            receiver_wallet_id = fallback.scalar_one_or_none()

        if receiver_wallet_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Recipient does not have an active wallet.",
            )

        # =====================================================================
        # STEP 4: Acquire row locks in ascending UUID order (deadlock prevention)
        # =====================================================================
        # Sort the two wallet IDs. Always lock the lower UUID first.
        # Every concurrent transfer in the entire system follows this rule,
        # making it impossible for two transactions to form a circular wait.
        # =====================================================================
        first_id, second_id = (
            (sender_wallet_id, receiver_wallet_id)
            if sender_wallet_id < receiver_wallet_id
            else (receiver_wallet_id, sender_wallet_id)
        )

        # Lock first (lower UUID) — any concurrent transfer touching this wallet blocks here
        first_result = await db.execute(
            select(Wallet).where(Wallet.id == first_id).with_for_update()
        )
        first_wallet: Wallet = first_result.scalar_one()

        # Lock second (higher UUID) — safe to lock now, no circular wait possible
        second_result = await db.execute(
            select(Wallet).where(Wallet.id == second_id).with_for_update()
        )
        second_wallet: Wallet = second_result.scalar_one()

        # Re-map locked wallets back to sender/receiver (lock order may differ from role)
        if first_wallet.id == sender_wallet_id:
            sender_wallet, receiver_wallet = first_wallet, second_wallet
        else:
            sender_wallet, receiver_wallet = second_wallet, first_wallet

        # =====================================================================
        # STEP 5: Post-lock validation — all checks use the LOCKED, current values
        # =====================================================================
        # After acquiring FOR UPDATE locks, no other transaction can modify
        # these rows until we commit. These checks reflect the true current state.
        # =====================================================================
        if not sender_wallet.is_active:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Your wallet is frozen. Contact support to restore access.",
            )

        if not receiver_wallet.is_active:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="The recipient's wallet is not currently accepting transfers.",
            )

        # Use Decimal(str(...)) to normalize the ORM-returned NUMERIC value
        sender_balance_before = Decimal(str(sender_wallet.balance))

        if sender_balance_before < amount:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Insufficient funds. "
                    f"Available: ${sender_balance_before:.2f}, "
                    f"Requested: ${amount:.2f}."
                ),
            )

        # =====================================================================
        # STEP 6: Compute the four balance values (before/after for each wallet)
        # =====================================================================
        sender_balance_after = sender_balance_before - amount

        receiver_balance_before = Decimal(str(receiver_wallet.balance))
        receiver_balance_after = receiver_balance_before + amount

        # =====================================================================
        # STEP 7: Generate the shared transfer correlation ID
        # =====================================================================
        # ONE uuid4 for this entire transfer event.
        # Both the DEBIT and CREDIT rows will carry this same UUID.
        # It is NOT a foreign key — it's a shared correlation value (no chicken-and-egg
        # insertion ordering problem, both rows can be inserted simultaneously).
        # =====================================================================
        transfer_reference_id = uuid.uuid4()

        # =====================================================================
        # STEP 8: Apply balance changes to both wallets
        # =====================================================================
        sender_wallet.balance = sender_balance_after
        receiver_wallet.balance = receiver_balance_after
        db.add(sender_wallet)
        db.add(receiver_wallet)

        # =====================================================================
        # STEP 9: Write the double-entry ledger rows
        # =====================================================================
        # DEBIT: sender's view — money LEAVES their wallet
        debit_tx = Transaction(
            wallet_id=sender_wallet.id,
            counterpart_wallet_id=receiver_wallet.id,
            amount=amount,
            balance_before=sender_balance_before,
            balance_after=sender_balance_after,
            transaction_type=TransactionType.DEBIT,
            # Synchronous transfer: COMPLETED immediately (both sides committed together)
            status=TransactionStatus.COMPLETED,
            description=description,
            transfer_reference_id=transfer_reference_id,
        )

        # CREDIT: receiver's view — money ENTERS their wallet
        credit_tx = Transaction(
            wallet_id=receiver_wallet.id,
            counterpart_wallet_id=sender_wallet.id,
            amount=amount,
            balance_before=receiver_balance_before,
            balance_after=receiver_balance_after,
            transaction_type=TransactionType.CREDIT,
            status=TransactionStatus.COMPLETED,
            description=description,
            transfer_reference_id=transfer_reference_id,
        )

        db.add(debit_tx)
        db.add(credit_tx)

        # =====================================================================
        # STEP 10: Flush to DB (within transaction) + refresh server-side values
        # =====================================================================
        # flush() sends the UPDATEs and INSERTs to PostgreSQL without committing.
        # PostgreSQL assigns UUIDs and timestamps (server-side defaults) at flush time.
        # refresh() re-reads those assigned values into the Python ORM objects.
        #
        # We MUST do this inside the block because we need debit_tx.id and
        # debit_tx.created_at to construct the response — those are server-generated
        # and don't exist in Python until PostgreSQL assigns them.
        # =====================================================================
        await db.flush()
        await db.refresh(debit_tx)
        await db.refresh(credit_tx)

        # Capture all return values before the commit.
        # expire_on_commit=False means these won't be cleared after commit,
        # but capturing them explicitly makes the intent crystal clear.
        debit_tx_id = debit_tx.id
        debit_tx_created_at = debit_tx.created_at
        sender_balance_after_final = sender_balance_after

        await db.commit()

    except Exception:
        await db.rollback()
        raise

    # =========================================================================
    # POST-COMMIT: cache invalidation and notifications
    # =========================================================================
    # We are now outside `async with db.begin():` — the DB transaction has
    # committed successfully. Any failure here does NOT roll back the transfer.
    # The transfer is permanent; these are best-effort side effects.
    # =========================================================================

    # Invalidate both wallets' Redis balance caches.
    # Both balances changed — both cache entries are stale.
    await redis.delete(_balance_cache_key(sender_user_id))
    await redis.delete(_balance_cache_key(receiver_user_id))

    # Queue Celery notifications — AFTER commit, never before.
    # If queued before commit and the commit fails: the task would run but find
    # no COMPLETED transaction → spurious failure notification. Wrong.
    # Phase 6: pass sender_email and receiver_email so the notification task
    # can compose meaningful messages without a DB round-trip inside the task.
    from app.workers.notification_tasks import send_transfer_notification  # avoid circular import
    send_transfer_notification.delay(
        str(debit_tx_id),
        "transfer_sent",
        str(sender_user_id),
        str(amount),
        str(transfer_reference_id),
        sender_email_str,
        receiver_email_str,
    )
    send_transfer_notification.delay(
        str(debit_tx_id),
        "transfer_received",
        str(receiver_user_id),
        str(amount),
        str(transfer_reference_id),
        sender_email_str,
        receiver_email_str,
    )

    return TransferResponse(
        transfer_reference_id=transfer_reference_id,
        amount=amount,
        sender_new_balance=sender_balance_after_final,
        debit_transaction_id=debit_tx_id,
        timestamp=debit_tx_created_at,
        message=(
            f"Transfer of ${amount:.2f} to {receiver_email} completed successfully."
        ),
    )


async def _get_primary_wallet(db: AsyncSession, user_id: uuid.UUID) -> Wallet:
    """
    Find the user's primary wallet without any lock.
    Used for read-only operations (balance check, transaction listing).
    """
    result = await db.execute(
        select(Wallet)
        .where(Wallet.user_id == user_id)
        .where(Wallet.wallet_tag == "primary")
    )
    wallet: Wallet | None = result.scalar_one_or_none()

    if wallet is None:
        # Fallback: any active wallet
        fallback = await db.execute(
            select(Wallet)
            .where(Wallet.user_id == user_id)
            .where(Wallet.is_active == True)  # noqa: E712
            .order_by(Wallet.created_at.asc())
            .limit(1)
        )
        wallet = fallback.scalar_one_or_none()

    if wallet is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No active wallet found for this account",
        )

    return wallet
