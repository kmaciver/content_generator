# ADR-012 — FFmpeg rendering inside a Celery task

- **Status:** Accepted
- **Date:** 2026-07-30
- **Supersedes:** [ADR-005](ADR-005-renderer-isolation.md), [ADR-007](ADR-007-rendering-engine.md)
- **Related:** decision D4; implemented and verified in M0-09
- **Verified by:** the M0 exit test (`make exit-test`)

## Context

Two premises behind the original renderer design expired:

1. **Motion was removed** (§1.0). Videos are still images with voice-over and
   captions — no pan, no zoom.
2. **Captions turned out to be simpler than assumed** (§1.0.2). Analysis of
   two reference videos showed single-word sequential display — function words
   and bare numerals each get their own frame — not karaoke highlighting.

Remotion's value proposition is animation as React code, paid for by rendering
every frame through headless Chromium. With neither motion nor
React-dependent typography, that cost buys nothing.

## Decision

**Render with FFmpeg, in an ordinary Celery task on a `render` queue**, through
the same task skeleton as every other stage.

Every requirement maps onto a native FFmpeg mechanism:

| Requirement | Mechanism |
|---|---|
| Still per scene, ~4s hold | `concat` demuxer with per-entry durations |
| Single-word captions, heavy outline | ASS: one dialogue event per word, `\an5` + `\pos`, `\bord` |
| Optional scale-pop on entry | ASS `\t` transform |
| Crossfade between images | `xfade` |
| Narration + music with ducking | `amix` + `volume` driven by the compiler's baked gain envelope |
| 1080×1920 H.264/AAC, `+faststart` | direct encode flags |

## Consequences

**Deleted, not merely replaced:** the Node runtime, Chromium, `pnpm` outside
the frontend, the Redis Streams contract and its `XAUTOCLAIM` reconciliation,
the HMAC callback endpoint, the renderer heartbeat — and finding **B5**, since
completion now runs through one code path by construction.

**Retired risks:** R2 (Remotion licensing), R4 (Chromium memory/throughput).
Renders drop from minutes to seconds, so NF10 stops being a target.

**Given up:** frame-accurate live preview (Remotion Studio). The substitute is
that a 3-second test render takes about a second — arguably a faster loop for
this content. Rich per-frame animation becomes genuinely hard rather than
merely unused.

**Boundaries that must hold** (encoded in `videoforge_workers/render.py`):

- ffmpeg is invoked with a **list argv and `shell=False`** — never a shell
  string. Filter graphs are full of shell metacharacters.
- Inputs come from MinIO via `get_bytes_verified`, so a corrupted asset fails
  the job rather than becoming garbage frames discovered at review.
- Output is **self-checked before upload**: ffprobe for duration and streams,
  plus a parse of the MP4 box structure confirming `moov` precedes `mdat`
  rather than trusting the `-movflags` flag.
- ffmpeg's stderr is scanned for libass font-resolution failures. The one
  silent failure mode of ASS is falling back to tofu boxes while exiting 0.

**The timeline JSON stays renderer-agnostic.** Abstract transition names,
resolved gain envelopes, and absolute millisecond offsets are in; anything
shaped like a React interpolation config is out. That discipline is what keeps
this decision reversible — and reversing it means re-solving B5 (ADR-005).
