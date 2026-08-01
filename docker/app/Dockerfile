# syntax=docker/dockerfile:1

# ============================================================================
# The application image.
#
# ONE image serves every Python runtime role -- API, all Celery worker classes,
# the FFmpeg render worker, the migrate one-shot, and beat -- differing only by
# the command compose supplies. One build, one dependency set, one thing to keep
# patched, and no chance of the API and its workers drifting onto different
# library versions (SADD 9.1).
#
# Build context is the REPOSITORY ROOT, because the uv workspace spans apps/ and
# packages/ and the lockfile lives at the top. See .dockerignore.
#
#   docker build -f docker/app/Dockerfile -t videoforge-app:local .
#
# Base is pinned to bookworm, not floating `python:3.12-slim`: uWSGI 2.0.x links
# PCRE1 (libpcre3), which Debian trixie dropped. Interpreter is 3.12 per ADR-002.
# ============================================================================
ARG PYTHON_IMAGE=python:3.12-slim-bookworm
ARG UV_VERSION=0.12.0


# --------------------------------------------------------------------------- #
# Builder: resolve and compile everything into a self-contained virtualenv.
# --------------------------------------------------------------------------- #
FROM ${PYTHON_IMAGE} AS builder
ARG UV_VERSION

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential \
      libpcre3-dev \
      libssl-dev \
 && rm -rf /var/lib/apt/lists/*

RUN pip install "uv==${UV_VERSION}"

WORKDIR /app

# Dependency layer first. Copying only the manifests means this layer -- which
# includes the expensive uWSGI compile -- is reused on every build where the
# dependency set is unchanged, which is almost all of them.
#
# --no-install-workspace installs third-party dependencies but skips the
# workspace members themselves, which have not been copied in yet.
COPY pyproject.toml uv.lock ./
COPY apps/backend/pyproject.toml      apps/backend/pyproject.toml
COPY apps/workers/pyproject.toml      apps/workers/pyproject.toml
COPY packages/shared/pyproject.toml   packages/shared/pyproject.toml
COPY packages/persistence/pyproject.toml packages/persistence/pyproject.toml
COPY packages/domain/pyproject.toml   packages/domain/pyproject.toml
COPY packages/providers/pyproject.toml packages/providers/pyproject.toml
COPY packages/prompts/pyproject.toml  packages/prompts/pyproject.toml
COPY packages/timeline/pyproject.toml packages/timeline/pyproject.toml

# --frozen: fail if uv.lock disagrees with the manifests, rather than silently
# re-resolving. A build must never quietly install something the lockfile did
# not pin.
# --no-dev: linters and pytest have no business in a runtime image.
# --group deploy: opts into uWSGI (see pyproject.toml).
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --group deploy --no-install-workspace

# Now the source, and install the workspace members on top.
COPY apps/ apps/
COPY packages/ packages/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --group deploy


# --------------------------------------------------------------------------- #
# Runtime: no compiler, no uv, no build headers.
# --------------------------------------------------------------------------- #
FROM ${PYTHON_IMAGE} AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:${PATH}" \
    VIRTUAL_ENV=/opt/venv

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      # uWSGI's runtime PCRE dependency (the -dev headers stay in the builder).
      libpcre3 \
      # The render worker shells out to this (D4: FFmpeg, not Remotion).
      ffmpeg \
      # Caption rendering needs a font on disk AND fontconfig to resolve it by
      # name. Installing one without the other yields boxes instead of glyphs.
      fontconfig \
      fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/* \
 # gid 2000 is shared with the nginx image so both can use the uWSGI socket at
 # mode 660. Keep the two in sync.
 && groupadd --gid 2000 uwsgisock \
 && useradd --uid 1000 --gid 2000 --create-home --shell /usr/sbin/nologin app \
 # setgid on the socket directory means sockets created here inherit the
 # uwsgisock group automatically. That is what lets uWSGI run as an unprivileged
 # user throughout: without it, uWSGI would have to start as root purely to
 # chown the socket after binding.
 && install -d -o app -g uwsgisock -m 2770 /run/uwsgi

# Assert the capabilities the pipeline actually depends on, at build time.
# Pinning an exact apt version is brittle (Debian security updates retire old
# version strings), so verify behaviour instead: a base image that ever ships an
# ffmpeg without libass would otherwise fail much later, mid-render, in M4.
RUN set -eu; \
    ffmpeg -hide_banner -version | head -1; \
    for f in concat xfade subtitles scale; do \
      ffmpeg -hide_banner -filters 2>/dev/null | grep -qw "$f" \
        || { echo "FATAL: ffmpeg lacks required filter: $f" >&2; exit 1; }; \
    done; \
    for c in libx264 aac; do \
      ffmpeg -hide_banner -encoders 2>/dev/null | grep -qw "$c" \
        || { echo "FATAL: ffmpeg lacks required encoder: $c" >&2; exit 1; }; \
    done; \
    fc-list | grep -qi dejavu \
      || { echo "FATAL: no caption font resolvable by fontconfig" >&2; exit 1; }; \
    echo "ffmpeg capability check: OK"

COPY --from=builder --chown=app:uwsgisock /opt/venv /opt/venv
WORKDIR /app
COPY --from=builder --chown=app:uwsgisock /app/apps /app/apps
COPY --from=builder --chown=app:uwsgisock /app/packages /app/packages
# Migrations ride in the same image: the migrate service is this image with
# `alembic upgrade head` as its command (SADD §7.3).
COPY --chown=app:uwsgisock database/ /app/database/

# Never root. uWSGI does not need it here (see the setgid directory above), and
# neither do the workers.
USER app

# Default is the API; compose overrides `command` for every other role:
#   worker-*      celery -A videoforge_workers.celery_app worker -Q <queue>
#   worker-render celery -A videoforge_workers.celery_app worker -Q render
#   beat          celery -A videoforge_workers.celery_app beat
#   migrate       alembic upgrade head
CMD ["uwsgi", "--ini", "/app/apps/backend/uwsgi/api.ini"]
