# Development

## The short version

```bash
make up          # development profile, hot reload
make check-all   # every quality gate
```

You do not need Python, Node, uv, or any linter installed. The toolchain runs
in containers ([ADR-014](adr/ADR-014-containerised-toolchain.md)), so local and
CI use identical versions by construction rather than through two lists someone
keeps in sync.

## Quality gates

`make check-all` runs exactly what CI runs:

| Target | Covers |
|---|---|
| `make lint` | ruff |
| `make fmt` / `fmt-check` | black + isort (+ ruff's safe fixes) |
| `make typecheck` | mypy, strict |
| `make test` | pytest |
| `make lint-js` / `typecheck-js` / `fmt-check-js` | eslint, tsc, prettier |

Three test files enforce architecture rather than behaviour, and are worth
knowing about before you get an unexpected failure:

- **`tests/test_workspace_structure.py`** — every workspace package imports;
  `domain/` imports no framework (flask, sqlalchemy, celery, redis, boto3,
  httpx, psycopg); `backend` and `workers` never import each other. AST-based,
  so a violation is caught even in a module nothing imports.
- **`tests/test_secret_isolation.py`** — provider keys reach only the
  provider-calling workers, in the compose files *and* in backend source.
- **`apps/workers/tests/test_celery_app.py`** — delivery-semantics settings,
  including an assertion that the broker visibility timeout exceeds the task
  time limit. Violating that is the duplicate-artifact bug R5 describes.

## An optional host environment

Nothing requires one — its only value is editor support (autocomplete,
go-to-definition, inline type errors).

```bash
pyenv virtualenv 3.12.8 content_generator
pyenv local content_generator
python -m pip install uv==0.12.0
pyenv rehash
UV_PROJECT_ENVIRONMENT="$(pyenv prefix content_generator)" uv sync --all-packages
```

Point your editor's interpreter at `$(pyenv prefix content_generator)/bin/python`.
The root `pyproject.toml` holds the mypy and ruff config, so your editor's
diagnostics match `make check`.

**Python 3.12, not 3.13** — decision D1. Python 3.13 removed the
interpreter-init and thread-state C API that uWSGI's Python plugin drives by
hand ([ADR-002](adr/ADR-002-wsgi-server-and-interpreter.md)).

Two things will be missing from a host environment on purpose: `uwsgi` (a
deployment concern, excluded so no developer machine triggers a C compile) and
`ffmpeg` (installed in the worker image). Both are why `make test` covers pure
helpers while execution is verified by `make exit-test`.

## Adding a dependency

```bash
# edit the relevant pyproject.toml, then:
make lock && make sync
```

`uv.lock` is committed. CI runs `uv lock --check` and fails if the lockfile
has drifted from the manifests — the same `--frozen` guarantee the runtime
image depends on.

Frontend: edit `apps/frontend/package.json`, then rebuild
(`make frontend-image`) and refresh `package-lock.json`.

Two version pins there are **deliberately behind `latest`**, both discovered
by build failure in M0-11:

- **TypeScript 6.0.3** — TypeScript 7 (the Go-native port) does not expose the
  JS compiler API Next.js requires.
- **ESLint 9.39.5** — `eslint-plugin-react`, bundled inside
  `eslint-config-next`, calls an API ESLint 10 changed.

Also: `eslint-config-next` 16 ships **native flat config**. Wrapping it in
`FlatCompat` double-processes it and fails with a circular-structure error.

## Adding a Celery task

Use the skeleton — a task defined without it should fail review:

```python
from videoforge_workers.skeleton import videoforge_task

@videoforge_task(name="stage.action", queue="llm")
def action(...): ...
```

Then register the module in `celery_app.py`'s `imports=(...)`. Do **not** add a
side-effect import at the bottom of `celery_app` — that reintroduces the
circular import M0-09 removed (`render → skeleton → celery_app → ping →
partially-initialised skeleton`).

`queue` is mandatory because a task without one lands on Celery's default
queue, which nothing consumes, and the failure mode is silence.

## Conventions worth knowing

- **All artifact I/O goes through `videoforge_shared.storage`.** Nothing else
  may speak S3 — that single choke point is the entire cloud-migration story
  (SADD §24).
- **Keys are content-addressed.** Identical bytes dedup automatically; nothing
  can be overwritten.
- **Correlation ids** propagate nginx → Flask → Celery → logs. Use
  `correlation_context` (or the token-based bind/unbind pair for split
  framework lifecycles) and every log line inside carries it.
- **The domain layer stays framework-free** so it is testable without a
  database — enforced by test, not convention.
