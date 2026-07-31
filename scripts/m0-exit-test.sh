#!/usr/bin/env bash
# The M0 exit test, executable.
#
# From the plan: "clean machine, `make up-prod`, all healthchecks green, `ping`
# round-trips, hello.mp4 appears in MinIO." This asserts that and the rest of
# what M0 built, end to end, against the production-local profile.
#
# Deliberately the same script locally and in CI (`make exit-test`): a green CI
# badge should mean the exact thing a developer can reproduce in one command.
#
# Runs fully offline — PROVIDERS__MODE defaults to mock (NF7).
set -euo pipefail

cd "$(dirname "$0")/.."

COMPOSE=(docker compose --project-directory .
  -f docker/compose/docker-compose.yml
  -f docker/compose/compose.prod.yml)

BASE_URL="http://localhost:${HTTP_PORT:-8080}"
PASS=0
FAIL=0

pass() { printf '  \033[32m✓\033[0m %s\n' "$1"; PASS=$((PASS + 1)); }
fail() { printf '  \033[31m✗\033[0m %s\n' "$1"; FAIL=$((FAIL + 1)); }
step() { printf '\n\033[1m%s\033[0m\n' "$1"; }

check() { # check <description> <expected> <actual>
  if [ "$2" = "$3" ]; then pass "$1"; else fail "$1 — expected '$2', got '$3'"; fi
}

cleanup() {
  if [ "${KEEP_STACK:-0}" != "1" ]; then
    step "Tearing down"
    "${COMPOSE[@]}" down --remove-orphans >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

# --------------------------------------------------------------------------- #
step "1/8  Bringing up the production-local profile"
# --------------------------------------------------------------------------- #
"${COMPOSE[@]}" up -d --build >/dev/null
echo "  stack started"

# --------------------------------------------------------------------------- #
step "2/8  Every service healthy"
# --------------------------------------------------------------------------- #
# Services with a healthcheck must report healthy; one-shots must have exited 0.
DEADLINE=$((SECONDS + 240))
EXPECT_HEALTHY=(postgres redis minio backend frontend nginx
                worker-llm worker-media worker-core worker-render)
while :; do
  UNHEALTHY=""
  for svc in "${EXPECT_HEALTHY[@]}"; do
    state=$("${COMPOSE[@]}" ps "$svc" --format '{{.Health}}' 2>/dev/null | head -1)
    [ "$state" = "healthy" ] || UNHEALTHY="$UNHEALTHY $svc:${state:-missing}"
  done
  [ -z "$UNHEALTHY" ] && break
  if [ $SECONDS -ge $DEADLINE ]; then
    fail "services never became healthy:$UNHEALTHY"
    "${COMPOSE[@]}" ps
    break
  fi
  sleep 5
done
[ -z "$UNHEALTHY" ] && pass "all ${#EXPECT_HEALTHY[@]} long-running services healthy"

for oneshot in migrate minio-bootstrap; do
  code=$("${COMPOSE[@]}" ps -a "$oneshot" --format '{{.ExitCode}}' 2>/dev/null | head -1)
  check "one-shot '$oneshot' exited 0" "0" "${code:-none}"
done

# --------------------------------------------------------------------------- #
step "3/8  Migrations applied"
# --------------------------------------------------------------------------- #
REV=$("${COMPOSE[@]}" exec -T postgres \
  psql -U "${POSTGRES_USER:-videoforge}" -d "${POSTGRES_DB:-videoforge}" \
  -tAc "SELECT version_num FROM alembic_version;" 2>/dev/null | tr -d '[:space:]')
if [ -n "$REV" ]; then pass "alembic_version present ($REV)"; else fail "no alembic_version row"; fi

# Models and migrations must not have drifted (SADD §10.4).
if "${COMPOSE[@]}" run --rm --no-deps migrate \
     alembic -c /app/database/alembic.ini check >/dev/null 2>&1; then
  pass "alembic check: no model/migration drift"
else
  fail "alembic check reports drift"
fi

# --------------------------------------------------------------------------- #
step "4/8  Ping round-trips every queue (broker, routing, correlation)"
# --------------------------------------------------------------------------- #
PING_OUT=$("${COMPOSE[@]}" run --rm --no-deps worker-core python - <<'PY' 2>/dev/null
from videoforge_workers.celery_app import QUEUES
from videoforge_workers.ping import enqueue_ping

CID = "cid-exit-test"
results = {q: enqueue_ping(q, correlation_id=CID) for q in QUEUES}
ok = 0
for q, r in results.items():
    payload = r.get(timeout=60)
    if payload["queue"] == q and payload["correlation_id"] == CID:
        ok += 1
print(f"QUEUES_OK={ok}/{len(QUEUES)}")
PY
) || true
QUEUES_OK=$(printf '%s' "$PING_OUT" | grep -oE 'QUEUES_OK=[0-9]+/[0-9]+' | cut -d= -f2)
check "all queues round-trip with correlation propagated" "7/7" "${QUEUES_OK:-none}"

# --------------------------------------------------------------------------- #
step "5/8  hello.mp4 renders and lands in MinIO"
# --------------------------------------------------------------------------- #
RENDER_OUT=$("${COMPOSE[@]}" run --rm --no-deps worker-core python - <<'PY' 2>/dev/null
from videoforge_workers.render import hello_render
from videoforge_workers.skeleton import enqueue

p = enqueue(hello_render, queue="render", correlation_id="cid-exit-test").get(timeout=180)
print(f"KEY={p['key']}")
print(f"DURATION={p['duration_s']}")
print(f"CODECS={','.join(p['codecs'])}")
print(f"FASTSTART={p['moov_before_mdat']}")
PY
) || true
KEY=$(printf '%s' "$RENDER_OUT" | grep -oE '^KEY=.*' | cut -d= -f2-)
check "render duration" "5.6" "$(printf '%s' "$RENDER_OUT" | grep -oE '^DURATION=.*' | cut -d= -f2)"
check "codecs"          "aac,h264" "$(printf '%s' "$RENDER_OUT" | grep -oE '^CODECS=.*' | cut -d= -f2)"
check "moov before mdat (+faststart)" "True" "$(printf '%s' "$RENDER_OUT" | grep -oE '^FASTSTART=.*' | cut -d= -f2)"
if [ -n "$KEY" ]; then pass "hello.mp4 in MinIO (artifacts/$KEY)"; else fail "render produced no key"; fi

# --------------------------------------------------------------------------- #
step "6/8  Asset serving through nginx (ADR-011)"
# --------------------------------------------------------------------------- #
if [ -n "$KEY" ]; then
  ASSET_URL="$BASE_URL/assets/artifacts/$KEY"
  check "full fetch"        "200" "$(curl -s -o /dev/null -w '%{http_code}' "$ASSET_URL")"
  check "content type"      "video/mp4" "$(curl -s -o /dev/null -w '%{content_type}' "$ASSET_URL")"
  check "range request"     "206" "$(curl -s -o /dev/null -w '%{http_code}' -r 100-199 "$ASSET_URL")"
  check "internal location sealed" "404" \
    "$(curl -s -o /dev/null -w '%{http_code}' "$BASE_URL/internal-assets/artifacts/$KEY")"
  check "scratch bucket refused"   "403" \
    "$(curl -s -o /dev/null -w '%{http_code}' "$BASE_URL/assets/tmp-render/x.png")"
  if curl -s -D- -o /dev/null "$ASSET_URL" | grep -qi "cache-control:.*immutable"; then
    pass "immutable cache header"
  else
    fail "immutable cache header missing"
  fi
else
  fail "skipping asset checks — no render key"
fi

# --------------------------------------------------------------------------- #
step "7/8  UI and BFF (S6)"
# --------------------------------------------------------------------------- #
check "UI served through nginx" "200" "$(curl -s -o /dev/null -w '%{http_code}' "$BASE_URL/")"
BFF=$(curl -s "$BASE_URL/api/bff/health")
check "BFF reports healthy" "ok" "$(printf '%s' "$BFF" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])' 2>/dev/null || echo parse-error)"
check "BFF component count" "3" "$(printf '%s' "$BFF" | python3 -c 'import json,sys; print(len(json.load(sys.stdin)["components"]))' 2>/dev/null || echo parse-error)"

# The token must never reach the browser. Positive control included: a zero
# result is only evidence if the same method finds a string that IS present.
TOKEN=$("${COMPOSE[@]}" exec -T frontend printenv API_TOKEN | tr -d '[:space:]')
HTML=$(curl -s "$BASE_URL/")
LEAKS=$(printf '%s' "$HTML" | grep -c "$TOKEN" || true)
for chunk in $(printf '%s' "$HTML" | grep -oE '/_next/static/[^"]+\.js' | sort -u); do
  n=$(curl -s "$BASE_URL$chunk" | grep -c "$TOKEN" || true)
  LEAKS=$((LEAKS + n))
done
check "API_TOKEN absent from all browser-delivered assets" "0" "$LEAKS"
CONTROL=$(printf '%s' "$HTML" | grep -c "VideoForge" || true)
if [ "$CONTROL" -gt 0 ]; then
  pass "positive control: scan finds a string that is present"
else
  fail "positive control failed — the leak scan proves nothing"
fi

# --------------------------------------------------------------------------- #
step "8/8  NF8 — provider keys reach workers only"
# --------------------------------------------------------------------------- #
if "${COMPOSE[@]}" exec -T backend sh -c 'env | grep -q "^OPENAI_API_KEY="'; then
  fail "backend holds OPENAI_API_KEY"
else
  pass "backend holds no provider keys"
fi

# --------------------------------------------------------------------------- #
step "Result"
# --------------------------------------------------------------------------- #
printf '  %d passed, %d failed\n\n' "$PASS" "$FAIL"
[ "$FAIL" -eq 0 ] || exit 1
echo "  M0 exit test PASSED"
