# Environment variable reference

Canonical documentation for every environment variable the platform reads.

> **Note:** the tracked template file `.env.example` could not be written by the
> assistant — this environment blocks writes to `.env*` paths. Create it from the
> block at the bottom of this page (`make env-example` does it for you), then:
>
> ```bash
> cp .env.example .env
> ```
>
> `.env` is git-ignored. This page and `.env.example` must be kept in step; when
> you add a setting, document it here.

Nested settings use a double underscore, matching Pydantic Settings:
`PROVIDERS__LLM__ADAPTER=anthropic` → `providers.llm.adapter`.

## Resolution order (M0-04)

Lowest precedence first:

```
code defaults  →  config/providers.yaml (provider sections only)  →  environment
```

There is deliberately **no in-process `.env` handling**: the repo-root `.env` is
consumed by *docker compose*, which materialises it as real environment for
each container. By the time Python starts, the environment IS the
configuration — one door, one story.

The models live in `packages/shared/src/videoforge_shared/settings.py`.
Two aggregates exist, and the split is a security boundary (NF8):
`AppSettings` (what every service may read — the backend uses only this) and
`WorkerSettings` (adds provider selection and keys; workers only). Enforced
statically by `tests/test_secret_isolation.py` and at runtime by
`make verify-secrets`.

| Variable | Default | Notes |
|---|---|---|
| `PROVIDERS_CONFIG_FILE` | `config/providers.yaml` | Path to the provider-selection YAML (no secrets in it — it is tracked). Missing file = layer skipped. Mounted read-only at `/app/config/providers.yaml` in provider workers. |

## Core

| Variable | Default | Notes |
|---|---|---|
| `ENVIRONMENT` | `development` | `development` \| `production-local` |
| `LOG_LEVEL` | `INFO` | `DEBUG` \| `INFO` \| `WARNING` \| `ERROR` |
| `LOG_FORMAT` | `json` | `pretty` is development-only |

## Host port bindings (development profile)

Only the `development` profile publishes these; `production-local` exposes
nothing but nginx and Flower. All are optional — the defaults below apply when
unset.

| Variable | Default | Notes |
|---|---|---|
| `BACKEND_PORT_HOST` | `5010` | **Not 5000.** macOS Control Center (AirPlay Receiver) holds `:5000` from Monterey onward, and `500x` tends to be crowded by other local projects. |
| `POSTGRES_PORT_HOST` | `5432` | For `psql` / GUI clients. |
| `REDIS_PORT_HOST` | `6379` | For `redis-cli`. |
| `MINIO_PORT_HOST` | `9000` | S3 API. |
| `MINIO_CONSOLE_PORT_HOST` | `9001` | MinIO web console. |
| `HTTP_PORT` | `8080` | nginx, `production-local` only. |
| `FLOWER_PORT` | `5555` | Celery monitoring, both profiles. |

## API authentication (SADD §21.1)

| Variable | Notes |
|---|---|
| `API_TOKEN` | Single static bearer token for v1, enforced at nginx **and** again in Flask. The browser never holds it — the Next.js BFF injects it server-side (**S6**). Generate with `openssl rand -hex 32`. |
| `INTERNAL_HMAC_SECRET` | Shared secret for internal service-to-service calls. Separate from `API_TOKEN` so rotating one does not force rotating the other. |

## PostgreSQL — state of record

`POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB`, `POSTGRES_HOST`, `POSTGRES_PORT`

## Redis — broker, result backend, pub/sub

`REDIS_URL`, `CELERY_BROKER_URL`, `CELERY_RESULT_BACKEND`

Separate logical databases per role keeps a `FLUSHDB` during debugging from
taking out the broker along with the cache.

## MinIO — all binary artefacts

`MINIO_ENDPOINT`, `MINIO_ROOT_USER`, `MINIO_ROOT_PASSWORD`, and the bucket names
`MINIO_BUCKET_ARTIFACTS`, `MINIO_BUCKET_ASSETS`, `MINIO_BUCKET_PACKAGES`,
`MINIO_BUCKET_TMP_RENDER`.

## Provider selection (SADD §15.3)

`PROVIDERS__MODE` controls whether real APIs are called at all:

| Mode | Behaviour |
|---|---|
| `mock` | No network, deterministic fixtures. **Default**, and what CI uses. |
| `real` | Live provider calls. Costs money. |
| `record` | Live calls, captured as fixtures. |
| `replay` | Recorded fixtures only. |

Per-capability: `PROVIDERS__LLM__{ADAPTER,MODEL,TIMEOUT_S}`,
`PROVIDERS__IMAGE__{ADAPTER,MODEL}`, `PROVIDERS__VOICE__{ADAPTER,VOICE_ID}`.

> Word-level timestamps are a **hard requirement** for voice adapters, not a
> preference (**B3**/**S5**): scene boundaries and captions both depend on them.
> An adapter that cannot supply them is disqualified rather than degraded, and
> the contract test fails at configuration time.

## Provider API keys — worker containers only

`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `ELEVENLABS_API_KEY`, `STABILITY_API_KEY`

Compose passes this block **exclusively to worker services**. The API, frontend,
and nginx must never receive them (NF8, SADD §21.3) — a compromised web tier then
cannot spend your provider credits. **M0-04 asserts this** with a test that boots
the backend image and confirms the keys are absent from its environment.

Leave blank while `PROVIDERS__MODE=mock`.

## Cost controls (SADD §21.4, **S10**)

| Variable | Default | Notes |
|---|---|---|
| `DAILY_COST_LIMIT` | `10.00` | Job creation is refused once the day's *estimated* spend exceeds this. At ~20 images per video this is a real guard against a regeneration loop, not a theoretical one. **Not yet enforced** — the `daily_spend` counter lands with M3-11 (S10). |
| `COST_CURRENCY` | `USD` | ISO 4217 code the limit and all estimates are expressed in. **A label, not a conversion**: estimates come from price tables inside each adapter, and vendors publish those in USD. If your credits were bought in another currency, that is an FX matter between you and the vendor — it does not change what a call costs in list terms. Change this only alongside the price tables. |

## Rendering (**D4**: FFmpeg, not Remotion)

`RENDER_WIDTH` (1080), `RENDER_HEIGHT` (1920), `RENDER_FPS` (30),
`RENDER_CRF` (20), `RENDER_PRESET` (`medium`).

---

## `.env.example` contents

```dotenv
ENVIRONMENT=development
LOG_LEVEL=INFO
LOG_FORMAT=json

# openssl rand -hex 32
API_TOKEN=
INTERNAL_HMAC_SECRET=

POSTGRES_USER=videoforge
POSTGRES_PASSWORD=
POSTGRES_DB=videoforge
POSTGRES_HOST=postgres
POSTGRES_PORT=5432

REDIS_URL=redis://redis:6379/0
CELERY_BROKER_URL=redis://redis:6379/1
CELERY_RESULT_BACKEND=redis://redis:6379/2

MINIO_ENDPOINT=http://minio:9000
MINIO_ROOT_USER=videoforge
MINIO_ROOT_PASSWORD=
MINIO_BUCKET_ARTIFACTS=artifacts
MINIO_BUCKET_ASSETS=assets
MINIO_BUCKET_PACKAGES=packages
MINIO_BUCKET_TMP_RENDER=tmp-render

PROVIDERS__MODE=mock
PROVIDERS__LLM__ADAPTER=mock
PROVIDERS__LLM__MODEL=
PROVIDERS__LLM__TIMEOUT_S=120
PROVIDERS__IMAGE__ADAPTER=mock
PROVIDERS__IMAGE__MODEL=
PROVIDERS__VOICE__ADAPTER=mock
PROVIDERS__VOICE__VOICE_ID=

# Worker containers only -- never the API, frontend, or nginx.
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
ELEVENLABS_API_KEY=
STABILITY_API_KEY=

DAILY_COST_LIMIT=10.00
COST_CURRENCY=USD

RENDER_WIDTH=1080
RENDER_HEIGHT=1920
RENDER_FPS=30
RENDER_CRF=20
RENDER_PRESET=medium
```
