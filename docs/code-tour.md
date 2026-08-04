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
| 2 | Trace A — browser click to a job row | ~830 | ~1 hr | ● |
| 3 | Trace B — worker picks it up, version appears | ~680 | ~1 hr | ☐ |
| 4 | The provider seam | ~490 | ~40 min | ☐ |
| 5 | The tables, properly | ~1,070 | ~1.5 hr | ☐ |
| 6 | Repositories and the unit of work | ~1,200 | ~1.5 hr | ☐ |
| 7 | Review and approval — the product's point | ~640 | ~1 hr | ☐ |
| 8 | Cross-cutting: config, logs, storage, secrets | ~1,000 | ~1.5 hr | ☐ |
| 9 | The frontend | ~770 | ~1 hr | ☐ |
| 10 | Infrastructure — compose, nginx, migrations | ~600 | ~1 hr | ☐ |
| 11 | Tests as the specification | ~900 | ~1 hr | ☐ |

**~8,900 lines of the 12,300 in the repo.** The gap is the skip list at the
bottom — deliberate, not an oversight.

Legend: ☐ not started · ◐ read, gaps recorded · ● confirmed by review

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

## Stage 2 — Trace A: a click becomes a job row

Follow "Generate script" from the browser to the database. Read **in call
order**, not file order. Skim anything about the artifact tables — stage 5.

| File | Lines | Read for |
|---|---|---|
| `apps/frontend/src/app/projects/[id]/script-review.tsx` | 339 | *Only* the generate button and its mutation |
| `apps/frontend/src/app/api/bff/[...path]/route.ts` | 110 | The BFF hop — why it exists at all |
| `apps/frontend/src/lib/server/backend.ts` | 64 | Where the API token is injected |
| `apps/backend/src/videoforge/api/projects.py` | 322 | The endpoint, and how thin it is |
| `apps/backend/src/videoforge/services/jobs.py` | 243 | The heart of this stage |
| `apps/backend/src/videoforge/services/dispatch.py` | 96 | Why publishing is *after* commit |

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

The same job, from the other side.

| File | Lines | Read for |
|---|---|---|
| `apps/workers/src/videoforge_workers/celery_app.py` | 121 | Queues, routing, what boots |
| `apps/workers/src/videoforge_workers/db.py` | 52 | Why workers make their own engine |
| `apps/workers/src/videoforge_workers/skeleton.py` | 277 | The shape every task shares |
| `apps/workers/src/videoforge_workers/script.py` | 227 | The first real stage |
| `apps/workers/src/videoforge_workers/outbox.py` | 124 | The drain — pairs with [ADR-003](adr/ADR-003-transactional-outbox.md) |

**The question:** how many transactions does one job use, and why not one?

**Confirmed when you can:**
- Name the three transactions and say what each protects.
- Explain why the *claim* commits immediately instead of joining the body.
- Explain what the outbox is for — specifically, which two-writes problem it
  removes. Read ADR-003 alongside.
- Say what happens when the same job is delivered twice, at each of the three
  points it could arrive.

---

## Stage 4 — The provider seam

| File | Lines |
|---|---|
| `packages/providers/src/videoforge_providers/protocols.py` | 52 |
| `packages/providers/src/videoforge_providers/models.py` | 106 |
| `packages/providers/src/videoforge_providers/mock.py` | 114 |
| `packages/providers/src/videoforge_providers/middleware.py` | 131 |
| `packages/providers/src/videoforge_providers/registry.py` | 82 |
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

---

## Stage 5 — The tables, properly

Now go back to what you skimmed.

| File | Lines |
|---|---|
| `packages/persistence/src/videoforge_persistence/columns.py` | 128 |
| `packages/persistence/src/videoforge_persistence/base.py` | 30 |
| `packages/persistence/src/videoforge_persistence/enum_types.py` | 61 |
| `packages/persistence/src/videoforge_persistence/models/artifact.py` | 185 |
| `packages/persistence/src/videoforge_persistence/models/job.py` | 139 |
| `packages/persistence/src/videoforge_persistence/models/review.py` | 104 |
| `packages/persistence/src/videoforge_persistence/models/audit.py` | 140 |
| `packages/persistence/src/videoforge_persistence/models/project.py` | 74 |
| `packages/persistence/src/videoforge_persistence/models/org.py` | 108 |
| `packages/persistence/src/videoforge_persistence/sql.py` | 163 |

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

---

## Stage 6 — Repositories and the unit of work

| File | Lines |
|---|---|
| `packages/persistence/src/videoforge_persistence/repositories/base.py` | 61 |
| `packages/persistence/src/videoforge_persistence/repositories/artifacts.py` | 282 |
| `packages/persistence/src/videoforge_persistence/repositories/jobs.py` | 271 |
| `packages/persistence/src/videoforge_persistence/repositories/reviews.py` | 108 |
| `packages/persistence/src/videoforge_persistence/repositories/projects.py` | 160 |
| `packages/persistence/src/videoforge_persistence/repositories/audit.py` | 179 |
| `packages/persistence/src/videoforge_persistence/uow.py` | 136 |
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

---

## Stage 7 — Review and approval

The reason the product exists. Everything else is delivery.

| File | Lines |
|---|---|
| `apps/backend/src/videoforge/services/review.py` | 226 |
| `apps/backend/src/videoforge/dto/__init__.py` | 254 |
| `packages/persistence/.../sql.py` | the `artifact_version_status` view — re-read |
| [sadd.md](architecture/sadd.md) §17, §18 | The workflow and the versioning rules |

**The question:** where does a version's status come from?

**Confirmed when you can:**
- Say why the status is **not stored anywhere** and what that buys (finding B1).
- Recite the view's `CASE` ordering, and explain why `REJECTED` must be tested
  before `SUPERSEDED`. Say it in product terms: what would the UI show if the
  order were flipped?
- Explain what an "edit" creates, and why it does not arrive approved.
- Explain what happens to v1 and v2 when v3 is approved — all three, precisely.

---

## Stage 8 — Cross-cutting concerns

| File | Lines | Theme |
|---|---|---|
| `packages/shared/src/videoforge_shared/settings.py` | 295 | Config, and the NF8 boundary |
| `packages/shared/src/videoforge_shared/correlation.py` | 103 | One id across five processes |
| `packages/shared/src/videoforge_shared/logging.py` | 135 | Structured logs |
| `packages/shared/src/videoforge_shared/storage.py` | 212 | All artifact I/O goes here |
| `packages/shared/src/videoforge_shared/hashing.py` | 62 | Content addressing ([ADR-004](adr/ADR-004-content-addressed-storage.md)) |
| `packages/shared/src/videoforge_shared/ids.py` | 24 | ULIDs — why not UUID4 |
| `packages/shared/src/videoforge_shared/tasks.py` | 58 | Publishing by name without importing workers |
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
| `apps/frontend/src/lib/api.ts` | 180 |
| `apps/frontend/src/app/providers.tsx` | 35 |
| `apps/frontend/src/app/projects/[id]/script-review.tsx` | 339 (now properly) |
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

---

## Stage 10 — Infrastructure

| File | Read for |
|---|---|
| `docker/compose/docker-compose.yml` | The topology, and `depends_on` ordering |
| `docker/compose/compose.dev.yml`, `compose.prod.yml` | What the profiles change |
| `docker/nginx/` | Routing, and the asset path |
| `Makefile` | The commands, and why the toolchain is containerised ([ADR-014](adr/ADR-014-containerised-toolchain.md)) |
| `database/migrations/env.py` | How autogenerate finds the models |
| `database/seed/demo.py` | 261 lines that document the intended states |

**The question:** what does `make up-prod` start, in what order, and what is
each thing waiting for?

**Confirmed when you can:**
- Explain why `migrate` is its own one-shot service rather than a step in the
  API's startup.
- Name which containers receive provider keys and which do not — and where that
  is declared.
- Explain why the seed writes through real repositories instead of INSERTs.

---

## Stage 11 — Tests as the specification

Read last, on purpose. Now they read as claims about the system rather than
noise.

| File | Reads as |
|---|---|
| `packages/domain/tests/test_artifact_lifecycle.py` | The FSM's actual contract |
| `tests/test_schema.py` | What the database enforces by itself |
| `tests/test_double_delivery.py` | The idempotency story, proven |
| `tests/test_script_stage.py` | The full stage, end to end |
| `tests/test_secret_isolation.py` | NF8, as an executable claim |
| `apps/frontend/e2e/review-flow.spec.ts` | M1's exit criterion in one file |

**The question:** which test fails first if someone makes artifacts mutable?

**Confirmed when you can:**
- Name a property that *only* exists in the database and cannot be unit-tested
  against a fake — and the test that covers it.
- Explain what a "positive control" is here and why a check that can only print
  "pass" is not a check. This idea recurs through the whole repo.
- Point at a test that would still pass if the feature were broken, if one
  exists. (Genuinely open — if you find one, that is a finding.)

---

## Deliberately not on the plan

Skipping is a decision, so here it is explicitly.

| Skipped | Why |
|---|---|
| `database/migrations/versions/…747ca6bd5ff6_core_schema.py` (1,028) | Machine-generated from the models you read in stage 5. Read the models. The only hand-written parts are the trigger and view installs — those live in `sql.py`. |
| `apps/workers/render.py` (316), `ping.py` (43) | M0 skeletons, not on the M1 path. They become relevant at M4. |
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
