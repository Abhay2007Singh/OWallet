"""
app/models/__init__.py

Import all models here so that:
1. Alembic's env.py can import this single module and discover all table metadata.
2. SQLAlchemy's relationship() forward references resolve correctly.

IMPORTANT: The import ORDER matters for foreign key resolution.
Import parent tables before child tables.
"""

from app.models.user import User, UserRole
from app.models.wallet import Wallet, WalletCurrency
from app.models.transaction import Transaction, TransactionType, TransactionStatus
from app.models.idempotency_key import IdempotencyKey, IdempotencyStatus

__all__ = [
    "User",
    "UserRole",
    "Wallet",
    "WalletCurrency",
    "Transaction",
    "TransactionType",
    "TransactionStatus",
    "IdempotencyKey",
    "IdempotencyStatus",
]
