"""
migrations/env.py

Alembic environment file — runs when any alembic command is invoked.
This version supports ASYNC SQLAlchemy (asyncpg driver).

How it works:
1. Alembic calls run_migrations_online() (or offline).
2. We create an async engine from our Settings.DATABASE_URL.
3. We connect to the DB and run migrations inside an async context.
4. Alembic compares Base.metadata (all model definitions) against the
   live DB schema to generate/apply migration changes.

Important: import app.models so that all model classes are registered
onto Base.metadata before autogenerate runs.
"""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

# -------------------------------------------------------------------------
# Load our application's settings and models.
# These imports must happen before any Alembic metadata inspection.
# -------------------------------------------------------------------------
from app.core.config import settings
from app.core.database import Base
import app.models  # noqa: F401 — registers all models onto Base.metadata

# -------------------------------------------------------------------------
# Alembic Config object — provides access to alembic.ini values.
# -------------------------------------------------------------------------
config = context.config

# -------------------------------------------------------------------------
# Configure Python logging from the alembic.ini [loggers] section.
# -------------------------------------------------------------------------
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# -------------------------------------------------------------------------
# Target metadata — tells autogenerate what the "desired" schema looks like.
# Without this, autogenerate would see an empty schema and try to drop everything.
# -------------------------------------------------------------------------
target_metadata = Base.metadata

# -------------------------------------------------------------------------
# Override the sqlalchemy.url from alembic.ini with our Settings value.
# This means the DB URL is read from .env at runtime, never hardcoded.
# -------------------------------------------------------------------------
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode.
    Generates SQL script without connecting to the DB.
    Useful for: reviewing what SQL will be executed, or for DBAs
    who apply migrations manually.

    Usage: alembic upgrade head --sql > migration.sql
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        # Compare server defaults so autogenerate detects default changes
        compare_server_defaults=True,
        # Compare type changes (e.g., VARCHAR(100) → VARCHAR(255))
        compare_type=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """
    Configure Alembic context with a live DB connection and run migrations.
    Called from both the sync wrapper inside run_migrations_online().
    """
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_server_defaults=True,
        compare_type=True,
        # Render SQL as text in migration files for readability
        render_as_batch=False,   # False for PostgreSQL (only needed for SQLite)
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """
    Async migration runner.
    Creates an async engine, connects, and passes the connection to
    the synchronous do_run_migrations() via run_sync().

    Why run_sync()?
    Alembic's internal migration logic is synchronous. run_sync() runs it
    inside the async connection without blocking the event loop.
    """
    # Build an async engine from alembic.ini config (URL overridden above)
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,  # NullPool: no persistent connections during migration
    )

    async with connectable.connect() as connection:
        # Hand control to the synchronous migration runner
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """
    Entry point for online migrations (the normal path).
    asyncio.run() creates a new event loop, runs the async migrations,
    then closes the loop.
    """
    asyncio.run(run_async_migrations())


# -------------------------------------------------------------------------
# Alembic calls this file as a script. Choose online vs offline mode.
# -------------------------------------------------------------------------
if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
