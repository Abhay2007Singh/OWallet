"""Phase 4: add P2P transfer fields to transactions

Changes:
  1. Add 'debit' and 'credit' values to the transactiontype PostgreSQL enum.
  2. Add transactions.counterpart_wallet_id — the other wallet in a P2P transfer.
  3. Add transactions.transfer_reference_id — shared UUID linking DEBIT and CREDIT rows.
  4. Add index on transfer_reference_id for fast "find both legs of a transfer" queries.

WHY each change:
  - DEBIT / CREDIT enum values: distinguish sender vs receiver role without a JOIN.
    A single "transfer" type on both rows creates ambiguity. DEBIT = money out
    (sender's row), CREDIT = money in (receiver's row). Standard double-entry terminology.
  - counterpart_wallet_id: allows the sender to know who received their DEBIT,
    and the receiver to know who sent their CREDIT — without leaking balances.
  - transfer_reference_id: the correlation ID. Both rows of a transfer share the
    same UUID. Dispute resolution: "show me transfer with ref X" → finds both legs.
    NOT a foreign key — both rows are inserted simultaneously, no ordering constraint.

PostgreSQL note on enum mutations:
  Adding values to a PostgreSQL ENUM requires ALTER TYPE ... ADD VALUE.
  Alembic's autogenerate does NOT detect enum value additions — this migration
  must be hand-written (which is exactly what this file is).

  Importantly: ALTER TYPE ADD VALUE cannot run inside a transaction in older
  PostgreSQL versions. As of PostgreSQL 12+, this restriction was lifted.
  We target PostgreSQL 15, so no special handling is needed.

  Downgrade: PostgreSQL does NOT support removing enum values.
  The downgrade() here drops the columns that USE the new values, but cannot
  remove 'debit' and 'credit' from the enum itself. The enum type remains
  with all five values after a downgrade. This is a PostgreSQL limitation.

Revision ID: 002
Revises: 001
Create Date: 2026-05-22
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# ─────────────────────────────────────────────────────────────────────────────
revision = "002"
down_revision = "001"
branch_labels = None
depends_on = None
# ─────────────────────────────────────────────────────────────────────────────


def upgrade() -> None:
    """Add Phase 4 transfer fields: enum values + two columns + one index."""

    # ─────────────────────────────────────────────────────────────────────────
    # Step 1: Extend the transactiontype enum with DEBIT and CREDIT values.
    #
    # ALTER TYPE ... ADD VALUE IF NOT EXISTS is idempotent — safe to run again
    # if this migration is applied to a DB that already has these values.
    #
    # Why 'debit' and 'credit' (lowercase)?
    # All existing enum values are lowercase ('deposit', 'withdraw', 'transfer').
    # The Python Enum maps: TransactionType.DEBIT = "debit". PostgreSQL stores
    # the literal string, so the case must match what the ORM sends.
    # ─────────────────────────────────────────────────────────────────────────
    op.execute("ALTER TYPE transactiontype ADD VALUE IF NOT EXISTS 'debit'")
    op.execute("ALTER TYPE transactiontype ADD VALUE IF NOT EXISTS 'credit'")

    # ─────────────────────────────────────────────────────────────────────────
    # Step 2: Add counterpart_wallet_id column.
    #
    # Nullable: existing deposit/withdrawal rows have no counterpart wallet.
    # Only DEBIT/CREDIT rows from P2P transfers will have this populated.
    #
    # FK to wallets.id with ondelete=SET NULL: if the counterpart wallet is
    # ever deleted (edge case), we don't cascade-delete the transaction.
    # The transaction remains as a historical record; the FK becomes NULL.
    # ─────────────────────────────────────────────────────────────────────────
    op.add_column(
        "transactions",
        sa.Column(
            "counterpart_wallet_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("wallets.id", ondelete="SET NULL"),
            nullable=True,
            comment=(
                "For DEBIT/CREDIT pairs: the other wallet in the transfer. "
                "NULL for deposits and withdrawals."
            ),
        ),
    )
    op.create_index(
        "ix_transactions_counterpart_wallet_id",
        "transactions",
        ["counterpart_wallet_id"],
    )

    # ─────────────────────────────────────────────────────────────────────────
    # Step 3: Add transfer_reference_id column.
    #
    # NOT a foreign key — it's a plain UUID that both DEBIT and CREDIT rows
    # carry with the same value. No FK means no insertion-ordering constraint:
    # both rows can be inserted in the same DB transaction with no chicken-and-egg
    # dependency. The shared value is how you find both legs of a transfer.
    #
    # Nullable: deposits and withdrawals don't have a transfer reference.
    # ─────────────────────────────────────────────────────────────────────────
    op.add_column(
        "transactions",
        sa.Column(
            "transfer_reference_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
            comment=(
                "Shared UUID linking the DEBIT and CREDIT rows of the same P2P transfer. "
                "NULL for deposits and withdrawals. NOT a foreign key."
            ),
        ),
    )
    op.create_index(
        "ix_transactions_transfer_reference_id",
        "transactions",
        ["transfer_reference_id"],
    )


def downgrade() -> None:
    """Remove Phase 4 transfer fields.

    NOTE: The 'debit' and 'credit' values added to the transactiontype enum
    CANNOT be removed in PostgreSQL. They will remain in the enum type.
    If you need a clean rollback, you must recreate the enum type (involves
    dropping and recreating the column) — out of scope for this migration.
    """
    op.drop_index("ix_transactions_transfer_reference_id", table_name="transactions")
    op.drop_column("transactions", "transfer_reference_id")

    op.drop_index("ix_transactions_counterpart_wallet_id", table_name="transactions")
    op.drop_column("transactions", "counterpart_wallet_id")

    # The 'debit' and 'credit' enum values intentionally remain —
    # PostgreSQL does not support ALTER TYPE DROP VALUE.
