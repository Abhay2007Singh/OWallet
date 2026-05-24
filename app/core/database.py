"""
app/core/database.py

Async SQLAlchemy engine, session factory, and FastAPI dependency.

Architecture:
  create_async_engine()    → low-level connection pool to PostgreSQL
  async_session_factory()  → creates Session objects from the pool
  get_db()                 → FastAPI dependency that yields a session per request
                             and guarantees cleanup (commit/rollback/close)
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import settings


# =============================================================================
# Async Engine
# =============================================================================
# create_async_engine creates a connection pool to PostgreSQL.
# The pool manages multiple real TCP connections and lends them to requests.
#
# pool_size=10     → keep 10 persistent connections alive at all times.
# max_overflow=20  → allow up to 20 extra temporary connections under load.
#                    Beyond 30 total, requests wait instead of crashing.
# pool_pre_ping=True → before handing a connection to a request, send a
#                      lightweight "SELECT 1" to verify it's still alive.
#                      Without this, stale connections after a DB restart
#                      cause cryptic errors instead of transparent reconnects.
# echo=False in prod → set echo=settings.APP_DEBUG to log SQL only in dev.
# =============================================================================
engine = create_async_engine(
    settings.DATABASE_URL,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
    echo=settings.APP_DEBUG,   # logs every SQL statement when DEBUG=true
)


# =============================================================================
# Session Factory
# =============================================================================
# async_sessionmaker creates AsyncSession objects from the engine.
# Each HTTP request gets exactly one session — a unit of work.
#
# expire_on_commit=False → after commit(), don't expire loaded attributes.
#   Without this, accessing obj.id after commit() triggers a new lazy DB call,
#   which is impossible in async context. Always False for async SQLAlchemy.
# =============================================================================
AsyncSessionLocal = async_sessionmaker(
    bind=engine,
    class_=AsyncSession,
    expire_on_commit=False,
)


# =============================================================================
# Declarative Base
# =============================================================================
# All SQLAlchemy models inherit from Base. This gives them the ORM machinery:
# __tablename__, Column declarations, relationships, and metadata tracking.
# Centralising Base here means Alembic can find all models by importing Base.
# =============================================================================
class Base(DeclarativeBase):
    """
    Shared declarative base for all ORM models.
    Import this class in every model file and inherit from it.
    """
    pass


# =============================================================================
# FastAPI Dependency: get_db
# =============================================================================
async def get_db() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency that provides a per-request database session.

    Usage in a route:
        @router.get("/example")
        async def example(db: AsyncSession = Depends(get_db)):
            result = await db.execute(select(User))

    Lifecycle:
        1. FastAPI calls get_db() before the route handler runs.
        2. A new AsyncSession is created from the pool.
        3. The session is yielded into the route handler.
        4. After the handler returns (or raises), the finally block runs.
        5. Session is closed — connection returns to the pool.

    Why not commit here? Service layer handles commit/rollback explicitly.
    This gives fine-grained control: a service can span multiple queries
    in one transaction and decide whether to commit or roll back.
    """
    async with AsyncSessionLocal() as session:
        try:
            yield session
        except Exception:
            await session.rollback()   # undo partial writes on any unhandled error
            raise
        finally:
            await session.close()      # always return connection to pool
