"""The uniform task skeleton (SADD §13).

Every stage task goes through this decorator — including render (D4). A task
defined without it should fail review on sight, because this is where the
guarantees live:

* **Correlation binding.** The producer sends the request id as a Celery
  header; the consumer rebinds it so every log line inside the task carries
  the id that started the HTTP request (§21.8).
* **The RUNNING-guard.** ``acks_late=True`` means a worker killed mid-task
  gets its message redelivered. That is the *correct* setting — losing work
  silently is worse — and it is only safe because the redelivered twin loses
  a compare-and-set and exits without running (§14.3).
* **Transactional completion.** The task body and the job's SUCCEEDED write
  share one transaction, so an artifact version cannot exist for a job that
  never finished, nor a SUCCEEDED job with nothing to show for it
  (§10.3 rule 6).

Job-bearing tasks receive a :class:`JobContext` and nothing else. Passing the
unit of work in — rather than letting the body open its own — is what makes
the shared transaction unavoidable rather than a convention.
"""

from __future__ import annotations

import functools
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

from celery import Task
from celery.result import AsyncResult
from videoforge_domain.job_lifecycle import may_retry

from videoforge_persistence.models import GenerationJob
from videoforge_persistence.uow import UnitOfWork
from videoforge_shared.correlation import correlation_context
from videoforge_shared.enums import JobStatus
from videoforge_workers.celery_app import app
from videoforge_workers.db import worker_unit_of_work

logger = logging.getLogger(__name__)

#: Header key for the Celery leg. Underscored (not ``X-Request-Id``) because
#: protocol-2 message headers can surface as attribute lookups on the task
#: request, and hyphens don't survive that.
CELERY_CORRELATION_HEADER = "x_request_id"

R = TypeVar("R")

__all__ = [
    "CELERY_CORRELATION_HEADER",
    "JobContext",
    "enqueue",
    "run_job",
    "videoforge_task",
]


@dataclass(slots=True)
class JobContext:
    """What a job-bearing task body is handed.

    It gets the unit of work rather than a session so the body reaches every
    repository through one transaction, and it gets the job row so it can read
    ``input_snapshot`` without a second query.
    """

    uow: UnitOfWork
    job: GenerationJob

    @property
    def input(self) -> dict[str, Any]:
        """The exact inputs this job was created against.

        Read from here, not from current state: a job queued three minutes ago
        must run against what it was asked to do, and re-deriving inputs at
        execution time silently makes a job mean something different from what
        the audit trail says it meant.
        """
        return dict(self.job.input_snapshot or {})


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


def run_job(
    job_id: str,
    body: Callable[[JobContext], None],
    *,
    task_name: str,
    celery_task_id: str | None = None,
) -> bool:
    """Claim the job, run ``body``, complete it. Returns whether it ran.

    Exposed separately from the decorator so the double-delivery test can
    exercise the guard without a broker, a worker, or Celery's eager mode —
    the property under test is about the database, and the test should not
    have to stand up a message bus to observe it.

    Three transactions, deliberately not one:

    1. **Claim.** Committed immediately so that RUNNING is visible to every
       other worker the instant this one wins. Holding the claim inside the
       body's transaction would keep the row invisible for the whole task and
       let a redelivered twin claim it too.
    2. **Body + completion.** One transaction, so the artifact version and
       SUCCEEDED are atomic.
    3. **Failure.** Its own transaction, because the body's has been rolled
       back and the failure still has to be recorded.
    """
    with worker_unit_of_work() as uow:
        won = uow.jobs.claim(job_id, celery_task_id=celery_task_id)
        job = uow.jobs.get(job_id)

    if job is None:
        # Nothing to run and nothing to record against. Not an error worth
        # retrying — a redelivery of a job whose project was deleted lands
        # here, and raising would just cycle it through the retry budget.
        logger.warning("job not found; dropping", extra={"job_id": job_id})
        return False

    if not won:
        # The guard did its work. Someone else owns this job — either a twin
        # that is running it now, or a past run that already finished.
        logger.info(
            "duplicate delivery ignored",
            extra={"job_id": job_id, "task": task_name, "status": job.status},
        )
        return False

    started = time.monotonic()
    try:
        with worker_unit_of_work() as uow:
            live_job = uow.jobs.get(job_id)
            assert live_job is not None
            body(JobContext(uow=uow, job=live_job))
            if not uow.jobs.mark_succeeded(job_id):  # pragma: no cover - defensive
                raise RuntimeError(
                    f"job {job_id} was not RUNNING at completion; "
                    "something moved it underneath this task"
                )
    except Exception as exc:
        duration_ms = int((time.monotonic() - started) * 1000)
        with worker_unit_of_work() as uow:
            failed = uow.jobs.get(job_id)
            assert failed is not None
            requeue = may_retry(JobStatus.FAILED, failed.attempt, failed.max_attempts)
            uow.jobs.mark_failed(
                job_id,
                {
                    "type": type(exc).__name__,
                    # str(exc) only. The traceback goes to the log, where an
                    # operator can see it; this column is read back into the
                    # UI, and provider errors have been known to carry request
                    # payloads.
                    "message": str(exc),
                    "task": task_name,
                    "duration_ms": duration_ms,
                },
                requeue=requeue,
            )
        logger.exception(
            "job failed",
            extra={
                "job_id": job_id,
                "task": task_name,
                "duration_ms": duration_ms,
                "will_retry": requeue,
            },
        )
        raise

    logger.info(
        "job succeeded",
        extra={
            "job_id": job_id,
            "task": task_name,
            "duration_ms": int((time.monotonic() - started) * 1000),
        },
    )
    return True


def videoforge_task(
    *, name: str, queue: str, job_bearing: bool = False
) -> Callable[[Callable[..., Any]], Any]:
    """Register a function as a platform task.

    ``queue`` is mandatory on purpose: a task with no explicit queue lands on
    Celery's default queue, which nothing consumes, and the failure mode is
    silence. Making it a required argument turns that mistake into a review
    conversation instead of a mystery.

    ``job_bearing=True`` wraps the body in :func:`run_job` — the task then
    takes ``job_id`` as a keyword and receives a :class:`JobContext`. Plain
    tasks (ping, the outbox drain) keep the logging-only behaviour: they carry
    no ``generation_job`` row, so there is nothing to guard.
    """

    def decorator(fn: Callable[..., Any]) -> Any:
        @functools.wraps(fn)
        def wrapper(self: Task[Any, Any], *args: Any, **kwargs: Any) -> Any:
            cid = _incoming_correlation(self.request)
            with correlation_context(cid):
                started = time.monotonic()
                logger.info(
                    "task started",
                    extra={"task": name, "queue": queue, "task_id": self.request.id},
                )
                try:
                    if job_bearing:
                        job_id = kwargs.pop("job_id", None) or (
                            args[0] if args else None
                        )
                        if job_id is None:
                            raise ValueError(
                                f"task {name!r} is job-bearing and requires job_id"
                            )
                        result: Any = run_job(
                            str(job_id),
                            fn,
                            task_name=name,
                            celery_task_id=self.request.id,
                        )
                    else:
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
    header attached. The single blessed way to send platform tasks *from a
    worker*; the API publishes by name instead (it cannot import tasks)."""
    headers = {CELERY_CORRELATION_HEADER: correlation_id} if correlation_id else {}
    return task.apply_async(args=args, kwargs=kwargs, queue=queue, headers=headers)
