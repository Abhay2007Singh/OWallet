"""
app/schemas/wallet.py

Pydantic v2 request/response schemas for the wallet system.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: Why Decimal is required — not float
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Floating-point arithmetic uses binary representation:
  0.1 in binary = 0.0001100110011... (infinite repeating fraction)
  Python: 0.1 + 0.2 = 0.30000000000000004

In a wallet with 50,000 daily transactions, these errors accumulate.
After one year, your reported balances could be cents or dollars wrong —
which constitutes a financial reporting error with regulatory consequences.

Python's Decimal uses base-10 arithmetic:
  Decimal("0.1") + Decimal("0.2") = Decimal("0.3") (exact)

Rule: every monetary value that touches user-facing code uses Decimal.
      PostgreSQL NUMERIC does the same thing at the DB layer.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: Why max 2 decimal places for deposits
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

Our DB stores 8 decimal places (for crypto sub-cent precision).
But user-facing USD deposits use cent precision: $100.00, not $100.001.

Allowing $100.001 as a deposit creates an inconsistency:
  - The user deposited $100.001
  - Their bank statement shows $100.00 (banks round to cents)
  - The reconciliation never balances

We reject it at the schema layer — fail fast, before any DB access.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
DEEP CONCEPT: What the transfer response MUST NOT reveal
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

A P2P transfer response is shown only to the SENDER. It must NEVER expose:

  1. receiver_balance / receiver_balance_after — the receiver's financial
     position is private. Revealing it lets the sender infer account wealth.
     Example attack: "I sent $1 and their new balance is $X" — not acceptable.

  2. receiver_wallet_id — a UUID that could be correlated against leaked
     data to identify the receiver's account across API calls.

  3. receiver's transaction_id for their CREDIT row — that ID could be used
     to probe the /transactions/{id} endpoint (IDOR fishing).

What we DO expose:
  - transfer_reference_id — a shared correlation UUID that BOTH parties
    receive. It proves the transfer happened without leaking individual balances.
  - sender's debit_transaction_id — the sender's own ledger entry.
  - sender's new balance — they need to know their own updated balance.
  - amount and timestamp — the event facts.
"""

import uuid
from datetime import datetime
from decimal import Decimal, InvalidOperation

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator

from app.models.transaction import TransactionStatus, TransactionType
from app.models.wallet import WalletCurrency


# =============================================================================
# Request Schemas
# =============================================================================

class _MonetaryAmountMixin:
    """
    Shared validators for any schema that accepts a user-facing monetary amount.

    Extracted so DepositRequest and TransferRequest stay DRY — both enforce
    the same precision and ceiling rules without duplicating the logic.

    Why a mixin instead of a base BaseModel subclass?
    Python's MRO makes multiple inheritance with Pydantic BaseModel fragile when
    both base and child define validators on the same field name. A plain mixin
    carries ONLY the validator classmethods — no field definitions, no __init__.
    Pydantic v2 picks up the validators correctly when the mixin is listed BEFORE
    BaseModel in the class definition.
    """

    @field_validator("amount", mode="before")
    @classmethod
    def coerce_to_decimal(cls, v: object) -> Decimal:
        if isinstance(v, Decimal):
            return v
        try:
            return Decimal(str(v))
        except InvalidOperation:
            raise ValueError(f"'{v}' is not a valid monetary amount")

    @field_validator("amount")
    @classmethod
    def validate_amount(cls, v: Decimal) -> Decimal:
        if v.is_nan() or v.is_infinite():
            raise ValueError(
                "Amount must be a finite number. NaN and Infinity are not valid monetary values."
            )
        sign, digits, exponent = v.as_tuple()
        if exponent < -2:
            raise ValueError(
                f"Amount {v} has more than 2 decimal places. "
                "Use cent precision (e.g., 100.00 or 100.50)."
            )
        if v > Decimal("1000000.00"):
            raise ValueError(
                "Amount exceeds the maximum per-operation limit of $1,000,000.00. "
                "Please contact support for large transfers."
            )
        return v


class DepositRequest(_MonetaryAmountMixin, BaseModel):
    """
    Request body for POST /wallet/deposit.

    amount constraints are enforced by _MonetaryAmountMixin:
      - Must be > 0 (Field gt=0 handles this at the Pydantic level)
      - Must not exceed 2 decimal places
      - Must not exceed $1,000,000 per operation
      - Must be finite (NaN and Infinity rejected)
    """

    amount: Decimal = Field(
        gt=Decimal("0"),
        max_digits=15,
        decimal_places=2,
        examples=[Decimal("500.00")],
        description=(
            "Deposit amount in USD. "
            "Must be positive. "
            "Maximum 2 decimal places. "
            "Maximum 15 significant digits. "
            "Maximum single deposit: $1,000,000.00"
        ),
    )

    description: str | None = Field(
        default=None,
        max_length=255,
        examples=["Bank transfer - salary"],
        description="Optional memo for the transaction record.",
    )


# =============================================================================
# Response Schemas
# =============================================================================

class WalletBalanceResponse(BaseModel):
    """
    Response for GET /wallet/balance.

    Includes `cached: bool` to indicate whether the balance came from Redis
    or was freshly read from PostgreSQL. Useful for debugging and monitoring.

    Why include `cached`?
    In a production system, cache inconsistencies are real bugs. Exposing
    whether the response is cached lets engineers debug discrepancies between
    what Redis shows and what the DB has. Remove this field in production
    if you don't want to leak implementation details to API consumers.
    """

    wallet_id: uuid.UUID = Field(description="Unique identifier of the wallet")
    balance: Decimal = Field(description="Current wallet balance")
    currency: WalletCurrency = Field(description="3-letter currency code")
    is_active: bool = Field(description="False if wallet is frozen or closed")
    cached: bool = Field(
        default=False,
        description="True if balance was served from Redis cache (max 30s stale)",
    )


class TransactionResponse(BaseModel):
    """
    Single transaction representation returned in API responses.

    from_attributes=True enables Pydantic to read from SQLAlchemy ORM instances.

    Why include balance_before and balance_after?
    These are forensic anchors. If a user disputes a transaction:
      "I had $500, you charged me $200, I should have $300 but I have $250"
    The balance_before/after fields let support agents verify the exact state
    at the moment of the transaction without reconstructing from the full history.

    The SQLAlchemy model stores amount/balance as Numeric with asdecimal=True,
    so the runtime type is already Decimal — Pydantic serializes correctly.

    Phase 4 additions:
    - counterpart_wallet_id: the other wallet in a P2P transfer. NULL for
      deposits/withdrawals. Present so users can verify who they sent to.
    - transfer_reference_id: the shared correlation UUID that links the sender's
      DEBIT row and receiver's CREDIT row. Both parties see the same reference_id.
      Useful for dispute resolution: "show me transfer with ref X" returns both
      legs of the double-entry pair.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    wallet_id: uuid.UUID
    amount: Decimal = Field(description="Transaction amount. Always positive.")
    balance_before: Decimal = Field(description="Wallet balance BEFORE this transaction")
    balance_after: Decimal = Field(description="Wallet balance AFTER this transaction")
    transaction_type: TransactionType
    status: TransactionStatus
    description: str | None
    external_reference: str | None
    # Phase 4: double-entry bookkeeping fields
    counterpart_wallet_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "For DEBIT/CREDIT pairs: the other wallet involved in the transfer. "
            "NULL for deposits and withdrawals."
        ),
    )
    transfer_reference_id: uuid.UUID | None = Field(
        default=None,
        description=(
            "Shared UUID linking the DEBIT and CREDIT rows of the same P2P transfer. "
            "NULL for deposits and withdrawals. "
            "Use this to look up the counterpart transaction."
        ),
    )
    created_at: datetime
    updated_at: datetime


class DepositResponse(BaseModel):
    """
    Response for POST /wallet/deposit (HTTP 201).

    The transaction starts in PENDING status. The Celery worker will
    update it to COMPLETED after simulating bank-side processing.

    new_balance: The immediately-updated balance (SELECT FOR UPDATE ensures
    this is accurate even under concurrent deposits).
    """

    message: str = "Deposit initiated successfully"
    transaction: TransactionResponse
    new_balance: Decimal = Field(
        description="Updated wallet balance after this deposit (immediately applied)"
    )


class TransferRequest(_MonetaryAmountMixin, BaseModel):
    """
    Request body for POST /wallet/transfer.

    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    Why receiver_email instead of receiver_wallet_id or receiver_user_id?
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    UX reason: users know each other's email addresses, not UUIDs.
    "Send $50 to alice@example.com" maps to real-world mental models.
    Wallet UUIDs are internal identifiers — they belong in the DB,
    not in client-facing inputs.

    Security reason: if we accepted wallet_id or user_id, a malicious
    client could enumerate IDs by probing which IDs accept transfers.
    Email addresses are already known between parties (they emailed each
    other to arrange the payment), so there is no additional information
    disclosure.

    The service layer resolves receiver_email → User → Wallet internally.
    """

    receiver_email: EmailStr = Field(
        description="Email address of the recipient user.",
        examples=["alice@example.com"],
    )

    amount: Decimal = Field(
        gt=Decimal("0"),
        max_digits=15,
        decimal_places=2,
        examples=[Decimal("100.00")],
        description=(
            "Transfer amount in USD. "
            "Must be positive. "
            "Maximum 2 decimal places. "
            "Maximum 15 significant digits. "
            "Maximum single transfer: $1,000,000.00"
        ),
    )

    description: str | None = Field(
        default=None,
        max_length=255,
        examples=["Splitting the dinner bill"],
        description="Optional memo. Visible to the sender in their transaction history.",
    )


class TransferResponse(BaseModel):
    """
    Response for POST /wallet/transfer (HTTP 200).

    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
    DELIBERATELY OMITTED FIELDS (see module docstring for full rationale)
    ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

    - receiver_balance / receiver_balance_after: private financial information.
    - receiver_wallet_id: internal identifier, not exposed to third parties.
    - credit_transaction_id: the receiver's CREDIT row ID — exposing it
      enables IDOR fishing against /transactions/{id}.

    What this response DOES expose:
    - transfer_reference_id: the shared correlation UUID. Both the sender's
      DEBIT row and the receiver's CREDIT row carry this same UUID.
      Either party can use it to find "the other side" of the transfer
      via their own transaction history endpoint.
    - debit_transaction_id: the sender's own ledger entry (DEBIT row).
      The sender owns this record and can query it freely.
    - sender_new_balance: the sender's updated balance. They own this data.
    - amount and timestamp: the event facts needed to confirm the transfer.
    """

    transfer_reference_id: uuid.UUID = Field(
        description=(
            "Shared UUID linking both legs of this transfer. "
            "The sender's DEBIT and the receiver's CREDIT both carry this UUID. "
            "Use it for dispute resolution or to reference the transfer."
        )
    )

    amount: Decimal = Field(
        description="Amount transferred (always positive, in the wallet's currency)."
    )

    sender_new_balance: Decimal = Field(
        description="Your wallet balance after the transfer was deducted."
    )

    debit_transaction_id: uuid.UUID = Field(
        description=(
            "ID of your DEBIT transaction record. "
            "Use GET /wallet/transactions/{id} to retrieve its full detail."
        )
    )

    timestamp: datetime = Field(
        description="UTC datetime when the transfer was committed to the ledger."
    )

    message: str = Field(
        default="Transfer completed successfully.",
        description="Human-readable confirmation message.",
    )


class PaginationMeta(BaseModel):
    """
    Pagination metadata included in all list responses.

    pages: ceil(total / page_size) — computed server-side so the client
    doesn't need to calculate it.

    has_next / has_prev: convenience flags. The client could derive these
    from page/pages, but explicit booleans prevent off-by-one errors in
    frontend pagination logic.
    """

    total: int = Field(description="Total number of transactions matching filters")
    page: int = Field(description="Current page number (1-based)")
    page_size: int = Field(description="Number of items per page")
    pages: int = Field(description="Total number of pages")
    has_next: bool = Field(description="True if there are more pages after this one")
    has_prev: bool = Field(description="True if there are pages before this one")


class PaginatedTransactionResponse(BaseModel):
    """
    Paginated list of transactions with metadata.

    Wrapping items + pagination in a consistent envelope means all list
    endpoints in the API look identical — easier to build clients against.
    """

    items: list[TransactionResponse]
    pagination: PaginationMeta


# =============================================================================
# Query Parameter Schema (used as a Depends in the route)
# =============================================================================

class TransactionFilters(BaseModel):
    """
    Query parameters for GET /wallet/transactions.

    FastAPI can automatically parse these from query string parameters
    when this model is used with Depends():
        async def list_transactions(filters: TransactionFilters = Depends()):

    page: 1-based page number
    page_size: items per page, capped at 100 to prevent DB abuse
    status: filter to a specific TransactionStatus (optional)
    date_from / date_to: ISO 8601 datetime strings (optional)
    """

    page: int = Field(default=1, ge=1, description="Page number (starts at 1)")
    page_size: int = Field(
        default=20,
        ge=1,
        le=100,
        description="Items per page (1-100)",
    )
    status: TransactionStatus | None = Field(
        default=None,
        description="Filter by transaction status",
    )
    date_from: datetime | None = Field(
        default=None,
        description="Return transactions created at or after this UTC datetime",
    )
    date_to: datetime | None = Field(
        default=None,
        description="Return transactions created at or before this UTC datetime",
    )

    @field_validator("date_to")
    @classmethod
    def validate_date_range(cls, date_to: datetime | None, info: object) -> datetime | None:
        """Ensure date_from is not after date_to."""
        if date_to is None:
            return date_to
        # Access other field values via info.data (Pydantic v2 pattern)
        data = getattr(info, "data", {})
        date_from = data.get("date_from")
        if date_from is not None and date_from > date_to:
            raise ValueError("date_from cannot be later than date_to")
        return date_to
