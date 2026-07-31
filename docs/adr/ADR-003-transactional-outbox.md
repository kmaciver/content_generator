# ADR-003 — Transactional outbox for event publication

- **Status:** Accepted
- **Date:** 2026-07-30
- **Related:** SADD §14.5

## Context

Workers must update state *and* announce it. Publishing to Redis inside the
database transaction is impossible (no distributed transaction); publishing
after it opens a window where the commit succeeds and the event is lost.

## Decision

Workers write an `outbox_event` row **in the same transaction** as the state
change. A dedicated `events` worker drains the outbox in order, publishes to
Redis pub/sub, and marks rows published. The frontend treats events purely as
**cache-invalidation hints** (React Query refetch), never as payloads.

## Consequences

- The database is unambiguously the source of truth; an event cannot exist
  without its state change, or vice versa.
- A lost or duplicated event degrades to the polling floor rather than to
  wrong UI state — which is what makes at-least-once delivery tolerable here.
- Cost: one extra table, one extra worker, and up to one drain-interval of
  latency. All three are cheap relative to debugging a lost-event bug.
