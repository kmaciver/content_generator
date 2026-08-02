"""The outbox drain (SADD §14.5, ADR-003).

The pattern solves one problem. A service that commits a database change and
*then* publishes an event can crash in between and lose it; one that publishes
first can announce a transaction that later rolls back. Writing the event into
the same transaction as the state change makes the two atomic, and this task
does the publishing afterwards, from committed rows only.

**Finding S7: there is no consumer.** The drain publishes to Redis pub/sub and
nothing subscribes, deliberately. The outbox itself is load-bearing for audit
and correctness regardless of who reads it, while SSE — the eventual consumer —
waits for M5, when there is real UI to judge whether it earns its complexity
(ADR-006 commits to polling first, and §19.6 admits the uWSGI SSE story is
brittle). Building the durable half now and the delivery half later is the
cheap ordering; the reverse would not be.

Redis pub/sub is fire-and-forget: a message with no subscriber is dropped, not
queued. That is *correct* here — the database is the state of record and these
events are cache-invalidation hints, not the truth. A client that missed one
polls and is immediately right again.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import redis

from videoforge_shared.settings import get_app_settings
from videoforge_shared.tasks import DRAIN_OUTBOX
from videoforge_workers.db import worker_unit_of_work
from videoforge_workers.skeleton import videoforge_task

logger = logging.getLogger(__name__)

#: One channel for everything. Per-project channels would let a subscriber
#: filter server-side, but with no subscriber at all (S7) that is speculative
#: design; splitting later is a one-line change and needs the consumer's real
#: filtering requirements to get right.
EVENTS_CHANNEL = "videoforge:events"

#: Rows per pass. Bounded so one pathological backlog cannot hold a
#: transaction — and its SKIP LOCKED locks — open for minutes.
DRAIN_BATCH = 200

_redis: redis.Redis | None = None

__all__ = ["DRAIN_BATCH", "EVENTS_CHANNEL", "drain_outbox", "publish_batch"]


def _client() -> redis.Redis:
    """Process-local Redis client, built on first use.

    Lazy for the same reason the database engine is: Celery prefork forks
    after import, and a connection created before the fork is shared by every
    child.
    """
    global _redis
    if _redis is None:
        _redis = redis.Redis.from_url(get_app_settings().redis.url)
    return _redis


def publish_batch(client: redis.Redis, events: list[dict[str, Any]]) -> int:
    """Publish events in one pipeline round-trip. Returns the count sent."""
    if not events:
        return 0
    pipe = client.pipeline(transaction=False)
    for event in events:
        pipe.publish(EVENTS_CHANNEL, json.dumps(event, separators=(",", ":")))
    pipe.execute()
    return len(events)


def drain_once(client: redis.Redis | None = None, limit: int = DRAIN_BATCH) -> int:
    """One pass: claim, publish, stamp. Returns how many were published.

    Ordering inside the transaction is publish-then-stamp, which means a crash
    between the two re-publishes on the next pass. That is **at-least-once, on
    purpose**: the alternative ordering (stamp, then publish) drops events
    silently on the same crash, and for cache-invalidation hints a duplicate is
    free while a loss is not.

    ``claim_unpublished`` takes ``FOR UPDATE SKIP LOCKED``, so a second drain —
    an overlapping beat tick, or a restart racing its predecessor — skips the
    rows this one holds rather than blocking on them or double-publishing.
    """
    published = 0
    with worker_unit_of_work() as uow:
        claimed = uow.outbox.claim_unpublished(limit=limit)
        if not claimed:
            return 0

        payloads = [
            {
                "id": event.id,
                "event_type": event.event_type,
                "payload": event.payload,
                "correlation_id": event.correlation_id,
                "created_at": event.created_at.isoformat(),
            }
            for event in claimed
        ]
        published = publish_batch(client or _client(), payloads)
        uow.outbox.mark_published([event.id for event in claimed])

    logger.info(
        "outbox drained",
        extra={"published": published, "channel": EVENTS_CHANNEL},
    )
    return published


@videoforge_task(name=DRAIN_OUTBOX.name, queue=DRAIN_OUTBOX.queue)
def drain_outbox() -> int:
    """Beat-scheduled drain.

    Not job-bearing: it carries no ``generation_job`` row, so there is nothing
    for the RUNNING-guard to guard. Its idempotency comes from
    ``published_at IS NULL`` in both the claim and the stamp.
    """
    return drain_once()
