"""
app/workers/cleanup_tasks.py

Scheduled Celery tasks for periodic database maintenance.

Enqueued by Celery Beat on a cron-style schedule defined in celery_app.py.
Executed by the regular celery_worker container (not Beat — Beat only schedules).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: Why scheduled cleanup is non-optional in fintech
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Problem 1: Stale PENDING transactions
  The simulate_bank_webhook task has max_retries=3 with exponential backoff.
  Total retry window: ~210 seconds after the first failure.
  After all retries, the transaction status remains PENDING indefinitely.

  Without cleanup:
    User deposits $100 → webhook task fails (DB was down for 5 minutes)
    Transaction shows "pending" in history for days
    User calls support: "is my money lost?"
    Support cannot definitively answer without checking the DB manually
    Bad UX. Manual operational overhead.

  With cleanup_stale_transactions (hourly):
    Transaction shows "pending" for at most 60 minutes
    Then automatically marked "failed" with context in description
    User sees: "Deposit failed — please try again or contact support"
    Clean, clear, self-service resolution.

Problem 2: Orphaned idempotency_keys rows
  Phase 5: every deposit and transfer creates a row in idempotency_keys.
  Keys expire after 24 hours (enforced by the Phase 5 service query:
    WHERE expires_at > NOW()). Expired keys cannot be used for replay.
  But the DB rows are NOT deleted by the expiry check — they accumulate.

  Without cleanup:
    Production fintech with 1M daily active users × 5 operations/user/day
    = 5M new rows/day × 365 = 1.8 BILLION rows per year
    The idempotency lookup (WHERE key=? AND user_id=? AND endpoint=? AND expires_at > NOW())
    becomes a slow table scan on a 1.8B-row table.
    Storage costs: 1.8B rows × ~200 bytes/row = ~360GB/year just for idempotency data.

  With cleanup_expired_idempotency_keys (every 6h):
    Only the last 30 hours of idempotency data is retained at most
    The table stays small. Lookups stay fast.
    Storage costs remain bounded regardless of traffic growth.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: Bulk UPDATE vs row-by-row processing
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Row-by-row approach (DON'T do this at scale):
  stale = await db.execute(SELECT ... WHERE status=PENDING AND created_at < cutoff)
  for tx in stale.scalars():
      tx.status = FAILED
      await db.commit()   ← one DB round-trip per row

  1000 stale transactions → 1000 DB round-trips → 1000 lock-acquire/release cycles
  Under heavy load: connection pool exhaustion, lock contention, seconds of latency.

Bulk UPDATE (our approach):
  await db.execute(
      UPDATE transactions
      SET status = 'failed'
      WHERE status = 'pending' AND created_at < cutoff
  )
  await db.commit()

  1000 stale transactions → 1 DB round-trip → 1 lock per row, released on commit
  Milliseconds regardless of row count.

Trade-off: we cannot do per-row side effects (like queuing a notification per
stale transaction) atomically inside the UPDATE. If needed, use the RETURNING
clause to get affected IDs, then queue side-effect tasks after commit.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: Cleanup task idempotency
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Both cleanup tasks are inherently idempotent:

cleanup_stale_transactions:
  UPDATE WHERE status=PENDING AND created_at < cutoff
  Running twice:
    First run: finds 5 stale rows, updates them to FAILED.
    Second run: finds 0 stale rows (they're now FAILED, not PENDING). 0 updates.
  ✓ Safe to run multiple times.

cleanup_expired_idempotency_keys:
  DELETE WHERE expires_at < NOW()
  Running twice:
    First run: deletes 100 expired rows.
    Second run: those rows are gone. 0 deletes.
  ✓ Safe to run multiple times.

This means Celery Beat can fire these tasks even if Beat has a scheduling
hiccup and enqueues the task twice. The second execution is a no-op.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: Why 1-hour threshold for stale transactions?
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The webhook task retries:
  Attempt 1: immediate
  Attempt 2: +30s (countdown = 30 × 2^0)
  Attempt 3: +60s (countdown = 30 × 2^1)
  Attempt 4: +120s (countdown = 30 × 2^2)

  Total time from first attempt to exhaustion: ~0s + 30s + 60s + 120s = 210s ≈ 3.5 min

So after 3.5 minutes, the webhook task has either:
  - Succeeded (transaction is COMPLETED), OR
  - Exhausted all retries (transaction stays PENDING or is marked FAILED by the task)

The 1-hour threshold (3600 seconds >> 210 seconds) gives:
  - 17× more time than the retry window — no risk of marking an actively-retrying task
  - Fast enough to surface stuck transactions to users within a reasonable window
  - A deposit older than 1 hour is unambiguously stuck — bank response would never take 1 hour

If your bank integration is slower (hypothetically, 30-minute bank responses):
  Adjust threshold to 2 hours. The constant is at the top of this file.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import delete, select, update

from app.core.database import AsyncSessionLocal
from app.models.idempotency_key import IdempotencyKey
from app.models.transaction import Transaction, TransactionStatus
from app.workers.celery_app import celery_app

logger = structlog.get_logger(__name__)

# Transactions PENDING for longer than this are considered stuck.
# The webhook task's total retry window is ~210s. One hour = 17× that window.
# See the "Why 1-hour threshold" concept doc above for full reasoning.
STALE_TRANSACTION_THRESHOLD_HOURS: int = 1


# =============================================================================
# cleanup_stale_transactions — runs hourly via Beat
# =============================================================================

@celery_app.task(
    bind=True,
    name="app.workers.cleanup_tasks.cleanup_stale_transactions",
    max_retries=2,
    acks_late=True,
)
def cleanup_stale_transactions(self) -> dict:
    """
    Scheduled task: mark stuck PENDING deposits as FAILED.

    A deposit transaction is "stuck" when:
      - status == PENDING (bank webhook never confirmed it)
      - created_at older than STALE_TRANSACTION_THRESHOLD_HOURS

    These are deposits whose simulate_bank_webhook task exhausted all
    retries and left the transaction in a permanent PENDING state.

    This task gives those transactions a clean terminal state (FAILED)
    so users and support agents understand what happened.

    Returns:
        dict: {"stale_count": N, "marked_failed": N, "cutoff_timestamp": "..."}
    """
    structlog.contextvars.bind_contextvars(task_name="cleanup_stale_transactions")
    log = logger.bind(task_id=str(self.request.id or ""))
    log.info("stale_cleanup_started")

    try:
        result = asyncio.run(_execute_stale_cleanup(log))
        log.info("stale_cleanup_completed", **result)
        return result
    except Exception as exc:
        log.error("stale_cleanup_failed", error=str(exc), error_type=type(exc).__name__)
        # Retry in 5 minutes — if DB was briefly unavailable, give it time to recover
        raise self.retry(exc=exc, countdown=300)
    finally:
        structlog.contextvars.clear_contextvars()


async def _execute_stale_cleanup(log) -> dict:
    """
    Bulk-UPDATE all PENDING transactions older than the threshold to FAILED.

    Two-step approach:
      1. SELECT the stale transaction IDs (for logging, no lock needed)
      2. Bulk UPDATE status=FAILED (single statement, efficient)

    Why SELECT before UPDATE?
      - We want to know how many rows were affected for logging/monitoring.
      - SELECT is cheap and doesn't hold locks.
      - The UPDATE's WHERE clause re-checks the condition atomically
        so there's no TOCTOU (time-of-check/time-of-use) issue.
    """
    stale_cutoff = datetime.now(tz=timezone.utc) - timedelta(hours=STALE_TRANSACTION_THRESHOLD_HOURS)

    async with AsyncSessionLocal() as session:
        # ─── Step 1: Count stale rows (no lock — read-only) ───────────────────
        id_result = await session.execute(
            select(Transaction.id).where(
                Transaction.status == TransactionStatus.PENDING,
                Transaction.created_at < stale_cutoff,
            )
        )
        stale_ids = [str(row[0]) for row in id_result.fetchall()]
        stale_count = len(stale_ids)

        if stale_count == 0:
            log.info("stale_cleanup_nothing_to_do", cutoff=stale_cutoff.isoformat())
            return {"stale_count": 0, "marked_failed": 0, "cutoff_timestamp": stale_cutoff.isoformat()}

        log.warning(
            "stale_cleanup_found_stuck_transactions",
            stale_count=stale_count,
            cutoff_timestamp=stale_cutoff.isoformat(),
            # Log first 10 IDs for incident investigation — don't log all (could be thousands)
            sample_ids=stale_ids[:10],
        )

        # ─── Step 2: Bulk UPDATE PENDING → FAILED ────────────────────────────
        # Single SQL statement: no loop, no per-row locks, no N round-trips.
        # PostgreSQL will hold per-row locks during the UPDATE and release all on COMMIT.
        # For very large batches in production, consider batching in chunks of 1000:
        #   UPDATE ... WHERE id IN (SELECT id ... LIMIT 1000)
        # This keeps lock duration short and avoids blocking concurrent reads.
        await session.execute(
            update(Transaction)
            .where(
                Transaction.status == TransactionStatus.PENDING,
                Transaction.created_at < stale_cutoff,
            )
            .values(status=TransactionStatus.FAILED)
        )
        await session.commit()

        log.warning(
            "stale_cleanup_marked_failed",
            marked_failed=stale_count,
            cutoff_timestamp=stale_cutoff.isoformat(),
        )

    return {
        "stale_count": stale_count,
        "marked_failed": stale_count,
        "cutoff_timestamp": stale_cutoff.isoformat(),
    }


# =============================================================================
# cleanup_expired_idempotency_keys — runs every 6 hours via Beat
# =============================================================================

@celery_app.task(
    bind=True,
    name="app.workers.cleanup_tasks.cleanup_expired_idempotency_keys",
    max_retries=2,
    acks_late=True,
)
def cleanup_expired_idempotency_keys(self) -> dict:
    """
    Scheduled task: delete expired rows from the idempotency_keys table.

    An idempotency key row is "expired" when expires_at < NOW().
    Expired rows cannot be used for replay (Phase 5 service filters them out
    with WHERE expires_at > NOW()). They are storage waste.

    The ix_idempotency_keys_expires_at index (created in migration 003) makes
    the DELETE WHERE expires_at < NOW() an efficient index scan.
    Without the index: full table scan on every cleanup run.

    Returns:
        dict: {"deleted_count": N, "cutoff_timestamp": "..."}
    """
    structlog.contextvars.bind_contextvars(task_name="cleanup_expired_idempotency_keys")
    log = logger.bind(task_id=str(self.request.id or ""))
    log.info("idempotency_cleanup_started")

    try:
        result = asyncio.run(_execute_idempotency_cleanup(log))
        log.info("idempotency_cleanup_completed", **result)
        return result
    except Exception as exc:
        log.error("idempotency_cleanup_failed", error=str(exc), error_type=type(exc).__name__)
        raise self.retry(exc=exc, countdown=300)
    finally:
        structlog.contextvars.clear_contextvars()


async def _execute_idempotency_cleanup(log) -> dict:
    """
    Delete all idempotency_keys rows where expires_at < NOW().

    We use a SELECT to count before DELETE so we can log the volume.
    In production with millions of rows, consider paginated DELETEs
    to avoid holding a large number of row locks simultaneously:
      DELETE FROM idempotency_keys
      WHERE id IN (SELECT id FROM idempotency_keys WHERE expires_at < NOW() LIMIT 10000)
    This keeps each batch fast and lock-time bounded.
    """
    now = datetime.now(tz=timezone.utc)

    async with AsyncSessionLocal() as session:
        # ─── Count expired rows before deleting ───────────────────────────────
        count_result = await session.execute(
            select(IdempotencyKey.id).where(IdempotencyKey.expires_at < now)
        )
        expired_count = len(count_result.fetchall())

        if expired_count == 0:
            log.info("idempotency_cleanup_nothing_to_do")
            return {"deleted_count": 0, "cutoff_timestamp": now.isoformat()}

        log.info(
            "idempotency_cleanup_deleting",
            expired_count=expired_count,
            cutoff_timestamp=now.isoformat(),
        )

        # ─── Bulk DELETE ──────────────────────────────────────────────────────
        # Single DELETE statement. The ix_idempotency_keys_expires_at index
        # makes the WHERE clause an efficient index range scan.
        await session.execute(
            delete(IdempotencyKey).where(IdempotencyKey.expires_at < now)
        )
        await session.commit()

        log.info("idempotency_cleanup_deleted", deleted_count=expired_count)

    return {"deleted_count": expired_count, "cutoff_timestamp": now.isoformat()}
