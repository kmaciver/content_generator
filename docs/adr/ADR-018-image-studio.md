# ADR-018 — Image Studio: free-form images as a third job scope

- **Status:** Accepted
- **Date:** 2026-11-15
- **Deciders:** kmaciver
- **Related:** extends the job-scope rule from ADR-016 / M3-04b; relies on
  ADR-004 (content-addressed storage), ADR-006/017 (polling), ADR-011 (asset
  serving), NF8 (provider keys only in workers)

## Context

The Google account behind the image pipeline is also wanted for one-off
images, by people who are not making a video. Google's own consoles (Cloud
Console, AI Studio) proved hard to use for that, and the Gemini app is billed
by subscription and cannot use the API key. So the studio lives in this app:
a prompt, a model, a shape, optional reference images, and a gallery.

Two requirements separate it from the pipeline:

1. **No house style.** The prompt reaches the provider verbatim — no series
   character, no style block, no prompt template, no negative prompt.
2. **Still metered.** Studio images cost the same money as pipeline images and
   must land inside the daily cap (S10).

## Decision

**A studio request is its own row, and its job is a third scope.**

- `studio_generation` holds the request (prompt, model, aspect ratio, count,
  reference keys). `studio_image` holds one row per picture; the bytes are in
  the assets bucket, content-addressed (ADR-004).
- `generation_job.studio_generation_id` is added, and `ck_generation_job_scope`
  becomes `num_nonnulls(project_id, series_id, studio_generation_id) = 1`.
- `POST /studio/generations` writes the request and the job and answers 202;
  `studio.generate` runs on the `image` queue in `worker-media`. The page polls
  the gallery while anything is in flight.
- Status is **not** stored on `studio_generation`. It is the job's status, read
  through the new foreign key, so a crashed worker cannot leave two sources of
  truth disagreeing.
- Choices and limits (`videoforge_shared.studio`) are served by
  `GET /studio/options` and enforced by the API; quoted prices are pinned to
  `pricing.py` by a test.

## Alternatives rejected

- **Make studio images artifacts.** `artifact.project_id` is NOT NULL (the same
  wall ADR-016 hit), and an artifact's FSM, review, staleness and pipeline
  position mean nothing for a one-off picture.
- **Hang studio jobs off a hidden "Studio" project or series.** Satisfies the
  constraint by lying to it: the fake project would show up in project lists,
  phase projection, and spend-per-project, and branding resolution would apply
  to it.
- **Call the provider from the API.** The backend holds no provider keys (NF8,
  `test_secret_isolation.py`), and the API never generates (SADD §19.2).
- **No job row.** Loses idempotency, orphan recovery and — because
  `provider_usage.job_id` is NOT NULL — spend metering entirely.

## Consequences

- Downgrading past this revision **fails while any studio job exists** (it has
  neither a project nor a series). Delete the studio rows first if you mean it.
- Imagen is not offered: it uses `generate_images`, not the `generate_content`
  call `GoogleImageProvider` makes. Adding it is an adapter change, not a
  studio change.
- The app has no authentication. Anyone who can reach nginx (`:8080` on every
  interface) can generate images on this account. Acceptable on a home
  network; **not** acceptable exposed to the internet.
- A run is one transaction, as with reference sheets: if image three of four
  fails, none of the rows land, though the stored objects remain.
