# VideoForge developer interface.
#
# Everything runs in Docker (NF1: a clean machine needs only Docker). Targets
# below never assume uv, ruff, mypy, or pytest exist on the host.
#
# Compose-backed targets are guarded: M0-03 creates the real compose topology,
# and until then they fail with an explanation rather than a stack trace.

.DEFAULT_GOAL := help
SHELL := /bin/bash

COMPOSE_DIR   := docker/compose
COMPOSE_BASE  := $(COMPOSE_DIR)/docker-compose.yml
COMPOSE_DEV   := $(COMPOSE_DIR)/compose.dev.yml
COMPOSE_PROD  := $(COMPOSE_DIR)/compose.prod.yml

# --project-directory pins every relative path in the compose files to the
# repository root, so `context: .` means the repo and not docker/compose/.
# It also tells compose where to find .env, which is optional: every setting has
# a working local default (NF1).
COMPOSE      := docker compose --project-directory "$(CURDIR)"
COMPOSE_D    := $(COMPOSE) -f $(COMPOSE_BASE) -f $(COMPOSE_DEV)
COMPOSE_P    := $(COMPOSE) -f $(COMPOSE_BASE) -f $(COMPOSE_PROD)

TOOLING_IMAGE := videoforge-tooling:local
TOOLING_DIR   := docker/tooling

# Named volumes keep uv's cache and the project venv out of the bind-mounted
# repo while still persisting between runs, so repeat invocations are fast and
# the host checkout stays free of Linux build artefacts.
# The docker socket lets testcontainers spawn a real Postgres as a SIBLING
# container (SADD §22). Because that sibling's port is published on the *host*,
# the tooling container reaches it via host.docker.internal — provided natively
# by Docker Desktop, and mapped by --add-host on Linux/CI.
TOOL := docker run --rm \
	-v "$(CURDIR)":/w \
	-v videoforge-uv-cache:/opt/uv-cache \
	-v videoforge-venv:/opt/venv \
	-v videoforge-tool-caches:/opt/caches \
	-v /var/run/docker.sock:/var/run/docker.sock \
	--add-host=host.docker.internal:host-gateway \
	-e TESTCONTAINERS_HOST_OVERRIDE=host.docker.internal \
	-e TESTCONTAINERS_RYUK_DISABLED=true \
	-w /w $(TOOLING_IMAGE)

.PHONY: help tooling lock sync lint fmt fmt-check typecheck test check check-all \
        frontend-image lint-js typecheck-js fmt-js fmt-check-js check-js \
        e2e e2e-image e2e-seed e2e-guard \
        up up-prod down ps logs status seed reset hooks clean \
        migrate migrate-check migrate-new migrate-history verify-secrets env-example \
        exit-test

help: ## Show available targets
	@echo "VideoForge — make targets"
	@echo
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'
	@echo

# --------------------------------------------------------------------------- #
# Tooling
# --------------------------------------------------------------------------- #

tooling: ## Build the developer tooling image (uv, ruff, black, isort, mypy, pytest)
	docker build -t $(TOOLING_IMAGE) $(TOOLING_DIR)

lock: tooling ## Resolve dependencies and write uv.lock
	$(TOOL) uv lock

sync: tooling ## Install the locked dependency set into .venv
	$(TOOL) uv sync --all-packages

# --------------------------------------------------------------------------- #
# Quality gates — the same commands CI runs (M0-12)
# --------------------------------------------------------------------------- #

lint: tooling ## Lint with ruff
	$(TOOL) uv run ruff check .

# Formatters run first and unconditionally. The leading `-` on ruff is
# deliberate: `ruff check --fix` exits non-zero when it finds issues it cannot
# fix automatically, which would otherwise abort this target and leave the code
# unformatted. Reporting those is `make lint`'s job, not this one's.
fmt: tooling ## Format with black + isort, and apply ruff's safe fixes
	$(TOOL) uv run isort .
	$(TOOL) uv run black .
	-$(TOOL) uv run ruff check --fix .

fmt-check: tooling ## Verify formatting without writing (CI mode)
	$(TOOL) uv run isort --check-only --diff .
	$(TOOL) uv run black --check --diff .

typecheck: tooling ## Type-check with mypy (strict)
	$(TOOL) uv run mypy .

# cache_dir is overridden on the command line rather than in pyproject.toml so
# that a host-side `pytest` (if anyone installs one) still behaves normally.
test: tooling ## Run the test suite
	$(TOOL) uv run pytest -o cache_dir=/opt/caches/pytest

# The frontend's gates run in its own image (Node lives only there).
#
# The anonymous volume on node_modules is load-bearing: the bind mount would
# otherwise shadow the image's installed dependencies with the host directory
# (which has none, and whose platform would be wrong anyway).
FRONT := docker run --rm \
	-v "$(CURDIR)/apps/frontend":/app \
	-v /app/node_modules \
	-v /app/.next \
	-w /app --entrypoint sh videoforge-frontend-dev:local -c

frontend-image: ## Build the frontend dev image (used by the JS gates)
	docker build -f docker/frontend/Dockerfile --target dev -t videoforge-frontend-dev:local .

lint-js: frontend-image ## Lint the frontend
	$(FRONT) "npm run lint"

typecheck-js: frontend-image ## Type-check the frontend
	$(FRONT) "npm run typecheck"

fmt-js: frontend-image ## Format the frontend
	$(FRONT) "npm run format"

fmt-check-js: frontend-image ## Verify frontend formatting (CI mode)
	$(FRONT) "npm run format:check"

check-js: lint-js typecheck-js fmt-check-js ## Every frontend gate

# --------------------------------------------------------------------------- #
# End-to-end (M1-11) — the milestone exit criterion
# --------------------------------------------------------------------------- #
#
# Runs against the PROD-LOCAL stack through nginx, on the mock provider. The
# runner joins the compose network so it reaches nginx by service name; that
# also means it needs no published ports, which is what the prod profile is
# about.
E2E_IMAGE := videoforge-e2e:local

e2e-image: ## Build the Playwright runner image
	docker build -f docker/e2e/Dockerfile -t $(E2E_IMAGE) docker/e2e

# The seed is a *precondition*, not a convenience. The flow's first action is
# creating a project, and POST /projects 409s when no workspace row exists —
# migrations build schema, never data. On a developer machine this is invisible
# because the dev and prod-local profiles share the same named volumes, so an
# earlier `make seed` already populated the database; on a clean machine (CI)
# the suite fails on its first click with an error about workspaces.
#
# Seeding here, through the prod files, makes `make e2e` self-sufficient after
# `make up-prod`. `database.seed` is idempotent, so repeat runs are free.
e2e-seed:
	$(COMPOSE_P) run --rm migrate python -m database.seed

# The suite must not be able to spend money (M4-12).
#
# `PROVIDERS__MODE` defaults to `mock` in the compose file and is overridden by
# `.env`, which on a developer machine is usually `real` — so `make e2e` on a
# working laptop billed the vendor for every run, and M4-12's flow drives
# images and a full narration rather than stopping at script. CI never noticed:
# it has no `.env`, so it gets the default and the suite is free there.
#
# Resolved by asking compose rather than by reading `.env`, because compose's
# own precedence (file default → .env → shell) is the thing that decides what
# the containers actually got, and any second implementation of it here would
# be a guess. `record` is refused with `real`: it wraps live calls.
e2e-guard:
	@mode=$$($(COMPOSE_P) config 2>/dev/null \
	  | grep -m1 'PROVIDERS__MODE:' | sed 's/.*: *//' | tr -d '"'); \
	case "$$mode" in \
	  real|record) \
	    echo "refusing to run the e2e suite with PROVIDERS__MODE=$$mode:"; \
	    echo "  it drives images and a full narration, and would bill the vendor."; \
	    echo "  Set PROVIDERS__MODE=mock (or replay) in .env and \`make up-prod\`."; \
	    exit 1 ;; \
	  *) echo "provider mode: $${mode:-mock}" ;; \
	esac

e2e: e2e-guard e2e-image e2e-seed ## Run the end-to-end suite (needs `make up-prod` first)
	docker run --rm \
	  --network videoforge_default \
	  -v "$(CURDIR)/apps/frontend":/app \
	  -v /app/node_modules \
	  -e BASE_URL=http://nginx \
	  -w /app $(E2E_IMAGE) \
	  sh -c "npm install --no-audit --no-fund && npx playwright test"

check: lint fmt-check typecheck test ## Run every Python quality gate, in CI order

check-all: check check-js ## Every gate, Python and frontend

# --------------------------------------------------------------------------- #
# Stack
# --------------------------------------------------------------------------- #

up: ## Start the stack, development profile (API on :5000, stores exposed)
	$(COMPOSE_D) up -d --build
	@$(MAKE) --no-print-directory status

up-prod: ## Start the stack, production-local profile (nginx + uWSGI on :8080)
	$(COMPOSE_P) up -d --build
	@$(MAKE) --no-print-directory status

down: ## Stop the stack, keeping volumes
	$(COMPOSE_D) down --remove-orphans

ps: ## Show service status
	$(COMPOSE_D) ps

status: ## Show health of every running service
	@$(COMPOSE_D) ps --format '  {{.Service}}\t{{.State}}\t{{.Status}}' 2>/dev/null || true

logs: ## Tail logs; narrow with `make logs svc=backend`
	$(COMPOSE_D) logs -f $(svc)

seed: ## Load deterministic demo data (idempotent)
	$(COMPOSE_D) run --rm migrate python -m database.seed

# --------------------------------------------------------------------------- #
# Migrations — run inside the compose network via the migrate service's image
# --------------------------------------------------------------------------- #

migrate: ## Apply migrations to the running stack's database
	$(COMPOSE_D) run --rm migrate alembic -c /app/database/alembic.ini upgrade head

migrate-check: ## Fail if models and migrations disagree (CI gate, SADD §10.4)
	$(COMPOSE_D) run --rm migrate alembic -c /app/database/alembic.ini check

migrate-new: ## Autogenerate a migration: make migrate-new m="add artifact tables"
	@test -n "$(m)" || { echo "usage: make migrate-new m=\"description\""; exit 1; }
	$(COMPOSE_D) run --rm -v "$(CURDIR)/database:/app/database" migrate \
	  alembic -c /app/database/alembic.ini revision --autogenerate -m "$(m)"

migrate-history: ## Show migration history and current head
	$(COMPOSE_D) run --rm migrate alembic -c /app/database/alembic.ini history --verbose

# Destroys the database, object storage, and the broker. Named volumes are
# removed, so everything generated so far is gone -- artifacts included.
reset: ## Destroy ALL volumes, then restart from empty (DESTRUCTIVE)
	@printf "This deletes pgdata, miniodata and redisdata. Type 'yes' to continue: "; \
	read ans; [ "$$ans" = "yes" ] || { echo "aborted"; exit 1; }
	$(COMPOSE_D) down -v --remove-orphans
	$(MAKE) up

# --------------------------------------------------------------------------- #
# Misc
# --------------------------------------------------------------------------- #

exit-test: ## Run the full M0 exit test against production-local (what CI runs)
	bash scripts/m0-exit-test.sh

verify-secrets: ## Boot real containers and assert provider keys reach workers only (NF8)
	sh scripts/verify-secret-isolation.sh

env-example: ## Generate .env.example from docs/env-reference.md
	@awk '/^```dotenv$$/{f=1;next} /^```$$/{f=0} f' docs/env-reference.md > .env.example
	@echo "wrote .env.example  ($$(grep -c '=' .env.example) settings)"
	@echo "next: cp .env.example .env"

hooks: ## Install pre-commit hooks (requires pre-commit on the host)
	@command -v pre-commit >/dev/null 2>&1 || { \
	  echo "pre-commit is not installed on this host."; \
	  echo "Hooks are optional — 'make check' runs the same gates in Docker."; \
	  echo "To enable them: pipx install pre-commit  (or brew install pre-commit)"; \
	  exit 1; \
	}
	pre-commit install

clean: ## Remove local caches and build artefacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
	rm -rf .ruff_cache .mypy_cache .pytest_cache
