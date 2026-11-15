# VideoForge — Handoff

**Written 2026-11-15.** For picking this project up on a different machine or
GitHub/Anthropic account. It records the things the repo does *not* already
tell you: what state the working tree is in, what breaks on a fresh machine,
and the one live bug.

Everything architectural is already written down and is not repeated here:

| Read this | For |
|---|---|
| [`README.md`](README.md) | What it is, quickstart, how the pieces fit |
| [`docs/architecture/sadd.md`](docs/architecture/sadd.md) | Full design |
| [`docs/adr/`](docs/adr/) | 17 decision records — the *why* behind everything |
| [`docs/code-tour.md`](docs/code-tour.md) | A 12-stage reading order |
| [`docs/runbook.md`](docs/runbook.md) | Operating it (incomplete — see M5-11) |
| [`docs/env-reference.md`](docs/env-reference.md) | Every config variable |
| [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) | Ticket-by-ticket history and rationale |

---

## 1. Do this first: there is uncommitted work

**47 files are uncommitted on `main`.** `origin/main` is in sync with local
`main`, so everything already committed is safely pushed — but this working
tree is the *only* copy of the following:

- **42 modified**, **5 untracked**
- **+1,882 / −158** lines across the modified set

The untracked files name their own tickets in their docstrings, so the change
set is identifiable:

| File | What |
|---|---|
| `apps/frontend/src/app/projects/[id]/budget-bar.tsx` | M5-07 — today's spend against the cap |
| `apps/frontend/src/app/projects/[id]/job-status.tsx` | M5-06 — what the worker is doing |
| `docs/adr/ADR-017-no-sse.md` | M5-10 — the decision not to ship SSE |
| `packages/shared/src/videoforge_shared/language.py` | `CONTENT_LANGUAGE` support |
| `packages/shared/tests/test_language.py` | its tests |

Together with the modified files (worker stages, prompt templates, the project
list, `settings.py`, `docs/env-reference.md`) this is the **M5 polish tier plus
multi-language content support**.

> **Commit or stash it before anything else.** A `make reset`, a branch switch,
> or a new clone loses it. Note that `IMPLEMENTATION_PLAN.md` already describes
> M5-06/07/08/10 as done — the plan was committed ahead of the code, so the
> tree is further along than `git log` suggests and *behind* what the plan
> claims.

```bash
git add -A && git commit -m "M5-06..10: job status, budget bar, dashboard grouping, ADR-017, CONTENT_LANGUAGE"
```

---

## 2. Where the project actually stands

**`README.md`'s status line is stale.** It says *"M0 and M1 complete; M2 in
progress."* That is wrong by three milestones. `IMPLEMENTATION_PLAN.md` is the
accurate source:

| Milestone | Tickets | State |
|---|---|---|
| M0 — Foundation | all | ✅ done |
| M1 — Domain spine + script slice | 12/12 | ✅ done |
| M2 — Full text pipeline | 13/13 | ✅ done |
| M3 — Media generation | 11/11 | ✅ done |
| M4 — Timeline + render | 12/12 | ✅ done |
| M5 — Packaging + polish | 10/12 | two open |
| M6 — Hardening | no tickets written | not started |

The money path works end to end: topic → research → script → scenes → prompts
→ images → voice → timeline → render → downloadable package with a verified
zip manifest.

### What is left

- **M5-09 — replay fixtures for image and voice.** `build_image_provider` and
  `build_voice_provider` still raise `UnknownAdapterError` for `record` and
  `replay`. Until this lands, `make e2e` proves the *pipeline* and **not** the
  adapters. The capture pass spends real money at four vendors and is
  deliberately an operator decision, not a build step.
- **M5-11 — seed a finished demo project, and finish `docs/runbook.md`.**
  Today someone cloning the repo cannot see a completed video without paying
  for one.
- **M6 — hardening.** Chaos tests (double delivery under load, killed workers
  mid-task, orphaned renders), the reconciler proper (`jobs.reconcile` has a
  `TaskSpec`, a queue and nothing driving it; `jobs.claim_orphans` exists
  unused), a render performance pass against NF10, docs freeze, `v1` tag.

Fix the README status line while you are in there.

---

## 3. Fresh machine / new account checklist

These are the things that will actually break. In order of how quickly you
will hit them.

### 3.1 The git remote uses an SSH host alias

```
origin  git@github.com-personal:kmaciver/content_generator.git
```

`github.com-personal` is **not a real host** — it is an alias from
`~/.ssh/config` on the old machine. On a new machine `git fetch`/`push` fails
with a DNS error until you either recreate the alias:

```
Host github.com-personal
  HostName github.com
  User git
  IdentityFile ~/.ssh/id_ed25519_personal
  IdentitiesOnly yes
```

…or point the remote at the plain host:

```bash
git remote set-url origin git@github.com:kmaciver/content_generator.git
```

`README.md`'s clone command already uses the plain form, so it and the
configured remote disagree. Under a **different GitHub account**, fork or
re-point `origin` to the new owner.

### 3.2 `.env` does not travel with the repo

`.env` is untracked by design — no secrets in git. A fresh clone has no `.env`
at all, which is fine (the stack boots entirely on mock defaults), but means
every key and password must be recreated:

```bash
make env-example && cp .env.example .env
```

Then fill in:

| Variable | How |
|---|---|
| `API_TOKEN` | `openssl rand -hex 32` |
| `INTERNAL_HMAC_SECRET` | `openssl rand -hex 32` |
| `POSTGRES_PASSWORD` | any value — it is a local container |
| `MINIO_ROOT_PASSWORD` | any value — same |
| `ANTHROPIC_API_KEY` | new key from the Console (see §4) |
| `ELEVENLABS_API_KEY` | voice |
| `STABILITY_API_KEY` | images |
| `GOOGLE_API_KEY` | Gemini images — **see the gap below** |
| `OPENAI_API_KEY` | only if you switch an adapter to `openai` |

> **`make env-example` omits `GOOGLE_API_KEY`.** The target generates
> `.env.example` by `awk`-ing the `dotenv` block out of
> `docs/env-reference.md`, and that block lists OPENAI / ANTHROPIC /
> ELEVENLABS / STABILITY but not GOOGLE — even though the prose above it
> documents `GOOGLE_API_KEY` and `packages/providers/.../google_adapter.py`
> exists. Add the line to the dotenv block in `docs/env-reference.md` (not just
> to your `.env`) so the generator stays correct. Observed in the working copy,
> where `docs/env-reference.md` is itself modified.

### 3.3 Anthropic account vs. API key

These are **two unrelated credentials**, and it is worth being explicit since
the account move is what prompted this document:

- Your **Claude subscription / Claude Code sign-in** is OAuth, per account.
- `ANTHROPIC_API_KEY` is a **Console** key, billed as API usage.

Changing one does nothing to the other. VideoForge only ever uses the API key.
Mint a fresh one at `console.anthropic.com` → API keys, in the workspace you
want billed.

### 3.4 Running it

Docker and `make` are the only host requirements — the whole toolchain is
containerised (ADR-014).

```bash
make up-prod      # nginx + uWSGI on :8080 — the full stack
```

| URL | What |
|---|---|
| `http://localhost:8080` | UI |
| `http://localhost:8080/api/v1/health/deep` | per-component health |
| `http://localhost:5555` | Flower — queues and workers |

```bash
make exit-test    # 22 assertions across the whole stack
make e2e          # review flow in a real browser (needs up-prod first)
make check-all    # every gate: ruff, black, isort, mypy, pytest, eslint, tsc, prettier
```

`make up` is the dev profile instead: hot reload, stores exposed, and the API
on **host port 5010** (not 5000 — macOS AirPlay Receiver holds :5000 from
Monterey onward). `make help`'s one-liner says ":5000", which is the *container*
port; the mapping is `${BACKEND_PORT_HOST:-5010}:5000`.

---

## 4. The one live bug: Anthropic 401

`scenes.generate` fails immediately with:

```
anthropic returned 401: {'type': 'authentication_error', 'message': 'invalid x-api-key'}
```

**Diagnosed 2026-11-15. The code and config are correct — the key value is
rejected.** Verified inside the running `worker-llm` container:

| Check | Result |
|---|---|
| `PROVIDERS__MODE` | `real` |
| `PROVIDERS__LLM__ADAPTER` | `anthropic` |
| `PROVIDERS__LLM__MODEL` | `claude-sonnet-5` |
| Key | 108 chars, `sk-ant-api03-` prefix |
| Quotes / whitespace / non-ASCII | none |
| `ANTHROPIC_BASE_URL`, proxies | unset |

So: not the empty-key path (the adapter raises at construction for that), not
`.env` quoting, not a trailing newline, not an admin key on the Messages API,
not a proxy. The credential is simply not active.

**On a new account this resolves itself** — you will mint a fresh key in §3.2.
Two things are worth carrying forward anyway:

1. **Compose bakes provider env at container *create* time.** There is no
   `env_file:` in `docker/compose/docker-compose.yml`; the keys arrive through
   `${ANTHROPIC_API_KEY:-}` interpolation in the `x-provider-secrets` anchor.
   Editing `.env` therefore does **nothing** to a running container, and
   `restart` will not pick it up:

   ```bash
   make down && make up-prod
   ```

2. **Compose reads `.env` from the repo root**, not from
   `docker/compose/`, because the Makefile passes
   `--project-directory "$(CURDIR)"`.

To test a key without involving the app:

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://api.anthropic.com/v1/models -H "x-api-key: $(grep '^ANTHROPIC_API_KEY=' .env | cut -d= -f2-)" -H "anthropic-version: 2023-06-01"
```

`200` = good key, stale container. `401` = dead key.

The 401 handling itself is correct and needs no change: `retryable=False`,
failed in 113 ms, no retries burned.

---

## 5. Landmines worth knowing

- **Provider keys reach worker containers only** (NF8, SADD §21.3). Never the
  API, frontend, or nginx. Enforced three ways: separate settings models
  (`ProviderKeys` is only constructible by `WorkerSettings`), static tests over
  the compose topology, and a runtime canary — `make verify-secrets`. If you
  add a service that needs a key, it has to be a worker.
- **Mock is the default and nothing can spend money by accident.**
  `config/providers.yaml` ships `mode: mock`; `PROVIDERS__MODE=real` is the
  only way to spend. `make e2e` *refuses* `real` and `record` outright — the
  suite approves twenty-odd stages, which on live providers is real money from
  a keystroke.
- **`DAILY_COST_LIMIT` defaults to `10.00`** and is checked *before* each
  provider call (M3-11), with a UTC day boundary computed database-side.
  `0` or blank means **no cap**, not "spend nothing". Surfaced on the project
  screen since M5-07; the API refuses a paid stage with 409 once spent.
- **`bills: true` is declared in `templates/pipeline.yaml`, not inferred from
  the queue.** `thumbnail` runs on the `image` queue and calls nobody;
  `caption` shares the `llm` queue and spends. A new paid stage that forgets
  the flag gets a worse error message; a free stage wrongly marked paid becomes
  unrunnable once the cap is hit, stranding a finished video.
- **`PROVIDERS__VOICE__VOICE_ID` must match `CONTENT_LANGUAGE`.** Setting
  `CONTENT_LANGUAGE=pt-BR` makes the text Portuguese; it does not make an
  English voice sound Brazilian. Image prompts stay English on purpose.
- **Artifacts are immutable and versioned.** Nothing is ever overwritten; every
  stage produces a reviewable version. Immutability is enforced by database
  triggers, so "just update the row" is not available to you.
- **`apps → packages` only.** No sideways imports between apps, enforced by
  tests rather than convention.
- **After changing code, run `graphify update .`** to keep `graphify-out/`
  current (AST-only, no API cost). See [`CLAUDE.md`](CLAUDE.md) for when the
  graph is worth querying — short version: architecture and reachability
  questions, not lookup.

---

## 6. Suggested first session on the new account

1. Commit the 47 uncommitted files (§1).
2. Fix the remote URL or recreate the SSH alias (§3.1), then push.
3. `make env-example && cp .env.example .env`; fill in the secrets; add the
   missing `GOOGLE_API_KEY` line to `docs/env-reference.md` (§3.2).
4. `make up-prod && make exit-test` on mock — confirms the stack before any
   money is involved.
5. Mint a new Anthropic key, set `PROVIDERS__MODE=real`, then
   `make down && make up-prod` (§4).
6. Correct the README status line (§2).
7. Pick up M5-09 or M5-11.
