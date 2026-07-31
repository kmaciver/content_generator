#!/bin/sh
# Runtime NF8 check: boot the real containers and read their actual environment.
#
# The static twin (tests/test_secret_isolation.py) proves the compose *files*
# are right; this proves the running *containers* are, which also catches
# mistakes the YAML can't express (a stray `env_file:`, an image baking a key).
#
# Method: plant a canary value in a provider key variable, then confirm the
# backend never sees it while a provider worker does. Uses `run --rm --no-deps`
# so nothing else needs to be up, and no service is left running.
#
# Requires docker on the host. Wired to `make verify-secrets`; CI runs it too.
set -eu

cd "$(dirname "$0")/.."

COMPOSE="docker compose --project-directory . \
  -f docker/compose/docker-compose.yml -f docker/compose/compose.prod.yml"

CANARY="canary-$(date +%s)-nf8"
export OPENAI_API_KEY="${CANARY}"

echo "==> planting canary in OPENAI_API_KEY and reading container environments"

fail() { echo "FAIL: $1" >&2; exit 1; }

# --- backend must NOT see the canary --------------------------------------- #
backend_env="$(${COMPOSE} run --rm --no-deps backend env)"
if printf '%s\n' "${backend_env}" | grep -q "^OPENAI_API_KEY="; then
  fail "backend container received OPENAI_API_KEY (NF8 violated)"
fi
echo "    backend: no provider keys  OK"

# --- a provider worker MUST see it (proves the canary methodology works; a
#     silently-broken plant would otherwise make the backend check vacuous) --- #
worker_env="$(${COMPOSE} --profile deferred run --rm --no-deps worker-llm env)"
if ! printf '%s\n' "${worker_env}" | grep -q "^OPENAI_API_KEY=${CANARY}$"; then
  fail "worker-llm did not receive the canary — check compose interpolation"
fi
echo "    worker-llm: canary present  OK"

# --- render worker needs no provider, so it must also be clean (D4) --------- #
render_env="$(${COMPOSE} --profile deferred run --rm --no-deps worker-render env)"
if printf '%s\n' "${render_env}" | grep -q "^OPENAI_API_KEY="; then
  fail "worker-render received OPENAI_API_KEY — it renders, it does not call providers"
fi
echo "    worker-render: no provider keys  OK"

echo "==> NF8 verified at runtime"
