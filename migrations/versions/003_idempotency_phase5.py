"""Phase 5: Rebuild idempotency_keys table for production-grade idempotency

This migration drops the old idempotency_keys table (which had incompatible
column names from the Phase 1 initial schema) and recreates it with the
Phase 5 schema:

  NEW columns:
    - endpoint (String 500) — was `request_path`, renamed for clarity
    - request_hash (String 64) — SHA256 of user_id+endpoint+body
    - response_body (JSONB) — stores response as structured JSON (replaces Text)
    - expires_at (DateTime with TZ) — key TTL, checked on every lookup

  REMOVED columns:
    - request_path → replaced by endpoint
    - cached_response (Text) → replaced by response_body (JSONB)

  CHANGED indexes:
    - Old: UNIQUE(key, user_id) → New: UNIQUE(key, user_id, endpoint)
    - Added: ix_idempotency_keys_expires_at (for efficient cleanup queries)

  UNCHANGED:
    - key, user_id, status, http_status_code, transaction_id columns
    - FK to users.id (CASCADE), FK to transactions.id (SET NULL)

WHY drop-and-recreate instead of ALTER TABLE?
  The column set changed significantly (rename + type change + new columns).
  ALTER TABLE would require:
    1. ADD COLUMN endpoint NOT NULL → fails on existing rows (no default)
    2. UPDATE ... SET endpoint = request_path
    3. DROP COLUMN request_path
    4. ALTER COLUMN cached_response TYPE JSONB USING cached_response::jsonb
    5. ADD COLUMN request_hash, expires_at

  In a development environment with no production data in this table,
  drop-and-recreate is cleaner, less error-prone, and easier to reason about.
  In production, use the step-by-step ALTER approach with a data migration.

Revision ID: 003
Revises: 002
Create Date: 2026-05-22
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# ─────────────────────────────────────────────────────────────────────────────
revision = "003"
down_revision = "002"
branch_labels = None
depends_on = None
# ─────────────────────────────────────────────────────────────────────────────


def upgrade() -> None:
    """Recreate idempotency_keys with the Phase 5 schema."""

    # Drop old table — safe in dev (no production data).
    # In production: use ALTER TABLE steps with a data migration script.
    op.drop_table("idempotency_keys")

    # Create Phase 5 idempotency_keys table
    op.create_table(
        "idempotency_keys",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        # ─── Client-supplied idempotency key ───────────────────────────────
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # ─── Endpoint scoping ─────────────────────────────────────────────
        # Was `request_path` in Phase 1. Renamed to `endpoint` for clarity.
        # Examples: /api/v1/wallet/transfer, /api/v1/wallet/deposit
        sa.Column("endpoint", sa.String(500), nullable=False),
        # ─── Payload fingerprint ──────────────────────────────────────────
        # SHA256(user_id + endpoint + body_bytes) as 64-char hex.
        # Detects reuse of the same key with a different payload.
        sa.Column("request_hash", sa.String(64), nullable=False),
        # ─── Lifecycle state ──────────────────────────────────────────────
        sa.Column(
            "status",
            postgresql.ENUM("processing", "completed", "failed", name="idempotencystatus", create_type=False),
            nullable=False,
            server_default="completed",
        ),
        # ─── Stored response — JSONB for structured JSON ──────────────────
        # JSONB stores the response payload as binary JSON in PostgreSQL:
        #   - Field-level extraction: response_body->>'transfer_reference_id'
        #   - GIN indexing for analytics queries (add if needed)
        #   - Storage is more compact than TEXT for structured data
        #   - PostgreSQL validates JSON structure on every INSERT/UPDATE
        sa.Column(
            "response_body",
            postgresql.JSONB,
            nullable=True,
            comment="Original response payload as JSONB; returned verbatim on replay.",
        ),
        sa.Column("http_status_code", sa.Integer(), nullable=True),
        # ─── Optional link to the transaction this op created ─────────────
        sa.Column(
            "transaction_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("transactions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        # ─── Key TTL ──────────────────────────────────────────────────────
        # Keys expire 24 hours after creation.
        # Queried as: WHERE expires_at > NOW() — expired keys are ignored.
        # A cleanup task (Phase 6) deletes rows WHERE expires_at < NOW().
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
            comment="After this timestamp the key is no longer used for replay.",
        ),
        # ─── Audit timestamps ─────────────────────────────────────────────
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    # ─── Indexes ──────────────────────────────────────────────────────────────

    # Primary lookup index — UNIQUE on (key, user_id, endpoint).
    # This is the query executed on every inbound payment request:
    #   SELECT * FROM idempotency_keys
    #   WHERE key = ? AND user_id = ? AND endpoint = ? AND expires_at > NOW()
    # UNIQUE ensures: one stored result per (key, user_id, endpoint) triple.
    # If two concurrent requests somehow both attempt to INSERT for the same
    # triple, PostgreSQL raises a unique violation — no duplicate stored.
    op.create_index(
        "ix_idempotency_keys_lookup",
        "idempotency_keys",
        ["key", "user_id", "endpoint"],
        unique=True,
    )

    # Cleanup index — used by the Phase 6 Celery task:
    #   DELETE FROM idempotency_keys WHERE expires_at < NOW()
    # Without this index, cleanup is a full table scan.
    op.create_index(
        "ix_idempotency_keys_expires_at",
        "idempotency_keys",
        ["expires_at"],
    )

    # User lookup — "show all idempotency keys for user X" (admin debugging)
    op.create_index(
        "ix_idempotency_keys_user_id",
        "idempotency_keys",
        ["user_id"],
    )


def downgrade() -> None:
    """Restore the Phase 1 idempotency_keys schema."""
    op.drop_table("idempotency_keys")

    op.create_table(
        "idempotency_keys",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("key", sa.String(255), nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("request_path", sa.String(500), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM("processing", "completed", "failed", name="idempotencystatus", create_type=False),
            nullable=False,
            server_default="processing",
        ),
        sa.Column("cached_response", sa.Text(), nullable=True),
        sa.Column("http_status_code", sa.Integer(), nullable=True),
        sa.Column(
            "transaction_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("transactions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_idempotency_keys_key_user",
        "idempotency_keys",
        ["key", "user_id"],
        unique=True,
    )
    op.create_index("ix_idempotency_keys_user_id", "idempotency_keys", ["user_id"])
    op.create_index("ix_idempotency_keys_created_at", "idempotency_keys", ["created_at"])
