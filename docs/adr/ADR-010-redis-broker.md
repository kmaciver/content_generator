# ADR-010 — Redis as the Celery broker, with Postgres reconciliation

- **Status:** Accepted
- **Date:** 2026-07-30
- **Related:** SADD §7.2, §14.3, §14.4; implemented in M0-08

## Context

Celery needs a broker. RabbitMQ has stronger delivery semantics; Redis is
already required for caching and pub/sub, so choosing it avoids running a
fourth stateful service for a single-operator local deployment.

## Decision

**Redis**, with its weaker guarantees compensated explicitly rather than
ignored:

- `acks_late` + `task_reject_on_worker_lost` — a crashed worker means
  redelivery, not silence.
- Visibility timeout (3600s) must exceed the longest task runtime, or Redis
  redelivers work that is still running. A unit test asserts this inequality.
- Tasks must be idempotent: unique `idempotency_key`, a RUNNING-guard
  compare-and-set on job status, content-addressed storage, and version
  numbers allocated inside the completion transaction.
- `GenerationJob` in Postgres is the durable record; a reconciler re-enqueues
  anything Redis lost.

## Consequences

- One less service to operate, at the cost of idempotency being mandatory
  rather than optional. That discipline is required for at-least-once
  delivery under any broker, so little is actually lost.
- Redis persistence is configured (AOF `everysec`, `maxmemory-policy
  noeviction`) — an eviction policy would silently drop queued tasks.
- Celery makes the broker swappable by URL if RabbitMQ is ever wanted.
