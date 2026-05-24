"""
app/workers/deposit_tasks.py

Celery background tasks for deposit processing.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: Why background jobs exist in fintech
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Real deposit flow at a bank/fintech:
  1. User requests deposit → API receives it
  2. API must call external systems:
     - Payment gateway (Stripe, Flutterwave, Paystack): 2-5 seconds
     - Bank verification API: 5-30 seconds
     - Anti-fraud scoring service: 1-10 seconds
     - AML (anti-money laundering) check: 1-60 seconds

  HTTP connections timeout after 30-60 seconds (client/load balancer).
  Holding a connection for 60 seconds burns thread resources.
  If the gateway call takes 45s and your server has 100 threads:
  → 100 slow deposits block ALL other users from ANY endpoint.

  Solution: Decouple receipt from processing.
    1. HTTP receives deposit → validates → stores PENDING → responds in <100ms
    2. Celery worker picks up the task → calls all external services → updates to COMPLETED
    3. User can poll GET /wallet/transactions/{id} for the current status

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: Eventual consistency
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

After the HTTP response returns, the transaction is PENDING.
The Celery worker will eventually update it to COMPLETED.
This is called "eventual consistency" — the system will reach a
consistent final state, but not necessarily immediately.

In PyWallet, we immediately credit the balance (strong consistency
for the balance number) but defer the status confirmation (eventual
consistency for the transaction lifecycle status).

This is a deliberate trade-off:
  Strong consistency for balance → user can immediately make transfers
  Eventual consistency for status → system can handle slow bank responses

Real fintech apps use webhooks to notify users when status changes.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: Celery architecture
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Components:
  Producer (API)    → calls process_deposit.delay(transaction_id)
                      → sends JSON message to Redis broker
  Broker (Redis)    → message queue; stores tasks until worker picks them up
  Worker (Celery)   → reads from broker; runs process_deposit()
  Backend (Redis)   → stores task results (success/failure/return value)

Message format in Redis:
  {
    "task": "app.workers.deposit_tasks.process_deposit",
    "id": "celery-task-uuid",
    "args": ["transaction-uuid"],
    "kwargs": {},
    "retries": 0
  }

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: task_acks_late and at-least-once delivery
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Default (acks_early): Worker acknowledges receiving the task before
running it. If the worker crashes mid-execution, the task is LOST.

task_acks_late=True: Worker acknowledges ONLY after the task completes
successfully. If the worker crashes, the task stays in the queue and
gets re-delivered to another worker.

Risk: if a task succeeds but the ack fails → task runs TWICE.
Solution: make tasks idempotent (safe to run multiple times).
  process_deposit checks: if status != PENDING → return early.
  Running it twice just hits the early return on the second run.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: asyncio.run() inside a Celery task
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Celery workers use the 'prefork' pool by default (one OS process per
worker). Each OS process has NO running event loop.

asyncio.run() creates a fresh event loop, runs the coroutine to
completion, then destroys the loop. This is safe and correct for
prefork workers. It allows us to reuse the same async SQLAlchemy
engine setup without adding a separate sync DB driver (psycopg2).

NOT safe with: --pool=eventlet or --pool=gevent (they monkey-patch
asyncio). For those pools, use a sync DB session instead.
"""

import asyncio
import time
import uuid
from datetime import datetime, timezone

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.models.transaction import Transaction, TransactionStatus
from app.workers.celery_app import celery_app

logger = structlog.get_logger(__name__)


# =============================================================================
# Celery Task: process_deposit
# =============================================================================

@celery_app.task(
    # bind=True gives the task access to `self` — needed for self.retry()
    # and to read self.request.retries (current retry count).
    bind=True,

    # Unique task name — must match the module path for auto-discovery.
    # Explicit naming prevents issues if the file is renamed or moved.
    name="app.workers.deposit_tasks.process_deposit",

    # Maximum number of retry attempts AFTER the first failure.
    # Total attempts = 1 (initial) + max_retries = 4 total attempts.
    max_retries=3,

    # Acknowledge the task AFTER it completes, not before it starts.
    # This guarantees at-least-once delivery — if the worker crashes
    # mid-processing, the task will be redelivered.
    acks_late=True,

    # Reject unacknowledged tasks back to the queue if the worker is killed.
    # Combined with acks_late: task survives worker crashes.
    reject_on_worker_lost=True,
)
def process_deposit(self, transaction_id: str) -> dict:
    """
    Celery task: simulate bank processing and mark deposit as COMPLETED.

    This function is SYNCHRONOUS (Celery tasks are sync by default).
    We use asyncio.run() to bridge into the async SQLAlchemy world.

    Idempotency guarantee:
    If this task runs twice (due to network issues causing re-delivery
    with acks_late=True), the second run detects status != PENDING and
    exits cleanly without corrupting data.

    Retry strategy — exponential backoff:
      Attempt 1 (immediate): fails
      Attempt 2 (after 60s): fails
      Attempt 3 (after 120s): fails
      Attempt 4 (after 240s): if still failing → mark FAILED

    Args:
        transaction_id: UUID string of the Transaction to process.

    Returns:
        dict with final status and processing details.
    """
    structlog.contextvars.bind_contextvars(
        transaction_id=transaction_id,
        retry_count=self.request.retries,
    )
    log = logger.bind(task_name="process_deposit", task_id=str(self.request.id or ""))
    log.info("deposit_task_started")
    start_time = time.monotonic()

    try:
        result = asyncio.run(_execute_deposit_processing(transaction_id))
        duration_ms = round((time.monotonic() - start_time) * 1000, 1)
        log.info("deposit_task_completed", duration_ms=duration_ms, **result)
        return result

    except _TransactionNotFoundError:
        log.error("deposit_transaction_not_found")
        return {"transaction_id": transaction_id, "status": "not_found"}

    except _AlreadyProcessedError:
        log.warning("deposit_already_processed")
        return {"transaction_id": transaction_id, "status": "already_processed"}

    except Exception as exc:
        retry_countdown = 60 * (2 ** self.request.retries)
        log.warning(
            "deposit_task_failed_will_retry",
            error=str(exc),
            error_type=type(exc).__name__,
            attempt_number=self.request.retries + 1,
            max_attempts=self.max_retries + 1,
            retry_in_seconds=retry_countdown,
        )

        if self.request.retries >= self.max_retries:
            log.error("deposit_task_max_retries_exhausted", error=str(exc))
            try:
                asyncio.run(_mark_transaction_failed(transaction_id, str(exc)))
                log.warning("deposit_marked_transaction_failed")
            except Exception as final_exc:
                log.critical("deposit_could_not_mark_failed", error=str(final_exc))
            raise exc

        raise self.retry(exc=exc, countdown=retry_countdown)

    finally:
        structlog.contextvars.clear_contextvars()


# =============================================================================
# Async implementation — called via asyncio.run()
# =============================================================================

class _TransactionNotFoundError(Exception):
    """Raised when the transaction_id doesn't exist in the DB."""


class _AlreadyProcessedError(Exception):
    """Raised when the transaction is not in PENDING state (idempotent guard)."""


async def _execute_deposit_processing(transaction_id: str) -> dict:
    """
    The actual async business logic for deposit processing.

    DB operations:
    1. Fetch the transaction by ID
    2. Guard: if not PENDING, return early (idempotent)
    3. Simulate bank API call (asyncio.sleep — represents the slow external call)
    4. Update transaction status to COMPLETED

    Why NOT update wallet.balance here?
    The wallet balance was already updated synchronously in wallet_service.deposit()
    BEFORE this task was queued. The task ONLY updates the transaction status.
    The balance is already correct — this task confirms the processing is done.

    This design mirrors real fintech:
    - Instant credit model: balance credited immediately
    - Async confirmation: bank side processing confirms later
    """
    async with AsyncSessionLocal() as session:
        # -------------------------------------------------------------------------
        # Step 1: Fetch transaction with a row lock to prevent concurrent processing
        # of the same transaction_id if the task somehow gets delivered twice.
        # -------------------------------------------------------------------------
        result = await session.execute(
            select(Transaction)
            .where(Transaction.id == uuid.UUID(transaction_id))
            .with_for_update()  # lock this row during processing
        )
        transaction = result.scalar_one_or_none()

        if transaction is None:
            raise _TransactionNotFoundError(f"Transaction {transaction_id} not found")

        # -------------------------------------------------------------------------
        # Step 2: Idempotency guard
        # If this task runs twice (at-least-once delivery), the second run
        # sees status=COMPLETED and exits without modifying anything.
        # -------------------------------------------------------------------------
        if transaction.status != TransactionStatus.PENDING:
            raise _AlreadyProcessedError(
                f"Transaction {transaction_id} has status={transaction.status} — skipping"
            )

        # -------------------------------------------------------------------------
        # Step 3: Simulate bank-side processing
        # In production: call payment gateway API, wait for confirmation.
        # Here: sleep 2-5 seconds to simulate the network call.
        # asyncio.sleep is non-blocking — allows other coroutines to run
        # (though in a Celery task there are no other coroutines, it's accurate).
        # -------------------------------------------------------------------------
        logger.info(
            "deposit_simulating_bank_processing",
            transaction_id=transaction_id,
            amount=str(transaction.amount),
        )
        await asyncio.sleep(3)  # simulate 3-second bank API call

        # -------------------------------------------------------------------------
        # Step 4: Update transaction status to COMPLETED
        # Only the status changes — amount, balance_before, balance_after
        # are IMMUTABLE and never touched after creation.
        # -------------------------------------------------------------------------
        transaction.status = TransactionStatus.COMPLETED
        # updated_at is set by the SQLAlchemy onupdate trigger in TimestampMixin
        session.add(transaction)
        await session.commit()

        logger.info(
            "deposit_transaction_completed",
            transaction_id=transaction_id,
            amount=str(transaction.amount),
        )

        return {
            "transaction_id": transaction_id,
            "status": "completed",
            "amount": str(transaction.amount),
            "processed_at": datetime.now(timezone.utc).isoformat(),
        }


async def _mark_transaction_failed(transaction_id: str, error_detail: str) -> None:
    """
    Mark a transaction as FAILED when all retries are exhausted.
    Called as a last resort from the Celery retry-exhausted handler.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(Transaction)
            .where(Transaction.id == uuid.UUID(transaction_id))
            .with_for_update()
        )
        transaction = result.scalar_one_or_none()

        if transaction is None or transaction.status != TransactionStatus.PENDING:
            return  # nothing to do

        transaction.status = TransactionStatus.FAILED
        # Store error context in the description field for audit purposes
        if transaction.description:
            transaction.description = (
                f"{transaction.description} | PROCESSING_FAILED: {error_detail[:200]}"
            )
        else:
            transaction.description = f"PROCESSING_FAILED: {error_detail[:200]}"

        session.add(transaction)
        await session.commit()

        logger.error(
            "deposit_transaction_marked_failed",
            transaction_id=transaction_id,
            error=error_detail,
        )
