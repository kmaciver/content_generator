# ADR-009 — Pipeline as a configurable DAG, not a hardcoded chain

- **Status:** Accepted
- **Date:** 2026-07-30
- **Related:** SADD §11, §12.4

## Context

The brief's pipeline is a chain. But image generation and voice generation
both depend only on approved scenes — running them in series wastes roughly
half the wall-clock time per video for no reason. Future stages (music
generation, animation) would each require touching dispatch logic if the
ordering were hardcoded.

## Decision

Declare the pipeline as a DAG in `templates/pipeline.yaml`: each stage lists
`requires` (artifact kinds that must be APPROVED), `produces`, `queue`, and
whether it fans out per scene. The same structure drives phase computation,
UI gating, and worker dispatch.

## Consequences

- Images ∥ voice run concurrently.
- Adding a stage is a config edit plus a worker, not a schema change.
- Staleness cascades correctly: the DAG knows script → scenes → {images,
  voice} → timeline, so approving a new script version marks downstream
  artifacts stale automatically.
- Cost: dispatch is data-driven and therefore a step less obvious to read
  than a hardcoded chain. Mitigated by keeping the YAML small and tested.
- **[2026-08-02, ADR-016] The graph is homogeneous, and stays that way.**
  Every `requires` entry is an `ArtifactKind` resolved against `artifact` rows
  of *this project*. Series-scoped branding (character, style) was considered
  as a second kind of dependency and kept out: it differs in resolution,
  staleness, what satisfies it, and what "unmet" means, so it is an admission
  check in the dispatch service rather than an edge. Before adding any
  dependency that is not a project-scoped artifact kind, read ADR-016.
