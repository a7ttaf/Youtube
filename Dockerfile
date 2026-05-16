# syntax=docker/dockerfile:1.9
# UMS Smart Revenue Control Center — backend container image
#
# Multi-stage build:
#   1. builder  — installs Python + project deps with uv, into a self-contained venv.
#   2. runtime  — slim base, non-root user, read-only filesystem-ready.
#
# Build:
#   docker build -t ums-smart-revenue:dev .
#
# Run:
#   docker run --rm -p 8000:8000 \
#     --env-file .env.local \
#     -e UMS_AUTHZ_SOURCE=headers \
#     ums-smart-revenue:dev

ARG PYTHON_VERSION=3.14
ARG UV_VERSION=0.11.8

############################
# Stage 1 — builder
############################
FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv-bin

FROM python:${PYTHON_VERSION}-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /build

# Bring uv binary in from the official image.
COPY --from=uv-bin /uv /usr/local/bin/uv

# Install only the manifests first to maximize Docker layer cache hits.
COPY pyproject.toml ./
COPY uv.lock ./uv.lock

# Install runtime deps into /opt/venv (no project source yet).
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

# Copy the source tree and install the project itself.
COPY backend ./backend
COPY alembic.ini ./alembic.ini

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

############################
# Stage 2 — runtime
############################
FROM python:${PYTHON_VERSION}-slim AS runtime

ARG APP_USER=app
ARG APP_UID=10001

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    VIRTUAL_ENV=/opt/venv \
    APP_HOME=/srv/app

# Minimal runtime deps. asyncpg implements the PostgreSQL protocol directly,
# so the runtime does not need libpq or the psql client.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates=20250419 \
        curl=8.14.1-2+deb13u2 \
        tini=0.19.0-3+b6 \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --system --gid ${APP_UID} ${APP_USER} \
 && useradd  --system --uid ${APP_UID} --gid ${APP_USER} \
             --home-dir ${APP_HOME} --shell /sbin/nologin ${APP_USER} \
 && mkdir -p ${APP_HOME} \
 && chown ${APP_USER}:${APP_USER} ${APP_HOME}

# Pull the virtualenv from the builder, then drop the source tree alongside.
COPY --from=builder --chown=${APP_USER}:${APP_USER} /opt/venv /opt/venv
COPY --from=builder --chown=${APP_USER}:${APP_USER} /build/backend ${APP_HOME}/backend
COPY --from=builder --chown=${APP_USER}:${APP_USER} /build/alembic.ini ${APP_HOME}/alembic.ini

WORKDIR ${APP_HOME}
ENV PYTHONPATH=${APP_HOME}/backend

USER ${APP_USER}

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl --fail --silent --max-time 4 http://localhost:8000/health || exit 1

# tini reaps zombies + forwards signals cleanly.
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "uvicorn", "ums_smart_revenue.app:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1"]
