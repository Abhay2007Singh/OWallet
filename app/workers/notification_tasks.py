"""
app/workers/notification_tasks.py

Celery tasks for post-transfer email/SMS notifications.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: Why notifications are Celery tasks, not inline code
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

When Alice transfers $100 to Bob, the HTTP response must return in <200ms.
A real notification (SendGrid email, Twilio SMS, Firebase push) takes:
  SendGrid email:   150–500ms
  Twilio SMS:       300–800ms
  Firebase push:    50–200ms

If we call these inline:
  - Latency: the transfer API feels slow
  - Coupling: if SendGrid is down, does the transfer FAIL? With inline code: yes.
  - Thread exhaustion: 50 concurrent notifications × 500ms each = 25 threads
    blocked waiting for external services

With Celery:
  1. Transfer commits to DB → task enqueued to Redis → HTTP 200 returned (< 50ms)
  2. Worker picks up task → calls SendGrid → notification delivered (~500ms later)
  3. Failure model: notification failure does NOT affect transfer status
     The money has already moved — that is irreversible and correct.
     A missed notification is a customer experience issue, not a financial error.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: Retry mechanisms in distributed notification systems
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Transient failures in notification services are common:
  - SendGrid rate limit: 429 Too Many Requests
  - SendGrid planned maintenance: 503 Service Unavailable (brief)
  - Network timeout: connection to api.sendgrid.com timed out
  - DNS failure: temporary resolver failure

These failures are TRANSIENT — they resolve within seconds to minutes.
A notification that fails at attempt 1 often succeeds at attempt 2 (30s later).

Retry strategy: exponential backoff
  Attempt 1 (immediate):    failure → wait 30s
  Attempt 2 (after 30s):   failure → wait 60s
  Attempt 3 (after 60s):   failure → wait 120s
  Attempt 4 (after 120s):  if still failing → final failure, log and move on

Why exponential backoff (not constant interval)?
  If the notification service is overloaded, constant-interval retries
  keep hammering it with the same load → it stays overloaded → thundering herd.
  Exponential backoff reduces the load geometrically, giving the service time
  to recover. This is a form of additive-increase/multiplicative-decrease (AIMD)
  used in TCP congestion control and distributed systems retries everywhere.

Why max 3 retries (not 10)?
  Notifications are non-critical side effects. A user waiting 10 minutes
  for a "transfer received" notification that never arrives is annoying
  but not a financial problem. After 3 retries (~210s), log the failure
  and move on. The money moved — that's what matters.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: Notification idempotency (a known limitation)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

With task_acks_late=True, a notification task may run twice:
  Worker sends email → SendGrid accepts → worker crashes before ACK
  → Task re-delivered → second worker sends the same email AGAIN
  → User receives "You sent $100 to Bob" twice

This is a known at-least-once delivery trade-off. Solutions:
  1. Idempotency key to SendGrid: each email has a unique ID
     SendGrid deduplicates on their end for 72 hours.
     Implementation: message_id = f"{transaction_id}:{notification_type}"
     Pass as X-Message-Id header.

  2. Notification dedup table: store (transaction_id, notification_type)
     in a notifications_sent table. Check before calling the API.
     Risk: DB failure between "check" and "send" still causes duplicates.

  3. Transactional outbox: store notification in the same DB transaction
     as the transfer. A separate relay process reads and sends.
     Guarantees exactly-once if the relay uses idempotency keys.

For Phase 6, we simulate the notification (asyncio.sleep + logging).
Simulated notifications are safe to run twice — logging twice is harmless.
Production deduplication: Phase 7 / notifications table.
"""

import asyncio
import time

import structlog

from app.workers.celery_app import celery_app

logger = structlog.get_logger(__name__)


# =============================================================================
# Celery Task: send_transfer_notification
# =============================================================================

@celery_app.task(
    bind=True,
    name="app.workers.notification_tasks.send_transfer_notification",
    max_retries=3,
    acks_late=True,
    reject_on_worker_lost=True,
)
def send_transfer_notification(
    self,
    transaction_id: str,
    notification_type: str,
    user_id: str,
    amount: str,
    reference_id: str,
    sender_email: str = "",
    receiver_email: str = "",
) -> dict:
    """
    Simulate sending a transfer notification (email/SMS/push) to a user.

    In production, this task would:
      1. Select the notification channel (email/SMS/push) from user preferences
      2. Render a message template with the transfer details
      3. Call the appropriate provider API:
           Email: POST https://api.sendgrid.com/v3/mail/send
           SMS:   POST https://api.twilio.com/2010-04-01/Accounts/.../Messages.json
           Push:  POST https://fcm.googleapis.com/fcm/send
      4. Store a record in a notifications table for audit
      5. Return the provider's message ID

    For Phase 6, we simulate with structured logging + asyncio.sleep(1).
    The simulation is idempotent — logging twice is harmless.

    Retry strategy — exponential backoff:
      Attempt 1 → immediate
      Attempt 2 → +30s   (30 × 2^0)
      Attempt 3 → +60s   (30 × 2^1)
      Attempt 4 → +120s  (30 × 2^2)
      After attempt 4: log final failure, no financial consequence.

    Args:
        transaction_id:    UUID string of the sender's DEBIT transaction.
        notification_type: "transfer_sent" | "transfer_received"
        user_id:           UUID string of the user to notify.
        amount:            Transfer amount as a string (e.g., "100.00").
        reference_id:      Shared transfer_reference_id UUID string.
        sender_email:      Sender's email address (for message content).
        receiver_email:    Receiver's email address (for message content and routing).

    Returns:
        dict with notification delivery metadata.
    """
    structlog.contextvars.bind_contextvars(
        transaction_id=transaction_id,
        notification_type=notification_type,
        retry_count=self.request.retries,
    )

    log = logger.bind(
        task_name="send_transfer_notification",
        task_id=str(self.request.id or ""),
        user_id=user_id,
        amount=amount,
        reference_id=reference_id,
    )
    log.info("notification_task_started")
    start_time = time.monotonic()

    try:
        result = asyncio.run(_execute_notification(
            transaction_id=transaction_id,
            notification_type=notification_type,
            user_id=user_id,
            amount=amount,
            reference_id=reference_id,
            sender_email=sender_email,
            receiver_email=receiver_email,
            log=log,
        ))
        duration_ms = round((time.monotonic() - start_time) * 1000, 1)
        log.info("notification_task_completed", duration_ms=duration_ms)
        return result

    except Exception as exc:
        # Retryable: external notification service down, timeout, rate-limit.
        countdown = 30 * (2 ** self.request.retries)
        log.warning(
            "notification_task_failed_will_retry",
            error=str(exc),
            error_type=type(exc).__name__,
            attempt_number=self.request.retries + 1,
            max_attempts=self.max_retries + 1,
            retry_in_seconds=countdown,
        )

        if self.request.retries >= self.max_retries:
            # All retries exhausted.
            # This is a notification failure — the transfer already succeeded.
            # Log the failure at ERROR level for alerting (e.g., PagerDuty alert
            # when error rate > threshold), but do NOT mark the transfer as failed.
            log.error(
                "notification_task_permanently_failed",
                error=str(exc),
                transaction_id=transaction_id,
                user_id=user_id,
                notification_type=notification_type,
            )
            # Do NOT raise — let the task complete as FAILURE in Celery's result
            # backend so Flower shows it, but don't propagate since there's nothing
            # the caller can do about a notification failure.
            return {
                "status": "failed",
                "notification_type": notification_type,
                "user_id": user_id,
                "transaction_id": transaction_id,
                "error": str(exc),
            }

        raise self.retry(exc=exc, countdown=countdown)

    finally:
        structlog.contextvars.clear_contextvars()


# =============================================================================
# Async implementation
# =============================================================================

async def _execute_notification(
    transaction_id: str,
    notification_type: str,
    user_id: str,
    amount: str,
    reference_id: str,
    sender_email: str,
    receiver_email: str,
    log,
) -> dict:
    """
    Simulate calling an external notification service.

    Resolves recipient and message content based on notification_type,
    then simulates the network round-trip to the notification API.

    In production, replace asyncio.sleep(1) with an httpx.AsyncClient call:
      async with httpx.AsyncClient() as client:
          response = await client.post(
              "https://api.sendgrid.com/v3/mail/send",
              headers={"Authorization": f"Bearer {settings.SENDGRID_API_KEY}"},
              json={"to": [{"email": recipient_email}], "subject": subject, ...},
          )
          response.raise_for_status()
    """
    if notification_type == "transfer_sent":
        # Sender notification: "You sent $X to Bob"
        recipient_email = sender_email or user_id
        recipient_label = "sender"
        message = (
            f"You sent ${amount} to {receiver_email or 'recipient'} "
            f"(ref: {reference_id[:8]}...)"
        )
    elif notification_type == "transfer_received":
        # Receiver notification: "You received $X from Alice"
        recipient_email = receiver_email or user_id
        recipient_label = "receiver"
        message = (
            f"You received ${amount} from {sender_email or 'sender'} "
            f"(ref: {reference_id[:8]}...)"
        )
    else:
        recipient_email = user_id
        recipient_label = "user"
        message = f"Transfer event ({notification_type}) — ${amount} (ref: {reference_id[:8]}...)"

    log.info(
        "notification_sending",
        recipient=recipient_email,
        recipient_role=recipient_label,
        channel="email",  # in production: email | sms | push
        message_preview=message,
    )

    # Simulate external notification API latency.
    # SendGrid typical response: 150–500ms.
    # asyncio.sleep is non-blocking — correct async pattern.
    await asyncio.sleep(1)

    log.info(
        "notification_delivered",
        recipient=recipient_email,
        channel="email",
        simulated=True,
    )

    return {
        "status": "delivered",
        "notification_type": notification_type,
        "recipient": recipient_email,
        "transaction_id": transaction_id,
        "reference_id": reference_id,
        "message": message,
        "simulated": True,
    }
