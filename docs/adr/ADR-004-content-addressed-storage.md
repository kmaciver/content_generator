# ADR-004 — Content-addressed immutable artifact storage

- **Status:** Accepted
- **Date:** 2026-07-30
- **Related:** SADD §18; implemented in M0-05; amended by M0-12 (see below)

## Context

Artifacts must be immutable (G3) and reproducible (NF5), and regeneration is
routine — so identical outputs recur often.

## Decision

Object keys are derived from content: `{sha256[:2]}/{sha256}/{filename}`.
Postgres stores metadata and hashes only; bytes live in MinIO.

## Consequences

- **Immutability by construction** — different bytes cannot collide with an
  existing key, so nothing can be overwritten.
- **Dedup is free** — identical regenerations skip the upload entirely.
- **Self-verifying reads** — a fetch can be checked against the digest its key
  claims (`get_bytes_verified`), which is how a corrupted asset fails a render
  job instead of becoming garbage frames found at review.
- **Caching is genuinely `immutable`** — a changed asset is a different URL,
  so the header is correct rather than optimistic (see ADR-011).

## Amendment (M0-12): metadata does not dedup

Skipping the upload on a dedup hit also skips *metadata* writes, so a change
to how metadata is derived never reaches objects stored earlier. Found live:
an mp4 written before content types were set kept serving as
`binary/octet-stream`, which makes browsers download rather than play it —
and CI on fresh volumes would never have caught it.

`put_bytes` now compares stored metadata on a dedup hit and repairs it in
place with `copy_object` (`MetadataDirective=REPLACE`). Bytes are still never
re-uploaded, and the check is free because the HEAD already happened.
