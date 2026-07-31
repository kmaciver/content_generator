# ADR-002 — uWSGI as WSGI server, on Python 3.12

- **Status:** Accepted (with review trigger)
- **Date:** 2026-07-30
- **Deciders:** kmaciver
- **Supersedes:** the draft ADR-002 sketched in SADD §26
- **Related:** decision D1 in `IMPLEMENTATION_PLAN.md` §1.4; verified by ticket M0-00

## Context

The brief mandates two things that are in potential conflict: **uWSGI** as the
application server ("Do NOT use Gunicorn") and **Python 3.13** as the interpreter.

uWSGI has been in maintenance mode since 2022. Its Python plugin does not embed
CPython through the stable ABI; it drives interpreter startup and thread-state by
hand, using the pre-PEP-587 initialisation API. Python has been removing exactly
that surface — `Py_SetProgramName`, `Py_SetPythonHome`, and `PyEval_InitThreads`
were deprecated in 3.11 and removed in 3.13. So the risk was concrete rather than
generic: a build failure, or worse, a build that succeeds and then misbehaves
under threads.

Two properties made this worth resolving before any other work:

1. It is the only M0 item whose failure changes the architecture rather than the code.
2. The costs are wildly asymmetric. Giving up 3.13 costs nothing this stack uses
   (the 3.13 feature set is a new REPL, better tracebacks, experimental
   free-threading which is irrelevant under prefork, and an experimental JIT).
   Giving up uWSGI contradicts the brief's most emphatic constraint. Patching
   uWSGI's C code means maintaining a fork of the web server, which is
   indefensible for a solo operator.

## Decision

**Keep uWSGI. Pin the interpreter to Python 3.12.**

- Interpreter: `python:3.12-slim-bookworm`, pinned to bookworm rather than
  floating on `python:3.12-slim`, because uWSGI 2.0.x links against PCRE1
  (`libpcre3`) and Debian trixie dropped that package.
- uWSGI: **2.0.31** (released 2025-10-11), installed from sdist so it compiles
  during the image build. There are no wheels on PyPI, so a C-API incompatibility
  fails the build loudly instead of at runtime — this is a feature, not a
  nuisance, and the build must not be "fixed" by relaxing it.

Python 3.12 has security support into late 2028, which is ample runway.

## Verification (M0-00, executed)

Not argued — measured. Topology: nginx → unix socket → uWSGI → Flask 3.1.3.

| Check | Result |
|---|---|
| Compile against 3.12 | Pass — built `uwsgi-2.0.31-cp312-cp312-linux_aarch64.whl` |
| Runtime versions | Python 3.12.13, uWSGI 2.0.31 |
| Request round-trip through `uwsgi_pass` | Pass |
| Socket permission model | `srw-rw---- app uwsgisock` — `chown-socket` + shared gid 2000 both effective |
| Correlation id nginx → uWSGI → Flask | Pass; nginx `map` also mints one when absent |
| **Concurrency under `threads = 2`** | **Pass** — 8 concurrent requests in 2.07s vs 16s serial (7.7×), across exactly 4 processes and 8 threads; 16 concurrent completed in 4.03s as two clean waves |
| Harakiri on a hung request | Pass — 502 at exactly 30s, verbose log naming the request |
| Worker recovery after harakiri | Pass — `Respawned uWSGI worker 2 (new pid: 117)`; capacity returned to 4 |
| Graceful SIGTERM | Pass — in-flight 6s request returned **200**; stop took 5s (not the 30s grace timeout, so no SIGKILL fallback); exit code 0 |
| Socket cleanup (`vacuum`) | Pass — `VACUUM: unix socket /run/uwsgi/api.sock removed` |
| JSON `logformat` | Pass — structured lines with `req_id`, `worker`, `pid`, `msecs` |

The concurrency check was treated as mandatory rather than nice-to-have,
precisely because the C-API surface at risk *is* the thread-state machinery:
"compiles and serves one request" would have been a weak signal. It ran clean.

## Consequences

- The stack constraint is honoured, and §23.1's uWSGI configuration is validated
  as written rather than aspirationally.
- The deviation from the brief is the interpreter version, recorded here so it is
  explicit rather than silent. SADD §7.1 and risk R1 need amending from 3.13 to 3.12.
- `harakiri = 30` combined with `uwsgi_read_timeout 60s` in nginx means a stuck
  request is attributed to uWSGI (502) rather than being masked by an nginx
  timeout. Keep that ordering when tuning either value.
- `die-on-term = true` is load-bearing: uWSGI's default SIGTERM behaviour is
  *reload*, which would make `docker stop` hang for the full grace period.

### Amendment (M0-02): uWSGI runs fully unprivileged

The original verification ran uWSGI as root so it could bind and `chown` the
socket before dropping to `app:uwsgisock`. M0-02 removed the root phase entirely.

The image now creates `/run/uwsgi` as a **setgid** directory
(`install -d -o app -g uwsgisock -m 2770`), so a socket created there inherits
the `uwsgisock` group from the directory rather than needing `chown-socket`.
uWSGI therefore starts as `app` and never holds privilege at all;
`uid`, `gid`, and `chown-socket` are absent from `api.ini` deliberately.

Re-verified after the change, against the shared application image:

| Check | Result |
|---|---|
| Process identity | `uid=1000(app) gid=2000(uwsgisock)` — no root phase |
| Socket | `srw-rw---- app uwsgisock`, group inherited via setgid |
| nginx reachability | round-trip 200 through `uwsgi_pass` |
| Concurrency | 8 concurrent in 2.05s (7.8×) across 4 processes — unchanged |
| Graceful SIGTERM | in-flight 6s request returned 200; stop in 6s; exit 0 |
| `vacuum` | socket removed on exit |

`pythonpath` was also dropped from `api.ini`: uv installs the workspace members
into `/opt/venv`, so `videoforge` is importable as an ordinary distribution.

## Review trigger

Revisit if any of these occur:

1. A future CPython release breaks the build and upstream does not ship a fix.
2. Python 3.12 approaches end of security support (late 2028).
3. SSE becomes a hard requirement — synchronous workers hold one worker per open
   connection (see ADR-006, which currently defers SSE behind a flag).

The exit remains cheap **only while the transport boundary stays clean**: views
translate HTTP ⇄ DTOs ⇄ services and contain no business logic. That discipline,
not this pin, is what makes a future swap a one-package change. Any leakage of
domain logic into `api/` should be treated as eroding this ADR.

## Alternatives considered

- **Python 3.13 with a patched uWSGI** — rejected: maintaining a C fork of the
  web server.
- **Gunicorn / uvicorn-class server** — excluded by the brief; would also
  invalidate §23.1 and the `uwsgi_pass` configuration in §23.2.
- **Waitress** — simpler, but loses the process-management controls (harakiri,
  `max-requests`, `reload-on-rss`) that this design actively uses.
