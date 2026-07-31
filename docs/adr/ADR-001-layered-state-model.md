# ADR-001 — Layered state model over a monolithic project FSM

- **Status:** Accepted
- **Date:** 2026-07-30
- **Related:** SADD §12; deviates from the brief's linear FSM

## Context

The brief sketched one linear state machine per project (`Draft → Research
Generated → … → Published`). That cannot express the things this pipeline
actually does: scene 4's image rejected while 1–3 are approved; voice
regenerating while a human edits captions; images and voice generating
concurrently. Encoding those combinations in one enum explodes
combinatorially, and every new stage multiplies it again.

## Decision

Three small machines plus one derived value:

1. **Artifact lifecycle** (per artifact) — `PENDING → GENERATING →
   AWAITING_APPROVAL → APPROVED | REJECTED`, with `SUPERSEDED` for
   non-approved siblings. The workhorse.
2. **Job execution** (per job) — `QUEUED → RUNNING → SUCCEEDED | FAILED |
   CANCELLED | ORPHANED`. Pure mechanics, invisible to approval logic.
3. **Project phase** — *computed* from artifact states against the pipeline
   DAG, then cached for cheap listing.

## Consequences

- Phase can never disagree with artifact truth, because it is derived from it.
- "Rollback" needs no special machinery: rejecting or superseding artifacts
  recomputes the phase backwards automatically.
- Partial regeneration is natural — per-scene artifacts carry `scene_ref`, so
  "redo scene 4's image" touches exactly one artifact.
- Cost: three vocabularies instead of one. Worth it; the alternative was a
  vocabulary nobody could enumerate.
