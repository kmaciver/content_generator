# ADR-007 — Remotion as the rendering engine

- **Status:** ⚠️ **SUPERSEDED by [ADR-012](ADR-012-ffmpeg-in-celery.md)** (decision D4, 2026-07-30)
- **Original status:** Accepted with review trigger
- **Related:** SADD §7.5, §16.3; decisions D2 and D4

> **Superseded — read ADR-012 first.**

## Original context and decision

Remotion renders React compositions to MP4 via headless Chromium. For "video
as code from JSON" it was the strongest fit: pan/zoom easings, captions, and
transitions become ordinary React components driven by the Timeline JSON —
reviewable, diffable, unit-testable.

Licensing was flagged as a review trigger: Remotion is source-available, free
for individuals and small teams, paid above a company-use threshold.

## Two things changed

**D2 (licensing) resolved harmlessly.** The project is personal and
non-commercial, so the free tier applied. This ADR retains that finding *and
its review trigger* — monetisation of the channel, or use for anyone else's
content, plausibly re-opens the licence question. That trigger only matters if
Remotion ever returns.

**D4 (engine) superseded the decision on technical grounds.** Removing motion
(§1.0) left Remotion's value resting entirely on caption typography. The
reference analysis (§1.0.2) then showed captions are **single-word sequential
display** — `of` and a bare year each get their own frame — which is a handful
of short-lived text events, not karaoke highlighting. ASS renders that
natively, and the reference's bold-white-with-heavy-black-outline look *is*
ASS's default `BorderStyle=1` rendering.

Remotion renders every frame through Chromium whether or not pixels change.
For genuinely static content that cost buys nothing.

## Consequences

- Superseded by ADR-012 (FFmpeg in a Celery task).
- Risks **R2 (licensing)** and **R4 (Chromium memory/throughput)** are retired.
- Frame-accurate live preview (Remotion Studio) is lost; the substitute is
  that a 3-second test render takes about a second.
- The trigger to revisit is a future desire for **real animation** — not
  caption tweaks, which ASS handles.
