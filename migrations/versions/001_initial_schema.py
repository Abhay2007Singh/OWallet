"""Initial schema — Phases 1, 2, and 3

Creates all tables: users, wallets, transactions, idempotency_keys.
Includes all enums: userrole, walletcurrency, transactiontype, transactionstatus, idempotencystatus.

Revision ID: 001
Revises: None (this is the first migration)
Create Date: 2026-05-22
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# ─────────────────────────────────────────────────────────────────────────────
# Alembic revision metadata
# ─────────────────────────────────────────────────────────────────────────────
revision = "001"
down_revision = None      # no parent migration — this is the starting point
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the complete Phase 1-3 schema from scratch."""

    # ─────────────────────────────────────────────────────────────────────────
    # Step 1: Create all ENUM types first.
    # PostgreSQL enums must exist before any column references them.
    # We use create_type=False guard via checkfirst=True in create() to
    # avoid "type already exists" errors on repeated runs.
    # ─────────────────────────────────────────────────────────────────────────

    userrole_enum = postgresql.ENUM(
        "user", "admin",
        name="userrole",
        create_type=False,
    )
    userrole_enum.create(op.get_bind(), checkfirst=True)

    walletcurrency_enum = postgresql.ENUM(
        "USD", "EUR", "GBP", "NGN",
        name="walletcurrency",
        create_type=False,
    )
    walletcurrency_enum.create(op.get_bind(), checkfirst=True)

    # Phase 3 values: deposit, withdraw, transfer
    # Phase 4 will add: debit, credit (in migration 002)
    transactiontype_enum = postgresql.ENUM(
        "deposit", "withdraw", "transfer",
        name="transactiontype",
        create_type=False,
    )
    transactiontype_enum.create(op.get_bind(), checkfirst=True)

    transactionstatus_enum = postgresql.ENUM(
        "pending", "completed", "failed", "reversed",
        name="transactionstatus",
        create_type=False,
    )
    transactionstatus_enum.create(op.get_bind(), checkfirst=True)

    idempotencystatus_enum = postgresql.ENUM(
        "processing", "completed", "failed",
        name="idempotencystatus",
        create_type=False,
    )
    idempotencystatus_enum.create(op.get_bind(), checkfirst=True)

    # ─────────────────────────────────────────────────────────────────────────
    # Step 2: users table
    # ─────────────────────────────────────────────────────────────────────────
    op.create_table(
        "users",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("phone_number", sa.String(20), nullable=True),
        sa.Column("full_name", sa.String(255), nullable=False),
        sa.Column("hashed_password", sa.String(255), nullable=False),
        sa.Column(
            "role",
            postgresql.ENUM("user", "admin", name="userrole", create_type=False),
            nullable=False,
            server_default="user",
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("is_verified", sa.Boolean(), nullable=False, server_default=sa.text("false")),
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
        sa.UniqueConstraint("email", name="uq_users_email"),
    )
    op.create_index("ix_users_email", "users", ["email"])
    op.create_index("ix_users_phone_number", "users", ["phone_number"])

    # ─────────────────────────────────────────────────────────────────────────
    # Step 3: wallets table
    # ─────────────────────────────────────────────────────────────────────────
    op.create_table(
        "wallets",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "balance",
            sa.Numeric(precision=18, scale=8),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "currency",
            postgresql.ENUM("USD", "EUR", "GBP", "NGN", name="walletcurrency", create_type=False),
            nullable=False,
            server_default="USD",
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("wallet_tag", sa.String(50), nullable=True),
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
    op.create_index("ix_wallets_user_id", "wallets", ["user_id"])
    op.create_index("ix_wallets_user_id_currency", "wallets", ["user_id", "currency"])

    # ─────────────────────────────────────────────────────────────────────────
    # Step 4: transactions table
    # Note: counterpart_wallet_id and transfer_reference_id are NOT here yet.
    #       Migration 002 adds those (Phase 4).
    # ─────────────────────────────────────────────────────────────────────────
    op.create_table(
        "transactions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "wallet_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("wallets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("amount", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("balance_before", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column("balance_after", sa.Numeric(precision=18, scale=8), nullable=False),
        sa.Column(
            "transaction_type",
            postgresql.ENUM("deposit", "withdraw", "transfer", name="transactiontype", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "status",
            postgresql.ENUM("pending", "completed", "failed", "reversed", name="transactionstatus", create_type=False),
            nullable=False,
            server_default="pending",
        ),
        # Self-referential FK for REVERSAL transactions (points to original)
        sa.Column(
            "reference_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("transactions.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("external_reference", sa.String(255), nullable=True),
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
    op.create_index("ix_transactions_wallet_id", "transactions", ["wallet_id"])
    op.create_index("ix_transactions_status", "transactions", ["status"])
    op.create_index("ix_transactions_created_at", "transactions", ["created_at"])
    op.create_index("ix_transactions_wallet_status", "transactions", ["wallet_id", "status"])
    op.create_index("ix_transactions_external_reference", "transactions", ["external_reference"])

    # ─────────────────────────────────────────────────────────────────────────
    # Step 5: idempotency_keys table
    # ─────────────────────────────────────────────────────────────────────────
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
    op.create_index("ix_idempotency_keys_key_user", "idempotency_keys", ["key", "user_id"], unique=True)
    op.create_index("ix_idempotency_keys_user_id", "idempotency_keys", ["user_id"])
    op.create_index("ix_idempotency_keys_created_at", "idempotency_keys", ["created_at"])


def downgrade() -> None:
    """Drop all tables and enums in reverse dependency order."""
    op.drop_table("idempotency_keys")
    op.drop_table("transactions")
    op.drop_table("wallets")
    op.drop_table("users")

    # Drop enum types after tables are gone (tables hold references)
    op.execute("DROP TYPE IF EXISTS idempotencystatus")
    op.execute("DROP TYPE IF EXISTS transactionstatus")
    op.execute("DROP TYPE IF EXISTS transactiontype")
    op.execute("DROP TYPE IF EXISTS walletcurrency")
    op.execute("DROP TYPE IF EXISTS userrole")
