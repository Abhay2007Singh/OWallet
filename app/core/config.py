"""
app/core/config.py

Central configuration module using Pydantic Settings.
All environment variables are declared here with types and defaults.
The Settings object is a singleton — import `settings` from this module.
"""

from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    Application settings loaded from environment variables / .env file.
    Pydantic validates every field on startup — bad config fails fast with
    a clear error message instead of a silent bug at runtime.
    """

    # -------------------------------------------------------------------------
    # Application
    # -------------------------------------------------------------------------
    APP_NAME: str = "PyWallet"
    APP_ENV: str = "development"       # development | staging | production
    APP_DEBUG: bool = True
    APP_HOST: str = "0.0.0.0"
    APP_PORT: int = 8000
    SECRET_KEY: str

    # -------------------------------------------------------------------------
    # PostgreSQL
    # asyncpg requires the +asyncpg driver prefix in the URL.
    # -------------------------------------------------------------------------
    POSTGRES_USER: str
    POSTGRES_PASSWORD: str
    POSTGRES_DB: str
    POSTGRES_HOST: str = "postgres"
    POSTGRES_PORT: int = 5432
    DATABASE_URL: str                  # full URL including driver

    # -------------------------------------------------------------------------
    # Redis
    # -------------------------------------------------------------------------
    REDIS_HOST: str = "redis"
    REDIS_PORT: int = 6379
    REDIS_DB: int = 0
    REDIS_URL: str

    # -------------------------------------------------------------------------
    # Celery
    # Uses separate Redis databases (1 and 2) to avoid key collisions with
    # the application cache that lives in database 0.
    # -------------------------------------------------------------------------
    CELERY_BROKER_URL: str
    CELERY_RESULT_BACKEND: str

    # -------------------------------------------------------------------------
    # JWT (Phase 2)
    # JWT_SECRET_KEY is intentionally separate from SECRET_KEY.
    # One key per purpose — if the app SECRET_KEY is ever rotated for a
    # different reason (e.g., session cookie rotation), JWT tokens remain valid.
    # -------------------------------------------------------------------------
    JWT_SECRET_KEY: str = "dev-jwt-secret-replace-in-production-with-64-char-random-string"
    JWT_ALGORITHM: str = "HS256"
    JWT_ACCESS_TOKEN_EXPIRE_MINUTES: int = 15   # 15 min — short window limits stolen-token damage
    JWT_REFRESH_TOKEN_EXPIRE_DAYS: int = 7

    # -------------------------------------------------------------------------
    # Pydantic Settings configuration
    # env_file: load values from .env if the variable isn't set in the shell
    # env_file_encoding: explicit UTF-8 avoids platform-specific encoding bugs
    # case_sensitive: DATABASE_URL ≠ database_url — be explicit
    # -------------------------------------------------------------------------
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
    )


@lru_cache
def get_settings() -> Settings:
    """
    Returns a cached Settings instance.

    @lru_cache means this function runs only ONCE per process lifetime.
    Every subsequent call returns the same object — no re-reading .env,
    no re-validation. This is the standard singleton pattern for settings.
    """
    return Settings()


# Module-level singleton for easy import: `from app.core.config import settings`
settings: Settings = get_settings()
