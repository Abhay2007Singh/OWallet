"""
app/models/idempotency_key.py

IdempotencyKey model — prevents duplicate financial operations.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: Why idempotency is non-negotiable in payment systems
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

The Distributed Systems Problem:
In a distributed system, a request can be received, processed, and succeed
on the server — but the client never sees the success response because:
  - Network dropped AFTER the server committed but BEFORE sending the response
  - Mobile app killed mid-flight (user switched apps, low battery)
  - Load balancer timeout before upstream server replied
  - Client-side timeout fired early (race between request + timeout)

The client's only safe choice is to retry. But a naive retry causes:
  - Transfer of $500: server processes it, client retries → $1,000 moved
  - Duplicate order: server creates two orders from one user action
  - Double subscription charge: user charged twice for one month

Idempotency solves this: a retry with the SAME Idempotency-Key returns
the original response without re-executing the operation. The client
cannot tell the difference between "first execution" and "replay" —
both return identical HTTP responses.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: Request hashing — payload mismatch detection
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

An idempotency key is NOT a general-purpose request deduplication key.
It is scoped to EXACTLY ONE logical operation with EXACTLY ONE payload.

If a client reuses key "abc123" with a different amount ($100 → $200),
that is a client bug. We detect it via request_hash:
  hash = SHA256(user_id + endpoint + request_body_bytes)

On the first request: hash is stored alongside the key.
On retry: computed hash is compared to stored hash.
  - Match → safe to replay cached response.
  - Mismatch → 422 Unprocessable Entity: "Payload mismatch."

This prevents a class of bugs where a client reuses old keys
(e.g., from a previous session or a misconfigured retry loop).

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: Scoping — per user, per endpoint
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Scope: (key, user_id, endpoint) must be globally unique.
This means:
  - User A's key "abc123" and User B's key "abc123" don't conflict.
    Two different users can independently use the same UUID.
  - Key "abc123" on /wallet/transfer and "abc123" on /wallet/deposit
    are treated as independent. Cross-endpoint reuse is blocked at the
    request_hash level (endpoint is part of the hash input).

Security: a malicious user cannot replay ANOTHER user's idempotent
response. The DB lookup always filters by user_id from the JWT.
The client cannot inject a different user_id — it comes from the token.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: Key expiration
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Idempotency keys expire after 24 hours (KEY_TTL_SECONDS=86400).
After expiry:
  - The key is treated as if it never existed.
  - A new request with the same key will be processed fresh.
  - This matches Stripe's behavior: keys are valid for 24 hours.

Why expire at all? Without expiry:
  - The idempotency_keys table grows forever.
  - "Idempotent" replay of a 6-month-old transfer would be confusing.
  - In practice, retries happen within seconds/minutes, not days.

Cleanup: a Celery periodic task (Phase 6) runs DELETE WHERE expires_at < NOW().
"""

import uuid
from datetime import datetime
from enum import Enum as PyEnum

from sqlalchemy import DateTime, Enum, ForeignKey, Index, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin


class IdempotencyStatus(str, PyEnum):
    PROCESSING = "processing"   # request received, not yet complete (Redis lock held)
    COMPLETED = "completed"     # operation finished; response_body is valid for replay
    FAILED = "failed"           # operation failed; NOT stored (clients must retry freely)


class IdempotencyKey(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """
    SQLAlchemy ORM model for the 'idempotency_keys' table.

    Each row represents one successfully completed idempotent operation.
    Failed operations are NEVER stored here — clients must be able to retry them.

    The triple (key, user_id, endpoint) uniquely identifies one logical operation.
    """

    __tablename__ = "idempotency_keys"

    # ─────────────────────────────────────────────────────────────────────────
    # The client-supplied idempotency key — a UUID v4 the client generates.
    # Scoped per user: two different users may use the same key string without conflict.
    # ─────────────────────────────────────────────────────────────────────────
    key: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="UUID v4 string supplied by the client in Idempotency-Key header",
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    # ─────────────────────────────────────────────────────────────────────────
    # The HTTP endpoint this key is scoped to.
    # Prevents a key used for /wallet/transfer from replaying on /wallet/deposit.
    # ─────────────────────────────────────────────────────────────────────────
    endpoint: Mapped[str] = mapped_column(
        String(500),
        nullable=False,
        comment="e.g. /api/v1/wallet/transfer — scopes key to one operation type",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # SHA256 hash of (user_id + endpoint + request body bytes).
    # Used to detect payload mismatch on retry.
    # 64 characters = exact length of a SHA256 hex digest.
    # ─────────────────────────────────────────────────────────────────────────
    request_hash: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment=(
            "SHA256(user_id + endpoint + request_body). "
            "Used to reject retries with a different payload."
        ),
    )

    status: Mapped[IdempotencyStatus] = mapped_column(
        Enum(IdempotencyStatus, name="idempotencystatus", values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=IdempotencyStatus.COMPLETED,
        comment="Always COMPLETED for stored records (FAILED ops are never stored).",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # JSONB response body — the exact response data returned on the first request.
    # Stored as native JSON in PostgreSQL, enabling:
    #   - O(1) field-level extraction for debugging: response_body->>'transfer_reference_id'
    #   - Type safety: PostgreSQL validates JSON structure on write
    #   - Better compression than TEXT for structured data
    #
    # Why JSONB over TEXT?
    # TEXT stores the raw string (no validation, no indexing, no field extraction).
    # JSONB decomposes the JSON into binary form:
    #   - Queries on JSON fields without full scan
    #   - Smaller storage (duplicate keys removed, whitespace stripped)
    #   - Support for GIN indexes on JSON paths (useful for analytics)
    # ─────────────────────────────────────────────────────────────────────────
    response_body: Mapped[dict | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="The original response payload — returned verbatim on idempotent replay.",
    )

    http_status_code: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="HTTP status code of the stored response (200, 201, etc.)",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Key TTL — once past this timestamp, treat the key as non-existent.
    # Cleanup: a scheduled Celery task (Phase 6) deletes expired rows.
    # ─────────────────────────────────────────────────────────────────────────
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        comment="After this timestamp, the key is treated as expired and not replayed.",
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Optional link to the transaction produced by this operation.
    # Useful for forensic queries: "what transaction did idempotency key X create?"
    # ─────────────────────────────────────────────────────────────────────────
    transaction_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("transactions.id", ondelete="SET NULL"),
        nullable=True,
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Indexes
    # ─────────────────────────────────────────────────────────────────────────
    __table_args__ = (
        # Primary lookup: "has this user already processed this key on this endpoint?"
        # UNIQUE ensures one row per (key, user_id, endpoint) triple.
        Index("ix_idempotency_keys_lookup", "key", "user_id", "endpoint", unique=True),
        # Secondary lookup: clean up expired keys efficiently
        Index("ix_idempotency_keys_expires_at", "expires_at"),
        Index("ix_idempotency_keys_user_id", "user_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<IdempotencyKey key={self.key!r} "
            f"user_id={self.user_id} "
            f"endpoint={self.endpoint!r} "
            f"status={self.status}>"
        )
