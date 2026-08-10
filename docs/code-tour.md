# Code Tour — a tracked reading plan for VideoForge

A structured way to read this codebase over several sittings, with progress
recorded here and understanding checked out loud rather than assumed.

**The rule this document exists to enforce:** a stage is not finished when you
have read the files. It is finished when you can answer its questions without
opening them. Reading is easy to fake, to yourself. Recall is not.

---

## For a fresh Claude session

If you are a new session picking this up, this file is the state. You need:

1. **This file** — the plan and the progress table below.
2. **`CLAUDE.md`** — repo conventions.
3. **Nothing else up front.** Do not pre-read the codebase, do not run
   `graphify query`. Open only the files the current stage names, when the
   current stage names them.

The user drives. Two things they will say:

- **"Let's do stage N"** → walk them through those files. Explain the *why*
  behind the code, not a line-by-line paraphrase — they can read syntax. Point
  at the decision each file embodies and the ADR or finding behind it.
- **"Review stage N"** → run the review protocol below. This is a quiz, not a
  recap. Do not restate the material first.

Then update the progress table and append to the session log at the bottom.

---

## The review protocol

When asked to review a stage:

1. **Ask that stage's questions — all of them, one at a time.** Not multiple
   choice. Not hints.
2. **The user answers from memory.** If they want to look something up, that
   answer is marked *partial* — which is fine, and worth knowing.
3. **Grade against the source**, not against this document. Open the files and
   cite `file.py:line` for anything wrong or vague. If this document turns out
   to be wrong, the source wins and this document gets fixed.
4. **Set the status:**
   - **● Confirmed** — every question answered from memory, in their own words.
   - **◐ Partial** — read, but at least one gap. Record the specific gap and
     the lines that close it. Do not round up to Confirmed.
   - **☐ Not started.**
5. **One follow-up question that the reading does not answer directly** — a
   "what would break if…". This is where understanding separates from
   memorisation.

Do not grade generously. A false ● costs more than an honest ◐, because the
whole point of a tracker is knowing where you actually are.

---

## Progress

| # | Stage | Lines | Sitting | Status |
|---|---|---|---|---|
| 0 | Orientation — the shape, before any code | docs only | ~40 min | ● |
| 1 | The vocabulary — states and transitions | ~680 | ~1 hr | ● |
| 1b | The pipeline, as data | ~520 | ~45 min | ◐ |
| 2 | Trace A — browser click to a job row | ~1,220 | ~1 hr | ● |
| 3 | Trace B — worker picks it up, version appears | ~1,030 | ~1.5 hr | ☐ |
| 3b | The other four stages — fan-out and rows | ~780 | ~1 hr | ☐ |
| 4 | The provider seam | ~960 | ~1 hr | ☐ |
| 5 | The tables, properly | ~1,190 | ~1.5 hr | ☐ |
| 6 | Repositories and the unit of work | ~1,280 | ~1.5 hr | ☐ |
| 7 | Derived state — review, approval, phase | ~800 | ~1 hr | ☐ |
| 8 | Cross-cutting: config, logs, storage, secrets | ~1,250 | ~1.5 hr | ☐ |
| 9 | The frontend | ~1,210 | ~1.5 hr | ☐ |
| 10 | Infrastructure — compose, nginx, migrations | ~700 | ~1 hr | ☐ |
| 11 | Tests as the specification | ~1,200 | ~1 hr | ☐ |

**~12,700 lines of the ~20,200 in the repo** (of which ~6,300 are tests, most
of them read in stage 11). The gap is the skip list at the bottom — deliberate,
not an oversight.

Legend: ☐ not started · ◐ read, gaps recorded · ● confirmed by review

**Why the letters.** M2 roughly doubled the codebase. Two of its additions were
whole subjects rather than extra files — the pipeline graph, and the four
generation stages — so they became 1b and 3b rather than being crammed into
their neighbours. Numbers stayed put so the session log below keeps meaning
what it said.

---

## Why this order

Two orders were possible and both are worse than the one used here.

**Dependency order** (`shared` → `domain` → `persistence` → apps) is how the
code is built but not how it is understood: you read a thousand lines of
plumbing before anything does anything, and nothing sticks because nothing has
a purpose yet.

**Directory order** is no order at all.

So: **vocabulary first, then one complete trace, then depth.**

Stage 1 is 680 lines with no framework, no I/O and no database, and it defines
the words every other file in the repo is written in. Stages 2 and 3 then
follow a single user action all the way through the system — you will *skim*
files you later read properly, and that is intentional. Models make sense once
you have watched something write to them. Stages 5–8 go back and fill in what
you skimmed, now with somewhere to put it.

---

## What M2 changed (read this before stage 3)

The plan was written against the M1 codebase, when one stage existed. M2
shipped thirteen tickets and the shape moved. If you read stages 0–2 before
this, nothing you learned is wrong — but four things are now different.

**1. There are five generation stages, not one.** `research` → `script` →
`scene_set` → `prompt`, with `image`/`voice` still ahead. `script.py` shrank
from 227 lines to 115 because everything a stage does *around* its provider
call moved into `stages.py` — one `complete_generation` that writes the
version, the usage row, the transition, the audit event, the outbox event,
auto-approval and the phase recompute. Four copies of that tail would have
drifted into four slightly different rules.

**2. The pipeline is data.** `templates/pipeline.yaml` declares the stages;
`videoforge_domain.pipeline` turns the declaration into a graph that answers
"what may run now", "what does approving this invalidate", "which phase is
this project in". Nothing hardcodes the stage order any more. That is stage 1b,
and stage 3 assumes it.

**3. Project phase and staleness are derived caches.** `projection.py` is the
single call every transition path ends with. It is the same idea as the
`artifact_version_status` view from stage 7 — a value nobody stores as truth —
applied one level up.

**4. A stage's output can be rows, and a job can produce N artifacts.**
`scene_set` writes `scene` rows in the same transaction as its version;
`prompt` writes one artifact per scene from a single job. Both are stage 3b.

Also: the real Anthropic adapter landed (stage 4), prompts became versioned
templates with content-pinned refs (stage 3b), and `script-review.tsx` was
replaced by `pipeline-review.tsx` plus three smaller components (stage 9).

---

## Stage 0 — Orientation

**No code.** Get the shape in your head first, so every file afterwards has a
place to land.

| Read | Why |
|---|---|
| [README.md](../README.md) | The 10,000-foot view and the quickstart |
| [sadd.md](architecture/sadd.md) §5, §6, §8 | Architecture, the diagram, and the repo layout rule |
| [sadd.md](architecture/sadd.md) §12 | The state machine — skim now, live here in stage 1 |
| [ADR-001](adr/ADR-001-layered-state-model.md) | Why state is layered and derived |
| [ADR-013](adr/ADR-013-shared-data-layer.md) | Why the data layer is a package, not an app |
| [ADR-015](adr/ADR-015-domain-package-placement.md) | Why `domain` is separate from `persistence` |

**The question:** what are the layers, and which way may an import point?

**Confirmed when you can:**
- Draw the box diagram from memory — browser, nginx, frontend/BFF, API,
  Postgres, Redis, workers, MinIO — and say which arrows exist.
- State the dependency rule and give an example it *forbids*.
- Say what the API is not allowed to do. (One sentence. It's the whole design.)

---

## Stage 1 — The vocabulary

The keystone. Everything downstream is written in these words.

| File | Lines |
|---|---|
| `packages/shared/src/videoforge_shared/enums.py` | 185 |
| `packages/domain/src/videoforge_domain/artifact_lifecycle.py` | 239 |
| `packages/domain/src/videoforge_domain/job_lifecycle.py` | 106 |
| `packages/domain/src/videoforge_domain/approval_policy.py` | 83 |
| `packages/domain/src/videoforge_domain/__init__.py` | 64 |

These import nothing — no SQLAlchemy, no Flask, no Celery, no clock. That is
enforced by a test, and it is why this package's 48 tests run in 0.05s. Run
them; break something on purpose and watch what fails.

**The question:** what states can an artifact be in, and what moves it between
them?

**Confirmed when you can:**
- Recite the artifact states, and from `AWAITING_APPROVAL` name every legal
  event and where each lands.
- Explain why the transition table is a **dict** rather than a chain of `if`s —
  what does that buy that correctness alone doesn't?
- Explain what `capabilities()` returns and who consumes it. (This one pays off
  in stage 9.)
- Say why `enums.py` lives in `shared` and not in `persistence`.

---

## Stage 1b — The pipeline, as data

The second vocabulary. Stage 1 defines what an *artifact* can be; this defines
what the *pipeline* is, and it is equally pure — a dict in, answers out.

| File | Lines |
|---|---|
| `templates/pipeline.yaml` | 86 |
| `packages/domain/src/videoforge_domain/pipeline.py` | 243 |
| `packages/domain/src/videoforge_domain/phases.py` | 72 |
| `packages/domain/src/videoforge_domain/duration.py` | 70 |
| `packages/shared/src/videoforge_shared/pipeline_file.py` | 49 |
| [ADR-009](adr/ADR-009-pipeline-as-dag.md), [ADR-016](adr/ADR-016-series-scoped-branding.md) | |

Read `pipeline.yaml` first, then `pipeline.py`. The YAML is the whole system's
stage order in 86 lines, and everything else in this stage is a question asked
of it.

**The question:** what does the system know about its own shape, and what does
it deliberately refuse to express?

**Confirmed when you can:**
- Say why `Pipeline.from_mapping` takes a **mapping** and not a path, and what
  that split buys — the answer is the same one ADR-015 gives, one level up.
- Explain the homogeneity rule (ADR-016): why a dependency on an approved
  *series* asset cannot be a `requires` entry. Four differences, and each one
  alone would be enough.
- Recite `derive_phase`'s three rules **in order**, and say why "anything
  generating wins" is not simply "the earliest unfinished stage". (Images and
  voice are the case that forces it.)
- Explain why `phase_generating` and `phase_review` are declared per stage
  rather than computed — what does adding a stage then *not* touch?

---

## Stage 2 — Trace A: a click becomes a job row

Follow "Generate script" from the browser to the database. Read **in call
order**, not file order. Skim anything about the artifact tables — stage 5.

| File | Lines | Read for |
|---|---|---|
| `apps/frontend/src/app/projects/[id]/pipeline-review.tsx` | 385 | *Only* the generate button and its mutation |
| `apps/frontend/src/app/api/bff/[...path]/route.ts` | 110 | The BFF hop — why it exists at all |
| `apps/frontend/src/lib/server/backend.ts` | 64 | Where the API token is injected |
| `apps/backend/src/videoforge/api/projects.py` | 318 | The endpoint, and how thin it is |
| `apps/backend/src/videoforge/services/jobs.py` | 249 | The heart of this stage |
| `apps/backend/src/videoforge/services/dispatch.py` | 96 | Why publishing is *after* commit |

> Confirmed against `script-review.tsx`, which M2-13 replaced with
> `pipeline-review.tsx`. The generate mutation is the same; it now targets a
> selected stage rather than always the script. Nothing that was confirmed
> changed meaning.

**The question:** where does the HTTP request stop, and what makes the work
carry on without it?

**Confirmed when you can:**
- Name the two checks `JobService.request` performs and their order — then say
  what user-visible bug appears if you swap them. (This was a real bug. The
  ordering is load-bearing.)
- Explain what the idempotency key is made of and which failure it defends
  against.
- Explain why dispatch is a separate call rather than the last line of the
  service method.
- Say why the browser never talks to Flask directly.

---

## Stage 3 — Trace B: a worker picks it up

The same job, from the other side. Read **in call order**: a message arrives,
the skeleton claims it, `script_body` runs, `complete_generation` writes
everything, the drain publishes.

| File | Lines | Read for |
|---|---|---|
| `apps/workers/src/videoforge_workers/celery_app.py` | 124 | Queues, routing, what boots |
| `apps/workers/src/videoforge_workers/db.py` | 52 | Why workers make their own engine |
| `apps/workers/src/videoforge_workers/skeleton.py` | 343 | The shape every task shares |
| `apps/workers/src/videoforge_workers/stages.py` | 275 | What every stage does around its provider call |
| `apps/workers/src/videoforge_workers/script.py` | 115 | One stage, now that the tail has moved out |
| `apps/workers/src/videoforge_workers/outbox.py` | 124 | The drain — pairs with [ADR-003](adr/ADR-003-transactional-outbox.md) |

`skeleton.py` and `stages.py` are the two halves of one idea and do not split:
the skeleton owns the *transaction*, `complete_generation` owns *what goes
inside it*. Read `script.py` between them and it is only forty lines of its own.

**The question:** how many transactions does one job use, and why not one?

**Confirmed when you can:**
- Name the three transactions in `run_job` and say what each protects.
- Explain why the *claim* commits immediately instead of joining the body.
- Explain what the outbox is for — specifically, which two-writes problem it
  removes. Read ADR-003 alongside. (Stage 2 left the outbox row as the one gap;
  close it here.)
- Say what happens when the same job is delivered twice, at each of the three
  points it could arrive.
- Recite what `complete_generation` writes — there are seven things — and say
  why the phase recompute is **last**, after auto-approval rather than before.
- Explain `_REQUEUE_IS_HONOURED = False`. A constant that disables a working
  policy is either a bug or an honest admission; say which, and what has to
  land before it flips.

---

## Stage 3b — The other four stages

What differs between stages, now that what they share is in one place.

| File | Lines | Read for |
|---|---|---|
| `packages/prompts/src/videoforge_prompts/__init__.py` | 164 | Versioned templates, and what a `prompt_ref` pins |
| `apps/workers/src/videoforge_workers/research.py` | 78 | The thinnest possible stage — the baseline to diff against |
| `apps/workers/src/videoforge_workers/scenes.py` | 214 | The first stage whose output is **rows** |
| `apps/workers/src/videoforge_workers/prompts_stage.py` | 143 | One job, N artifacts |
| `packages/persistence/.../models/scene.py` | 118 | `scene_set` / `scene`, and the FK cycle |
| `packages/persistence/.../repositories/scenes.py` | 64 | "The scenes of the approved set", once |

**The question:** what can a stage do that the skeleton does not already do for
it — and what does each of those exceptions cost?

**Confirmed when you can:**
- Explain the `after_version` hook: why the scene rows are written by a
  callback inside `complete_generation` rather than by the stage afterwards.
- Say why the scene set stores its scenes **both** as rows and as the version's
  inline JSON, and which of the two anything downstream is required to read.
- Explain why the prompt stage is *batched* (one job, N artifacts) while images
  will *fan out* (N jobs). Three differences; the third is about the reviewer.
- Explain why `prompts_body` writes a manifest version for its own trigger
  artifact — and what happens to the project's phase if it does not.
- Say what a `prompt_ref` looks like and what each of its three parts defends
  against.
- Explain why duration mismatch is a **warning** and an empty scene list is a
  **failure**. Same function, opposite verdicts.

---

## Stage 4 — The provider seam

| File | Lines |
|---|---|
| `packages/providers/src/videoforge_providers/protocols.py` | 52 |
| `packages/providers/src/videoforge_providers/models.py` | 111 |
| `packages/providers/src/videoforge_providers/mock.py` | 193 |
| `packages/providers/src/videoforge_providers/middleware.py` | 131 |
| `packages/providers/src/videoforge_providers/registry.py` | 116 |
| `packages/providers/src/videoforge_providers/anthropic_adapter.py` | 222 |
| `packages/providers/src/videoforge_providers/record_replay.py` | 133 |
| `packages/shared/src/videoforge_shared/settings.py` | skim the settings split only |

**The question:** what does the application depend on instead of a vendor SDK —
and where, exactly, do API keys enter the system?

**Confirmed when you can:**
- Explain **NF8** without using the word "config": what structurally prevents
  the API container from holding a provider key? The answer is about *what each
  process is able to construct*.
- Say why the recorder wraps the retrier rather than the other way round.
- Explain why `mock.py` seeds with `sha256` and not `hash()`.
- Say why `registry.py` takes keys as an argument instead of reading them.
- Explain how the Anthropic adapter guarantees structured output, and why
  "ask for JSON in the prompt and parse the reply" is not the same guarantee.
- Explain the retryable/non-retryable split, and why `temperature` is omitted
  rather than defaulted. (That one was a real outage: a default turned every
  call into a 400.)
- Say what `fixture_key` deliberately excludes, and why including it would make
  replay fail for a reason that has nothing to do with the request.

---

## Stage 5 — The tables, properly

Now go back to what you skimmed.

| File | Lines |
|---|---|
| `packages/persistence/src/videoforge_persistence/columns.py` | 128 |
| `packages/persistence/src/videoforge_persistence/base.py` | 30 |
| `packages/persistence/src/videoforge_persistence/enum_types.py` | 61 |
| `packages/persistence/src/videoforge_persistence/models/artifact.py` | 207 |
| `packages/persistence/src/videoforge_persistence/models/job.py` | 163 |
| `packages/persistence/src/videoforge_persistence/models/review.py` | 104 |
| `packages/persistence/src/videoforge_persistence/models/audit.py` | 140 |
| `packages/persistence/src/videoforge_persistence/models/project.py` | 74 |
| `packages/persistence/src/videoforge_persistence/models/org.py` | 108 |
| `packages/persistence/src/videoforge_persistence/sql.py` | 170 |

`sql.py` is the densest file in the repo per line. Take it slowly.

**The question:** which tables can never be updated, and what actually stops it?

**Confirmed when you can:**
- List the immutable tables and name the one that is deliberately *not* on the
  list, with the reason.
- Explain `UNIQUE (project_id, kind, scene_ref) NULLS NOT DISTINCT` — what
  `NULLS NOT DISTINCT` changes, and what goes ambiguous without it (finding S1).
- Explain why `pg_enum` needs `values_callable`, and what silently goes wrong
  without it.
- State the rule about foreign keys leaving an immutable table, and why
  `ON DELETE SET NULL` made deleting a user impossible (finding M1-04a).
- Explain why `generation_job.idempotency_key` is a **partial** unique index
  rather than a plain constraint — which statuses it excludes, and the failure
  it was written to end. (A live job holds its key forever; that turned one 529
  into an unrecoverable project.)
- Say why `artifact.scene_ref` needs `use_alter=True`, and what the cycle is.

---

## Stage 6 — Repositories and the unit of work

| File | Lines |
|---|---|
| `packages/persistence/src/videoforge_persistence/repositories/base.py` | 61 |
| `packages/persistence/src/videoforge_persistence/repositories/artifacts.py` | 282 |
| `packages/persistence/src/videoforge_persistence/repositories/jobs.py` | 307 |
| `packages/persistence/src/videoforge_persistence/repositories/reviews.py` | 108 |
| `packages/persistence/src/videoforge_persistence/repositories/projects.py` | 160 |
| `packages/persistence/src/videoforge_persistence/repositories/audit.py` | 179 |
| `packages/persistence/src/videoforge_persistence/uow.py` | 141 |
| `packages/persistence/src/videoforge_persistence/engine.py` | 37 |

**The question:** who owns the transaction boundary — and how do you know by
looking?

**Confirmed when you can:**
- Explain what a repository may not do. (Hint: it never appears in these files.)
- Explain why `UnitOfWork` is deliberately **not** `slots=True`. Small detail,
  real constraint, and it will bite you if you "tidy" it.
- Trace one write — new artifact version — through repository, session, commit,
  and say where it becomes visible to another process.
- Explain how a version number is allocated without two workers colliding.
- Explain why `live_by_idempotency_key` exists alongside `by_idempotency_key`,
  and which of the two the reservation path uses.

---

## Stage 7 — Derived state: review, approval, phase

The reason the product exists. Everything else is delivery.

Three derived values, one idea: **anything that can be computed from truth is
not stored as truth.** A version's status, the project's phase, and
`stale_since` are all downstream of artifact rows, and none of them can
disagree with those rows because none of them is independently writable.

| File | Lines |
|---|---|
| `apps/backend/src/videoforge/services/review.py` | 238 |
| `apps/backend/src/videoforge/dto/__init__.py` | 382 |
| `packages/persistence/src/videoforge_persistence/projection.py` | 182 |
| `packages/persistence/.../sql.py` | the `artifact_version_status` view — re-read |
| [sadd.md](architecture/sadd.md) §17, §18 | The workflow and the versioning rules |

**The question:** where does a version's status come from — and the project's
phase?

**Confirmed when you can:**
- Say why the status is **not stored anywhere** and what that buys (finding B1).
- Recite the view's `CASE` ordering, and explain why `REJECTED` must be tested
  before `SUPERSEDED`. Say it in product terms: what would the UI show if the
  order were flipped?
- Explain what an "edit" creates, and why it does not arrive approved.
- Explain what happens to v1 and v2 when v3 is approved — all three, precisely.
- Explain why `_states` takes the **least advanced** artifact of a kind, and
  what a project would claim about itself if it took the most advanced.
- Say why `refresh_project_state` does staleness **before** the phase.
- Explain why `stale_since` is not a rejection, and why series supersession
  deliberately does not come through this path (ADR-016).

---

## Stage 8 — Cross-cutting concerns

| File | Lines | Theme |
|---|---|---|
| `packages/shared/src/videoforge_shared/settings.py` | 340 | Config, and the NF8 boundary |
| `packages/shared/src/videoforge_shared/correlation.py` | 103 | One id across five processes |
| `packages/shared/src/videoforge_shared/logging.py` | 135 | Structured logs |
| `packages/shared/src/videoforge_shared/storage.py` | 212 | All artifact I/O goes here |
| `packages/shared/src/videoforge_shared/hashing.py` | 62 | Content addressing ([ADR-004](adr/ADR-004-content-addressed-storage.md)) |
| `packages/shared/src/videoforge_shared/ids.py` | 24 | ULIDs — why not UUID4 |
| `packages/shared/src/videoforge_shared/tasks.py` | 83 | Publishing by name without importing workers |
| `apps/backend/src/videoforge/app.py` | 77 | App factory |
| `apps/backend/src/videoforge/api/middleware.py` | 46 | Where the correlation id is picked up |
| `apps/backend/src/videoforge/api/errors.py` | 95 | One error shape |
| `apps/backend/src/videoforge/api/assets.py` | 76 | X-Accel-Redirect ([ADR-011](adr/ADR-011-asset-serving.md)) |

**The question:** how do you follow one user action through five processes'
logs?

**Confirmed when you can:**
- Trace the correlation id: where it is born, every hop it survives, and the
  one place it would be lost if someone wrote the obvious thing.
- Explain why `tasks.py` exists at all — what would importing the worker module
  from the backend break?
- Explain how a 200 MB video reaches the browser without passing through Python
  (ADR-011), and why the API still authorises it.
- Explain what content-addressed storage makes free, and what it makes awkward.

---

## Stage 9 — The frontend

| File | Lines |
|---|---|
| `apps/frontend/src/lib/api.ts` | 208 |
| `apps/frontend/src/app/providers.tsx` | 35 |
| `apps/frontend/src/app/projects/[id]/pipeline-review.tsx` | 385 (now properly) |
| `apps/frontend/src/app/projects/[id]/stage-rail.tsx` | 112 |
| `apps/frontend/src/app/projects/[id]/stage-content.tsx` | 135 |
| `apps/frontend/src/app/projects/[id]/scene-selector.tsx` | 118 |
| `apps/frontend/src/app/projects/[id]/version-switcher.tsx` | 72 |
| `apps/frontend/src/app/projects/project-list.tsx` | 104 |
| `apps/frontend/src/lib/state-colors.ts` | 43 |
| [ADR-006](adr/ADR-006-polling-first-job-ux.md) | Why polling, not sockets |

**The question:** why does the UI never decide whether a button is enabled?

**Confirmed when you can:**
- Explain the `capabilities` payload end to end — from the domain FSM in stage 1
  to a disabled button — and say what class of bug it makes impossible.
- Explain the polling predicate, and why polling on *another* query's state was
  a bug (finding M1-09a). This one is subtle and worth getting exactly right.
- Say why the selected version is derived rather than held in an effect.
- Explain where `can_generate` and `unmet` on a `StageSummary` come from, and
  why the rail cannot compute them itself. (Same answer as `capabilities`,
  one level up — the pipeline graph is server-side.)
- Say why selecting a scene resets when the stage changes.

---

## Stage 10 — Infrastructure

| File | Read for |
|---|---|
| `docker/compose/docker-compose.yml` | The topology, and `depends_on` ordering |
| `docker/compose/compose.dev.yml`, `compose.prod.yml` | What the profiles change |
| `docker/nginx/` | Routing, and the asset path |
| `Makefile` | The commands, and why the toolchain is containerised ([ADR-014](adr/ADR-014-containerised-toolchain.md)) |
| `database/migrations/env.py` | How autogenerate finds the models |
| `database/seed/demo.py` | The intended states, documented as data |
| `docker/*/Dockerfile` | Which images copy `templates/`, and why that was a bug |

**The question:** what does `make up-prod` start, in what order, and what is
each thing waiting for?

**Confirmed when you can:**
- Explain why `migrate` is its own one-shot service rather than a step in the
  API's startup.
- Name which containers receive provider keys and which do not — and where that
  is declared.
- Explain why the seed writes through real repositories instead of INSERTs.
- Say what `make e2e` needs that `make up` does not, and why the gap stayed
  invisible on a developer machine for as long as it did.
- Explain why an env var can be set in `.env`, read correctly by settings, and
  still never reach the process. (`PROVIDERS__LLM__MODEL` did exactly this.)

---

## Stage 11 — Tests as the specification

Read last, on purpose. Now they read as claims about the system rather than
noise.

| File | Reads as |
|---|---|
| `packages/domain/tests/test_artifact_lifecycle.py` | The FSM's actual contract |
| `packages/domain/tests/test_pipeline.py`, `test_phases.py` | The graph's contract, with dict literals and no database |
| `tests/test_schema.py` | What the database enforces by itself |
| `tests/test_migrations.py` | That the migrations and the models agree |
| `tests/test_double_delivery.py` | The idempotency story, proven |
| `tests/test_pipeline_stages.py` | Four stages, end to end |
| `tests/test_projection.py` | Phase and staleness against real rows |
| `tests/test_secret_isolation.py` | NF8, as an executable claim |
| `apps/frontend/e2e/review-flow.spec.ts` | M1's exit criterion in one file |

**The question:** which test fails first if someone makes artifacts mutable?

**Confirmed when you can:**
- Name a property that *only* exists in the database and cannot be unit-tested
  against a fake — and the test that covers it.
- Explain what a "positive control" is here and why a check that can only print
  "pass" is not a check. This idea recurs through the whole repo.
- Point at a test that would still pass if the feature were broken, if one
  exists. (Genuinely open — if you find one, that is a finding. Two have been
  found so far: a domain-purity test scanning an empty directory, and
  `test_seed.py` never running in the container.)
- Say why the pipeline tests build graphs from dict literals rather than
  loading `pipeline.yaml` — and what that means the tests are *not* covering.

---

## Deliberately not on the plan

Skipping is a decision, so here it is explicitly.

| Skipped | Why |
|---|---|
| `database/migrations/versions/…747ca6bd5ff6_core_schema.py` (1,028) | Machine-generated from the models you read in stage 5. Read the models. The only hand-written parts are the trigger and view installs — those live in `sql.py`. |
| The other migrations, **except two** | Same reason. The exceptions are the pair that release state stranded by the old failure handling — they are data fixes with reasoning in the docstring, and they are the clearest surviving record of the 529 incident. Read those two in stage 3, alongside `_REQUEUE_IS_HONOURED`. |
| `apps/workers/render.py` (316), `ping.py` (43) | M0 skeletons, not on the M2 path. They become relevant at M4. |
| `api/health.py` (147), `health-panel.tsx` (128) | Diagnostics. Useful when something breaks; teach you nothing about the design. |
| Every `__init__.py` under ~50 lines | Re-exports. Two exceptions, both on the plan: `videoforge_domain/__init__.py` and `dto/__init__.py`. |
| `apps/backend/src/videoforge/{orm,domain,repositories,events}/__init__.py` | Empty placeholders from M0. Worth *noticing* — they're a map of what moved into `packages/`. |
| `docs/adr/ADR-005, 007, 008, 012` | Decisions about rendering and codegen. Read at M4, when they bite. |

---

## Session log

Appended after each session — what was covered, and any gap worth returning to.

<!-- Newest last. Format: date · stage · outcome · gaps -->

### 2026-08-02 · Stage 0 · ◐ Partial

**Solid:** the nginx fan-out, including the `/assets/` → MinIO branch most
people miss. Redis as broker. Why a worker-to-API transition callback is
unrecoverable — answered cleanly, including the "stuck in GENERATING with no
way to tell it apart from still-running" case.

**Gaps to close before stage 5:**

1. **The dependency rule itself** — answered with the NF8 secret boundary
   instead. These are two different rules with two different enforcing tests
   (`test_workspace_structure.py` vs `test_secret_isolation.py`). Close with
   [sadd.md §8 "Rationale highlights"](architecture/sadd.md:277) and
   [ADR-013](adr/ADR-013-shared-data-layer.md).
2. **Postgres's two writers** — API writes the job + outbox row in one
   transaction; workers write the artifact version, the state flip, the
   transition and audit rows in one transaction. This is the fact ADR-015
   turns on, so it matters more than it looks.
3. **ADR-013 moved two things** (settings, and the ORM + repositories); ADR-015
   moved a third (the FSM). Only the third was recalled.

**Ahead of plan:** reached for isolation-by-boundary unprompted, which is
stage 4's question. The mechanism to correct then — isolation is by *what a
process can construct* (`ProviderKeys`), not by what it can import;
`packages/providers` is importable by both apps.

**Next:** ~15 min re-read, then a two-question re-review, then stage 1.

### 2026-08-02 · Stage 0 re-review · ● Confirmed

Dependency rule stated correctly, with a valid forbidden import
(`videoforge.api.errors` from a worker). Both Postgres writers named. The
causal chain forcing the FSM into `packages/` given cleanly, including both
rejected alternatives unprompted.

**Carry into stage 3:** answered that workers "only change the state". They
write the `artifact_version` row, the state flip, `state_transition`,
`audit_event` and an outbox row — all one transaction. Flagged rather than
re-quizzed because it contradicts the atomicity argument they gave correctly
in the first review, so it reads as recall slippage, not a wrong model. Verify
directly against `skeleton.py`.

**Residual recall detail** (closed by reading code, not by re-quizzing):
`tests/test_workspace_structure.py` as the enforcement point — asked twice,
missed twice; and that ADR-013 moved settings *and* the ORM before ADR-015
moved the FSM.

### 2026-08-02 · Stage 1 · ◐ Partial

**Solid:** all four events legal from `AWAITING_APPROVAL` with correct
destinations, and spotted the `HUMAN_EDITED` self-transition unprompted. "Every
absent key is a deliberate no" — the point of the table, stated cleanly.
`capabilities()` and its consumer. Why `enums` sits in `shared` (with one
correction: the layer at risk is `domain`, not `shared`).

**The one gap, appearing twice:** fluency with what `_TABLE` actually contains,
and what is derived from it versus hand-maintained.

1. The six `ArtifactState` members were not recalled (asked twice).
2. Asked what changes to add a terminal `PUBLISHED` state, named `is_terminal`
   as needing an update. It is `not legal_events(state)` — derived, and the one
   function in the file guaranteed to need zero maintenance. Terminality is
   created by *absence* of outgoing keys. Missed the `ALTER TYPE` migration too
   (`ArtifactState` is a native Postgres enum).

Notable: the principle behind (2) is exactly what they got right in Q2
("absence is data, therefore enumerable"). Principle held, application didn't —
so this is fluency, not a wrong model.

**Close with:** write the 14 table cells out by hand as six state-rows; then
`grep -n "legal_events\|_TABLE"` — six readers, one writer.

### 2026-08-02 · Stage 1 re-review · ● Confirmed

Six states and all six transitions for `PENDING`/`FAILED`/`APPROVED` recalled
correctly. Named every one of the eight functions whose behaviour follows from
`_TABLE` — including `is_terminal`, the inversion from the first pass. Both
gaps closed; the pen-and-paper exercise worked.

**Carry into stage 5** (noted, not re-quizzed): adding a state also requires
`ALTER TYPE ... ADD VALUE`, because `ArtifactState` is a native Postgres enum
type. Meets it concretely in `columns.py` / `pg_enum`.

### 2026-08-03 · Stage 2 · ● Confirmed (no re-review needed)

Four of five clean on the first pass. Strongest stage so far.

**Correct:** the idempotency-before-FSM ordering and the double-click → 409
symptom; all three parts of the key, and why a client token cannot catch
broker redelivery; the dispatch split, including the recovery asymmetry
(QUEUED row with no message is recoverable, message with no row is not).

**Q5 (concurrent `reserve`) answered in full** — `INSERT … ON CONFLICT DO
NOTHING`, the lookup on conflict, *and* the uncommitted-concurrent-insert
branch that carries `# pragma: no cover`. That is `packages/persistence`,
i.e. stage 6 material. Added the missing half: the guarantee lives in the
unique index, not in Python — `ON CONFLICT` defers to it rather than creating
it.

**Only gap:** listing the five writes in `JobService.request`, the **outbox
row** was missed (substituted the conditional artifact creation, which is real
but only fires on first generate). Not re-quizzed, because the *concept* is
demonstrably held — the dual-write problem was diagnosed correctly in stage 0
Q4 and again in stage 2 Q4. What is missing is the row, not the reasoning.
Stage 3 opens on the drain; confirm it there.

### 2026-08-03 · Plan revised for M2

Not a reading session. M2 landed thirteen tickets and the codebase went from
~12,300 lines to ~20,200, so the plan was re-fitted to the code rather than
left describing a repo that no longer exists.

**Two stages added**, lettered so the numbering in the entries above keeps
meaning what it said:

- **1b — the pipeline, as data.** `pipeline.yaml`, `domain/pipeline.py`,
  `phases.py`, `duration.py`, `shared/pipeline_file.py`. This is a second
  vocabulary and it is as pure as stage 1; stage 3 assumes it.
- **3b — the other four stages.** `research`, `scenes`, `prompts_stage`,
  the `scene` tables, and `packages/prompts`. Fan-out, rows, and what a stage
  supplies that the skeleton does not.

**Stage 3 grew** to include `stages.py`: `script.py` lost its completion tail
to it, so the two do not split. **Stage 7 was renamed** from "review and
approval" to "derived state" and gained `projection.py` — the version status
view and the project phase are the same idea at two levels, and reading them
together is the point.

**Stage 4** gained the real Anthropic adapter and record/replay. **Stage 9**
gained the three components that replaced `script-review.tsx`.

Six new questions come from bugs found in M2 rather than from the design:
the partial idempotency index, `_REQUEUE_IS_HONOURED`, the omitted
`temperature`, the un-copied `templates/`, the un-forwarded env var, and the
two tests that were passing without checking anything. These are the most
instructive parts of the milestone and they are not documented anywhere else.

**Still true and worth stating:** stages 0–2 are ● Confirmed and were not
re-opened. Nothing M2 changed contradicts what was confirmed there.

### 2026-08-03 · Stage 1b · ◐ Partial

**Solid:** the domain/IO split and its testing payoff, from cold. All three
phase rules in order, with the right reason for rule 2 — concurrency means a
stage can be reviewable while a sibling is still generating. The four ADR-016
axes, once the axes were named. Topology-versus-product-vocabulary as a
distinction.

**Two gaps, and they are the same gap:**

1. **Q2 needed scaffolding.** From a cold "why isn't a series asset an edge",
   only the resolution axis came out. With "what happens on approval / what
   satisfies it / what does unmet mean" as prompts, all four followed cleanly.
2. **Q4 stopped at the principle.** "Topology and product phase are different
   concepts" is right and is not the answer to "what does adding a stage then
   not touch". The answer is that `phases.py` never changes, and the case that
   kills the positional alternative is *insertion* — a stage added in the
   middle would silently rewrite the phases of everything downstream.

Both are the pattern already recorded against stage 1 (`is_terminal`):
**principle held, consequence not run forward.** Third occurrence; worth
treating as the thing to practise rather than as three separate misses. The
drill that worked in stage 1 was pen-and-paper — do the same here: write out
what a `music` stage would change, file by file, before reading.

**One slip corrected:** projects pin character versions; series own them. Said
"series created after", which inverts the ownership.

**Follow-up (the "what would break if"):** answered "stuck generating, no
worker picks it up". Wrong outcome, and instructive — that is precisely the
failure `STAGE_TASKS` exists to prevent, and [tasks.py:65] names it. Actual
answer: the stage renders in the rail, `can_generate` is False, and a direct
POST is a 400 with the implemented list. **The instinct is right one step
over:** add the `STAGE_TASKS` entry and forget the worker, and the stuck job
happens exactly as described. The guard is one registry entry wide.

**Earned, not asked for:** ADR-009's "adding a stage is a config edit plus a
worker, never a schema change" is not quite true. A genuinely new
`ArtifactKind` is a native Postgres enum member and needs
`ALTER TYPE … ADD VALUE`. That is the stage 1 carry-forward arriving from a
different direction — it was flagged for stage 5 and turned up here first.

**Close with:** the `music` exercise above, then a two-question re-review
(Q2 cold, Q4's consequence). ~20 minutes.
