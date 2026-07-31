# ADR-006 — Polling-first job UX; SSE additive and deferred

- **Status:** Accepted
- **Date:** 2026-07-30
- **Related:** SADD §19.2, §19.6; amended by finding S7

## Context

Generation is asynchronous, so the UI must learn when a job finishes. SSE is
the obvious push mechanism — but under synchronous uWSGI workers each open
connection occupies a worker for its lifetime, and the SADD's own mitigation
(a second uWSGI pool, `processes=1 threads=16`) caps concurrent viewers at 16
while adding a second server topology to operate.

## Decision

**Polling is the floor and ships first.** `POST` returns `202 + job_id`;
clients poll `GET /jobs/{id}` with React Query's `refetchInterval` until a
terminal state. SSE is additive, behind a feature flag, and carries hints
only — never payloads.

Per **S7**, SSE is deferred out of M1 entirely: the outbox and its drain
worker are built (they are load-bearing for audit and correctness) and publish
to Redis pub/sub with no consumer. SSE arrives in M5, once there is real UI to
judge whether the latency actually bothers anyone.

## Consequences

- The UX bar is met without SSE; adding it later changes no state handling,
  because events were only ever invalidation hints (ADR-003).
- Polling costs redundant requests. At single-operator scale this is noise.
- Decision to revisit in M5 with evidence rather than speculation.
