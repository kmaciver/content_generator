# VideoForge

Local-first orchestration platform that turns a topic into a short-form
educational video — AI-generated illustrations, AI narration, captions, and
background music, assembled into a 1080×1920 MP4 by a deterministic FFmpeg
render.

Human-in-the-loop by design: every stage produces **immutable, versioned
artifacts** that you review and approve before the pipeline advances. Nothing
is ever overwritten.

> **Status: M0 and M1 complete; M2 in progress.** The foundation and the domain
> spine are built, tested, and verified end to end — topic → generate → reject
> → regenerate → edit → approve, with an audit trail that explains every step,
> run by CI on every push. M2 is adding the rest of the text pipeline: scenes
> and prompts, the pipeline DAG, derived project phase, and the staleness
> cascade. The decisions behind it are recorded in [`docs/adr/`](docs/adr/).

---

## Quickstart

You need **Docker** and **make**. Nothing else — not Python, not Node, not uv.
The toolchain is containerised ([ADR-014](docs/adr/ADR-014-containerised-toolchain.md)).

```bash
git clone git@github.com:kmaciver/content_generator.git
cd content_generator
make up-prod
```

That builds and starts the full stack. When it settles:

| URL | What |
|---|---|
| <http://localhost:8080> | The UI (component health map, for now) |
| <http://localhost:8080/api/v1/health/deep> | Component-by-component API health |
| <http://localhost:5555> | Flower — Celery queues and workers |

Verify the whole thing actually works:

```bash
make exit-test
```

That runs 22 assertions across the entire stack — services healthy, migrations
applied, all seven queues round-tripping, a real FFmpeg render landing in
MinIO, asset serving with byte ranges, the UI, the BFF, and secret isolation.

Then drive the review flow through a real browser:

```bash
make e2e
```

Topic → generate → reject → regenerate → edit → approve, through nginx, on the
mock provider — followed by reading the audit trail back to check it explains
every step. Both commands are what CI runs, against one boot of the same stack.

Everything runs offline. Provider calls default to `mock`, so no API key is
needed and nothing can cost money until you deliberately change that.

## Common commands

```bash
make help          # every target, described
make up            # development profile: API on :5010, stores exposed, hot reload
make up-prod       # production-local: nginx on :8080, uWSGI, no exposed stores
make down          # stop, keep data
make logs svc=backend
make check-all     # every quality gate: ruff, black, isort, mypy, pytest, eslint, tsc, prettier
make exit-test     # the full M0 verification (stack, queues, render, NF8)
make e2e           # M1's review flow in a real browser (needs `make up-prod`)
make migrate       # apply database migrations
make reset         # DESTRUCTIVE: wipe all volumes and start fresh
```

## Configuration

The stack boots with working defaults and **no `.env` at all**. To customise
or to use real providers:

```bash
make env-example && cp .env.example .env
```

Every variable is documented in [`docs/env-reference.md`](docs/env-reference.md).

Provider API keys reach **worker containers only** — never the API, the
frontend, or nginx. A compromised web tier cannot spend your provider credits.
That boundary is enforced three ways: separate settings models, static tests
over the compose topology and backend source, and a runtime canary
(`make verify-secrets`).

## How it fits together

```
Browser ──► nginx ─┬─► Next.js (UI + BFF; injects the API token server-side)
                   ├─► Flask/uWSGI (thin API: creates jobs, reads state)
                   └─► /assets/ ──► X-Accel-Redirect ──► MinIO

Flask ──► Postgres (state of record) ──► Redis (broker) ──► Celery workers
                                                             ├─ llm
                                                             ├─ image, voice
                                                             ├─ timeline, package, events
                                                             └─ render (FFmpeg)
```

Long-running work **never** happens in a request handler: the API creates a
durable job and returns immediately.

## Documentation

| Document | What it covers |
|---|---|
| [Architecture (SADD)](docs/architecture/sadd.md) | The full design, with M0 amendments marked |
| [ADRs](docs/adr/) | 16 decision records, including two superseded and one withdrawn |
| [Code tour](docs/code-tour.md) | A tracked reading plan — twelve stages, in the order that makes them stick |
| [Runbook](docs/runbook.md) | Operating it: recovery, diagnosis, routine tasks |
| [Development](docs/development.md) | Working on it, including an optional host environment |
| [Environment reference](docs/env-reference.md) | Every configuration variable |

## Project layout

```
apps/         backend (Flask API) · workers (Celery) · frontend (Next.js)
packages/     shared · persistence · domain · providers · prompts · timeline
database/     Alembic migrations and seed data
docker/       app · frontend · nginx · tooling · compose · redis · minio
docs/         architecture · adr · api
scripts/      exit test, secret-isolation canary
```

The dependency arrow points **apps → packages**, never sideways between apps —
a rule enforced by tests, not convention.
