"""The uniform task skeleton (SADD §13) — M0-08 stub.

Today: correlation binding, structured start/finish/fail logging, duration.
M1 adds the machinery that makes tasks safe under at-least-once delivery:
the GenerationJob RUNNING-guard, input_snapshot reads, and the transactional
completion (artifact version + state transition + audit + outbox in one
commit). Every stage task — including render (D4) — goes through this
decorator; a task defined without it should fail review on sight.

Correlation travel (SADD §21.8): the producer sends the id as a Celery message
header via :func:`enqueue`; the consumer side rebinds it here so every log
line inside the task carries the id that started the HTTP request.
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable
from typing import Any, TypeVar

from celery import Task
from celery.result import AsyncResult

from videoforge_shared.correlation import correlation_context
from videoforge_workers.celery_app import app

logger = logging.getLogger(__name__)

#: Header key for the Celery leg. Underscored (not ``X-Request-Id``) because
#: protocol-2 message headers can surface as attribute lookups on the task
#: request, and hyphens don't survive that.
CELERY_CORRELATION_HEADER = "x_request_id"

R = TypeVar("R")


def _incoming_correlation(request: Any) -> str | None:
    """Fish the correlation id out of a task request, tolerating both places
    Celery's protocol can put custom headers (the headers dict, or merged as
    request attributes)."""
    headers = getattr(request, "headers", None) or {}
    from_headers = headers.get(CELERY_CORRELATION_HEADER)
    if from_headers:
        return str(from_headers)
    from_attr = getattr(request, CELERY_CORRELATION_HEADER, None)
    return str(from_attr) if from_attr else None


def videoforge_task(*, name: str, queue: str) -> Callable[[Callable[..., R]], Any]:
    """Register a function as a platform task.

    ``queue`` is mandatory on purpose: a task with no explicit queue lands on
    Celery's default queue, which nothing consumes, and the failure mode is
    silence. Making it a required argument turns that mistake into a review
    conversation instead of a mystery.
    """

    def decorator(fn: Callable[..., R]) -> Any:
        @functools.wraps(fn)
        def wrapper(self: Task[Any, Any], *args: Any, **kwargs: Any) -> R:
            cid = _incoming_correlation(self.request)
            with correlation_context(cid):
                started = time.monotonic()
                logger.info(
                    "task started",
                    extra={"task": name, "queue": queue, "task_id": self.request.id},
                )
                try:
                    result = fn(*args, **kwargs)
                except Exception:
                    logger.exception(
                        "task failed",
                        extra={
                            "task": name,
                            "queue": queue,
                            "task_id": self.request.id,
                            "duration_ms": int((time.monotonic() - started) * 1000),
                        },
                    )
                    raise
                logger.info(
                    "task finished",
                    extra={
                        "task": name,
                        "queue": queue,
                        "task_id": self.request.id,
                        "duration_ms": int((time.monotonic() - started) * 1000),
                    },
                )
                return result

        return app.task(name=name, bind=True)(wrapper)

    return decorator


def enqueue(
    task: Task[Any, Any],
    *args: Any,
    queue: str,
    correlation_id: str | None = None,
    **kwargs: Any,
) -> AsyncResult[Any]:
    """Producer-side helper: enqueue with an explicit queue and the correlation
    header attached. The single blessed way to send platform tasks."""
    headers = {CELERY_CORRELATION_HEADER: correlation_id} if correlation_id else {}
    return task.apply_async(args=args, kwargs=kwargs, queue=queue, headers=headers)
