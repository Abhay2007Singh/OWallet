"""
app/models/wallet.py

Wallet model — each user can have one or more wallets (e.g., USD, EUR).

Critical: balance uses NUMERIC(18, 8), not FLOAT.
See app/models/base.py for UUID/timestamp mixin explanations.
"""

import uuid
from enum import Enum as PyEnum

from sqlalchemy import Boolean, Enum, ForeignKey, Index, Numeric, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin


class WalletCurrency(str, PyEnum):
    """
    Supported currencies. Stored as PostgreSQL ENUM — not a free-form string.
    This prevents inserting "usd" instead of "USD", "Usd", or a typo like "UDS".
    Changing this enum later requires an Alembic migration.
    """
    USD = "USD"
    EUR = "EUR"
    GBP = "GBP"
    NGN = "NGN"   # Nigerian Naira (example African fintech currency)


class Wallet(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """
    SQLAlchemy ORM model for the 'wallets' table.

    Each wallet belongs to exactly one user (user_id FK).
    A user may have multiple wallets in different currencies.
    """

    __tablename__ = "wallets"

    # -------------------------------------------------------------------------
    # Foreign key — links wallet to its owner
    # ondelete="CASCADE" → deleting the user row also deletes wallet rows in DB.
    # This is the DB-level enforcement; SQLAlchemy cascade="all, delete-orphan"
    # on the User.wallets relationship handles the ORM layer.
    # -------------------------------------------------------------------------
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,    # we frequently query "all wallets for user X"
    )

    # -------------------------------------------------------------------------
    # Balance — NUMERIC(18, 8) is mandatory for financial values.
    #
    # NUMERIC(precision, scale):
    #   precision = total number of significant digits (18)
    #   scale     = digits after the decimal point (8)
    #
    # 18 digits total supports balances up to: 9,999,999,999.99999999
    # (ten billion dollars with 8 decimal places of sub-cent precision)
    # This range is safe for consumer wallets.
    #
    # asdecimal=True → SQLAlchemy returns Python Decimal objects, not floats.
    # Python's Decimal type uses arbitrary-precision decimal arithmetic —
    # exactly what you need for money calculations.
    # -------------------------------------------------------------------------
    balance: Mapped[float] = mapped_column(
        Numeric(precision=18, scale=8, asdecimal=True),
        nullable=False,
        default=0,
        comment="Account balance in the wallet's currency. NUMERIC ensures no floating-point rounding.",
    )

    currency: Mapped[WalletCurrency] = mapped_column(
        Enum(WalletCurrency, name="walletcurrency", values_callable=lambda x: [e.value for e in x]),
        nullable=False,
        default=WalletCurrency.USD,
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        comment="False = wallet is frozen or closed; no transactions allowed",
    )

    wallet_tag: Mapped[str | None] = mapped_column(
        String(50),
        nullable=True,
        comment="Optional human-readable label: 'savings', 'business', etc.",
    )

    # -------------------------------------------------------------------------
    # Relationships
    # -------------------------------------------------------------------------
    owner: Mapped["User"] = relationship(  # type: ignore[name-defined]  # noqa: F821
        "User",
        back_populates="wallets",
    )

    transactions: Mapped[list["Transaction"]] = relationship(  # type: ignore[name-defined]  # noqa: F821
        "Transaction",
        back_populates="wallet",
        foreign_keys="Transaction.wallet_id",
        lazy="selectin",
        cascade="all, delete-orphan",
    )

    # -------------------------------------------------------------------------
    # Table-level constraints
    # -------------------------------------------------------------------------
    __table_args__ = (
        # A user cannot have two wallets in the same currency
        # UniqueConstraint on (user_id, currency) enforces this at DB level
        Index("ix_wallets_user_id", "user_id"),
        Index("ix_wallets_user_id_currency", "user_id", "currency"),
    )

    def __repr__(self) -> str:
        return f"<Wallet id={self.id} user_id={self.user_id} currency={self.currency} balance={self.balance}>"
