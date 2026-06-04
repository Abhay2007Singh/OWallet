# =============================================================================
# PyWallet — Multi-stage Dockerfile
#
# STAGE 1 (builder): installs gcc + compiles C extensions (asyncpg, bcrypt),
#   outputs a clean virtual environment at /opt/venv with no build tools.
#
# STAGE 2 (runtime): copies only /opt/venv and app code into a minimal image.
#   No gcc, no pip, no build cache. ~150MB final image vs ~800MB naive build.
#
# Non-root user: the app runs as `pywallet` (UID 1000).
#   Containers run as root by default — this limits blast radius if exploited.
#
# Build:
#   docker build -t pywallet:latest .
#
# Run:
#   docker compose up --build
# =============================================================================


# ─────────────────────────────────────────────────────────────────────────────
# Stage 1 — Builder
# Has: gcc, libpq-dev, pip, and all Python packages compiled into /opt/venv
# Does NOT appear in the final image — it is a transient build environment.
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS builder

# Build-time environment
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # pip: avoid writing to the default global site-packages
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    # Tell pip to install into our venv (activated by PATH below)
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# Install build tools needed to compile C extensions:
#   gcc        — C compiler for asyncpg, bcrypt, hiredis
#   libpq-dev  — PostgreSQL client headers for asyncpg
# RUN layer combines update + install + cleanup to minimize layer size.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Create the virtual environment.
# Using a venv inside /opt/venv instead of installing system-wide:
#   - Clean separation from the OS Python
#   - Trivial to copy to the runtime stage (just COPY /opt/venv /opt/venv)
RUN python -m venv /opt/venv

WORKDIR /build

# Copy requirements before app code (Docker cache: only re-runs pip on
# requirements.txt change, not on every app code change).
COPY requirements.txt .

# Install all Python packages into the venv.
# bcrypt and asyncpg compile C extensions here — gcc is available in this stage.
RUN pip install --upgrade pip && \
    pip install -r requirements.txt


# ─────────────────────────────────────────────────────────────────────────────
# Stage 2 — Runtime
# Has: Python interpreter, /opt/venv (no gcc), app code, non-root user.
# gcc, libpq-dev, pip cache, __pycache__, tests are NOT present.
# ─────────────────────────────────────────────────────────────────────────────
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# Install only runtime-required OS libraries (no build tools):
#   libpq5  — PostgreSQL client library (asyncpg links against this at runtime)
#   curl    — Used by the HEALTHCHECK below
# No gcc — we don't compile anything in this stage.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create a non-root system user and group.
# UID/GID 1000 is a conventional non-privileged user.
# --system: no password, no home directory by default.
# --group:  create a matching group named `pywallet`.
RUN groupadd --gid 1000 pywallet && \
    useradd --uid 1000 --gid pywallet --shell /bin/bash --create-home pywallet

# Copy the compiled virtual environment from the builder stage.
# This includes all installed packages with their compiled C extensions.
# gcc is NOT copied — it stays in the builder stage only.
COPY --from=builder /opt/venv /opt/venv

WORKDIR /app

# Copy application source code.
# Excluded by .dockerignore (if present): .git, __pycache__, tests/, .env
COPY app/ ./app/
COPY alembic.ini .
COPY migrations/ ./migrations/

# Transfer ownership of the working directory to the non-root user.
# Must happen AFTER COPY commands so the user owns the files.
RUN chown -R pywallet:pywallet /app

# Switch to the non-root user.
# All subsequent commands (including CMD) run as `pywallet`.
USER pywallet

# Expose the port the app listens on.
EXPOSE 8000

# Health check: curl the /api/v1/health endpoint every 30 seconds.
# --fail: curl returns non-zero on HTTP error codes.
# --silent: suppress progress output.
# start_period: 15s grace period on container startup before checks count.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl --fail --silent http://localhost:8000/api/v1/health || exit 1

# Production command:
#   --workers 4     : 4 Uvicorn worker processes (CPU-bound scaling)
#   --host 0.0.0.0  : bind to all interfaces inside Docker
#   --port 8000     : matches EXPOSE above
#
# Why 4 workers? Rule of thumb: 2 × CPU cores + 1 for I/O-bound workloads.
# Adjust via the WEB_CONCURRENCY env var in docker-compose.yml.
# For async apps, a single uvicorn worker handles many concurrent requests
# via asyncio — use multiple workers for CPU-bound operations only.
#
# Development override (in docker-compose.yml):
#   command: uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
CMD ["sh", "-c", "alembic upgrade head && uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 4 --log-level info"]
