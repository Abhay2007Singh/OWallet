"""
app/models/base.py

Shared SQLAlchemy mixins applied to every model.
Mixins add columns without requiring a model to inherit from a table class.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column


class UUIDPrimaryKeyMixin:
    """
    Adds a UUID primary key column named 'id'.

    Why server_default with gen_random_uuid()?
    PostgreSQL generates the UUID — even if application code forgets to set it,
    the DB guarantees every row has a valid UUID. gen_random_uuid() is a
    PostgreSQL built-in that uses cryptographically random bytes.
    """

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,            # Python-side default for new objects
        server_default=func.gen_random_uuid(),  # DB-side default as a safety net
        index=True,
    )


class TimestampMixin:
    """
    Adds created_at and updated_at columns to every model.

    Why server_default=func.now()?
    The database clock is authoritative — if two app servers have clock drift,
    DB timestamps are still consistent. func.now() calls PostgreSQL's NOW().

    Why onupdate=func.now() on updated_at?
    SQLAlchemy calls this Python-side on every UPDATE statement, ensuring
    updated_at always reflects the last modification time.
    """

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=lambda: datetime.now(timezone.utc),  # Python-side update trigger
        nullable=False,
    )
