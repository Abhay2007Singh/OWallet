"""
app/workers/celery_app.py

Celery application instance and global configuration.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: How Celery works internally
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Celery is a Producer-Consumer system with three components:

  1. PRODUCER (FastAPI API process)
     wallet_service calls simulate_bank_webhook.delay(tx_id):
       - Serializes the task message to JSON
       - Pushes message to the Redis broker queue (Redis LIST: RPUSH)
       - Returns an AsyncResult immediately — non-blocking
       - HTTP response is sent BEFORE the task executes

  2. BROKER (Redis database 1)
     - Stores task messages as items in a Redis LIST
     - Workers dequeue via BLPOP (blocking list pop)
     - Tasks survive API restarts (messages persist in Redis until consumed)
     - Redis AOF persistence (enabled in docker-compose.yml) survives Redis restarts
     - Database 1 is separate from db 0 (app cache) — no key namespace collision

  3. WORKER (celery_worker container)
     - Runs BLPOP continuously on the queue — blocks until a message arrives
     - When a message arrives: deserialize → find task function → call it
     - Uses prefork pool (default): one OS process per concurrency slot
     - With task_acks_late=True: ACKs the message ONLY after task success
     - Result backend stores return value and final state in Redis db 2

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: task_acks_late — at-least-once delivery guarantee
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Default behavior (task_acks_late=False):
  Worker receives message → immediately ACKs → then runs task
  If worker crashes AFTER ACK but DURING task: task is LOST forever.
  No retry. No recovery. The deposit stays PENDING indefinitely.

With task_acks_late=True:
  Worker receives message → runs task → ACKs ONLY on success
  If worker crashes DURING task: message is NOT ACKed
  Redis re-delivers the message to the next available worker
  Task runs again from the beginning

REQUIREMENT: Tasks must be IDEMPOTENT (safe to run N times, same result).
PyWallet tasks satisfy this:
  simulate_bank_webhook: checks status=PENDING before updating.
    If already COMPLETED → early return. Running twice = running once.
  cleanup_stale_transactions: UPDATE WHERE status=PENDING.
    Second run finds 0 PENDING rows from previous run → 0 updates.
  send_transfer_notification: logging + simulated HTTP call.
    Running twice sends two notifications (acceptable; dedup is Phase 7+).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: task_reject_on_worker_lost — crash recovery
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Scenario: simulate_bank_webhook is running. Worker process OOM-killed.

Without task_reject_on_worker_lost:
  Celery marks the task FAILURE (because the ACK never arrived)
  BUT does NOT re-queue the message.
  Result: deposit stays PENDING until cleanup_stale_transactions fires (1h later).
  User experience: "my deposit is pending for over an hour" → support ticket.

With task_reject_on_worker_lost=True:
  Celery rejects (nacks) the message → Redis re-queues it
  Another worker picks it up and processes it normally
  The idempotency guard prevents double-crediting if the original
  worker had already completed the DB write before dying
  Result: deposit confirmed within seconds of the crash.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: worker_max_tasks_per_child — memory leak prevention
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Celery's prefork pool: each worker process is a separate OS process.
Python processes accumulate memory over time even without explicit leaks:
  - Reference cycles that GC hasn't collected yet
  - Fragmented heap that Python won't return to the OS
  - Leaked file descriptors from SQLAlchemy connections
  - Growing in-memory caches in imported libraries

After 1000 tasks, the worker process is gracefully replaced by a fresh one:
  - Current task finishes (graceful shutdown)
  - Process exits cleanly
  - New process spawns, imports all modules fresh
  - Begins processing tasks with a clean memory footprint

Why 1000?
  - Not too low (e.g., 10): process restart overhead is significant
    (importing SQLAlchemy + all models takes ~500ms per new process)
  - Not too high (e.g., 100,000): memory bloat grows unchecked for hours
  - 1000 is the standard recommendation for API + DB worker processes

For PyWallet at current scale: this setting has minimal observable effect.
In a production fintech handling 10,000 tasks/minute per worker: critical.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: Celery Beat — distributed cron replacement
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Celery Beat is a lightweight scheduler process (celery beat command).
It sits beside the workers and periodically enqueues tasks into the broker.

Beat vs. OS cron:
  cron → runs shell commands → no retry, no result tracking, no monitoring
  Beat → enqueues Celery tasks → same retry/observability as all other tasks

Beat is a SINGLE process — do NOT run multiple Beat instances simultaneously
or tasks will be enqueued multiple times. In Kubernetes: a Deployment with
replicas=1 and a disruption budget of 0. We run it as a separate container.

PersistentScheduler: stores last-run timestamps in a local file
(/tmp/celerybeat-schedule). Survives restarts — knows which tasks it already
ran and won't double-fire if the container restarts during the minute.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: Redis as message broker
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Redis is simple, fast, and already in the stack. Trade-offs vs RabbitMQ:

  Redis (our choice):
    ✓ Already deployed (app cache is Redis) — no additional infra
    ✓ Sub-millisecond enqueue/dequeue
    ✓ Handles 10k+ tasks/second without breaking a sweat
    ✗ No native dead-letter queue (Celery adds one on top)
    ✗ AOF persistence is not as durable as RabbitMQ's disk journaling
    ✗ No message TTL or priority queues at the AMQP-protocol level

  RabbitMQ (production fintech, $1B/day):
    ✓ AMQP-level acknowledgment semantics
    ✓ True dead-letter queues with per-message TTL
    ✓ Priority queues (high-value deposits processed first)
    ✓ Durable queues survive broker restart without AOF
    ✗ Separate infrastructure to operate and monitor

For PyWallet at this phase: Redis is the correct choice.
"""

from celery import Celery
from celery.schedules import crontab

from app.core.config import settings

# ─────────────────────────────────────────────────────────────────────────────
# Initialize structured logging when this module is imported.
# This runs in:
#   - The Celery worker process (on import during startup)
#   - The Celery Beat process (on import during startup)
#   - The FastAPI process (indirectly, when wallet_service imports celery tasks)
# ─────────────────────────────────────────────────────────────────────────────
from app.core.logging_config import configure_logging

configure_logging()

# ─────────────────────────────────────────────────────────────────────────────
# Celery application instance
# "pywallet" = the application name — appears in Flower and Celery logs
# ─────────────────────────────────────────────────────────────────────────────
celery_app = Celery(
    "pywallet",
    broker=settings.CELERY_BROKER_URL,        # redis://redis:6379/1
    backend=settings.CELERY_RESULT_BACKEND,   # redis://redis:6379/2
    include=[
        "app.workers.deposit_tasks",       # Phase 3: deposit bank confirmation
        "app.workers.notification_tasks",  # Phase 4 & 6: transfer notifications
        "app.workers.webhook_tasks",       # Phase 6: bank webhook simulation
        "app.workers.cleanup_tasks",       # Phase 6: scheduled DB maintenance
    ],
)

# =============================================================================
# Celery Configuration
# =============================================================================
celery_app.conf.update(

    # ─── Serialization ────────────────────────────────────────────────────────
    # NEVER use pickle (Celery's default).
    # Pickle deserializes arbitrary Python objects — a compromised broker
    # can inject malicious pickle payloads → remote code execution.
    # JSON is safe: only primitives, strings, and numbers. Perfectly sufficient
    # for passing UUIDs, emails, and amounts between tasks.
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",

    # ─── Timezone ─────────────────────────────────────────────────────────────
    # All financial timestamps must be UTC. DST changes can make timestamps
    # ambiguous — "1:30 AM" on DST transition night refers to two different
    # moments. UTC has no DST. No ambiguity. No timezone conversion errors.
    timezone="UTC",
    enable_utc=True,

    # ─── At-least-once delivery ───────────────────────────────────────────────
    # ACK the task message ONLY after the task function returns successfully.
    # If the worker crashes mid-execution, the unACKed message stays in Redis
    # and is re-delivered to another worker.
    # REQUIRES: all tasks must be idempotent (re-running = same result).
    task_acks_late=True,

    # One task at a time per worker process.
    # Prevents a slow task from holding up the worker's single concurrency slot
    # while multiple tasks are prefetched but not yet executing.
    worker_prefetch_multiplier=1,

    # Recycle worker processes after 1000 tasks to prevent memory bloat.
    # Graceful recycling: current task finishes before the process exits.
    worker_max_tasks_per_child=1000,

    # ─── Worker crash recovery ────────────────────────────────────────────────
    # Re-queue tasks whose worker process was killed (OOM, SIGKILL, container crash).
    # Without this: task is marked FAILURE and never retried automatically.
    # With this: task message goes back to the queue, picked up by another worker.
    task_reject_on_worker_lost=True,

    # ─── Timeouts ─────────────────────────────────────────────────────────────
    # Prevent runaway tasks (infinite loops, hung DB connections) from blocking workers.
    # soft limit → raises SoftTimeLimitExceeded (catchable, allows cleanup)
    # hard limit → sends SIGKILL after this many seconds (cannot be caught)
    task_soft_time_limit=120,   # 2 minutes: task can handle SoftTimeLimitExceeded
    task_time_limit=180,        # 3 minutes: unconditional kill

    # ─── Result backend ───────────────────────────────────────────────────────
    # Store task results (return values + state) for 24 hours.
    # After 24h, Redis automatically deletes result keys.
    # Without this, results accumulate indefinitely and Redis fills up.
    result_expires=86400,

    # ─── Broker connection ────────────────────────────────────────────────────
    # Retry broker connection attempts at startup instead of failing immediately.
    # Redis may not be ready when the worker container starts — especially
    # in docker-compose when all containers start simultaneously.
    broker_connection_retry_on_startup=True,
    broker_connection_max_retries=10,

    # ─── Beat schedule — periodic maintenance tasks ───────────────────────────
    #
    # Celery Beat enqueues these tasks on the defined schedule.
    # The actual execution happens in the celery_worker container.
    # Beat must run as a SINGLE process — never scale it horizontally
    # or tasks will be enqueued multiple times.
    beat_schedule={

        # ── Stale PENDING transaction cleanup — runs every hour ──────────────
        #
        # WHY this exists:
        # simulate_bank_webhook runs with max_retries=3 and exponential backoff.
        # Retry schedule: immediately, +30s, +60s, +120s → exhausted ~210s after start.
        # If all retries fail (DB down for extended period), the transaction
        # stays in PENDING state forever. The user's history shows "pending"
        # indefinitely — confusing and misleading.
        #
        # This task rescues those orphaned transactions by marking them FAILED
        # after 1 hour, allowing the user to understand what happened.
        #
        # The 1-hour threshold:
        # 3 retries × maximum backoff (~120s each) = ~360s total retry window.
        # A 1-hour threshold gives 10× that window of buffer — we only mark
        # FAILED transactions that are genuinely stuck, not ones still retrying.
        "cleanup-stale-transactions-hourly": {
            "task": "app.workers.cleanup_tasks.cleanup_stale_transactions",
            "schedule": crontab(minute=0),  # fires at: 01:00, 02:00, 03:00, ...
        },

        # ── Expired idempotency key cleanup — runs every 6 hours ─────────────
        #
        # WHY this exists:
        # Every deposit and transfer creates a row in idempotency_keys.
        # Keys expire after 24 hours (expires_at TTL, enforced by Phase 5 service).
        # Expired keys can never be used for replay — they are dead weight.
        #
        # Without cleanup:
        # 1,000 users × 10 transactions/day × 365 days = 3.65 million rows/year
        # The lookup query (WHERE expires_at > NOW()) scans an ever-growing table.
        # Storage costs grow unbounded.
        #
        # With cleanup every 6 hours:
        # Keys are deleted within 30 hours of expiry (24h TTL + up to 6h cleanup lag).
        # The ix_idempotency_keys_expires_at index makes the DELETE an index scan.
        "cleanup-expired-idempotency-keys-6h": {
            "task": "app.workers.cleanup_tasks.cleanup_expired_idempotency_keys",
            "schedule": crontab(minute=0, hour="*/6"),  # 00:00, 06:00, 12:00, 18:00
        },
    },
)
