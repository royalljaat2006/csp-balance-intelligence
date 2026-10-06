# syntax=docker/dockerfile:1

# --- builder: resolve deps with uv, using the locked, reproducible set -----
FROM python:3.12-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build

# Cache-friendly: install deps before copying application code.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --group dbt

# --- runtime ----------------------------------------------------------------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    DJANGO_SETTINGS_MODULE=config.settings.production

# curl is needed for the container healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --uid 10001 appuser

COPY --from=builder /opt/venv /opt/venv
WORKDIR /app
COPY --chown=appuser:appuser app/ ./app
COPY --chown=appuser:appuser dbt/ ./dbt
COPY --chown=appuser:appuser scripts/ ./scripts
COPY --chown=appuser:appuser pyproject.toml uv.lock ./

RUN mkdir -p /app/staticfiles /data/incoming /data/processed /data/failed \
    && chown -R appuser:appuser /app/staticfiles /data \
    && chmod +x /app/scripts/entrypoint.sh

USER appuser

EXPOSE 8000

# Liveness only (not /health/ready) — a container healthcheck failing
# because the DB is briefly unavailable should not make an orchestrator
# treat an otherwise-fine process as unhealthy. See common/views.py.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD curl -f http://127.0.0.1:8000/health/live || exit 1

# `web` (default) runs migrate + gunicorn; `worker` runs the ingestion poll
# loop — same image, role picked by the compose `command:` (Phase 7/8).
ENTRYPOINT ["/app/scripts/entrypoint.sh"]
CMD ["web"]
