# ADR-017 — No SSE. Polling stays, and the measurement says why

- **Status:** Accepted
- **Date:** 2026-08-19
- **Deciders:** kmaciver
- **Related:** closes finding **S7**; confirms ADR-006 (polling first) as
  permanent rather than provisional; constrained by ADR-002 (uWSGI)
- **Verified by:** ticket M5-10

## Context

ADR-006 chose polling and said SSE would be reconsidered "once there is real UI
to feel the latency". The transactional outbox (ADR-003) has drained to a Redis
pub/sub channel with **no subscriber** since M1-05, deliberately, so that this
decision could be made against measurements instead of intuition. §19.6 already
warned that the uWSGI story for SSE is brittle.

M5-10's exit criterion was written as a test, not a task: *ship it measured or
not at all — the keep-or-drop criterion is whether it removes latency a reviewer
can actually feel.*

## What was measured

**How long a reviewer actually waits.** From `provider_usage.latency_ms`, which
is timed in Python with `time.monotonic` and is therefore trustworthy, over the
real (non-mock) generations in this deployment:

| stage | provider | p50 | max |
|---|---|---|---|
| `image.generate` | google | **129.0 s** | 149.5 s |
| `prompts.generate` | anthropic | **69.6 s** | 82.5 s |
| `references.generate` | google | 24.4 s | 27.8 s |
| `research.generate` | anthropic | 19.9 s | 21.6 s |
| `scenes.generate` | anthropic | 15.4 s | 18.8 s |
| `script.generate` | anthropic | 7.9 s | 8.5 s |
| `caption.generate` | anthropic | 4.5 s | 4.5 s |
| `voice.generate` | elevenlabs | 2.9 s | 3.3 s |

The **fastest** real stage is 2.9 seconds. The two heaviest are one and two
minutes. Stages that call nobody (`timeline`, `render`, `thumbnail`, `package`)
do not appear because their provider latency is zero by construction; their wall
time is dominated by FFmpeg and Pillow, not by anything a stream would announce
sooner.

**What polling costs against that.** The review screen polls at 1500 ms only
while a stage is GENERATING, so the delay SSE would remove is uniform on
[0, 1500 ms] — a mean of **750 ms**. Against a 2.9-second wait that is 26%;
against the 129-second image stage it is 0.6%.

**Where the fast interactions actually are.** Approve, reject, edit and delete
are mutations, and they do not poll at all: they invalidate and refetch on
success, so they already resolve in one round trip. SSE would not touch the
only interactions in this product that happen at human speed.

**What it would cost to serve.** From uWSGI's own startup log rather than the
config file: `spawned uWSGI worker 1..4 (cores: 2)` — **4 processes × 2 threads
= 8 concurrent request slots** for the entire API. (§19.6 and the M5 roadmap
both said `processes=1 threads=16`; both were wrong, and the real number is
half.) An SSE stream holds one of those slots for its lifetime, so a reviewer
with two tabs open would permanently consume **25%** of the API's total
concurrency to save 750 ms.

And it would not survive its own first minute: `harakiri = 30` kills any request
that outlives 30 seconds **and recycles its worker**. A long-lived stream is
exactly the request that setting exists to catch. Supporting SSE means either
raising harakiri globally — discarding the watchdog that enforces NF2's
`p95 < 500 ms` on every other endpoint — or carving out a second listener with
its own limits, which is a separate deployment surface for a 750 ms saving.

## Decision

**No SSE.** Polling is the delivery mechanism, permanently, and ADR-006's
"first" is now "only".

## Consequences

- The measured benefit is a mean of 750 ms removed from waits of 2.9–129
  seconds. At the end of a two-minute wait nobody is watching the pixel, and
  the interactions people *do* watch never polled in the first place. This is
  not latency a reviewer can feel, which is precisely the criterion M5-10 set.
- The saving would be bought with 1/8th of the API's concurrency per open tab,
  a stream that harakiri kills every 30 seconds, and a **second delivery path
  that must agree with the first** — polling cannot be removed either way, since
  a dropped stream must fall back to it. SSE here is purely additive
  complexity.
- **The outbox stays, and nothing about it changes.** It is load-bearing for
  audit and correctness (ADR-003) regardless of who reads it; that was the
  argument for building the durable half first and it survives this decision
  intact. The drain keeps publishing to `videoforge:events`: the publish is one
  pipelined command per batch on a connection that already exists, Redis
  pub/sub drops messages with no subscriber rather than queueing them, and a
  live event stream is the obvious substrate for a future consumer that is not
  a browser — a desktop notifier, the M6 reconciler, an external webhook. What
  is rejected is *SSE to the browser*, not eventing.
- If this is ever revisited, the thing that would change the arithmetic is not
  a faster stream but a **faster pipeline**. Reopen when a p50 stage drops below
  roughly two seconds, or when the API stops being served by a
  fixed-slot, harakiri-bounded WSGI server.

## Alternatives rejected

- **SSE behind a feature flag.** A flag does not reduce the cost of the code
  paths; it doubles them and adds a configuration under which the app behaves
  differently, which is the worst outcome for a two-path delivery problem. The
  flag was the M5 plan's own proposal, and the measurement removed its premise.
- **WebSockets.** Strictly worse under ADR-002: the same held slot, the same
  harakiri, plus a protocol upgrade uWSGI would have to be configured for.
- **Long polling.** Held slots again, with harakiri now the timeout — the
  brittleness of SSE with none of its ergonomics.
- **Shortening the poll interval to 500 ms.** Triples the request rate to
  recover 500 ms of the same 750 ms, against waits measured in tens of seconds.
  It buys the same imperceptible thing at a real cost, which is this ADR's
  argument turned on the cheaper option.
- **Deferring the decision to M6**, as the roadmap suggested. The evidence is in
  hand now; shelving it would mean re-deriving it later against the same
  numbers.
