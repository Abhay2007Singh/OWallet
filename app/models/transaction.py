"""
app/models/transaction.py

Transaction model — the immutable financial ledger of PyWallet.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: Double-Entry Bookkeeping
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Double-entry bookkeeping is a 700-year-old accounting principle:
every financial event creates exactly TWO entries:
  1. A DEBIT: money leaves one account
  2. A CREDIT: money enters another account

The invariant: SUM(all DEBITs) == SUM(all CREDITs) at all times.
This is called "the ledger balances." If it doesn't balance, money
was either created from nothing or destroyed — both are bugs.

In PyWallet, a transfer of $100 from Alice to Bob creates:
  Row 1: wallet_id=ALICE, type=DEBIT,  amount=100, balance_before=500, balance_after=400
  Row 2: wallet_id=BOB,   type=CREDIT, amount=100, balance_before=200, balance_after=300

Both rows share the same transfer_reference_id — a UUID that groups
the two legs of one transfer. You can always find "the other side" by
querying WHERE transfer_reference_id = X AND type != current_type.

Why are both rows needed?
  - Alice's wallet needs a record: "I sent $100"
  - Bob's wallet needs a record: "I received $100"
  - Each appears in their own transaction history independently
  - The shared reference_id proves they are the same event

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: Why financial ledgers must be immutable
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

A transaction row, once committed, must never be:
  - DELETED: removes the audit trail; regulators will fine you
  - UPDATEd (amount): creates a lie in the historical record

What if a transfer is wrong? Create a REVERSAL:
  - New CREDIT row for Alice (returns $100)
  - New DEBIT row for Bob (removes $100)
  - Both rows reference the original transfer_reference_id
  - The history shows: transfer happened, then it was reversed

This append-only model means the ledger is forensically sound:
  "Find every transaction ever involving wallet X" → SELECT all rows
  "Reconstruct the balance at any point in time" → SUM up to that timestamp
  "Prove the ledger balances" → SUM(DEBIT) == SUM(CREDIT)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: ACID transactions and why they matter here
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ACID = Atomicity, Consistency, Isolation, Durability

Atomicity: The DEBIT and CREDIT rows are inserted in one transaction.
  Either BOTH succeed or NEITHER does. There is no universe where
  Alice is debited but Bob is not credited. PostgreSQL guarantees this.

Consistency: The database moves from one valid state to another.
  Before: Alice=$500, Bob=$200, total=$700
  After:  Alice=$400, Bob=$300, total=$700
  The total money in the system is unchanged. Consistency guaranteed.

Isolation: While the transfer is in progress, other transactions
  cannot see the intermediate state (Alice=$400, Bob=$200 — $100 missing!).
  PostgreSQL's READ COMMITTED isolation level ensures this.

Durability: Once committed, the data survives power loss, OS crash,
  or hardware failure. PostgreSQL writes to WAL (Write-Ahead Log)
  before acknowledging the commit. The WAL persists to disk.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: Failure scenarios and why atomicity saves us
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Scenario 1: Server crash after DEBIT, before CREDIT
  Without atomicity: Alice loses $100, Bob never gets it. Money destroyed.
  With atomicity: The transaction is incomplete → PostgreSQL rolls back.
  Alice's balance is unchanged. Bob's balance is unchanged. Retry safely.

Scenario 2: Disk full during commit
  PostgreSQL: commit fails → automatic rollback. No partial state.
  The WAL protects us: data is either fully written or not at all.

Scenario 3: DB connection lost mid-transaction
  asyncpg/psycopg: connection exception → SQLAlchemy rollback.
  The exception propagates → get_db dependency rollback (idempotent).
  FastAPI returns 503. No money was moved.

Scenario 4: Application bug raises exception after DEBIT update
  async with db.begin(): catches any exception → automatic rollback.
  The DEBIT update is never committed. Balance unchanged.

The pattern async with db.begin(): is the key: any exception,
anywhere inside the block, triggers rollback. No money is lost.
"""

import uuid
from enum import Enum as PyEnum

from sqlalchemy import Enum, ForeignKey, Index, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin


class TransactionType(str, PyEnum):
    """
    Classifies the direction and nature of a monetary event.

    DEPOSIT  → external money enters a wallet (bank top-up, card load)
    WITHDRAW → money leaves a wallet to an external destination
    DEBIT    → money leaves a wallet in a P2P transfer (sender's view)
    CREDIT   → money enters a wallet in a P2P transfer (receiver's view)
    TRANSFER → legacy type, kept for backward compatibility

    Why DEBIT and CREDIT instead of a single TRANSFER type?
    A single TRANSFER type on both rows creates ambiguity:
      Was this wallet the sender or receiver?
    DEBIT and CREDIT answer that instantly without joining another table.
    This is standard double-entry bookkeeping terminology.
    """
    DEPOSIT = "deposit"
    WITHDRAW = "withdraw"
    TRANSFER = "transfer"   # kept for backward compatibility
    DEBIT = "debit"         # sender's leg in a P2P transfer
    CREDIT = "credit"       # receiver's leg in a P2P transfer


class TransactionStatus(str, PyEnum):
    """
    Lifecycle state of a transaction record.

    PENDING   → created, awaiting external confirmation (deposits only)
    COMPLETED → money has moved; final state for P2P transfers
    FAILED    → processing failed; balance unchanged
    REVERSED  → a completed transaction that was subsequently reversed

    P2P transfers (DEBIT/CREDIT rows) are COMPLETED immediately —
    the transfer is synchronous (both balances updated in one DB commit).
    Only deposits are PENDING initially (async bank confirmation via Celery).
    """
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    REVERSED = "reversed"


class Transaction(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """
    SQLAlchemy ORM model for the 'transactions' table.

    THE GOLDEN RULE: once a row is committed, its monetary fields
    (amount, balance_before, balance_after, wallet_id, transaction_type)
    are NEVER updated by application code. They are the immutable audit log.

    Only `status` may change (PENDING → COMPLETED or FAILED).
    Everything else is write-once.
    """

    __tablename__ = "transactions"

    # -------------------------------------------------------------------------
    # Core wallet reference
    # -------------------------------------------------------------------------
    wallet_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("wallets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    counterpart_wallet_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("wallets.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment=(
            "For DEBIT/CREDIT pairs: the other wallet in the transfer. "
            "Sender's DEBIT row: counterpart = receiver wallet. "
            "Receiver's CREDIT row: counterpart = sender wallet."
        ),
    )

    # -------------------------------------------------------------------------
    # Amount — always POSITIVE regardless of direction.
    # Direction is encoded in transaction_type (DEBIT = out, CREDIT = in).
    # This makes SUM queries unambiguous: no sign confusion.
    # -------------------------------------------------------------------------
    amount: Mapped[float] = mapped_column(
        Numeric(precision=18, scale=8, asdecimal=True),
        nullable=False,
        comment="Always positive. Direction determined by transaction_type.",
    )

    # -------------------------------------------------------------------------
    # Balance snapshots — forensic accounting anchors.
    # These allow balance reconstruction without replaying all transactions,
    # and let support agents verify any disputed transaction in seconds.
    # -------------------------------------------------------------------------
    balance_before: Mapped[float] = mapped_column(
        Numeric(precision=18, scale=8, asdecimal=True),
        nullable=False,
        comment="Wallet balance immediately BEFORE this transaction was applied.",
    )

    balance_after: Mapped[float] = mapped_column(
        Numeric(precision=18, scale=8, asdecimal=True),
        nullable=False,
        comment="Wallet balance immediately AFTER this transaction was applied.",
    )

    transaction_type: Mapped[TransactionType] = mapped_column(
        Enum(TransactionType, name="transactiontype"),
        nullable=False,
    )

    status: Mapped[TransactionStatus] = mapped_column(
        Enum(TransactionStatus, name="transactionstatus"),
        nullable=False,
        default=TransactionStatus.PENDING,
        index=True,
    )

    # -------------------------------------------------------------------------
    # transfer_reference_id — the double-entry correlation field
    #
    # For every P2P transfer, BOTH the DEBIT row and the CREDIT row get the
    # SAME transfer_reference_id (a freshly generated UUID4 per transfer).
    #
    # Why NOT a foreign key?
    # A FK would require one row to exist before the other, creating a
    # chicken-and-egg insertion ordering problem. Since both rows are
    # inserted in the same transaction, neither has a committed ID yet.
    # A plain UUID is the correct choice: both rows carry the same value.
    #
    # How to find the other leg of a transfer:
    #   SELECT * FROM transactions
    #   WHERE transfer_reference_id = :ref_id AND id != :my_id
    #
    # Indexed because: "show me all transfers with reference X" is a
    # common query for dispute resolution and accounting.
    # -------------------------------------------------------------------------
    transfer_reference_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        nullable=True,
        index=True,
        comment=(
            "Shared UUID linking DEBIT and CREDIT rows of the same P2P transfer. "
            "NULL for deposits and withdrawals. "
            "NOT a foreign key — both rows carry the same UUID value."
        ),
    )

    # -------------------------------------------------------------------------
    # reference_id — for REVERSAL transactions only
    # Links a reversal row back to the original transaction it reverses.
    # Separate from transfer_reference_id — different semantic.
    # -------------------------------------------------------------------------
    reference_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("transactions.id", ondelete="SET NULL"),
        nullable=True,
        comment=(
            "For REVERSED transactions: points to the original transaction. "
            "NOT the same as transfer_reference_id."
        ),
    )

    description: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Human-readable memo shown on account statements.",
    )

    external_reference: Mapped[str | None] = mapped_column(
        String(255),
        nullable=True,
        index=True,
        comment="External gateway or bank reference for reconciliation.",
    )

    # -------------------------------------------------------------------------
    # Relationships
    # -------------------------------------------------------------------------
    wallet: Mapped["Wallet"] = relationship(  # type: ignore[name-defined]  # noqa: F821
        "Wallet",
        foreign_keys=[wallet_id],
        back_populates="transactions",
    )

    # -------------------------------------------------------------------------
    # Indexes — tuned for the most critical query patterns
    # -------------------------------------------------------------------------
    __table_args__ = (
        Index("ix_transactions_wallet_id", "wallet_id"),
        Index("ix_transactions_status", "status"),
        Index("ix_transactions_created_at", "created_at"),
        Index("ix_transactions_wallet_status", "wallet_id", "status"),
        # Critical for double-entry: find both legs of a transfer instantly
        Index("ix_transactions_transfer_reference_id", "transfer_reference_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<Transaction id={self.id} type={self.transaction_type} "
            f"status={self.status} amount={self.amount} ref={self.transfer_reference_id}>"
        )
