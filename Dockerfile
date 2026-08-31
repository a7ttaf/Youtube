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
ARG PYTHON_BASE_DIGEST=sha256:7a500125bc50693f2214e842a621440a1b1b9cbb2188f74ab045d29ed2ea5856
ARG UV_VERSION=0.11.8
ARG UV_IMAGE_DIGEST=sha256:3b7b60a81d3c57ef471703e5c83fd4aaa33abcd403596fb22ab07db85ae91347

############################
# Stage 1 — builder
############################
FROM ghcr.io/astral-sh/uv:${UV_VERSION}@${UV_IMAGE_DIGEST} AS uv-bin

FROM python:${PYTHON_VERSION}-slim@${PYTHON_BASE_DIGEST} AS builder

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
    uv sync --locked --no-dev --no-install-project

# Copy the source tree and install the project itself.
COPY backend ./backend
COPY alembic.ini ./alembic.ini

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

############################
# Stage 2 — runtime
############################
FROM python:${PYTHON_VERSION}-slim@${PYTHON_BASE_DIGEST} AS runtime

ARG APP_USER=app
ARG APP_UID=10001

# Mount point for the durable application-data mount (docker-compose.yml).
# Export artifacts and connector blobs live under here instead of in the
# container's writable layer, which a rebuild or `docker compose down`
# discards. Keep this path in sync with UMS_EXPORT_ARTIFACT_DIR /
# UMS_LOCAL_STORE_ROOT in docker-compose.yml's x-app-storage-env anchor.
#
# The directory and its two subdirectories are created and chowned in the
# runtime RUN below. That stays load-bearing for every mount-free use of the
# image (a plain `docker run` writes into the writable layer as uid
# ${APP_UID} and needs the directories to exist and be owned) and for any
# future named-volume mount (Docker seeds an empty named volume from the
# image content at the mount point, ownership included). It deliberately
# does NOT cover the compose HOST BIND mount: a bind mount overlays the
# image entirely, so image-time ownership never reaches the host directory —
# on a fresh Linux checkout the daemon even creates the missing bind source
# root-owned. docker-compose.yml therefore runs the one-shot `app-data-init`
# service (same image, root, same bind path) to provision the host side and
# prove uid ${APP_UID} writability with a probe file before `app` starts.
# Without either mechanism every export would fail with
# ExportArtifactStorageError("artifact storage unavailable"), surfacing as a
# permanent 503 on download, because FileSystemExportArtifactStore.save()
# only mkdir()s the leaf directories and has no fallback when the parent is
# not writable (backend/ums_smart_revenue/reports/artifact_storage.py).
#
# No `VOLUME` instruction on purpose: VOLUME would make every plain
# `docker run` of this image spawn an anonymous volume that nothing ever
# reclaims. The mount is declared in docker-compose.yml where it belongs.
ARG APP_DATA_HOME=/var/lib/ums

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:${PATH}" \
    VIRTUAL_ENV=/opt/venv \
    APP_HOME=/srv/app \
    APP_UID=${APP_UID}

# Minimal runtime deps. psycopg pulls its client bindings from the Python wheel,
# so the runtime does not need libpq or the psql client.
#
# apt versions are deliberately unpinned. Reproducibility here comes from
# PYTHON_BASE_DIGEST, which pins the base image by sha256; the three packages
# below (ca-certificates, curl, tini) receive Debian security updates, and
# pinning exact versions breaks the build the moment an older version is dropped
# from the archive. Evidence (checked 2026-08-08 UTC inside this exact base
# digest; dates here are UTC to match the commit and PR timeline):
# apt resolves curl to 8.14.1-2+deb13u4 — already the fourth trixie revision —
# and the image's apt sources point at the live deb.debian.org index (the
# snapshot.debian.org URLs in its sources file are comments only), so each
# +deb13uN security release replaces the previous one at `apt-get update` time
# and an exact pin goes stale on the next curl advisory.
# DL3008 is therefore suppressed for both analyzers that lint this file:
# skipcq for DeepSource (DOK-DL3008) and the hadolint ignore directive directly
# below, because the repo's Dockerfile lane (ci/checks/lint.sh ->
# lint::run_docker, tool=hadolint per ci/config/checks.yml) does not recognize
# the DeepSource skipcq comment and would otherwise still report DL3008.
# skipcq: DOK-DL3008
# hadolint ignore=DL3008
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        tini \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/* \
 && case "${APP_UID}" in ''|*[!0-9]*) echo "APP_UID must be a positive integer" >&2; exit 1 ;; esac \
 && [ "${APP_UID}" -ge 1 ] \
 && [ "${APP_UID}" -le 2147483647 ] \
 && groupadd --system --gid "${APP_UID}" "${APP_USER}" \
 && useradd  --system --uid "${APP_UID}" --gid "${APP_USER}" \
             --home-dir "${APP_HOME}" --shell /sbin/nologin "${APP_USER}" \
 && mkdir -p "${APP_HOME}" \
 && chown "${APP_USER}:${APP_USER}" "${APP_HOME}" \
 && mkdir -p "${APP_DATA_HOME}/artifacts" "${APP_DATA_HOME}/blobs" \
 && chown -R "${APP_USER}:${APP_USER}" "${APP_DATA_HOME}"

# Pull the virtualenv from the builder, then drop the source tree alongside.
COPY --from=builder --chown=${APP_USER}:${APP_USER} /opt/venv /opt/venv
COPY --from=builder --chown=${APP_USER}:${APP_USER} /build/backend ${APP_HOME}/backend
COPY --from=builder --chown=${APP_USER}:${APP_USER} /build/alembic.ini ${APP_HOME}/alembic.ini
COPY --chown=${APP_USER}:${APP_USER} scripts/compose_storage.py ${APP_HOME}/scripts/compose_storage.py

WORKDIR ${APP_HOME}
ENV PYTHONPATH=${APP_HOME}/backend

USER ${APP_USER}

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl --fail --silent --max-time 4 http://localhost:8000/livez || exit 1

# tini reaps zombies + forwards signals cleanly. The storage gate also applies
# to explicit `--no-deps app` starts; it execs CMD only after init readiness.
ENTRYPOINT ["/usr/bin/tini", "--", "python", "/srv/app/scripts/compose_storage.py", \
            "container-exec", "--path", "/var/lib/ums", "--"]
CMD ["python", "-m", "uvicorn", "ums_smart_revenue.app:app", \
     "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers"]
