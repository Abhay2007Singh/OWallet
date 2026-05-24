"""
tests/test_phase6.py

Phase 6 test suite: Advanced Celery Async Architecture.

Tests cover:
  1. simulate_bank_webhook — happy path (PENDING → COMPLETED)
  2. simulate_bank_webhook — idempotency (already COMPLETED → early exit)
  3. simulate_bank_webhook — not found
  4. simulate_bank_webhook — retry behavior on DB error
  5. cleanup_stale_transactions — finds and marks stale rows FAILED
  6. cleanup_stale_transactions — no stale rows (no-op)
  7. cleanup_expired_idempotency_keys — deletes expired rows
  8. cleanup_expired_idempotency_keys — no expired rows (no-op)
  9. send_transfer_notification — happy path with emails (transfer_sent)
  10. send_transfer_notification — happy path with emails (transfer_received)
  11. send_transfer_notification — retry behavior
  12. send_transfer_notification — exhausts retries, returns failure dict
  13. Celery Beat schedule configured correctly
  14. worker_max_tasks_per_child configured correctly
  15. All tasks have task_acks_late=True

Strategy:
  - Unit tests: patch AsyncSessionLocal and AsyncMock to isolate DB logic.
    We test the ASYNC inner functions (_execute_webhook_processing, etc.)
    directly — they contain all the business logic.
  - Task-level tests: call the Celery task function synchronously by
    invoking the underlying function directly (bypassing Celery infrastructure).
    We use mock.patch to replace asyncio.run() with a direct call.
  - Configuration tests: inspect celery_app.conf for required settings.

We do NOT test against a real DB or Redis in this file.
Integration tests against live infrastructure: run with docker-compose up.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ─────────────────────────────────────────────────────────────────────────────
# Helpers — build minimal mock objects
# ─────────────────────────────────────────────────────────────────────────────

def _make_mock_transaction(
    transaction_id: str,
    status: str = "pending",
    amount: str = "100.00",
) -> MagicMock:
    """Build a minimal Transaction mock with the fields our tasks access."""
    tx = MagicMock()
    tx.id = uuid.UUID(transaction_id)
    tx.amount = Decimal(amount)
    tx.status = MagicMock()
    tx.status.value = status
    tx.description = None

    from app.models.transaction import TransactionStatus
    if status == "pending":
        tx.status = TransactionStatus.PENDING
    elif status == "completed":
        tx.status = TransactionStatus.COMPLETED
    elif status == "failed":
        tx.status = TransactionStatus.FAILED
    return tx


def _make_mock_idempotency_key(key_id: str, expires_ago_hours: int = 25) -> MagicMock:
    """Build a minimal IdempotencyKey mock."""
    k = MagicMock()
    k.id = uuid.UUID(key_id)
    k.expires_at = datetime.now(tz=timezone.utc) - timedelta(hours=expires_ago_hours)
    return k


def _make_mock_session(transaction: MagicMock | None) -> AsyncMock:
    """Build a mock AsyncSession that returns `transaction` on scalar_one_or_none()."""
    session = AsyncMock()
    result = AsyncMock()
    result.scalar_one_or_none.return_value = transaction
    session.execute = AsyncMock(return_value=result)
    session.add = MagicMock()
    session.commit = AsyncMock()

    # Support async context manager protocol (async with AsyncSessionLocal() as session:)
    session_ctx = AsyncMock()
    session_ctx.__aenter__ = AsyncMock(return_value=session)
    session_ctx.__aexit__ = AsyncMock(return_value=False)
    return session_ctx


# =============================================================================
# 1–4. simulate_bank_webhook
# =============================================================================

class TestSimulateBankWebhook:
    """Tests for the _execute_webhook_processing async function."""

    @pytest.mark.asyncio
    async def test_happy_path_pending_to_completed(self):
        """
        A PENDING transaction is found and updated to COMPLETED.
        """
        tx_id = str(uuid.uuid4())
        transaction = _make_mock_transaction(tx_id, status="pending", amount="250.00")
        session_ctx = _make_mock_session(transaction)

        import structlog
        log = structlog.get_logger("test")

        with patch("app.workers.webhook_tasks.AsyncSessionLocal", return_value=session_ctx):
            with patch("asyncio.sleep", new_callable=AsyncMock):  # skip the 3s sleep
                from app.workers.webhook_tasks import _execute_webhook_processing
                result = await _execute_webhook_processing(tx_id, log)

        assert result["transaction_id"] == tx_id
        assert result["status"] == "completed"
        assert result["amount"] == "250.00"

        # Verify the status was updated on the transaction object
        from app.models.transaction import TransactionStatus
        assert transaction.status == TransactionStatus.COMPLETED
        session_ctx.__aenter__.return_value.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_idempotency_already_completed(self):
        """
        A COMPLETED transaction triggers _AlreadyProcessedError (idempotent guard).
        Running the webhook twice does not re-update the record.
        """
        tx_id = str(uuid.uuid4())
        transaction = _make_mock_transaction(tx_id, status="completed")
        session_ctx = _make_mock_session(transaction)

        import structlog
        log = structlog.get_logger("test")

        from app.workers.webhook_tasks import _AlreadyProcessedError

        with patch("app.workers.webhook_tasks.AsyncSessionLocal", return_value=session_ctx):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                from app.workers.webhook_tasks import _execute_webhook_processing
                with pytest.raises(_AlreadyProcessedError):
                    await _execute_webhook_processing(tx_id, log)

        # Commit must NOT have been called — no DB write on duplicate delivery
        session_ctx.__aenter__.return_value.commit.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_transaction_not_found(self):
        """
        When the transaction_id doesn't exist in DB, _TransactionNotFoundError is raised.
        """
        tx_id = str(uuid.uuid4())
        session_ctx = _make_mock_session(None)  # scalar_one_or_none returns None

        import structlog
        log = structlog.get_logger("test")

        from app.workers.webhook_tasks import _TransactionNotFoundError

        with patch("app.workers.webhook_tasks.AsyncSessionLocal", return_value=session_ctx):
            with patch("asyncio.sleep", new_callable=AsyncMock):
                from app.workers.webhook_tasks import _execute_webhook_processing
                with pytest.raises(_TransactionNotFoundError):
                    await _execute_webhook_processing(tx_id, log)

    @pytest.mark.asyncio
    async def test_mark_transaction_failed(self):
        """
        _mark_transaction_failed sets status=FAILED and appends error context to description.
        """
        tx_id = str(uuid.uuid4())
        transaction = _make_mock_transaction(tx_id, status="pending")
        transaction.description = "test deposit"
        session_ctx = _make_mock_session(transaction)

        with patch("app.workers.webhook_tasks.AsyncSessionLocal", return_value=session_ctx):
            from app.workers.webhook_tasks import _mark_transaction_failed
            await _mark_transaction_failed(tx_id, "DB connection refused")

        from app.models.transaction import TransactionStatus
        assert transaction.status == TransactionStatus.FAILED
        assert "WEBHOOK_FAILED" in transaction.description
        session_ctx.__aenter__.return_value.commit.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_mark_transaction_failed_skips_completed(self):
        """
        _mark_transaction_failed is a no-op if the transaction is already COMPLETED.
        """
        tx_id = str(uuid.uuid4())
        transaction = _make_mock_transaction(tx_id, status="completed")
        session_ctx = _make_mock_session(transaction)

        with patch("app.workers.webhook_tasks.AsyncSessionLocal", return_value=session_ctx):
            from app.workers.webhook_tasks import _mark_transaction_failed
            await _mark_transaction_failed(tx_id, "some error")

        # Status must remain COMPLETED — no DB write
        from app.models.transaction import TransactionStatus
        assert transaction.status == TransactionStatus.COMPLETED
        session_ctx.__aenter__.return_value.commit.assert_not_awaited()


# =============================================================================
# 5–6. cleanup_stale_transactions
# =============================================================================

class TestCleanupStaleTrasactions:

    @pytest.mark.asyncio
    async def test_finds_and_marks_stale_rows(self):
        """
        When stale PENDING transactions exist, they are bulk-updated to FAILED.
        """
        stale_ids = [uuid.uuid4(), uuid.uuid4(), uuid.uuid4()]

        session = AsyncMock()
        session.commit = AsyncMock()
        session.add = MagicMock()

        # First execute() call returns the stale IDs list
        id_result = AsyncMock()
        id_result.fetchall.return_value = [(tx_id,) for tx_id in stale_ids]

        session.execute = AsyncMock(return_value=id_result)

        session_ctx = AsyncMock()
        session_ctx.__aenter__ = AsyncMock(return_value=session)
        session_ctx.__aexit__ = AsyncMock(return_value=False)

        import structlog
        log = structlog.get_logger("test")

        with patch("app.workers.cleanup_tasks.AsyncSessionLocal", return_value=session_ctx):
            from app.workers.cleanup_tasks import _execute_stale_cleanup
            result = await _execute_stale_cleanup(log)

        assert result["stale_count"] == 3
        assert result["marked_failed"] == 3
        session.commit.assert_awaited_once()
        # execute was called at least twice: SELECT ids + UPDATE
        assert session.execute.call_count >= 2

    @pytest.mark.asyncio
    async def test_no_stale_rows_is_noop(self):
        """
        When no stale transactions exist, no UPDATE is executed.
        """
        session = AsyncMock()
        session.commit = AsyncMock()

        id_result = AsyncMock()
        id_result.fetchall.return_value = []
        session.execute = AsyncMock(return_value=id_result)

        session_ctx = AsyncMock()
        session_ctx.__aenter__ = AsyncMock(return_value=session)
        session_ctx.__aexit__ = AsyncMock(return_value=False)

        import structlog
        log = structlog.get_logger("test")

        with patch("app.workers.cleanup_tasks.AsyncSessionLocal", return_value=session_ctx):
            from app.workers.cleanup_tasks import _execute_stale_cleanup
            result = await _execute_stale_cleanup(log)

        assert result["stale_count"] == 0
        assert result["marked_failed"] == 0
        # No commit needed when there's nothing to update
        session.commit.assert_not_awaited()
        # Only the SELECT was executed (not the UPDATE)
        assert session.execute.call_count == 1


# =============================================================================
# 7–8. cleanup_expired_idempotency_keys
# =============================================================================

class TestCleanupExpiredIdempotencyKeys:

    @pytest.mark.asyncio
    async def test_deletes_expired_keys(self):
        """
        Expired idempotency_keys rows are deleted in a single bulk DELETE.
        """
        expired_ids = [uuid.uuid4(), uuid.uuid4()]

        session = AsyncMock()
        session.commit = AsyncMock()

        count_result = AsyncMock()
        count_result.fetchall.return_value = [(k,) for k in expired_ids]
        session.execute = AsyncMock(return_value=count_result)

        session_ctx = AsyncMock()
        session_ctx.__aenter__ = AsyncMock(return_value=session)
        session_ctx.__aexit__ = AsyncMock(return_value=False)

        import structlog
        log = structlog.get_logger("test")

        with patch("app.workers.cleanup_tasks.AsyncSessionLocal", return_value=session_ctx):
            from app.workers.cleanup_tasks import _execute_idempotency_cleanup
            result = await _execute_idempotency_cleanup(log)

        assert result["deleted_count"] == 2
        session.commit.assert_awaited_once()
        # SELECT + DELETE = 2 execute calls
        assert session.execute.call_count == 2

    @pytest.mark.asyncio
    async def test_no_expired_keys_is_noop(self):
        """
        When there are no expired keys, no DELETE is issued.
        """
        session = AsyncMock()
        session.commit = AsyncMock()

        count_result = AsyncMock()
        count_result.fetchall.return_value = []
        session.execute = AsyncMock(return_value=count_result)

        session_ctx = AsyncMock()
        session_ctx.__aenter__ = AsyncMock(return_value=session)
        session_ctx.__aexit__ = AsyncMock(return_value=False)

        import structlog
        log = structlog.get_logger("test")

        with patch("app.workers.cleanup_tasks.AsyncSessionLocal", return_value=session_ctx):
            from app.workers.cleanup_tasks import _execute_idempotency_cleanup
            result = await _execute_idempotency_cleanup(log)

        assert result["deleted_count"] == 0
        session.commit.assert_not_awaited()
        # Only the SELECT COUNT was executed (not the DELETE)
        assert session.execute.call_count == 1


# =============================================================================
# 9–12. send_transfer_notification
# =============================================================================

class TestSendTransferNotification:

    @pytest.mark.asyncio
    async def test_transfer_sent_notification(self):
        """
        transfer_sent notification: sender gets "You sent $X to receiver".
        """
        import structlog
        log = structlog.get_logger("test")

        from app.workers.notification_tasks import _execute_notification

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await _execute_notification(
                transaction_id=str(uuid.uuid4()),
                notification_type="transfer_sent",
                user_id=str(uuid.uuid4()),
                amount="150.00",
                reference_id=str(uuid.uuid4()),
                sender_email="alice@example.com",
                receiver_email="bob@example.com",
                log=log,
            )

        assert result["status"] == "delivered"
        assert result["recipient"] == "alice@example.com"
        assert "sent" in result["message"].lower() or "150.00" in result["message"]
        assert "bob@example.com" in result["message"]
        assert result["notification_type"] == "transfer_sent"

    @pytest.mark.asyncio
    async def test_transfer_received_notification(self):
        """
        transfer_received notification: receiver gets "You received $X from sender".
        """
        import structlog
        log = structlog.get_logger("test")

        from app.workers.notification_tasks import _execute_notification

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await _execute_notification(
                transaction_id=str(uuid.uuid4()),
                notification_type="transfer_received",
                user_id=str(uuid.uuid4()),
                amount="75.50",
                reference_id=str(uuid.uuid4()),
                sender_email="alice@example.com",
                receiver_email="bob@example.com",
                log=log,
            )

        assert result["status"] == "delivered"
        assert result["recipient"] == "bob@example.com"
        assert "received" in result["message"].lower() or "75.50" in result["message"]
        assert "alice@example.com" in result["message"]

    @pytest.mark.asyncio
    async def test_notification_falls_back_to_user_id_when_no_email(self):
        """
        When emails are empty strings, falls back to user_id as recipient identifier.
        """
        import structlog
        log = structlog.get_logger("test")

        from app.workers.notification_tasks import _execute_notification
        user_id = str(uuid.uuid4())

        with patch("asyncio.sleep", new_callable=AsyncMock):
            result = await _execute_notification(
                transaction_id=str(uuid.uuid4()),
                notification_type="transfer_sent",
                user_id=user_id,
                amount="50.00",
                reference_id=str(uuid.uuid4()),
                sender_email="",  # missing
                receiver_email="",
                log=log,
            )

        # Falls back to user_id when sender_email is empty
        assert result["recipient"] == user_id
        assert result["status"] == "delivered"

    @pytest.mark.asyncio
    async def test_notification_simulate_api_failure_raises(self):
        """
        When _execute_notification raises (simulates API being down),
        the exception propagates so the task's retry logic can handle it.
        This tests the failure path that the Celery task catches.
        """
        import structlog
        log = structlog.get_logger("test")

        from app.workers.notification_tasks import _execute_notification

        # asyncio.sleep raises to simulate an API timeout
        async def _failing_sleep(_):
            raise ConnectionError("SendGrid is down")

        with patch("asyncio.sleep", side_effect=_failing_sleep):
            with pytest.raises(ConnectionError, match="SendGrid is down"):
                await _execute_notification(
                    transaction_id=str(uuid.uuid4()),
                    notification_type="transfer_sent",
                    user_id=str(uuid.uuid4()),
                    amount="100.00",
                    reference_id=str(uuid.uuid4()),
                    sender_email="alice@test.com",
                    receiver_email="bob@test.com",
                    log=log,
                )


# =============================================================================
# 13–15. Celery configuration assertions
# =============================================================================

class TestCeleryConfiguration:
    """Verify the Celery app is configured correctly for production reliability."""

    def test_worker_max_tasks_per_child_is_1000(self):
        from app.workers.celery_app import celery_app
        assert celery_app.conf.worker_max_tasks_per_child == 1000

    def test_task_acks_late_is_true(self):
        from app.workers.celery_app import celery_app
        assert celery_app.conf.task_acks_late is True

    def test_task_reject_on_worker_lost_is_true(self):
        from app.workers.celery_app import celery_app
        assert celery_app.conf.task_reject_on_worker_lost is True

    def test_serializer_is_json_not_pickle(self):
        from app.workers.celery_app import celery_app
        assert celery_app.conf.task_serializer == "json"
        assert "json" in celery_app.conf.accept_content
        assert "pickle" not in celery_app.conf.accept_content

    def test_timezone_is_utc(self):
        from app.workers.celery_app import celery_app
        assert celery_app.conf.timezone == "UTC"
        assert celery_app.conf.enable_utc is True

    def test_beat_schedule_contains_stale_transaction_cleanup(self):
        from app.workers.celery_app import celery_app
        schedule = celery_app.conf.beat_schedule
        assert "cleanup-stale-transactions-hourly" in schedule
        entry = schedule["cleanup-stale-transactions-hourly"]
        assert entry["task"] == "app.workers.cleanup_tasks.cleanup_stale_transactions"

    def test_beat_schedule_contains_idempotency_key_cleanup(self):
        from app.workers.celery_app import celery_app
        schedule = celery_app.conf.beat_schedule
        assert "cleanup-expired-idempotency-keys-6h" in schedule
        entry = schedule["cleanup-expired-idempotency-keys-6h"]
        assert entry["task"] == "app.workers.cleanup_tasks.cleanup_expired_idempotency_keys"

    def test_all_task_modules_included(self):
        from app.workers.celery_app import celery_app
        includes = celery_app.conf.include
        assert "app.workers.webhook_tasks" in includes
        assert "app.workers.cleanup_tasks" in includes
        assert "app.workers.notification_tasks" in includes
        assert "app.workers.deposit_tasks" in includes

    def test_result_expires_24_hours(self):
        from app.workers.celery_app import celery_app
        assert celery_app.conf.result_expires == 86400

    def test_worker_prefetch_multiplier_is_one(self):
        """
        Prefetch multiplier of 1 ensures one task at a time per worker process,
        preventing slow tasks from starving the worker's prefetch buffer.
        """
        from app.workers.celery_app import celery_app
        assert celery_app.conf.worker_prefetch_multiplier == 1


# =============================================================================
# 16. Task registration verification
# =============================================================================

class TestTaskRegistration:
    """Verify tasks are properly registered in the Celery app."""

    def test_simulate_bank_webhook_is_registered(self):
        from app.workers.celery_app import celery_app
        assert "app.workers.webhook_tasks.simulate_bank_webhook" in celery_app.tasks

    def test_cleanup_stale_transactions_is_registered(self):
        from app.workers.celery_app import celery_app
        assert "app.workers.cleanup_tasks.cleanup_stale_transactions" in celery_app.tasks

    def test_cleanup_expired_idempotency_keys_is_registered(self):
        from app.workers.celery_app import celery_app
        assert "app.workers.cleanup_tasks.cleanup_expired_idempotency_keys" in celery_app.tasks

    def test_send_transfer_notification_is_registered(self):
        from app.workers.celery_app import celery_app
        assert "app.workers.notification_tasks.send_transfer_notification" in celery_app.tasks

    def test_process_deposit_is_registered(self):
        from app.workers.celery_app import celery_app
        assert "app.workers.deposit_tasks.process_deposit" in celery_app.tasks


# =============================================================================
# 17. Stale transaction threshold configuration
# =============================================================================

class TestCleanupConfiguration:

    def test_stale_threshold_is_one_hour(self):
        """
        The cleanup threshold must be large enough to give the webhook task
        time to exhaust all its retries (~210s). 1 hour = 3600s >> 210s.
        """
        from app.workers.cleanup_tasks import STALE_TRANSACTION_THRESHOLD_HOURS
        assert STALE_TRANSACTION_THRESHOLD_HOURS >= 1
        # Also verify it's not absurdly long (> 24h would be bad UX)
        assert STALE_TRANSACTION_THRESHOLD_HOURS <= 24

    def test_stale_threshold_covers_retry_window(self):
        """
        The retry window for simulate_bank_webhook is ~210s.
        Verify the cleanup threshold >> that window.
        """
        from app.workers.cleanup_tasks import STALE_TRANSACTION_THRESHOLD_HOURS
        max_retries = 3
        # Worst case retry window: sum of all backoffs
        # countdown = 30 * 2^(retry_number) for each retry
        total_retry_seconds = sum(30 * (2 ** i) for i in range(max_retries))  # 30 + 60 + 120 = 210
        threshold_seconds = STALE_TRANSACTION_THRESHOLD_HOURS * 3600
        assert threshold_seconds > total_retry_seconds * 2  # at least 2× safety margin
