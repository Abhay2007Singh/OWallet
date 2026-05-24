"""
app/schemas/__init__.py

Central export point for all Pydantic schemas.
"""

from app.schemas.auth import (
    LoginRequest,
    LoginResponse,
    LogoutResponse,
    MeResponse,
    RefreshRequest,
    RefreshResponse,
    RegisterRequest,
    RegisterResponse,
    TokenResponse,
    UserResponse,
)
from app.schemas.wallet import (
    DepositRequest,
    DepositResponse,
    PaginatedTransactionResponse,
    PaginationMeta,
    TransactionFilters,
    TransactionResponse,
    WalletBalanceResponse,
)

__all__ = [
    # auth
    "LoginRequest",
    "LoginResponse",
    "LogoutResponse",
    "MeResponse",
    "RefreshRequest",
    "RefreshResponse",
    "RegisterRequest",
    "RegisterResponse",
    "TokenResponse",
    "UserResponse",
    # wallet
    "DepositRequest",
    "DepositResponse",
    "PaginatedTransactionResponse",
    "PaginationMeta",
    "TransactionFilters",
    "TransactionResponse",
    "WalletBalanceResponse",
]
