# ADR-014 — The toolchain is containerised; the host needs only Docker

- **Status:** Accepted
- **Date:** 2026-07-30
- **Related:** NF1; implemented in M0-01, extended in M0-11 and M0-12

## Context

NF1 says a clean machine with only Docker installed runs the full stack. The
development *tooling* — uv, ruff, black, isort, mypy, pytest, and later
eslint/tsc/prettier — was not obviously covered by that. Neither `uv` nor
`pre-commit` was present on the development machine.

## Decision

Tooling runs in containers, not on the host:

- `docker/tooling/Dockerfile` — uv plus the Python toolchain at pinned versions.
- `docker/frontend/Dockerfile` (`dev` target) — the JS toolchain.
- The Makefile drives both; `make check-all` is the single entry point, and
  **CI runs the same targets**.

Caches and virtualenvs live in named volumes (`UV_CACHE_DIR`,
`UV_PROJECT_ENVIRONMENT`, `RUFF_CACHE_DIR`, `MYPY_CACHE_DIR`, plus anonymous
volumes over `node_modules`), never in the bind-mounted checkout.

## Consequences

- Contributors install nothing; NF1 holds for development, not just runtime.
- **Local and CI use identical tool versions by construction** rather than via
  two lists someone keeps in sync — CI installs no Python, uv, Node, or linters.
- A Linux `.venv` never lands in a macOS checkout, and linter caches do not
  litter the repo.
- Cost: each gate pays container startup (~1s). Irrelevant next to the class of
  "works on my machine" bug it removes.
- A host-side environment remains *optional* for editor support (autocomplete,
  go-to-definition); `docs/development.md` covers it. Nothing depends on it.
- pre-commit stays optional for the same reason: `make check-all` is the
  source of truth, and CI does not consult the hooks.
