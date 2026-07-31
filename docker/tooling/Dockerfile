# syntax=docker/dockerfile:1

# Developer tooling image: uv, linters, formatters, type checker, test runner.
#
# Why this exists rather than "pip install uv" on the host: NF1 says a clean
# machine needs only Docker. Keeping lint/format/typecheck/test inside a
# container means contributors install nothing, CI and local runs use byte
# identical tool versions, and the host Python (whatever pyenv happens to be
# pointing at) is irrelevant.
#
# Interpreter matches the runtime image (ADR-002) so mypy resolves the same
# stdlib and the same syntax level the code will actually run on.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # Keep uv's cache and the project venv OUT of the bind-mounted repo. Both
    # are backed by named volumes (see the Makefile) so they survive between
    # runs. Without UV_PROJECT_ENVIRONMENT, uv would create /w/.venv full of
    # Linux binaries inside the host checkout -- git-ignored, but confusing the
    # first time someone points a host interpreter at it.
    UV_CACHE_DIR=/opt/uv-cache \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    # Same reasoning for the linters' caches: without these, ruff and mypy
    # scatter .ruff_cache/ and .mypy_cache/ through the host checkout.
    RUFF_CACHE_DIR=/opt/caches/ruff \
    MYPY_CACHE_DIR=/opt/caches/mypy \
    # The bind-mounted repo may carry a host .python-version naming a pyenv
    # virtualenv that means nothing in here. Requesting the interpreter
    # explicitly stops uv warning about it on every single invocation.
    UV_PYTHON=/usr/local/bin/python3

# Pinned: tool version drift is a source of "works on my machine" lint diffs.
# Bump deliberately, never incidentally.
ARG UV_VERSION=0.12.0
RUN pip install --no-cache-dir "uv==${UV_VERSION}"

# git is needed because uv and some tooling inspect repository state; without it
# they emit confusing warnings inside the container.
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /w

# No CMD: the Makefile always supplies the command to run.
