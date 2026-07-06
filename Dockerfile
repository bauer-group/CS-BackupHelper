# =============================================================================
#  BAUER GROUP · BackupHelper — Central Backup Engine
# -----------------------------------------------------------------------------
#  Pluggable multi-source backups (PostgreSQL / MariaDB / MySQL / S3-bucket /
#  filesystem / env) → S3-compatible or local storage, with sha256 manifests,
#  retention, notifications, optional client-side encryption and a restore CLI.
#
#  This is the CENTRAL image. Consuming repos ship a ~20-line meta-Dockerfile
#  `FROM ghcr.io/bauer-group/backuphelper:<ver>` that only sets labels, pins DB
#  client majors, and (optionally) adds app-specific Source plugins.
#
#  Build    : multi-stage with an integrated pytest gate — the prod image cannot
#             be assembled unless the test stage passes (COPY --from=test).
#  Base     : python:3.14-alpine. pg_client major pinned via PG_CLIENT_VERSION.
#  Runtime  : non-root `backup` user (uid/gid 1000), tini as PID 1.
# =============================================================================

ARG PG_CLIENT_VERSION=18

# ---------------------------------------------------------------------------
# Stage 1 · builder — resolve + install the package and its deps into /install
# ---------------------------------------------------------------------------
FROM python:3.14-alpine AS builder
RUN apk add --no-cache build-base libffi-dev
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir --prefix=/install .

# ---------------------------------------------------------------------------
# Stage 2 · test — pytest gate (build fails if tests fail)
# ---------------------------------------------------------------------------
FROM python:3.14-alpine AS test
ARG PG_CLIENT_VERSION
RUN apk add --no-cache build-base libffi-dev \
        "postgresql${PG_CLIENT_VERSION}-client" mariadb-client
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ ./src/
COPY tests/ ./tests/
RUN pip install --no-cache-dir ".[test]"
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN pytest tests/ -q

# ---------------------------------------------------------------------------
# Stage 3 · prod — minimal runtime
# ---------------------------------------------------------------------------
FROM python:3.14-alpine AS prod
ARG PG_CLIENT_VERSION
ARG IMAGE_VERSION=0.1.0

LABEL vendor="BAUER GROUP"
LABEL maintainer="Karl Bauer <kb@de.bauer-group.com>"

LABEL org.opencontainers.image.title="BackupHelper"
LABEL org.opencontainers.image.description="Central pluggable backup engine — DB/S3/filesystem sources, S3+local destinations, retention, notifications, encryption and a restore CLI - BAUER GROUP Edition"
LABEL org.opencontainers.image.vendor="BAUER GROUP"
LABEL org.opencontainers.image.authors="Karl Bauer <kb@de.bauer-group.com>"
LABEL org.opencontainers.image.licenses="MIT"
LABEL org.opencontainers.image.source="https://github.com/bauer-group/BackupHelper"
LABEL org.opencontainers.image.base.name="docker.io/library/python:3.14-alpine"
LABEL org.opencontainers.image.version="${IMAGE_VERSION}"

# Runtime deps: DB clients (mariadb-client covers MariaDB 11/12 + MySQL 8/9),
# encryption tools (gnupg + age), tini, tzdata, ca-certificates, procps (pgrep).
RUN apk add --no-cache \
        "postgresql${PG_CLIENT_VERSION}-client" mariadb-client \
        gnupg age tini tzdata ca-certificates procps \
    && addgroup -g 1000 backup \
    && adduser -u 1000 -G backup -h /app -D backup

COPY --from=builder /install /usr/local
# Hard dependency on the test stage passing (the gate).
COPY --from=test /app/pyproject.toml /tmp/_gate
RUN rm /tmp/_gate

WORKDIR /app

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 BACKUP_DATA_DIR=/data

RUN mkdir -p /data && chown backup:backup /data

VOLUME ["/data"]

USER backup

# Functional healthcheck: reflects last-backup staleness, not just liveness.
HEALTHCHECK --interval=60s --timeout=10s --start-period=20s --retries=3 \
    CMD backuphelper healthcheck || exit 1

ENTRYPOINT ["/sbin/tini", "--", "backuphelper"]
# Usage:
#   (default)  scheduler daemon
#   --now      run every job once and exit
#   <command>  create / list / show / verify / restore / prune / config ...
