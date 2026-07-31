"""Ping tasks — one per queue (M0 exit test: a ping round-trips everywhere).

Trivial by design: they prove the broker, the routing, the per-queue worker
subscriptions, the result backend, correlation propagation, and Flower's
event stream, all before any task does real work. They stay in the codebase
after M0 — an operator's cheapest "is the pipe alive" probe.
"""

from __future__ import annotations

import os
import socket
import time
from typing import Any

from celery import Task

from videoforge_shared.correlation import get_correlation_id
from videoforge_workers.celery_app import QUEUES
from videoforge_workers.skeleton import enqueue, videoforge_task


def _make_ping(queue: str) -> Any:
    @videoforge_task(name=f"ping.{queue}", queue=queue)
    def ping() -> dict[str, Any]:
        return {
            "queue": queue,
            "hostname": socket.gethostname(),
            "pid": os.getpid(),
            "correlation_id": get_correlation_id(),
            "ts": time.time(),
        }

    return ping


#: queue name → registered ping task.
PING_TASKS: dict[str, Task[Any, Any]] = {queue: _make_ping(queue) for queue in QUEUES}


def enqueue_ping(queue: str, *, correlation_id: str | None = None) -> Any:
    """Send a ping down a specific queue; returns the AsyncResult."""
    return enqueue(PING_TASKS[queue], queue=queue, correlation_id=correlation_id)
