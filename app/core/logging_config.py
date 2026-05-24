"""
app/core/logging_config.py

Structlog configuration for PyWallet.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
WHY STRUCTURED LOGGING IN A FINTECH ASYNC SYSTEM
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

A deposit in PyWallet produces log lines across multiple processes:
  - FastAPI API process  → "deposit received"
  - Celery worker 1      → "simulate_bank_webhook started"
  - Celery worker 2      → "notification sent"
  - Celery Beat          → "cleanup ran"

Unstructured logging:
  2026-05-22 10:30:01 INFO  process_deposit started tx=abc123 retry=0
  2026-05-22 10:30:04 INFO  simulate_bank_webhook completed tx=abc123

  Problem: extracting `tx=abc123` requires a regex. Log aggregators
  (Datadog, ELK, GCP Cloud Logging) cannot filter on the value directly.

Structured logging (structlog):
  {"event": "task_started", "task_name": "simulate_bank_webhook",
   "transaction_id": "abc123", "retry_count": 0, "timestamp": "...", "level": "info"}

  Benefit: Datadog query `transaction_id:abc123` instantly finds ALL logs
  across ALL processes related to this one deposit, in chronological order.
  No regex. No grep. Filter by any field, any value, from any service.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
PROCESSOR PIPELINE
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

structlog processes each log event through an ordered list of processors.
Each processor receives the event dict and returns an (optionally modified) dict.

  logger.info("task_started", transaction_id="abc", retry_count=0)

  →  Initial event dict:
     {"event": "task_started", "transaction_id": "abc", "retry_count": 0}

  →  merge_contextvars  → merges any per-request context (e.g., request_id)
  →  add_log_level      → adds "level": "info"
  →  add_logger_name    → adds "logger": "app.workers.webhook_tasks"
  →  TimeStamper        → adds "timestamp": "2026-05-22T10:30:01.234Z"
  →  StackInfoRenderer  → renders any exc_info stack traces as strings
  →  ExceptionPrettyPrinter → formats exception tracebacks cleanly
  →  ConsoleRenderer    → colored terminal output (development)
     OR JSONRenderer    → single-line JSON string (production/staging)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
CONTEXT VARIABLES (distributed tracing)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

structlog.contextvars allows binding fields that automatically appear
in ALL subsequent log calls within the same async task or thread:

  structlog.contextvars.bind_contextvars(
      request_id="req-abc",
      user_id="usr-123",
  )
  logger.info("doing_thing")  # → includes request_id and user_id automatically
  logger.warning("another_thing")  # → also includes them

  structlog.contextvars.clear_contextvars()  # clear at end of request/task

In PyWallet's Celery tasks:
  - Bind transaction_id at the start of every task
  - All log lines inside that task automatically include transaction_id
  - No need to pass it to every sub-function explicitly
"""

import logging
import sys

import structlog

from app.core.config import settings


def configure_logging() -> None:
    """
    Configure structlog as the application-wide logging system.

    This function is called once at process startup:
      - FastAPI: inside the lifespan context manager (before yield)
      - Celery workers: at module import time in celery_app.py

    Calling this multiple times is safe — structlog.configure() is idempotent.

    Output format:
      - development (APP_ENV=development): colored, human-readable console output
      - staging/production: single-line JSON per event for log aggregators
    """
    shared_processors: list = [
        # ── Merge per-request/per-task context vars into the event dict ─────────
        # Any fields bound via structlog.contextvars.bind_contextvars()
        # are automatically injected into every log event.
        structlog.contextvars.merge_contextvars,

        # ── Add standard metadata fields ─────────────────────────────────────
        structlog.stdlib.add_log_level,          # "level": "info"
        structlog.stdlib.add_logger_name,        # "logger": "app.workers.webhook_tasks"
        structlog.processors.TimeStamper(fmt="iso", utc=True),  # "timestamp": "2026-05-22T10:30:01Z"

        # ── Exception and stack trace rendering ──────────────────────────────
        # StackInfoRenderer: renders `stack_info=True` calls into the event dict
        structlog.processors.StackInfoRenderer(),
        # ExceptionRenderer: renders exc_info as a formatted traceback string field
        structlog.processors.ExceptionRenderer(),
    ]

    if settings.APP_ENV == "development":
        # Human-readable, color-coded output for local development.
        # Colors: event name in bold, log level color-coded (green=info, red=error).
        # Example output:
        #   2026-05-22T10:30:01Z [info     ] task_started [webhook_tasks] task=simulate_bank_webhook tx=abc123
        processors = shared_processors + [
            structlog.dev.ConsoleRenderer(
                colors=True,
                exception_formatter=structlog.dev.plain_traceback,
            ),
        ]
    else:
        # Machine-parseable JSON for staging/production.
        # Log aggregators ingest these as structured records:
        #   {"event": "task_started", "level": "info", "timestamp": "...",
        #    "logger": "app.workers.webhook_tasks", "task_name": "simulate_bank_webhook",
        #    "transaction_id": "abc123"}
        processors = shared_processors + [
            structlog.processors.JSONRenderer(),
        ]

    structlog.configure(
        processors=processors,
        # make_filtering_bound_logger: wraps a stdlib-style logger with structlog.
        # The filtering level (DEBUG or INFO) is applied before any processor runs —
        # DEBUG events are dropped immediately in production without hitting the pipeline.
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.DEBUG if settings.APP_DEBUG else logging.INFO
        ),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        # Cache the bound logger after first use — avoids re-processing the
        # configuration dict on every logger.info() call.
        cache_logger_on_first_use=True,
    )

    # ─────────────────────────────────────────────────────────────────────────
    # stdlib logging: configure root logger
    # ─────────────────────────────────────────────────────────────────────────
    # Libraries like SQLAlchemy, httpx, asyncpg, and celery use stdlib logging.
    # basicConfig() configures the root handler so their output goes to stdout
    # at the appropriate level, using the same format as structlog.
    # force=True: overrides any existing handler configuration (important when
    # called in Celery workers which may have already configured logging).
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.DEBUG if settings.APP_DEBUG else logging.INFO,
        force=True,
    )

    # Suppress noisy third-party loggers in all environments.
    # These generate many low-signal messages per request.
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
    logging.getLogger("asyncio").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("amqp").setLevel(logging.WARNING)
    logging.getLogger("celery.beat").setLevel(logging.INFO)
    logging.getLogger("kombu").setLevel(logging.WARNING)
