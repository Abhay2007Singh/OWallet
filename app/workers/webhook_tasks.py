"""
app/workers/webhook_tasks.py

Celery task that simulates an inbound bank webhook confirming a deposit.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: What is a payment webhook?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

A webhook is an HTTP POST that an external system sends to YOUR API
when an event occurs on their side. In payments:

Real deposit flow with Stripe/Paystack/Razorpay:
  1. Your app POSTs to the gateway:
       POST https://api.stripe.com/v1/charges
       {"amount": 10000, "currency": "usd", "source": "tok_card"}
     Gateway responds immediately: {"id": "ch_abc", "status": "pending"}

  2. Gateway processes asynchronously (bank auth, fraud scoring, settlement)
     This takes 3–30 seconds, sometimes minutes.

  3. Gateway POSTs to YOUR webhook endpoint:
       POST https://api.yourapp.com/webhooks/stripe
       {"type": "charge.succeeded", "data": {"id": "ch_abc", "status": "succeeded"}}

  4. Your webhook handler:
       - Validates the signature (Stripe-Signature header)
       - Finds the transaction by gateway reference
       - Updates status: PENDING → COMPLETED
       - Returns HTTP 200 to tell Stripe: "received and processed"

Why webhooks instead of polling?
  Polling: GET /charges/ch_abc every 5 seconds → wastes requests, adds latency
  Webhooks: bank calls you when done → 0 wasted requests, instant notification

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: How PyWallet simulates this with Celery
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

In production, three separate systems are involved:
  1. PyWallet API: submits deposit to payment gateway
  2. Payment gateway: processes bank authorization asynchronously
  3. Payment gateway: POSTs webhook to PyWallet's /webhooks/payment endpoint

In simulation, we collapse all three into one Celery task:
  1. wallet_service.deposit() creates PENDING transaction + enqueues the task
  2. simulate_bank_webhook sleeps 3s (simulates bank processing delay)
  3. simulate_bank_webhook updates PENDING → COMPLETED (simulates webhook handler)

The pattern is architecturally identical to production:
  - HTTP request returns < 200ms (user gets response immediately)
  - Background task handles the async confirmation
  - Transaction status updates independently of the HTTP lifecycle

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: Idempotent webhook handling — critical for fintech
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Payment gateways deliver webhooks with AT-LEAST-ONCE semantics.
The same event may be delivered 2, 3, or even 10 times due to:
  - Network retry: your server returned 5xx → gateway retries automatically
  - Duplicate delivery: gateway bug (rare, but happens with Stripe, Razorpay)
  - Replay: gateway's manual "resend failed webhooks" tool
  - Celery at-least-once: same task can run twice (task_acks_late)

A non-idempotent webhook handler:
  First delivery: PENDING → COMPLETED, balance = $500 + $100 = $600 ✓
  Second delivery: COMPLETED → another UPDATE? balance = $600 + $100 = $700 ✗
  Result: $100 phantom money created from a duplicate webhook.

Our idempotent handler:
  First delivery: status=PENDING → update to COMPLETED ✓
  Second delivery: status=COMPLETED → early return, no update ✓
  Third delivery: status=COMPLETED → early return, no update ✓
  Runs N times = runs once. Balance is always correct.

The idempotency check is done inside a SELECT FOR UPDATE lock.
This prevents two concurrent deliveries from BOTH seeing PENDING:
  T1: SELECT FOR UPDATE → sees PENDING → updates to COMPLETED
  T2: SELECT FOR UPDATE → BLOCKED (T1 holds the lock)
  T1: COMMIT → releases lock
  T2: unblocked → sees COMPLETED → returns early
  Result: exactly one COMPLETED update. ✓

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: Failure scenarios and recovery
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Scenario 1: Worker killed during asyncio.sleep (before DB write)
  → task_reject_on_worker_lost=True: message re-queued
  → New worker picks up, sleeps 3s again, runs DB update
  → Deposit confirmed correctly on retry

Scenario 2: Worker killed after DB write, before ACK
  → task_acks_late=True: message NOT acked, stays in queue
  → New worker picks up, runs simulate_bank_webhook again
  → Status check: COMPLETED → returns _AlreadyProcessedError → early exit
  → No double-crediting. Deposit stays COMPLETED. ✓

Scenario 3: DB unavailable (connection refused, timeout)
  → Exception raised in _execute_webhook_processing
  → asyncio.run() propagates exception to the sync Celery task
  → self.retry() schedules a retry: +30s, +60s, +120s
  → If all retries fail: _mark_transaction_failed() is called
  → cleanup_stale_transactions rescues it at the next hourly run anyway

Scenario 4: Redis broker restarts and AOF was disabled
  → Queued messages are lost
  → Deposits stay PENDING
  → cleanup_stale_transactions marks them FAILED after 1 hour
  → Our docker-compose.yml enables AOF: redis-server --appendonly yes
  → This scenario is mitigated by the Redis configuration

Scenario 5: Notification API is down during send_transfer_notification
  → That task fails independently and retries independently
  → simulate_bank_webhook is NOT affected
  → Deposit confirms, notification may be delayed but will eventually deliver
"""

import asyncio
import time
import uuid

import structlog
from sqlalchemy import select

from app.core.database import AsyncSessionLocal
from app.models.transaction import Transaction, TransactionStatus
from app.workers.celery_app import celery_app

logger = structlog.get_logger(__name__)


# =============================================================================
# Private sentinel exceptions for task flow control
# =============================================================================

class _TransactionNotFoundError(Exception):
    """The transaction_id does not exist in the DB. Non-retryable."""


class _AlreadyProcessedError(Exception):
    """Transaction is not PENDING — it has already been processed. Idempotency guard."""


# =============================================================================
# Celery Task: simulate_bank_webhook
# =============================================================================

@celery_app.task(
    # bind=True: the task receives `self` as first argument.
    # Required for self.retry(), self.request.retries, self.max_retries.
    bind=True,

    # Explicit task name: must match the import path.
    # Without this, Celery auto-generates the name from the function path.
    # Explicit names are stable: safe to rename the file or function later.
    name="app.workers.webhook_tasks.simulate_bank_webhook",

    # 3 retry attempts after the first failure = 4 total attempts.
    max_retries=3,

    # ACK the message ONLY after task function returns (at-least-once delivery).
    acks_late=True,

    # Re-queue the task if the worker process is killed mid-execution.
    # Combined with acks_late: task survives OOM kills, SIGKILL, container crashes.
    reject_on_worker_lost=True,
)
def simulate_bank_webhook(self, transaction_id: str) -> dict:
    """
    Simulate an inbound bank webhook confirming a deposit.

    This task is queued by wallet_service.deposit() immediately after
    the DB commit that creates the PENDING transaction.

    Full execution:
      1. Simulate bank processing delay (asyncio.sleep 3s)
      2. Load the transaction row with SELECT FOR UPDATE (concurrent-safe)
      3. Idempotency check: if not PENDING → return early
      4. Update status: PENDING → COMPLETED
      5. Commit

    Retry strategy — exponential backoff:
      Attempt 1 (immediate): +0s
      Attempt 2 (after 30s):  retries=1, countdown=30×(2^0)=30s
      Attempt 3 (after 60s):  retries=2, countdown=30×(2^1)=60s
      Attempt 4 (after 120s): retries=3, countdown=30×(2^2)=120s
      After attempt 4: _mark_transaction_failed() + raise

    Args:
        transaction_id: UUID string of the PENDING deposit Transaction.

    Returns:
        dict with final status, amount, and duration metadata.
    """
    # Bind per-task context so ALL log lines inside this task include
    # these fields automatically (via merge_contextvars in the processor chain).
    structlog.contextvars.bind_contextvars(
        transaction_id=transaction_id,
        retry_count=self.request.retries,
    )

    log = logger.bind(
        task_name="simulate_bank_webhook",
        task_id=str(self.request.id or ""),
    )
    log.info("webhook_task_started")
    start_time = time.monotonic()

    try:
        result = asyncio.run(_execute_webhook_processing(transaction_id, log))
        duration_ms = round((time.monotonic() - start_time) * 1000, 1)
        log.info("webhook_task_completed", duration_ms=duration_ms, **result)
        return result

    except _TransactionNotFoundError:
        # Non-retryable: the transaction_id doesn't exist in the DB.
        # Retrying won't help — the row was never created or was deleted.
        log.error("webhook_transaction_not_found")
        return {"transaction_id": transaction_id, "status": "not_found"}

    except _AlreadyProcessedError as exc:
        # Idempotent early exit: another delivery already confirmed this transaction.
        # This is the EXPECTED path for duplicate webhook deliveries.
        log.info("webhook_already_processed", detail=str(exc))
        return {"transaction_id": transaction_id, "status": "already_processed"}

    except Exception as exc:
        # Retryable: DB connection error, timeout, network issue, etc.
        # Exponential backoff: 30s → 60s → 120s
        retry_countdown = 30 * (2 ** self.request.retries)

        log.warning(
            "webhook_task_failed_will_retry",
            error=str(exc),
            error_type=type(exc).__name__,
            attempt_number=self.request.retries + 1,
            max_attempts=self.max_retries + 1,
            retry_in_seconds=retry_countdown,
        )

        if self.request.retries >= self.max_retries:
            # All retries exhausted. Mark transaction FAILED so the user
            # and support team can see what happened.
            log.error(
                "webhook_task_max_retries_exhausted",
                error=str(exc),
                transaction_id=transaction_id,
            )
            try:
                asyncio.run(_mark_transaction_failed(transaction_id, str(exc)))
                log.warning("webhook_marked_transaction_failed")
            except Exception as inner_exc:
                # Even the failure-marking failed. cleanup_stale_transactions
                # will rescue this transaction at the next hourly run.
                log.critical(
                    "webhook_could_not_mark_failed",
                    original_error=str(exc),
                    mark_failed_error=str(inner_exc),
                )
            # Re-raise so Celery records this task as FAILURE in the result backend.
            # Flower will show it as a failed task, making it visible to operators.
            raise exc

        # Schedule the retry. self.retry() raises Retry which Celery catches
        # to re-enqueue the message with the specified delay.
        raise self.retry(exc=exc, countdown=retry_countdown)

    finally:
        # Always clear the per-task context variables.
        # Without this, bound vars leak into the next task if Celery reuses
        # the same thread/coroutine context.
        structlog.contextvars.clear_contextvars()


# =============================================================================
# Async implementation — called via asyncio.run()
# =============================================================================

async def _execute_webhook_processing(transaction_id: str, log) -> dict:
    """
    Core async business logic for the webhook simulation.

    Why asyncio.run() + async function instead of a sync DB call?
      - asyncpg (our PostgreSQL driver) is async-only.
      - We share the same AsyncSessionLocal factory as the FastAPI app.
      - No need to install a second sync driver (psycopg2).
      - asyncio.run() creates a fresh event loop per Celery task call.
        Safe for Celery's default prefork pool (each process = one OS process).
        Not safe with --pool=eventlet or --pool=gevent (they patch asyncio).

    The SELECT FOR UPDATE lock:
      - Prevents two concurrent webhook deliveries from both seeing PENDING
        and both executing the UPDATE.
      - Lock window: SELECT → status check → UPDATE → COMMIT ≈ 5-10ms.
      - PostgreSQL automatically releases the lock on COMMIT.
    """
    # ─── Step 1: Simulate bank async processing time ──────────────────────────
    # In production: your API submits to the bank, bank processes, then POSTs back.
    # That round-trip takes 3–30 seconds depending on the bank.
    # Here: asyncio.sleep(3) simulates those 3 seconds.
    #
    # asyncio.sleep() is non-blocking (yields control to the event loop).
    # Though Celery tasks don't have other coroutines competing in the same loop,
    # using asyncio.sleep() (not time.sleep()) is the correct async pattern.
    log.info("webhook_simulating_bank_processing", delay_seconds=3)
    await asyncio.sleep(3)

    async with AsyncSessionLocal() as session:
        # ─── Step 2: Load transaction with exclusive row lock ─────────────────
        result = await session.execute(
            select(Transaction)
            .where(Transaction.id == uuid.UUID(transaction_id))
            .with_for_update()  # SELECT ... FOR UPDATE — exclusive lock
        )
        transaction = result.scalar_one_or_none()

        if transaction is None:
            raise _TransactionNotFoundError(f"Transaction {transaction_id} not found in DB")

        # ─── Step 3: Idempotency guard ────────────────────────────────────────
        # This is the most important check in the entire function.
        # If we've already processed this webhook, do nothing.
        # This handles: duplicate delivery, worker crash-and-retry, double-queue.
        if transaction.status != TransactionStatus.PENDING:
            raise _AlreadyProcessedError(
                f"Transaction {transaction_id} has status={transaction.status.value} "
                "— already processed, skipping"
            )

        log.info(
            "webhook_updating_transaction",
            amount=str(transaction.amount),
            current_status=transaction.status.value,
            new_status="completed",
        )

        # ─── Step 4: Mark COMPLETED ───────────────────────────────────────────
        # Only the status changes. amount, balance_before, balance_after are
        # immutable financial audit fields — never modified after creation.
        transaction.status = TransactionStatus.COMPLETED
        session.add(transaction)
        await session.commit()

        # Capture values while session is still open (before expiry on close)
        amount_str = str(transaction.amount)

    log.info(
        "webhook_transaction_confirmed",
        amount=amount_str,
        new_status="completed",
    )

    return {
        "transaction_id": transaction_id,
        "status": "completed",
        "amount": amount_str,
    }


async def _mark_transaction_failed(transaction_id: str, error_detail: str) -> None:
    """
    Last-resort: mark a transaction FAILED after all webhook retries are exhausted.

    Called only when self.request.retries >= self.max_retries and the final
    attempt still raised an exception. This is a best-effort operation —
    if it fails, cleanup_stale_transactions will rescue the transaction
    at the next hourly run.
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

        # Append error context to description for audit/support purposes.
        # The description field is the only mutable non-status field.
        error_suffix = f"WEBHOOK_FAILED: {error_detail[:200]}"
        if transaction.description:
            transaction.description = f"{transaction.description} | {error_suffix}"
        else:
            transaction.description = error_suffix

        session.add(transaction)
        await session.commit()
