# Software Architecture Design Document (SADD)

## Short-Form Educational Video Orchestration Platform

| | |
|---|---|
| **Status** | Approved; amended during M0 |
| **Version** | 0.2.0 |
| **Amendments** | An architecture review before implementation raised seven blocking defects and eleven should-fixes; all were accepted and applied during M0. The resulting decisions are recorded in `docs/adr/`. Amended passages below are marked **[AMENDED M0]**. |
| **Type** | Architecture RFC |
| **Deployment target** | Local, Docker Compose |
| **Audience** | Engineering |

---

## 1. Executive Summary

This document proposes the architecture for a local-first, production-grade orchestration platform that generates short-form educational videos (Instagram Reels, TikTok, YouTube Shorts) from a topic prompt. The platform composes videos from AI-generated *illustrations*, AI narration, subtitles, and background music — deliberately avoiding AI video generation in v1 for cost, quality control, and determinism.

**[AMENDED M0]** Camera motion (Ken Burns pan/zoom) was removed from scope: videos are still images stitched together with voice-over and captions. That change is what led to replacing the render engine — see ADR-012.

The system is a **human-in-the-loop pipeline**: a directed workflow of generation stages (research → script → scenes → images → voice → timeline → render → publishing package), where each stage produces **immutable, versioned artifacts** that a human reviews and approves before the workflow advances. Long-running work executes exclusively in Celery workers; the Flask API only creates durable jobs and reads state. Rendering is fully deterministic: a **Timeline JSON** document is compiled from approved artifacts and rendered to MP4 by **FFmpeg, inside an ordinary Celery task** on the `render` queue (**[AMENDED M0 — ADR-012]**; originally Remotion in a dedicated Node container).

Key architectural positions taken in this document:

1. **Artifacts are the unit of approval and versioning; jobs are the unit of execution.** These are separate models with separate lifecycles. Nothing is ever overwritten; regeneration always produces a new version.
2. **The state machine is layered, not monolithic.** A single linear project FSM cannot cleanly express partial regeneration ("redo image for scene 4 only"), parallel stages (images and voice can generate concurrently), or per-artifact rejection. We model (a) a coarse project *phase*, (b) per-artifact lifecycles, and (c) per-job execution states. This is a deliberate deviation from the linear FSM sketched in the brief, with rationale in §12.
3. **The pipeline is a DAG, not a strict chain.** Image generation and voice generation both depend only on approved scenes and can run in parallel, cutting wall-clock time per video roughly in half. The DAG is declared in configuration, not hard-coded in workers.
4. **Providers (LLM, image, voice, …) are the only external dependencies**, hidden behind narrow interfaces, selected by configuration, and exercised through contract tests and recorded fixtures so the entire pipeline runs offline in CI.
5. **State changes and event publication are made atomic** via a transactional outbox, so the database is always the source of truth and events can never be lost or duplicated inconsistently.
6. **[AMENDED M0 — decision D4]** The renderer is **FFmpeg invoked from an ordinary Celery task**, not an isolated Node service. With motion removed and captions confirmed to be single-word sequential display (which ASS renders natively), Remotion's per-frame Chromium cost bought nothing. This deleted the Redis Streams contract, the HMAC callback endpoint, the renderer heartbeat, and finding B5 along with them. See ADR-012, which supersedes ADR-005 and ADR-007.

Two Compose profiles are provided: `development` (hot reload, direct ports) and `production-local` (Nginx + uWSGI, hardened settings), sharing the same images and topology so that "works in dev" strongly predicts "works in prod-local".

Risks called out explicitly (§25): uWSGI is in maintenance mode and its interpreter compatibility must be pin-verified in CI (ADR-002 — **[AMENDED M0]** resolved: Python 3.12, verified); Celery-on-Redis has delivery semantics (visibility timeout, at-least-once) that force idempotent task design (§14). **[AMENDED M0]** The Remotion licensing risk (R2) is retired with ADR-012.

## 2. Goals

- **G1 — Automated end-to-end pipeline.** From topic to a downloadable publishing package with no manual editing tools (CapCut excluded from the production path).
- **G2 — Human approval gates.** Every creative artifact (research, script, scenes, images, voice, render) is reviewable, approvable, rejectable, regenerable, editable, and commentable from the frontend.
- **G3 — Immutable, versioned artifacts.** Full history of everything generated; rejection never destroys work; any released video is exactly reproducible from its recorded inputs.
- **G4 — Deterministic local rendering. [AMENDED M0]** Scene timing, captions, transitions, audio mixing, and MP4 export are computed locally by **FFmpeg** from a Timeline JSON. Same timeline + same assets ⇒ byte-stable visual output. (Motion removed; Remotion superseded — ADR-012.)
- **G5 — Provider independence.** Swapping OpenAI ↔ Anthropic ↔ local Ollama (LLM), or ElevenLabs ↔ OpenAI TTS (voice), or DALL·E ↔ Stability ↔ Flux (image) is a configuration change, not a code change.
- **G6 — Local-first infrastructure.** `git clone && docker compose up` yields a fully working platform. No cloud services except AI provider APIs.
- **G7 — Maintainability & testability.** Layered backend (ORM / repositories / domain / services / DTOs), migrations, seed data, mocked providers, and an offline CI story.
- **G8 — Evolvability.** Clean seams for future animation providers, music generation, research providers, auto-publishing, multi-user accounts, and cloud migration.

## 3. Non-Goals

- **N1 — AI video generation** (Runway, Sora, Kling, etc.). Explicitly out of scope for v1; the `AnimationProvider` seam exists but is unimplemented.
- **N2 — Automatic publishing to social platforms.** v1 ends at the publishing package. `PublishingProvider` is a future seam.
- **N3 — Multi-tenant SaaS.** Single operator, local deployment. The schema anticipates workspaces/users, but no auth federation, billing, or tenant isolation is built.
- **N4 — Cloud deployment.** No AWS/K8s manifests. §24 documents the migration path so nothing painted-into-a-corner, but nothing is built.
- **N5 — Real-time collaboration.** No concurrent multi-editor semantics; last-write-wins with optimistic locking on review actions is sufficient.
- **N6 — A general-purpose video editor.** The frontend reviews artifacts and tweaks constrained timeline parameters (pan/zoom presets, caption styles); it is not a track-based NLE.
- **N7 — Horizontal scale.** Single-host throughput (a handful of concurrent video projects) is the target. Scaling paths are documented, not implemented.

## 4. Requirements

### 4.1 Functional

| ID | Requirement |
|----|-------------|
| F1 | Create a VideoProject from a topic (optionally within a Series that carries style presets). |
| F2 | Generate research for a topic via a ResearchProvider/LLMProvider; store as versioned artifact. |
| F3 | Generate a narration script from approved research; versioned. |
| F4 | Break an approved script into scenes (narration text per scene, visual description, target duration); versioned per scene set. |
| F5 | Generate one image prompt per scene from approved scenes; versioned. |
| F6 | Generate illustrations per scene via ImageProvider; store originals in MinIO; versioned per scene. |
| F7 | Generate narration audio via VoiceProvider with word/segment timestamps; versioned. |
| F8 | **[AMENDED M0]** Compile a Timeline JSON from approved artifacts (scene durations from voice timing, captions, music track, transitions). No pan/zoom — see §1.0 of the implementation plan. |
| F9 | **[AMENDED M0]** Render Timeline JSON to MP4 (1080×1920, H.264/AAC) via **FFmpeg** (ADR-012); store render + thumbnail in MinIO; versioned. |
| F10 | Assemble a downloadable publishing package (zip: video, thumbnail, caption text, hashtags, metadata, assets). |
| F11 | Frontend review screens for research, script, scenes, images, voice, render — each with Approve / Reject / Regenerate / Edit / Comment. |
| F12 | Regeneration (whole stage or a single scene's image/prompt) creates new artifact versions; prior versions retained and browsable. |
| F13 | Manual edit of a text artifact creates a new version with `origin=human_edit`. |
| F14 | Job progress visibility (queued / running / failed / done) with retry from the UI. |
| F15 | Full audit trail: every state transition, review decision, job execution, and provider call is recorded. |

### 4.2 Non-Functional

| ID | Requirement |
|----|-------------|
| NF1 | `docker compose --profile development up` brings up the full stack on a clean machine with only Docker installed. |
| NF2 | Flask request handlers must respond < 500 ms p95; all generation happens in workers. |
| NF3 | At-least-once job execution with idempotent tasks; a crashed worker never loses a job or corrupts state. |
| NF4 | Artifacts immutable at the storage layer (object versioning conventions + DB constraints). |
| NF5 | Reproducibility: a render is fully determined by (timeline JSON hash, asset content hashes, renderer image digest). |
| NF6 | All provider calls logged with tokens/cost metadata (ProviderUsage). |
| NF7 | Entire test suite runs offline (mock providers, recorded fixtures). |
| NF8 | Secrets never baked into images; provider keys visible only to worker containers. |
| NF9 | Observability: structured JSON logs with request/job correlation IDs; Flower for Celery; health endpoints per service. |
| NF10 | **[AMENDED M0]** A 60 s video (≈15–25 scenes — see plan §1.0.1) renders in ≤ ~5 min on a modern laptop. Comfortably met: FFmpeg renders in seconds, not minutes (ADR-012). |

## 5. High-Level Architecture

The system decomposes into five planes:

1. **Edge / UI plane** — Nginx (prod-local) fronting Next.js (review UI) and the Flask API. Serves/streams rendered MP4s and proxies MinIO assets.
2. **Control plane (API)** — Flask + uWSGI. Owns the HTTP contract: project CRUD, review actions, job creation, artifact listing, SSE/polling for progress. Thin: validates (Pydantic DTOs), calls application services, returns. Never generates anything.
3. **Execution plane (workers)** — Celery workers segmented by queue (`llm`, `image`, `voice`, `timeline`, `package`, `events`) plus the **Node renderer worker** on its own Redis stream. Workers are fire-and-forget: consume → call provider / compute → persist artifact (MinIO + Postgres) → update state → publish event → exit. No worker ever blocks waiting on another job.
4. **State plane** — PostgreSQL (single source of truth: entities, versions, jobs, transitions, audit, outbox), Redis (broker + result backend + pub/sub fanout), MinIO (all binary artifacts, content-addressed keys).
5. **Provider plane** — the only external boundary. `LLMProvider`, `ImageProvider`, `VoiceProvider` (+ future seams) behind Python protocols, chosen via configuration, with per-provider adapters, retries, and usage metering.

### 5.1 Canonical request flow (e.g., "Generate script")

1. UI `POST /api/v1/projects/{id}/script/generations` → Flask validates phase & inputs, inserts `GenerationJob(status=QUEUED)` **and** an outbox row in one transaction, enqueues Celery task, returns `202 {job_id}` immediately.
2. `script_worker` consumes the task: loads approved research (repository), calls `LLMProvider.complete()` through the prompt package, writes the script text to MinIO, inserts `ArtifactVersion` + `ScriptVersion`, flips job → `SUCCEEDED`, advances artifact state → `AWAITING_APPROVAL`, writes `StateTransition` + `AuditEvent` + outbox event — all in one DB transaction.
3. The `events` worker drains the outbox → publishes to Redis pub/sub → SSE endpoint pushes to the browser (or the UI polls).
4. Reviewer approves → `POST /reviews` records `ReviewDecision`, artifact → `APPROVED`, project phase recomputed; the next stage's "Generate" button unlocks (or auto-triggers, per Series policy).

## 6. Architecture Diagram

```
                                   ┌────────────────────────────────────────────┐
                                   │                 Browser                    │
                                   └─────────────────────┬──────────────────────┘
                                                         │ :80 (prod-local) / :3000+:5000 (dev)
                                   ┌─────────────────────▼──────────────────────┐
                                   │                   NGINX                    │
                                   │  /            → next:3000 (proxy)          │
                                   │  /api/        → uwsgi_pass backend:5000    │
                                   │  /assets/     → auth_request → minio:9000  │
                                   │  /api/events  → SSE (buffering off)        │
                                   └───────┬─────────────────────┬──────────────┘
                                           │                     │
                              ┌────────────▼───────┐   ┌─────────▼─────────┐
                              │  Next.js frontend  │   │  Flask API (uWSGI)│
                              │  React Query, TS   │   │  DTOs → services  │
                              └────────────────────┘   └───┬───────┬───────┘
                                                           │       │ enqueue
                                             SQL (txn:     │       ▼
                                             job+outbox)   │   ┌───────────┐
                                                           │   │   Redis    │
                                                           │   │ broker/    │
                                                           │   │ pubsub/    │
                                                           │   │ streams    │
                                                           │   └─┬───────┬──┘
                                                           │     │       │ render.jobs (stream)
                        ┌──────────────────────────────────▼─┐   │       │
                        │             PostgreSQL              │   │  ┌────▼──────────────────┐
                        │ entities · versions · jobs · outbox │   │  │  Renderer (Node)      │
                        │ transitions · audit · usage         │   │  │  FFmpeg (in worker)   │
                        └──────────────────▲──────────────────┘   │  │  + FFmpeg             │
                                           │                      │  └────┬─────────▲────────┘
                     ┌─────────────────────┴───────────┐          │       │ mp4/    │ assets
                     │        Celery workers           ◄──────────┘       │ thumb   │
                     │ queues: llm │ image │ voice │    │                  │         │
                     │ timeline │ package │ events      │            ┌─────▼─────────┴─────┐
                     └───┬─────────────────────────▲────┘            │        MinIO        │
                         │ provider calls          │ artifacts       │  content-addressed  │
                 ┌───────▼───────────────┐         └────────────────►│  buckets            │
                 │   Provider plane      │                           └─────────────────────┘
                 │ LLM │ Image │ Voice   │            Flower :5555 (Celery monitoring)
                 │ (external APIs only)  │
                 └───────────────────────┘
```

Dev profile removes Nginx (direct ports) and runs Flask/Next with reload; topology otherwise identical.


## 7. Technology Decisions

Each decision: choice, rationale, trade-offs, alternatives, and evolution. Longer discussions are formalized as ADRs (§26).

### 7.1 Flask + uWSGI (constraint honored, with eyes open)

**Choice:** Flask 3.x served by uWSGI behind Nginx (`uwsgi_pass`, binary uwsgi protocol over a unix socket).

**Rationale:** Flask's synchronous model is a *good fit* here precisely because the API is thin — every slow operation is delegated to Celery, so request handlers are short DB reads/writes. uWSGI + Nginx over the uwsgi protocol avoids an HTTP hop, gives mature process management (prefork, cheaper, harakiri, memory recycling), and is the stack you asked for.

**Trade-offs / honest challenge:** uWSGI has been in **maintenance mode since 2022**. **[AMENDED M0 — decision D1]** Python 3.13 removed the interpreter-init and thread-state C API that uWSGI's Python plugin drives by hand, so the interpreter is pinned to **3.12** and uWSGI to **2.0.31**, verified under concurrent load (ADR-002). SSE through synchronous workers ties up a worker per open connection, so SSE runs in a *dedicated small uWSGI listener/pool* or falls back to polling (§19.6). Pydantic v2 pairs more naturally with FastAPI, but we get the same boundary validation by hand-wiring Pydantic DTOs into Flask views — a small, contained cost.

**Alternatives considered:** Gunicorn (excluded by requirement), FastAPI+Uvicorn (better async/SSE story, rejected to honor the stack; the layered design makes a later swap a transport-layer change only), Waitress (simpler, fewer prod controls).

**Evolution:** because views only translate HTTP ⇄ DTOs ⇄ services, replacing Flask/uWSGI later touches ~one package.

### 7.2 Celery + Redis

**Choice:** Celery 5.x, Redis broker + result backend, multiple named queues, one logical worker role per queue.

**Rationale:** Mature, well-understood, first-class Flower support (required), per-queue concurrency control (e.g., `image` queue concurrency capped to respect provider rate limits; `llm` higher).

**Trade-offs:** Redis broker = at-least-once with visibility-timeout redelivery; tasks **must be idempotent** (§14.3). Redis persistence must be configured (AOF `everysec`) or a host crash can drop queued (not yet started) jobs — mitigated because `GenerationJob` rows in Postgres are the durable record and a reconciler re-enqueues orphans (§14.4).

**Alternatives:** RabbitMQ (better delivery semantics, one more service to run — deferred; Celery makes the broker swappable via config), Dramatiq/RQ (lighter but weaker ecosystem/Flower), Postgres-as-queue e.g. Procrastinate (fewer moving parts, weaker tooling).

### 7.3 PostgreSQL + SQLAlchemy 2.0 + Alembic

Typed 2.0-style ORM (`Mapped[]`, `mapped_column`), one Alembic head, autogenerate + hand review, migrations run by a dedicated one-shot `migrate` compose service so app containers never race to migrate. JSONB for provider payloads/timeline snapshots; strict relational modeling for versioning and audit (§10).

### 7.4 MinIO

S3-compatible, local, presigned-URL capable — makes the eventual cloud move (real S3) a config change. All binaries (images, audio, MP4s, zips, even large text artifacts) live here; Postgres stores metadata + content hashes only. Keys are content-addressed (§18.3) which gives immutability, dedup, and cache-forever semantics.

### 7.5 FFmpeg renderer  **[AMENDED M0 — superseded by ADR-012]**

> The original text below chose Remotion in an isolated Node service. Decision
> **D4** replaced it with FFmpeg inside a Celery task after motion was removed
> from scope (see the amended §1 above) and the reference captions proved to be
> single-word sequential display, which ASS renders natively. ADR-012 records
> the replacement; ADR-005 and ADR-007 are superseded. Retained for context:

Remotion renders React compositions to MP4 via headless Chromium and bundles FFmpeg for encoding. It is the strongest fit for "video as code from JSON": captions, pan/zoom easings, and transitions are ordinary React components driven entirely by the Timeline JSON — reviewable, diffable, unit-testable.

**Licensing (must acknowledge):** Remotion is source-available, **free for individuals and small teams but requires a paid company license beyond a threshold** — verify current terms before commercial use (ADR-007 records the pure-FFmpeg fallback: zoompan + ASS subtitles + filter graphs; markedly worse DX and typography).

**Isolation decision:** the renderer is its own container/service with Node + Chromium + FFmpeg, consuming a Redis Stream — *not* a subprocess of Python workers — because (a) dependency surfaces don't overlap, (b) renders are the heaviest jobs and the first thing to scale/offload, (c) crash isolation (Chromium OOM shouldn't kill Celery). See §16.

### 7.6 Next.js + TypeScript + Tailwind + React Query

App Router, server components for shells, client components for review UIs. React Query is the backbone of the async-job UX: mutations create jobs; queries poll job/artifact endpoints with `refetchInterval` until terminal state; SSE (when enabled) invalidates queries instead of carrying payloads (§20).

### 7.7 Schema sharing across languages

**[AMENDED M0 — withdrawn, ADR-008]** This section assumed the timeline crossed a Python ⇄ TypeScript boundary, because the renderer was TypeScript. Decision **D4** made the renderer Python, so the timeline compiler and its only consumer now share the same models in the same repository — there is no boundary to guard. `packages/schemas`, `datamodel-code-generator`, and `json-schema-to-typescript` are not introduced; plain Pydantic models are the single definition. Revisit only if a second language ever consumes the timeline directly.

*Original text:* JSON Schema documents in `/packages/schemas` as source of truth, code-generated into Pydantic and TS types at build time, preventing drift between the Python compiler and the TS renderer.

## 8. Repository Organization

Monorepo. One clone = whole system; atomic cross-cutting changes (e.g., timeline schema bump touches compiler, renderer, and frontend in one PR); single CI pipeline.

```
/
├── apps/
│   ├── frontend/                 # Next.js app (TS, Tailwind, React Query)
│   ├── backend/                  # Flask API + application core
│   │   └── src/videoforge/
│   │       ├── api/              # Blueprints, request/response DTO wiring, error handlers
│   │       ├── dto/              # Pydantic v2 request/response models
│   │       ├── services/         # Application services (use-cases, transactions)
│   │       ├── domain/           # [AMENDED M1-02 — moved to packages/domain, ADR-015]
│   │       ├── repositories/     # [AMENDED M1-03 — moved to packages/persistence]
│   │       ├── orm/              # [AMENDED M0-07 — moved to packages/persistence, ADR-013]
│   │       ├── events/           # Outbox writer, event types, publisher
│   │       └── config/           # Pydantic Settings, provider registry wiring
│   └── workers/                  # Celery app + task modules per stage
│       └── src/videoforge_workers/{celery_app.py, skeleton.py, ping.py, render.py,
│            research.py, script.py, scenes.py, prompts.py, images.py, voice.py,
│            timeline.py, package.py, events.py, reconciler.py}
│                                  # [AMENDED M0] apps/renderer/ does not exist:
│                                  # rendering is render.py on the `render` queue (ADR-012)
├── packages/                    # [AMENDED M0 — ADR-013]
│   ├── providers/                # Provider protocols + adapters (openai, anthropic, elevenlabs,
│   │                             #  stability, mock/) — importable by backend & workers
│   ├── prompts/                  # Versioned prompt templates (Jinja2) + rendering helpers
│   ├── timeline/                 # Timeline compiler: approved artifacts → Timeline JSON
│   ├── persistence/              # SQLAlchemy Base, engine/session factories, the 13 core
│   │                             #  ORM models (M1-01), and the repositories (M1-03). Lives
│   │                             #  here, not in the backend: workers write to the same
│   │                             #  schema and the apps must not import each other.
│   ├── domain/                   # [NEW M1-02 — ADR-015] Pure workflow rules: artifact FSM,
│   │                             #  job FSM, ApprovalPolicy. No SQLAlchemy, Flask, Celery,
│   │                             #  clock, or I/O — workers cause transitions too, so the
│   │                             #  rules cannot live under the backend.
│   └── shared/                   # Logging, ids (ULID), hashing, storage client, correlation,
│                                 #  AND settings — workers need them as much as the API does.
│                                 #  packages/schemas/ was withdrawn (ADR-008).
├── database/
│   ├── migrations/               # Alembic env + versions (single head)
│   └── seed/                     # Deterministic dev seed (demo series/project/fixtures)
├── docker/
│   ├── nginx/                    # nginx.conf, conf.d/, mime tweaks
│   ├── postgres/  redis/  minio/ # init scripts, redis.conf (AOF), bucket bootstrap
│   └── compose/                  # docker-compose.yml + compose.dev.yml + compose.prod.yml
├── docs/
│   ├── architecture/             # this SADD, diagrams (source + rendered)
│   ├── adr/                      # ADR-001..NNN (MADR format)
│   └── api/                      # OpenAPI spec (generated from DTOs) + examples
├── scripts/                      # dev.sh, seed.sh, reset.sh, fixtures-record.sh, lint/test wrappers
├── assets/                       # music library (licensed), fonts, brand elements
├── templates/                    # Series style presets, caption themes, hashtag templates
└── tests/                        # cross-cutting e2e/integration (unit tests live beside code)
```

**Rationale highlights:**
- `apps/` vs `packages/`: apps are deployable containers; packages are import-only libraries — the dependency arrow always points apps → packages, never sideways between apps. Backend and workers share domain logic *only* through packages + the database, preventing the classic "worker imports Flask app" tangle.
- `orm/` vs `domain/`: SQLAlchemy models are persistence records; domain models hold rules (e.g., "may this artifact be approved in this phase?") and are unit-testable without a DB.
- `packages/prompts` is separate and versioned because prompt text is a *behavioral input* to the system: every ArtifactVersion records the prompt template version that produced it (reproducibility, F15/NF5).
- `database/` at root (not inside backend) because migrations are an infrastructure concern executed by their own compose service, and seed data is shared by tests and dev.
- `docker/compose` keeps profile files together; `docker compose` is run via a thin `Makefile`/`scripts/dev.sh` so contributors don't memorize `-f` chains.
- `templates/` vs `assets/`: templates are structured config (JSON/YAML presets) that the app reads; assets are binary media bundled for local use.

## 9. Docker Architecture

### 9.1 Services

| Service | Image / build | Responsibilities | Ports (host) | Volumes | Depends on (healthy) |
|---|---|---|---|---|---|
| `nginx` | nginx:alpine + conf | Edge proxy, uwsgi_pass, asset auth proxy, MP4 range streaming, security headers | 80, 443 | conf (ro), tls (ro) | backend, frontend, minio |
| `frontend` | apps/frontend | Review UI | 3000 (dev only) | src bind-mount (dev) | backend |
| `backend` | apps/backend | Flask API via uWSGI (prod-local) / flask run --reload (dev) | 5000 (dev only) | src (dev), uwsgi socket vol (prod) | postgres, redis, minio, migrate |
| `migrate` | apps/backend | One-shot `alembic upgrade head`; exits 0 | — | — | postgres |
| `worker-llm` | apps/workers | research/script/scenes/prompts tasks (queue `llm`) | — | — | postgres, redis, minio, migrate |
| `worker-media` | apps/workers | image + voice tasks (queues `image`,`voice`) | — | — | same |
| `worker-core` | apps/workers | timeline, package, events(outbox), reconciler beat (queues `timeline`,`package`,`events`) | — | — | same |
| `worker-render` | apps/workers | **[AMENDED M0]** Celery worker on queue `render`; FFmpeg → MinIO upload (ADR-012). No Node, no Chromium, no stream contract. | — | — | same |
| `frontend` | apps/frontend | **[AMENDED M0]** Next.js UI + BFF route handlers (S6) | 3000 (dev only) | src (dev) | backend |
| `postgres` | postgres:16 | State of record | 5432 (dev) | pgdata vol | — |
| `redis` | redis:7 + redis.conf | Broker, results, pub/sub, streams (AOF everysec) | 6379 (dev) | redisdata vol | — |
| `minio` | minio + mc bootstrap | Object storage; bootstrap job creates buckets/policies | 9000, 9001 (dev) | miniodata vol | — |
| `flower` | mher/flower | Celery monitoring | 5555 | — | redis |

Worker containers share one image (different `celery -Q` args) — one build, role-per-container isolation, per-queue scaling (`docker compose up --scale worker-media=2`).

### 9.2 Health checks & startup order

- postgres: `pg_isready`; redis: `redis-cli ping`; minio: `mc ready local` (via live endpoint); backend: `GET /api/v1/health` (checks DB + Redis + MinIO reachability, returns component map); workers: `celery inspect ping` wrapper; renderer: heartbeat key `renderer:heartbeat` refreshed each loop, healthcheck reads it; nginx: `service_healthy` deps + `nginx -t` at build.
- `depends_on: condition: service_healthy` gives order: stores → migrate (completes) → backend/workers/renderer → nginx. Apps still retry connections at runtime (compose ordering is a convenience, not a guarantee).

### 9.3 Profiles

- **`development`**: no nginx; `flask --debug` (auto-reload) on **:5010** (macOS Control Center holds :5000), `next dev` on :3000, source bind-mounts, host ports open on Postgres/Redis/MinIO for tooling, verbose pretty logs. **[AMENDED M0]** No Remotion Studio — there is no Remotion (ADR-012); iterate with short test renders instead, which take about a second.
- **`production-local`**: nginx is the only entry point; uWSGI over unix socket; `next build && next start`; no source mounts; no store ports exposed; JSON logs; resource limits (`mem_limit` on renderer/chromium); restart policies `unless-stopped`.

Two profiles exist because the dev loop (reload speed, direct DB access) and the fidelity loop (verifying uWSGI/Nginx/socket behavior, header handling, MP4 streaming) have conflicting needs; sharing one compose topology + images keeps drift near zero. CI runs the `production-local` profile for e2e.


## 10. Database Design

### 10.1 Layering

```
HTTP ──► DTOs (pydantic) ──► Application Services ──► Domain models + policies
                                    │                        ▲
                                    ▼                        │ map
                              Repositories ──► SQLAlchemy ORM models ──► PostgreSQL
```

- **ORM models** (`orm/`): tables, columns, relationships, constraints. No business methods.
- **Repositories**: the only code that touches the session. Expose intent (`artifact_versions.latest_for(project, kind)`, `jobs.claim_orphans(older_than)`), return domain objects, own query shape. Unit-of-work: services open one transaction per use case.
- **Domain models** (`domain/`): dataclasses + the state machines and approval policies. Pure; property-based-testable. **[AMENDED M1-02 — ADR-015]** These live in `packages/domain`, not under the backend: §12.2's transitions are caused by job success and failure, which happen in workers, and workers must never import the backend. Same contradiction and same resolution as the data layer (ADR-013).
  Repositories return **ORM models** rather than separate domain entities — `videoforge_domain` holds *rules*, not entities, so there is nothing to map to. The property that matters, ORM objects never reaching the API, is enforced at the DTO boundary instead.
- **DTOs** (`dto/`): Pydantic v2 request/response shapes; OpenAPI generated from them. DTOs never leak ORM objects.
- **Services**: orchestration — validate phase, write job + outbox atomically, enqueue, record audit.

This costs some mapping boilerplate; it buys DB-free domain tests, a stable API contract, and swappability at every seam. For an app whose core complexity is *workflow rules*, the trade is clearly favorable.

### 10.2 Entity model (core tables)

All PKs are ULIDs (sortable, mergeable, URL-safe). All tables carry `created_at`; mutable tables carry `updated_at`. Immutable tables (`*_version`, transitions, audit, usage, decisions) have **no** update path — enforced by a Postgres trigger raising on UPDATE.

```
workspace (id, name, settings jsonb)
app_user (id, workspace_id→, email, display_name, role)                -- future-proofing; seeded single user in v1
        -- [AMENDED M0 — finding B7] named app_user, not `user`: USER is a reserved
        -- SQL keyword and would need quoting in every hand-written query.
series (id, workspace_id→, title, style_preset jsonb, voice_preset jsonb,
        music_policy jsonb, hashtag_template, auto_approve_policy jsonb)
video_project (id, series_id→?, workspace_id→, topic, title, phase, phase_updated_at,
               active_pointers jsonb, settings jsonb)
        -- phase: coarse enum (§12); active_pointers caches approved-version ids per artifact kind

artifact (id, project_id→, kind enum{research,script,scene_set,scene,prompt,image,voice,
          timeline,render,package,music}, scene_ref?→scene.id, state enum, current_version_no int,
          stale_since timestamptz?)
        -- [AMENDED M0] finding S1: UNIQUE NULLS NOT DISTINCT (project_id, kind, scene_ref),
        --   else two artifacts of one kind make active_pointers ambiguous.
        -- finding S2: stale_since gives §12.4's staleness cascade a column to live in
        --   (a nullable timestamp, not a boolean — the UI wants to show *when*).
artifact_version (id, artifact_id→, version_no int, origin enum{generated,human_edit,import},
          generation_job_id?→, parent_version_id?→artifact_version.id,
          storage_key?, content_hash, inline_content jsonb?, meta jsonb,
          prompt_template_ref?, provider_ref?, created_by→user, created_at)
        UNIQUE(artifact_id, version_no); immutable

research(id, artifact_id→ …)  script(id, artifact_id→)  script_version(id, artifact_version_id→,
          text_key, word_count, reading_time_s)          -- thin typed extensions where useful
scene_set(id, artifact_id→, script_version_id→)
scene (id, scene_set_id→, index int, narration_text, visual_brief, target_duration_ms)
prompt (artifact for scene: prompt_text, negative_prompt, style_tags jsonb)
generated_asset (artifact_version extension for binaries: mime, width, height, duration_ms,
          bytes, storage_key, content_hash)              -- images, audio, mp4, zip
voice_track (…, timestamps_key /* word+segment timing JSON in MinIO */, voice_id, sample_rate)
music_track (id, library_ref, title, license_ref, duration_ms, storage_key)
timeline (artifact_version extension: schema_version, timeline_key, input_snapshot jsonb
          /* exact version ids of every input artifact */)
render (artifact_version extension: timeline_version_id→, renderer_image_digest,
          codec_params jsonb, mp4_key, thumbnail_key, duration_ms, fps, filesize)
publishing_package (…, zip_key, manifest jsonb)

generation_job (id, project_id→, artifact_id→?, task_name, queue, status enum{QUEUED,RUNNING,
          SUCCEEDED,FAILED,CANCELLED,ORPHANED}, attempt int, max_attempts, celery_task_id,
          input_snapshot jsonb, error jsonb?, idempotency_key UNIQUE, queued_at, started_at?, finished_at?)
provider_usage (id, job_id→, provider, model, operation, input_tokens?, output_tokens?,
          images?, audio_seconds?, unit_cost_estimate numeric, latency_ms, raw_meta jsonb)
review_decision (id, artifact_version_id→, decision enum{APPROVE,REJECT}, comment,
          reviewer_id→, decided_at)                       -- immutable
comment (id, artifact_version_id→, author_id→, body, anchor jsonb?)   -- non-decision notes
state_transition (id, subject_type enum{project_phase,artifact,job}, subject_id, from_state,
          to_state, cause enum{job_succeeded,job_failed,review,edit,system,reconciler},
          actor_id?, job_id?, correlation_id, created_at) -- immutable
audit_event (id, event_type, subject_type, subject_id, actor_id?, payload jsonb,
          correlation_id, created_at)                     -- immutable, superset log
outbox_event (id, event_type, payload jsonb, created_at, published_at?)  -- §14.5
```

### 10.3 Versioning & audit rules

1. **Artifact = identity; ArtifactVersion = content.** Approval, rejection, and comments attach to a *version*. The artifact row tracks lifecycle state and the current version counter only.
2. **Regeneration** inserts version N+1 with `parent_version_id` = the version it replaces (lineage graph → "show me how script evolved"). Rejected versions remain queryable forever.
3. **Human edits** are versions with `origin=human_edit` — indistinguishable in pipeline mechanics, distinguishable in audit.
4. **Reproducibility chain:** render.version → timeline.version (whose `input_snapshot` pins exact image/voice/scene version ids + content hashes) → each version pins `prompt_template_ref`, `provider_ref`, and provider params in `meta`. NF5 satisfied by construction.
5. **[AMENDED M0 — finding B1]** Version status is **derived, not stored**. `artifact_version` has no status column and is immutable (a trigger raises on UPDATE), so "mark siblings SUPERSEDED" was unimplementable as written. `APPROVED` / `REJECTED` / `SUPERSEDED` / `AWAITING_APPROVAL` are computed from `review_decision` rows plus the active pointer, exposed through an `artifact_version_status` view so the API and domain layer share one definition. `video_project.active_pointers` remains a cache, always recomputable.
6. Every write path that changes state also inserts `state_transition` and `audit_event` in the same transaction. No trigger magic for transitions (explicit in services/workers) — triggers only *enforce* immutability.

### 10.4 Migrations & seed

Alembic single-head, autogenerate + mandatory human review, `alembic check` in CI; enum changes via explicit `ALTER TYPE` migrations. `database/seed` provides a deterministic demo workspace/series/project with pre-recorded fixture artifacts so the review UI is explorable without any provider key.

## 11. Domain Model

The domain layer expresses the rules the database merely stores:

- **`ProjectPhase` / `pipeline DAG`** — a declarative graph: each stage lists `requires` (artifact kinds that must be APPROVED), `produces` (artifact kind), `queue`, `parallelizable_per_scene: bool`. Loaded from `templates/pipeline.yaml`; the same structure drives phase computation, UI gating, and worker dispatch. Adding a future stage (music generation) is a config + worker addition, not a schema rewrite.
- **`ArtifactLifecycle`** — the per-artifact FSM (§12.2) with guard methods (`can_approve`, `can_regenerate`) used by services *and* rendered into the UI capabilities payload so buttons never lie.
- **`ApprovalPolicy`** — per-Series auto-approve flags per stage (challenge to the brief: six mandatory human gates per video is heavy at volume; policy makes gates configurable while defaulting to all-manual).
- **`TimelineCompiler` input/output models** — pure functions from approved artifacts → `Timeline` (§16.2), fully unit-testable.
- **`Money/UsageMeter`** value objects for provider cost accounting.

## 12. State Machine

### 12.1 Why the brief's single linear FSM is rejected (deliberately)

A monolithic project FSM with states like `Images Awaiting Approval` cannot express: scene 4's image rejected while scenes 1–3 are approved; voice regenerating while a human edits captions; images and voice generating in parallel. Encoding those in one enum explodes combinatorially. Instead: **three small machines + one derived value.**

### 12.2 Artifact lifecycle (per artifact — the workhorse)

```
                 ┌──────────► FAILED ──────► (retry → GENERATING)
                 │
 PENDING ──► GENERATING ──► AWAITING_APPROVAL ──► APPROVED
                 ▲                │    │
   (regenerate = new version)     │    └─► REJECTED ──► (regenerate → GENERATING, new version)
                 └────────────────┘
 Any non-approved sibling version when another is approved: SUPERSEDED
 Approved artifact later replaced (re-approval of newer version): previous → SUPERSEDED
```

**[AMENDED M1-01]** The SUPERSEDED rule above is too broad as written, and the
`artifact_version_status` view (finding B1) implements a narrowed form. Read
literally, *every* non-approved sibling becomes SUPERSEDED the moment any
version is approved — including a version created **after** the approval.
That is the regenerate-after-approve case, which is ordinary: approve v2, ask
for another take, get v3. Under the literal rule v3 is instantly SUPERSEDED, so
the review UI renders the one version awaiting a human decision as obsolete and
nobody looks at it.

The view therefore restricts SUPERSEDED to versions the approval has genuinely
moved *past*:

- `APPROVED` — holds the artifact's most recent APPROVE decision.
- `REJECTED` — its own latest decision is REJECT. Ranked above SUPERSEDED so an
  explicit human "no" is never relabelled as merely outdated.
- `SUPERSEDED` — **older** than the standing approval, or previously approved
  and since replaced (the §12.5 rollback).
- `AWAITING_APPROVAL` — everything else, including any version newer than the
  standing approval.

Covered by `tests/test_schema.py::TestArtifactVersionStatusView`.

Transitions are caused only by: job success/failure, review decision, human edit (jumps PENDING→AWAITING_APPROVAL with origin=human_edit), or reconciler (RUNNING job orphaned → artifact back to FAILED-retryable). Every transition writes `state_transition`.

### 12.3 Job execution states

`QUEUED → RUNNING → SUCCEEDED | FAILED(attempt<max → re-QUEUED) | CANCELLED | ORPHANED` — pure execution mechanics, invisible to approval logic except as transition causes.

### 12.4 Project phase (derived, coarse)

`phase` is *computed* from artifact states against the pipeline DAG, then cached on `video_project` for cheap listing/filtering:

```
DRAFT → RESEARCHING → RESEARCH_REVIEW → SCRIPTING → SCRIPT_REVIEW → SCENING → SCENES_REVIEW
      → MEDIA_GENERATION (images ∥ voice) → MEDIA_REVIEW → TIMELINE_READY → RENDERING
      → RENDER_REVIEW → PACKAGING → READY_TO_PUBLISH → PUBLISHED
      (+ orthogonal flags: HAS_FAILURES, ARCHIVED)
```

Because it is derived, it can never disagree with artifact truth; "rollback" is simply rejecting/superseding artifacts, which recomputes the phase backward automatically. Approving a *new* script version after images exist marks downstream artifacts `STALE` (a flag on artifact) and the phase falls back to SCRIPT_REVIEW-completed — the DAG knows script → scenes → {images, voice} → timeline dependencies and cascades staleness; stale artifacts remain viewable but must be regenerated or explicitly re-affirmed.

### 12.5 Retry / failure / partial-regeneration semantics

- **Retry**: same artifact version slot is *not* reused; a retried job that succeeds creates the next version (the failed attempt's partial output is never referenced).
- **Partial regeneration**: image artifacts are per-scene (`artifact.scene_ref`), so "regenerate scene 4 image" touches one artifact; the scene-set and other images are untouched; timeline recompiles from active pointers.
- **Rollback**: "return to script stage" = reject current script version (or approve an older version — allowed; approval always targets an explicit version id), cascade staleness.

## 13. Worker Architecture

One Celery app, task modules per stage, queue routing in config:

| Task | Queue | Provider | Output artifact | Notes |
|---|---|---|---|---|
| `research.generate` | llm | LLM(+web tool if provider supports) | research vN | |
| `script.generate` | llm | LLM | script vN | consumes approved research version id from job input_snapshot |
| `scenes.generate` | llm | LLM | scene_set vN (+scene rows) | validates durations sum ≈ target length |
| `prompts.generate` | llm | LLM | prompt vN per scene | batched: one job, N prompt artifacts |
| `images.generate` | image | ImageProvider | image vN (per scene) | fan-out: one job per scene for isolation & retry granularity |
| `voice.generate` | voice | VoiceProvider | voice vN + timestamps | **[AMENDED M0 — finding B3 revised]** ONE call for the whole script, with word timestamps; scene boundaries derived by walking the word list sequentially. Per-scene synthesis was rejected: at ~20 sentence-length scenes, twenty isolated reads each end with a terminal fall and concatenate into a list rather than a narration. Word timestamps are a **hard provider requirement** (finding S5) — whisperx is cut from the design. |
| `timeline.compile` | timeline | — (pure) | timeline vN | deterministic; no provider |
| `render.compose` | render | — | render vN | **[AMENDED M0 — ADR-012]** FFmpeg in-process on the `render` queue. No stream, no callback; completion uses the standard skeleton. |
| `package.assemble` | package | LLM (captions/hashtags) | package vN | zip in MinIO |
| `events.drain_outbox` | events | — | — | beat, every 1s batch |
| `reconciler.sweep` | events | — | — | beat: orphan jobs, stuck streams, stale heartbeats |

**Uniform task skeleton (enforced by a decorator):**
1. Load `GenerationJob` by id; assert QUEUED/RETRYABLE; mark RUNNING (guarded UPDATE — the idempotency gate).
2. Read inputs strictly from `input_snapshot` (version ids pinned at enqueue time — a later approval cannot change a running job's inputs).
3. Call provider through the registry; stream/large outputs go to MinIO under content-addressed keys.
4. Single transaction: insert artifact_version (+typed extension), update artifact state, job → SUCCEEDED, provider_usage, state_transition, audit_event, outbox_event.
5. On exception: transactionally record FAILED + error payload + transition; Celery `autoretry_for` with exponential backoff + jitter for retryable classes (rate limit, timeout); non-retryable (validation, content policy) go straight to FAILED-terminal for human action.

**[AMENDED M0 — ADR-012]** There is no separate renderer process. Rendering is a task on the `render` queue running the skeleton above, so there is exactly one completion path in the codebase — which is what removed finding **B5** (the callback endpoint duplicated the state machine inside the API, contradicting §5.2's "the API never generates anything"). Redis Streams, consumer groups, `XAUTOCLAIM` recovery and the HMAC callback are all deleted.

## 14. Queue Design

### 14.1 Queues & concurrency

Separate queues per resource class so slow media work never starves cheap LLM calls: `llm` (concurrency 4, prefetch 1), `image` (2 — provider rate limits), `voice` (2), `timeline` (2), `package` (2), `events` (1, ordered), plus Redis Stream `render.jobs` (renderer concurrency 1 per container; scale containers, not threads — Chromium is memory-hungry).

### 14.2 Celery settings that matter

`acks_late=True` + `task_reject_on_worker_lost=True` (crash ⇒ redelivery, safe because of the RUNNING-guard idempotency), `worker_prefetch_multiplier=1` (fair long-task scheduling), visibility timeout ≥ max task runtime per queue, `task_time_limit`/`soft_time_limit` per queue (llm 300s, image 600s), results kept 24 h (state of record is Postgres, not the result backend), `worker_max_tasks_per_child` to recycle leaky provider SDK memory.

### 14.3 Idempotency (non-negotiable with Redis broker)

Delivery is at-least-once. Defenses: (a) `generation_job.idempotency_key` unique — duplicate enqueue is a no-op; (b) RUNNING-guard compare-and-set on job status — a redelivered task whose twin already ran observes SUCCEEDED and exits; (c) content-addressed storage — re-uploading identical bytes is harmless; (d) version numbers allocated inside the completion transaction, never before.

### 14.4 Durability reconciliation

Postgres `generation_job` is the durable intent record. The reconciler re-enqueues QUEUED jobs whose Celery task id is unknown to the broker (Redis lost pre-ack messages), marks RUNNING jobs past `queue_timeout` as ORPHANED→retry, and `XAUTOCLAIM`s abandoned render stream entries. Redis AOF narrows the window; the reconciler closes it.

### 14.5 Events: transactional outbox → Redis pub/sub → SSE/UI

Publishing to Redis inside the DB transaction is impossible; after it is a lost-event window. So: workers write `outbox_event` in the completion transaction; the `events` worker drains the outbox (ordered, batched, marks `published_at`) and publishes to `events:{project_id}` channels; the SSE endpoint subscribes; the frontend treats events purely as *cache-invalidation hints* (React Query refetch), so a missed event degrades to the polling fallback rather than wrong UI state. Exactly-once *processing* is achieved at the read model even though delivery is at-least-once.


## 15. Provider Interfaces

### 15.1 Design principles

Providers are the *only* external dependency; everything else must be ignorant of which is configured (G5). Rules: narrow protocol per capability; adapters own SDK quirks, auth, retries-at-the-edge, and normalization; a registry resolves configuration → instances; every call is metered; a `mock` implementation of every protocol exists and is a first-class citizen (used by CI, seed data, and `PROVIDERS_MODE=mock` local runs).

### 15.2 Protocols (Python `typing.Protocol`)

```python
class LLMProvider(Protocol):
    name: str
    def complete(self, req: LLMRequest) -> LLMResult: ...
    # LLMRequest: messages, model_hint, temperature, max_tokens, response_schema (JSON mode),
    #             tools?: [web_search]           LLMResult: text|parsed, usage, provider_meta

class ImageProvider(Protocol):
    name: str
    def generate(self, req: ImageRequest) -> ImageResult: ...
    # ImageRequest: prompt, negative_prompt, width, height, style_ref?, seed?, n
    # ImageResult: [ImageBytes(mime, data, seed_used)], usage

class VoiceProvider(Protocol):
    name: str
    def synthesize(self, req: VoiceRequest) -> VoiceResult: ...
    def capabilities(self) -> VoiceCaps: ...     # has_word_timestamps, formats, voices()
    # VoiceResult: audio(mime,data,sample_rate), timestamps?: [WordStamp(word,start_ms,end_ms)]

# Future seams (declared, unimplemented): AnimationProvider, MusicProvider,
# ResearchProvider (LLM+search composite), PublishingProvider (per-platform upload).
```

Requests/results are Pydantic models in `packages/providers` — no SDK types cross the boundary. Capability discovery (`VoiceCaps`) lets the voice worker choose provider timestamps vs. local forced alignment without knowing the vendor.

### 15.3 Configuration & registry

```yaml
# config/providers.yaml (env-overridable: PROVIDERS__LLM__ADAPTER=anthropic)
llm:    {adapter: anthropic, model: claude-sonnet-latest, timeout_s: 120}
image:  {adapter: stability, model: sd3-large, size: 1080x1920}
voice:  {adapter: elevenlabs, voice_id: ${SERIES_OVERRIDE}, format: mp3_44100}
mode: real   # real | mock | record | replay
```

Series/project settings may override model/voice per project (recorded into artifact meta). `record`/`replay` modes wrap real adapters with fixture capture for deterministic integration tests (§22).

### 15.4 Cross-cutting adapter middleware

Decorator stack applied by the registry: usage metering (→ `provider_usage`), structured logging with correlation id, timeout, bounded retry on transport/429 (task-level retry handles the rest), circuit breaker (open after N consecutive failures → jobs fail fast with a clear operator message), and API-key injection from worker-only env (§21).

## 16. Rendering Pipeline

### 16.1 Flow

**[AMENDED M0 — ADR-012]**

```
approved artifacts ──timeline.compile (py, pure)──► Timeline JSON (schema-versioned, MinIO)
   ──enqueue on `render` queue──► render worker (py) ──► fetch assets (verified)
   ──► ffmpeg: concat + xfade + ASS subtitles + audio mix
   ──► final.mp4 + thumbnail.png → MinIO ──same task skeleton──► render artifact vN → REVIEW
```

No stream, no callback: completion runs through the standard task skeleton
like every other stage, which is what removed finding B5.

### 16.2 Timeline JSON (schema `timeline/v1`, excerpt)

```jsonc
{
  "schema": "videoforge.timeline/v1",
  "project_id": "01J…", "fps": 30, "width": 1080, "height": 1920,
  "inputs": { "script_version": "01J…", "voice_version": "01J…", "scene_versions": ["…"] },
  "audio": {
    "narration": { "src": "s3://artifacts/sha256/ab…/voice.mp3", "offset_ms": 0 },
    "music": { "src": "s3://assets/music/calm-01.mp3", "gain_db": -18,
               "duck": { "target_db": -26, "attack_ms": 200, "release_ms": 400 },
               "fade_out_ms": 1500 }
  },
  "scenes": [{
      "index": 0, "start_ms": 0, "duration_ms": 6420,          // from voice timestamps
      "image": { "src": "s3://artifacts/sha256/cd…/scene0.png", "width": 1080, "height": 1920 },
      "motion": { "type": "ken_burns", "from": {"scale":1.0,"x":0,"y":0},
                  "to": {"scale":1.12,"x":-30,"y":-18}, "easing": "ease_in_out" },
      "transition_out": { "type": "crossfade", "duration_ms": 400 }
  }],
  "captions": { "style_ref": "templates/captions/bold-bottom", "mode": "word_karaoke",
                "words": [{"w":"Octopuses","s":0,"e":410}, …] }
}
```

Scene `duration_ms` comes from voice segment timing (+ configured padding); the compiler validates total ≤ platform max (90 s), asset existence and hashes, and motion bounds (no over-zoom revealing edges). The compiled JSON is itself an artifact version — diffs between timeline v1 and v2 are human-readable.

### 16.3 Composition  **[AMENDED M0 — superseded by ADR-012]**

> Rendering is an FFmpeg filter graph built in `videoforge_workers/render.py`:
> `concat` for scene holds → `xfade` for transitions → `subtitles=` burning an
> ASS document for captions → `format=yuv420p` → libx264/AAC with
> `+faststart`. Captions use ASS `BorderStyle=1` with a heavy outline, which is
> exactly the reference style (plan §1.0.2). Determinism comes from a pinned
> ffmpeg, a bundled font, and a fixed argv — verified by the M0 exit test,
> which parses the output's MP4 box structure rather than trusting flags.
>
> Original Remotion text retained for context:

`<ShortVideo timeline={t}>` maps scenes → `<Scene>` (image via `<Img>`, `<KenBurns>` interpolating transform with `interpolate(frame, …)`), `<Captions>` (word-karaoke from timestamps), `<Audio>` layers for narration + music (ducking pre-baked into gain automation points by the compiler — keeps the renderer dumb and the mix deterministic). Render via `@remotion/renderer` Node API (`renderMedia`, `concurrency` tuned, `--gl=angle` off — software rendering for determinism), H.264 yuv420p, AAC 48 kHz, `-movflags +faststart` (via FFmpeg post-pass) for instant browser scrubbing through Nginx.

Determinism measures: fixed fonts bundled in the image, no `random()` without seed, renderer image digest recorded on the render row (NF5). Remotion Studio in the dev profile gives frame-accurate preview of any timeline artifact — the primary tool for developing motion/caption styles without paying for renders.

### 16.4 Renderer worker contract

Consume stream entry `{render_job_id, timeline_key}` → download assets to tmp (verify sha256) → bundle once at boot, render per job → upload mp4+thumbnail (frame at 15%) → signed callback with duration/fps/filesize/log tail. Failures: callback with error class; Chromium crash → process exits → container restarts → `XAUTOCLAIM` recovers the entry. Memory-limited via compose; one render at a time per container.

## 17. Approval Workflow

Each reviewable kind gets a screen backed by the same capability model:

| Screen | Presents | Extras |
|---|---|---|
| Research | outline + sources + claims | per-claim comment anchors |
| Script | text w/ reading-time + version diff | inline edit → new human_edit version |
| Scenes | table: narration / visual brief / est. duration | reorder & split/merge → new scene_set version |
| Images | per-scene grid, versions side-by-side, prompt shown | regenerate-this-scene; edit prompt then regenerate |
| Voice | player w/ word-highlight follow | per-segment retake note |
| Render | streaming MP4 (range requests via Nginx) + thumbnail | frame-step preview; approve → package |

Uniform actions: **Approve** (targets explicit version id, optimistic-lock on artifact `current_version_no` to prevent approving stale views), **Reject** (requires comment), **Regenerate** (optional guidance text merged into the prompt context of the new job), **Edit** (text kinds → human_edit version), **Comment** (anchored, non-blocking). Auto-approve per stage is a Series policy (default off). Approvals recompute phase and, if the Series enables `auto_advance`, enqueue the next stage's job automatically.

## 18. Artifact Versioning & Storage

1. **Never overwrite** — enforced three ways: DB immutability triggers, content-addressed object keys, MinIO bucket policy denying overwrite/delete to the app credential.
2. **Content addressing:** binary keys are `artifacts/{sha256[0:2]}/{sha256}/{filename}` — identical regenerations dedupe; caches can be `immutable`.
3. **Buckets:** `artifacts` (generated, versioned-forever), `assets` (music/fonts library, read-mostly), `packages` (zips), `tmp-render` (lifecycle-expired 24 h).
4. **Text artifacts** ≤ 64 KB may also be mirrored `inline_content` (JSONB) for fast list/diff endpoints; MinIO copy remains canonical.
5. **Retention:** nothing auto-deletes in v1; an `archive project` action exists but merely flags. Storage is cheap; history is the product's safety net.

## 19. API Design

Base `/api/v1`, JSON, ULID ids, cursor pagination, `Idempotency-Key` honored on all POSTs that create jobs. Errors: RFC-9457 problem+json with `correlation_id`.

### 19.1 Resources (representative)

```
POST   /projects                                  create (topic, series_id?)
GET    /projects?phase=&series=&cursor=           list
GET    /projects/{id}                             detail (+active_pointers, capabilities)
GET    /projects/{id}/artifacts?kind=&scene=      artifact + version summaries
GET    /artifacts/{id}/versions/{no}              version detail (+signed asset URLs)
GET    /artifacts/{id}/versions/{no}/diff/{no2}   text diff (script/scenes/prompts)

POST   /projects/{id}/generations                 {stage, scene_id?, guidance?} → 202 {job_id}
GET    /jobs/{id}                                 status, attempts, error, usage
POST   /jobs/{id}/retry                           re-enqueue failed terminal job
GET    /projects/{id}/jobs?status=                job list

POST   /artifact-versions/{id}/reviews            {decision, comment?, expected_version_no}
POST   /artifact-versions/{id}/comments
PUT    /artifacts/{id}/content                    human edit → new version (text kinds)

POST   /projects/{id}/renders                     compile timeline (if stale) + submit render
GET    /renders/{id}                              status + mp4/thumbnail signed URLs
POST   /projects/{id}/package                     assemble publishing package
GET    /packages/{id}/download                    302 → signed zip URL

GET    /events?project_id=                        SSE stream (prod-local; dev too)
GET    /health | /health/deep                     liveness | dependency map
```

### 19.2 Async pattern

Every generation returns `202 + job_id + Location: /jobs/{id}`. Clients poll `GET /jobs/{id}` (React Query `refetchInterval: 2000`, backoff after 30 s) *and/or* subscribe to SSE. SSE carries `{type, project_id, subject}` hints only — no payloads — so ordering/loss are non-issues (§14.5).

### 19.3–19.6 Contract details

- **Approval endpoint** is optimistic-locked (`expected_version_no`) → `409` if a newer version appeared mid-review.
- **Artifact asset URLs** are MinIO presigned (15 min) rewritten to pass through Nginx `/assets/` (§21.6) so the browser never needs MinIO network access.
- **OpenAPI** generated from Pydantic DTOs (`docs/api/openapi.json`), TS client types generated from it in the frontend build — end-to-end type safety.
- **SSE under uWSGI:** served by a small dedicated uWSGI pool (`/api/events` routed by Nginx to a second socket, 8 async-friendly workers with `http-timeout` long, heartbeat comment every 15 s); if this proves brittle, feature-flag SSE off — polling alone meets the UX bar (ADR-006).

## 20. Frontend Architecture

- **Structure:** App Router; routes `/(dashboard)`, `/projects/[id]` with a **pipeline rail** (stage chips colored by artifact state) and stage panels lazy-loaded per kind. `features/{review,projects,jobs}/` colocate components+hooks+api.
- **Data:** React Query everywhere; query keys `[project, id]`, `[artifacts, projectId, kind]`, `[job, id]`. Mutations (generate/review) optimistic where safe (comments) and strict elsewhere. A single `useProjectEvents(projectId)` hook owns the SSE `EventSource` and translates events → `queryClient.invalidateQueries` — the *only* coupling between push and state.
- **Review UX primitives:** `<VersionSwitcher/>`, `<DiffView/>` (text kinds), `<SceneGrid/>`, `<AudioKaraoke/>` (word timestamps drive highlight), `<VideoReview/>` (native `<video>` + range streaming). Capabilities from the API (`can_approve`, `can_regenerate`, reasons) drive button state — no duplicated FSM logic in TS.
- **Forms/validation:** zod schemas generated from OpenAPI; Tailwind + a small design-token layer (Series style presets preview uses the same tokens as captions templates).

## 21. Security

Scope: single trusted operator on a local machine — but built so hardening is additive, not a rewrite.

1. **API auth (v1):** one static bearer token (env `API_TOKEN`), enforced in Nginx (`map` + 401) *and* Flask (defense in depth). Frontend holds it via server-side session cookie set by a tiny `/auth/login`. Internal callbacks (renderer) use a separate HMAC-signed shared secret + timestamp.
2. **Future accounts:** `user`/`workspace` tables exist day one; auth swaps to session/OIDC later without schema surgery. All audit rows already record `actor_id`.
3. **Secrets:** `.env` (git-ignored) + `.env.example`; compose passes provider keys **only to worker containers** — the API, frontend, renderer, and Nginx never see them (blast-radius containment; a compromised web tier cannot spend your OpenAI credits).
4. **Provider isolation:** all egress from workers via the provider registry; timeouts, breakers, and usage caps (`daily_cost_limit` halts job creation with a clear error) guard runaway spend.
5. **[AMENDED M0 — finding B4, ADR-011] Asset serving:** the original design combined presigned URLs with nginx `auth_request`, which cannot work — presigning makes `auth_request` redundant, and without it nginx would have to compute SigV4, which it cannot do. Replaced by: **stable content-addressed public URL → Flask authorizes and presigns internally → `X-Accel-Redirect` → `internal;` nginx location proxies the signed URL to MinIO.** The browser's URL never changes (so `immutable` caching is correct), MinIO still validates a real signature (nothing is anonymous), and nginx never signs. Verified end to end including Range/206 and a 304 revalidation.
6. **Input validation:** every request body through Pydantic strict mode; uploads (future) size-capped at Nginx (`client_max_body_size 100m`) and content-type verified; prompt-injection surface acknowledged: research/script prompts wrap user topic in delimited context and generated *text* is always human-reviewed before driving downstream generation (the approval gates are also a safety boundary).
7. **Rate limiting:** Nginx `limit_req` on `/api/` (burst-friendly) — mostly protects against runaway frontend loops locally.
8. **Audit:** immutable `audit_event` + `state_transition` (§10.3); Nginx access logs with request id; job logs correlated by `correlation_id` propagated HTTP → Celery headers → renderer.
9. **Headers/TLS:** security header set (§23.2); TLS optional locally (self-signed profile flag) but config-ready.

## 22. Testing

| Layer | Tools | What & how |
|---|---|---|
| Unit (domain) | pytest | FSM guards, DAG/phase derivation, timeline compiler (golden JSON), prompt rendering. No I/O; property tests (hypothesis) on transitions: *no sequence of events reaches an invalid state or skips a required approval*. |
| Unit (frontend) | vitest + RTL | capability-driven buttons, karaoke timing math, query-key invalidation map. |
| Repository/integration | pytest + testcontainers-postgres | migrations up-to-date (`alembic check`), immutability triggers actually raise, optimistic locking, outbox draining. factory_boy + Faker factories for every entity (deterministic seeds). |
| Worker | pytest + celery `task_always_eager=False` against real Redis container | full task skeleton with **mock providers**: idempotent double-delivery test (deliver twice, assert one version), retry/backoff classes, orphan reconciliation. |
| Provider contract | pytest, `record`/`replay` fixtures | each real adapter has recorded HTTP cassettes (sanitized); replay in CI; a nightly opt-in `record` run against live keys refreshes and detects vendor drift. Mock providers implement the same contract-test suite. |
| Render | **[AMENDED M0]** pytest + ffmpeg | Pure helpers (ASS document, filter graph, argv, MP4 box parser) unit-tested offline. Execution verified by the M0 exit test: a real render asserting duration, codecs, and `moov`-before-`mdat` parsed from the bytes. Golden-frame diffing arrives in M4 by extracting frames with ffmpeg. |
| E2E | Playwright vs `production-local` profile, `PROVIDERS_MODE=mock` | the money path: create project → generate/approve every stage → render (tiny fixture) → download package → assert zip manifest. Runs in CI on every PR to main. |

CI order: lint/type (ruff, mypy, tsc) → unit → integration (containers) → render smoke → e2e. Everything offline (NF7).

## 23. Local Deployment

### 23.1 uWSGI configuration (prod-local)

```ini
[uwsgi]
module = videoforge.wsgi:app
strict = true                     ; typo-safe config
master = true
processes = 4                     ; CPU-bound-light API; 2×cores is overkill here
threads = 2                       ; SQLAlchemy sessions are per-thread scoped
enable-threads = true
socket = /run/uwsgi/api.sock      ; unix socket to Nginx (no TCP overhead, no exposed port)
chmod-socket = 660
uid = app  gid = app
vacuum = true                     ; clean socket on exit
die-on-term = true                ; SIGTERM = graceful shutdown (Docker stop semantics)
hook-master-start = unix_signal:15 gracefully_kill_them_all
harakiri = 30                     ; hard kill stuck request (API must be fast; §NF2)
max-requests = 1000               ; recycle workers (leak hygiene)
reload-on-rss = 512               ; MB memory ceiling per worker
buffer-size = 16384               ; headroom for cookies/headers
post-buffering = 8192
lazy-apps = true                  ; per-worker app init → no fork-related DB/redis fd sharing
logformat = json (custom: ts, method, uri, status, msecs, request_id)
log-x-forwarded-for = true
```
Second tiny instance (or `--emperor` vassal) for SSE: `processes=1 threads=16 http-timeout=3600 socket=/run/uwsgi/sse.sock`. Dev mode skips uWSGI entirely (`flask --debug run`) — reload fidelity beats parity during feature work; parity is validated by the prod-local profile and CI.

### 23.2 Nginx configuration (essentials)

```nginx
upstream api  { server unix:/run/uwsgi/api.sock; }
upstream sse  { server unix:/run/uwsgi/sse.sock; }
upstream next { server frontend:3000; }
upstream s3   { server minio:9000; }

map $http_x_request_id $req_id { default $http_x_request_id; "" $request_id; }

server {
  listen 80;
  client_max_body_size 100m;

  add_header X-Content-Type-Options nosniff always;
  add_header X-Frame-Options DENY always;
  add_header Referrer-Policy strict-origin-when-cross-origin always;
  add_header Content-Security-Policy "default-src 'self'; img-src 'self' blob: data:; media-src 'self' blob:;" always;

  location /api/events {              # SSE
    include uwsgi_params; uwsgi_pass sse;
    uwsgi_param HTTP_X_REQUEST_ID $req_id;
    uwsgi_read_timeout 1h; uwsgi_buffering off; add_header X-Accel-Buffering no;
  }
  location /api/ {
    limit_req zone=api burst=40 nodelay;
    include uwsgi_params; uwsgi_pass api;
    uwsgi_param HTTP_X_REQUEST_ID $req_id;
    uwsgi_read_timeout 30s;
  }
  location /assets/ {                 # authenticated proxy to MinIO (images, mp4)
    auth_request /api/v1/internal/asset-auth;
    proxy_pass http://s3/;            # presigned path rewritten by API
    proxy_buffering off;              # stream large mp4s
    proxy_set_header Range $http_range;      # byte-range → scrubbing works
    add_header Accept-Ranges bytes always;
    add_header Cache-Control "public, max-age=31536000, immutable";  # content-addressed!
  }
  location / { proxy_pass http://next; proxy_set_header Upgrade $http_upgrade;
               proxy_set_header Connection "upgrade"; }              # HMR ws in dev-w/-nginx
  access_log /dev/stdout json_combined;   # includes $req_id, $upstream_response_time
}
```
Notes: MP4 streaming needs only correct `Range` passthrough (files are `+faststart`); immutable cache headers are safe *because* keys are content hashes; request id generated at the edge and propagated everywhere (logs, Celery headers, renderer callback).

### 23.3 Operator experience

`make up` / `make up-prod` / `make seed` / `make logs svc=…` / `make reset` (drop volumes, re-migrate, re-seed). First-run bootstrap: minio bucket job, migrate job, seed job — all idempotent. `docs/runbook.md` covers: rotate provider key, drain a queue, recover an orphaned render, restore from a pg_dump.

## 24. Future Cloud Migration (path only, no work now)

Deliberate seams that make this cheap later: MinIO→S3 (endpoint config — and the M0-05 `s3v4` fix means presigned URLs already work against real S3), Redis→ElastiCache (Celery broker URL), Postgres→RDS (DSN), **[AMENDED M0]** the render worker→a larger instance or a batch service consuming the same `render` queue (no stream contract to port — ADR-012), Nginx→ALB+CloudFront (immutable content-addressed assets are CDN-perfect; note `/assets/` becomes CloudFront + Origin Access Control, and the *public URL shape survives*, which is what the frontend depends on — ADR-011), secrets→SSM. The one real rewrite risk — sticky local filesystem assumptions — is avoided now by keeping *all* artifact I/O behind the storage client (`packages/shared/storage.py`).

## 25. Technical Risks

| # | Risk | Likelihood / Impact | Mitigation |
|---|---|---|---|
| R1 | ~~uWSGI maintenance-mode × Python 3.13~~ | **RETIRED (M0-00)** | Resolved by decision D1: Python **3.12** + uWSGI **2.0.31**, compiled in the image build and verified under concurrent load, graceful SIGTERM and harakiri (ADR-002). Recurs on every future CPython release; the mitigation is the clean transport boundary, not the pin. |
| R2 | ~~Remotion licensing~~ | **RETIRED (M0-09)** | Decision D4 replaced Remotion with FFmpeg (ADR-012); there is no longer a licensed dependency. The non-commercial finding and its monetisation trigger survive in ADR-007 in case Remotion ever returns. |
| R3 | Voice providers without reliable **word timestamps** | M / **H** | **[AMENDED M0]** Escalated: revised finding B3 derives scene boundaries *and* captions from word timestamps, so an adapter lacking them cannot drive the pipeline at all. There is no degraded mode — whisperx was cut (finding S5). Mitigation is a hard capability check in the adapter contract test, failing at configuration time rather than mid-render. |
| R4 | ~~Chromium renders slow/OOM~~ | **RETIRED (M0-09)** | No Chromium: FFmpeg renders in seconds (ADR-012). `worker-render` keeps its own container with a CPU limit so encoding cannot starve the other queues. |
| R5 | Celery/Redis **redelivery** causing duplicate artifacts | M / H if unhandled | Idempotency keys + RUNNING-guard + version allocation in txn (§14.3) — designed out, verified by double-delivery tests. |
| R6 | **Provider drift** (API/behavior changes) breaking adapters silently | H / M | Contract tests with replay fixtures + opt-in nightly live run; adapters isolated per vendor. |
| R7 | Illustration **style inconsistency** across scenes | H / **H** (top product risk) | **[AMENDED M0]** Escalated: motion used to distract from mismatched stills, and ~20 hard cuts per video (plan §1.0.1) no longer do. Reference analysis (§1.0.2) shows the mitigation that actually works — a *radically reductive* character convention (pale round heads, dot eyes) that diffusion models reproduce reliably, plus a tight palette. Style-consistency work is pulled forward into M3 as first-class scope, not polish. |
| R8 | **Scope creep** toward a full editor | M / H | Non-goal N6; timeline tweaks constrained to presets; ADR gate for any track-level editing feature. |
| R9 | Human gates → **throughput bottleneck** | M / M | Per-stage auto-approve policy (§11); batch review UI later. |
| R10 | Provider **cost runaway** (loops, retries) | L / M | Daily cost cap, per-job max_attempts, usage dashboard from provider_usage. |

## 26. Architecture Decision Records (seeded)

ADRs live in `docs/adr` (MADR format); the following are drafted with this document:

**[AMENDED M0]** All ADRs are now written in full under `docs/adr/`. Current status:

| ADR | Subject | Status |
|---|---|---|
| 001 | Layered state model (artifact FSM + job FSM + derived phase) | Accepted |
| 002 | uWSGI as WSGI server; **Python 3.12** (decision D1) | Accepted; amended M0-02 (runs fully unprivileged) |
| 003 | Transactional outbox for events | Accepted |
| 004 | Content-addressed immutable storage | Accepted; amended M0-12 (metadata self-heal on dedup) |
| 005 | Renderer as isolated Node service on Redis Streams | ⚠️ **Superseded by ADR-012** |
| 006 | Polling-first job UX; SSE deferred to M5 (finding S7) | Accepted |
| 007 | Remotion as rendering engine | ⚠️ **Superseded by ADR-012** (licensing finding retained) |
| 008 | JSON Schema cross-language codegen | ⚠️ **Withdrawn** — never implemented (finding S8) |
| 009 | Pipeline as a configurable DAG | Accepted |
| 010 | Redis as Celery broker + Postgres reconciliation | Accepted |
| 011 | Asset serving via X-Accel-Redirect (finding B4) | Accepted; verified live |
| 012 | **FFmpeg rendering inside a Celery task** (decision D4) | Accepted; supersedes 005 and 007 |
| 013 | Settings and data layer in `packages/`, not the backend | Accepted |
| 014 | Containerised toolchain; the host needs only Docker | Accepted |
| 015 | Workflow rules in `packages/domain`, not the backend | Accepted |

## 27. Implementation Roadmap

Vertical slices; each milestone ships working, tested software; **no milestone starts before the previous is approved.**

**M0 — Foundation — ✅ COMPLETE (14 tickets).** Monorepo per §8 (amended); Docker Compose with both profiles; Nginx + uWSGI over unix sockets; Flask app factory with RFC-9457 errors, correlation middleware, `/health` + `/health/deep`; Postgres + Alembic baseline + migrate service; Redis with AOF; MinIO + bucket bootstrap; Celery with seven queues, task skeleton, ping tasks, beat, Flower; **[AMENDED M0]** an FFmpeg render task (not a Remotion container) producing `hello.mp4` in MinIO; layered Pydantic Settings; structured JSON logging with request-id propagation; Next.js UI with a BFF; nginx asset serving via X-Accel-Redirect; CI.

**Exit test — executable and passing:** `make exit-test` runs 22 assertions (services healthy, migrations applied, all seven queues round-tripping, a real render landing in MinIO, asset serving with byte ranges, UI, BFF, and NF8 secret isolation). CI runs the identical command.

**M1 — Domain spine + first vertical slice (Script).** Core schema (workspace/user/series/project/artifact/version/job/transition/audit/outbox/usage) + immutability triggers; job service + idempotency; outbox → SSE/polling; **mock LLM provider**; script generate → review screen (approve/reject/regenerate/edit/comment/versions/diff) end-to-end in the UI. Exit: e2e test drives topic→approved script with mock provider.

**M2 — Full text pipeline.** Research + scenes + prompts stages; DAG/phase derivation; staleness cascade; real LLM adapter (+record/replay fixtures); prompt template versioning.

**M3 — Media generation.** Image fan-out per scene + grid review UI; voice + timestamps (+alignment fallback) + karaoke review player; parallel media phase; provider usage/cost surfaces.

**M4 — Timeline + render. [AMENDED M0]** Timeline compiler (golden JSON tests; no motion block, per the scope amendment in §1); FFmpeg composition work — ASS caption generation from word timestamps, `xfade` transitions, baked audio-ducking envelope (finding S3), music selection (finding S4); MP4 review screen on the asset path already proven in M0-10. No render stream, no callback, no reconciliation — rendering is a normal Celery task (ADR-012). Golden-frame tests by extracting frames with ffmpeg. Also here: the **renderer-neutrality rule** — the timeline schema must not acquire concepts only one engine can express, which is what keeps ADR-012 reversible.

**M5 — Packaging + polish.** Caption/hashtag generation; zip assembly + download; dashboard/pipeline rail; retries UI; runbook; cost caps; seed demo content.

**M6 — Hardening.** Double-delivery/orphan chaos tests; SSE flag decision; perf pass on render; docs freeze; v1 tag.

---

*End of document. Awaiting review — per the working agreement, no production code will be written until this SADD is approved, and implementation will proceed one milestone at a time with explicit approval between milestones.*