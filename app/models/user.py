"""
app/models/user.py

User model — represents an account holder in the PyWallet system.

Design decisions:
- email has a unique index: the natural lookup key for login
- phone_number is optional but indexed for lookup by phone
- is_active / is_verified support soft-disable and email verification flows
- hashed_password is stored, never plaintext (Phase 2 handles hashing)
"""

import uuid
from enum import Enum as PyEnum

from sqlalchemy import Boolean, Enum, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.base import TimestampMixin, UUIDPrimaryKeyMixin


class UserRole(str, PyEnum):
    """
    User roles determine authorization level.
    str mixin means the enum value is a plain string — serializes cleanly
    to JSON and works seamlessly with PostgreSQL ENUM type.
    """
    USER = "user"
    ADMIN = "admin"


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """
    SQLAlchemy ORM model for the 'users' table.
    """

    __tablename__ = "users"

    # -------------------------------------------------------------------------
    # Identity columns
    # -------------------------------------------------------------------------
    email: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        unique=True,   # enforced at DB level — UniqueConstraint not needed separately
    )

    phone_number: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
    )

    full_name: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    # -------------------------------------------------------------------------
    # Authentication
    # -------------------------------------------------------------------------
    hashed_password: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
    )

    # -------------------------------------------------------------------------
    # Account state
    # -------------------------------------------------------------------------
    role: Mapped[UserRole] = mapped_column(
        Enum(UserRole, name="userrole"),  # creates a PostgreSQL ENUM type named 'userrole'
        default=UserRole.USER,
        nullable=False,
    )

    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
    )

    is_verified: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        comment="True after email/phone verification completes",
    )

    # -------------------------------------------------------------------------
    # Relationships
    # back_populates creates a bidirectional link: user.wallets / wallet.owner
    # lazy="selectin" means SQLAlchemy loads related wallets in a second
    # SELECT (not a JOIN) — safer with async and avoids N+1 in most cases.
    # -------------------------------------------------------------------------
    wallets: Mapped[list["Wallet"]] = relationship(  # type: ignore[name-defined]  # noqa: F821
        "Wallet",
        back_populates="owner",
        lazy="selectin",
        cascade="all, delete-orphan",   # deleting a user deletes their wallets
    )

    # -------------------------------------------------------------------------
    # Composite indexes for common query patterns
    # -------------------------------------------------------------------------
    __table_args__ = (
        Index("ix_users_email", "email"),
        Index("ix_users_phone_number", "phone_number"),
        UniqueConstraint("email", name="uq_users_email"),
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email} role={self.role}>"
