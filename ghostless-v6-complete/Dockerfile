# ─────────────────────────────────────────────────────────────────────────────
# Ghostless API — Multi-stage Production Dockerfile
#
# Stages:
#   builder   — install Python deps into a venv (layer-cached)
#   runtime   — copy venv + source, run as non-root
#
# Build args:
#   APP_VERSION   (default: dev)
#   GIT_SHA       (default: unknown)
#   BUILD_DATE    (default: now)
#
# Build:
#   docker build --build-arg APP_VERSION=6.0.0 --build-arg GIT_SHA=$(git rev-parse HEAD) -t ghostless:6.0.0 .
#
# Run (API):
#   docker run -p 8000:8000 --env-file .env ghostless:6.0.0
#
# Run (Worker):
#   docker run --env-file .env ghostless:6.0.0 celery -A app.celery_app worker -Q default,webhooks,scoring
# ─────────────────────────────────────────────────────────────────────────────

ARG PYTHON_VERSION=3.12
ARG APP_VERSION=dev
ARG GIT_SHA=unknown
ARG BUILD_DATE=unknown

# ── Stage 1: Builder ──────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS builder

WORKDIR /build

# System deps for building C extensions (psycopg2, argon2-cffi)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Create isolated virtualenv — keeps runtime image clean
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Install deps first (cached layer unless requirements.txt changes)
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -r requirements.txt

# ── Stage 2: Runtime ──────────────────────────────────────────────────────────
FROM python:${PYTHON_VERSION}-slim AS runtime

ARG APP_VERSION
ARG GIT_SHA
ARG BUILD_DATE

# OCI image labels
LABEL org.opencontainers.image.title="Ghostless API" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.created="${BUILD_DATE}" \
      org.opencontainers.image.description="Trust scoring and fraud detection API for crowdsourcing platforms" \
      org.opencontainers.image.vendor="Ghostless"

# Runtime-only system deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user
RUN groupadd -r ghostless && useradd -r -g ghostless -s /sbin/nologin ghostless

WORKDIR /app

# Copy virtualenv from builder
COPY --from=builder /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy source (respect .dockerignore)
COPY --chown=ghostless:ghostless . .

# Bake version into the image
ENV APP_VERSION=${APP_VERSION} \
    GIT_SHA=${GIT_SHA} \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app

USER ghostless

EXPOSE 8000

# ── Health check (used by Docker and docker-compose) ──────────────────────────
HEALTHCHECK --interval=20s --timeout=10s --start-period=30s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# ── Default: API server ───────────────────────────────────────────────────────
# Override CMD for workers:
#   celery -A app.celery_app worker -Q default,webhooks,scoring --concurrency=4
CMD ["uvicorn", "app.main:app", \
     "--host", "0.0.0.0", \
     "--port", "8000", \
     "--workers", "4", \
     "--loop", "uvloop", \
     "--http", "httptools", \
     "--proxy-headers", \
     "--forwarded-allow-ips", "*", \
     "--access-log"]
