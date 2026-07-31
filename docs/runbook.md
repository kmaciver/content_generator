# Runbook

Operating VideoForge locally. Written for the person who has to fix something
at an inconvenient moment — symptom first, then what to do.

Every command assumes the repository root.

---

## Orientation

```bash
make status                    # health of every service
make logs svc=worker-render    # follow one service
make exit-test                 # 22 assertions across the whole stack
```

`make exit-test` is the fastest way to answer "is anything broken, and what".
It tears the stack down afterwards unless you set `KEEP_STACK=1`.

Two profiles, and they differ deliberately:

| | `make up` (development) | `make up-prod` (production-local) |
|---|---|---|
| Entry point | API direct on **:5010** | nginx on **:8080** only |
| App server | Flask dev server, hot reload | uWSGI, unix socket |
| Stores | ports published for `psql`/`redis-cli` | not published |
| Source | bind-mounted | baked into the image |

**A one-off command run under the prod profile executes the baked image, not
your working tree.** This cost real debugging time in M0-05: a fix looked like
it had failed when the container was simply running older code. Use the dev
profile when you need your edits to take effect.

---

## Symptoms

### A service never becomes healthy

```bash
make status
make logs svc=<name>
```

Ordering is enforced by `depends_on`, so a failure early in the chain stalls
everything after it. Check in this order: **postgres → migrate → minio-bootstrap
→ backend → nginx**. The one-shots (`migrate`, `minio-bootstrap`) must exit
**0**; `make status` will not show them once they have finished, so use
`docker compose ... ps -a`.

### The API is up but `/health/deep` returns 503

Expected during a store outage — the endpoint's job is to say *which*
component is down, and it names it with the underlying error. Liveness
(`/health`) stays 200 on purpose: deep health is deliberately **not** wired
into any container healthcheck, because restarting the API because Postgres is
down only adds chaos to an outage.

### Jobs are queued but nothing runs

1. Are the workers alive? `make status` — each should be `healthy`.
2. Is the queue being consumed? <http://localhost:5555> shows workers and
   their subscriptions.
3. Round-trip a ping down the suspect queue:

```bash
docker compose --project-directory . -f docker/compose/docker-compose.yml \
  -f docker/compose/compose.prod.yml run --rm --no-deps worker-core \
  python -c "from videoforge_workers.ping import enqueue_ping; print(enqueue_ping('llm').get(timeout=30))"
```

Silence with healthy workers usually means the task was enqueued **without an
explicit queue** and landed on Celery's default queue, which nothing consumes.
That is why `videoforge_task` makes `queue` a required argument.

### A render fails

The render task self-checks before uploading, so failures are usually loud and
specific:

- **`FfmpegError: caption font problem`** — libass could not resolve the font.
  The image installs `fonts-dejavu-core` and `fontconfig`; if either is
  missing the render would otherwise silently produce tofu boxes and exit 0.
- **`IntegrityError`** — a source asset's bytes do not match the digest in its
  key. Real corruption; investigate rather than retry.
- **timeout** — the task's ffmpeg call exceeded its limit. Check whether
  `worker-render`'s CPU limit is starving it.

### Everything is slow

`worker-render` is CPU-limited on purpose so encoding cannot starve the LLM
and image queues. If renders are the bottleneck and you have headroom, raise
`cpus` on that service.

---

## Routine tasks

### Apply migrations

```bash
make migrate           # apply
make migrate-check     # fail if models and migrations disagree
make migrate-history   # what is applied, and the current head
```

`migrate` also runs automatically on every `make up` / `make up-prod`, and is
idempotent.

### Create a migration

```bash
make migrate-new m="add artifact tables"
```

Autogenerate is a **draft**. Read it before committing (SADD §10.4) — it
routinely misses server defaults and enum changes.

### Rotate a provider key

Edit `.env`, then recreate only the workers (nothing else ever holds the keys):

```bash
docker compose --project-directory . -f docker/compose/docker-compose.yml \
  -f docker/compose/compose.prod.yml up -d --force-recreate \
  worker-llm worker-media worker-core
make verify-secrets    # confirm the boundary still holds
```

### Inspect object storage

Dev profile publishes the MinIO console at <http://localhost:9001>
(credentials from `.env`, defaults `videoforge` / `videoforge-dev`).

Artifact keys are content-addressed: `{sha256[:2]}/{sha256}/{filename}`. The
digest in the key is the integrity check — a fetch can always be verified
against it.

### Start completely fresh

```bash
make reset             # DESTRUCTIVE: drops pgdata, miniodata, redisdata
```

Prompts for confirmation. Everything generated so far is gone, artifacts
included. There is no undo and no backup.

---

## Before you conclude "it works"

Two false-negative traps bit during M0. Both produced confident, wrong
"everything is fine" results:

1. **`/dev/tcp` is a bash feature.** Under zsh it fails for every port, so a
   port-reachability loop reports "closed" regardless of truth. Use `nc -z` or
   `docker compose ps`.
2. **A zero-result search proves nothing without a positive control.** When
   checking that a secret is absent, also confirm the same method *finds* a
   string that is present. The exit test does this.

The general rule: a check that can only ever print "pass" is not a check.
