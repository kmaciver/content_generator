# ADR-005 — Renderer as an isolated Node service on Redis Streams

- **Status:** ⚠️ **SUPERSEDED by [ADR-012](ADR-012-ffmpeg-in-celery.md)** (decision D4, 2026-07-30)
- **Original status:** Accepted
- **Related:** SADD §7.5, §13, §16.4

> **Superseded — read ADR-012 first.** Kept because the reasoning explains why
> the original topology existed, and because anyone proposing to reintroduce an
> out-of-process renderer needs to know what it cost.

## Original context and decision

Remotion requires Node, headless Chromium, and FFmpeg — a dependency surface
sharing nothing with the Python workers. Renders were expected to be the
heaviest jobs and the first thing to need GPU or remote scaling, and a
Chromium OOM must not take down Celery.

So the renderer was a separate container consuming a Redis **Stream** (not the
Celery broker), with `XREADGROUP` claim semantics, `XAUTOCLAIM` recovery for
dead consumers, and an HMAC-signed callback (`POST /internal/render-callbacks`)
to report completion.

## Why it was superseded

The premise was **Ken Burns motion**. §1.0 of the implementation plan removed
motion from the product entirely (still images, voice-over, captions), and
§1.0.2's analysis of the reference videos showed captions are single-word
sequential display — which ASS subtitles render natively. With no motion and
no React-dependent typography, Remotion's per-frame Chromium cost bought
nothing, and the entire isolation rationale (Node deps, Chromium crashes)
evaporated with it.

## What removing it deleted

Not just an engine swap — a whole distributed-systems seam:

- the Redis Streams contract, consumer groups, and `XAUTOCLAIM` reconciliation;
- the HMAC-signed callback endpoint;
- the renderer heartbeat service;
- **finding B5 entirely** — the callback duplicated the state-machine
  completion path in the API, which contradicted "the API never generates
  anything". As a Celery task it runs through the same skeleton as every other
  stage, so there is one completion path by construction rather than by
  discipline.

## If this is ever revisited

Reintroducing an out-of-process renderer reintroduces B5. The timeline JSON
remains renderer-agnostic (see the neutrality rule in the M4 plan), so the
swap stays possible — but the callback-duplication defect must be solved
again, not rediscovered.
