"""Job execution states (SADD §12.3) — mechanics only.

Deliberately disjoint from approval logic. A job succeeding is a *cause* of an
artifact transition, never the transition itself; nothing in this module knows
what an artifact is. That separation is what makes §12.1's argument work — one
combined machine would have to express "scene 4's image job is retrying while
scenes 1–3 await approval" as a single enum value.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, auto

from videoforge_shared.enums import JobStatus

__all__ = [
    "IllegalJobTransitionError",
    "JobEvent",
    "JobTransition",
    "apply_job_event",
    "is_finished",
    "may_retry",
    "next_status_after_failure",
]


class JobEvent(StrEnum):
    CLAIMED = auto()
    SUCCEEDED = auto()
    FAILED = auto()
    CANCELLED = auto()
    #: The reconciler found a RUNNING job whose worker is gone (§14.4).
    ORPHANED = auto()
    #: A failed or orphaned job is being put back on the queue.
    REQUEUED = auto()


class IllegalJobTransitionError(RuntimeError):
    def __init__(self, status: JobStatus, event: JobEvent) -> None:
        super().__init__(
            f"cannot apply {event.value!r} to a job with status {status.value!r}"
        )
        self.status = status
        self.event = event


@dataclass(frozen=True, slots=True)
class JobTransition:
    from_status: JobStatus
    to_status: JobStatus
    event: JobEvent


_TABLE: dict[tuple[JobStatus, JobEvent], JobStatus] = {
    (JobStatus.QUEUED, JobEvent.CLAIMED): JobStatus.RUNNING,
    (JobStatus.QUEUED, JobEvent.CANCELLED): JobStatus.CANCELLED,
    (JobStatus.RUNNING, JobEvent.SUCCEEDED): JobStatus.SUCCEEDED,
    (JobStatus.RUNNING, JobEvent.FAILED): JobStatus.FAILED,
    (JobStatus.RUNNING, JobEvent.CANCELLED): JobStatus.CANCELLED,
    (JobStatus.RUNNING, JobEvent.ORPHANED): JobStatus.ORPHANED,
    (JobStatus.FAILED, JobEvent.REQUEUED): JobStatus.QUEUED,
    (JobStatus.ORPHANED, JobEvent.REQUEUED): JobStatus.QUEUED,
}

#: Nothing moves a job out of these.
#:
#: ``SUCCEEDED`` is absent from the retry paths on purpose: re-running a job
#: that already produced a version is the double-delivery bug the whole
#: idempotency design exists to prevent (§14.3, and M1-04's test).
_FINAL: frozenset[JobStatus] = frozenset({JobStatus.SUCCEEDED, JobStatus.CANCELLED})


def apply_job_event(status: JobStatus, event: JobEvent) -> JobTransition:
    try:
        to_status = _TABLE[(status, event)]
    except KeyError:
        raise IllegalJobTransitionError(status, event) from None
    return JobTransition(from_status=status, to_status=to_status, event=event)


def is_finished(status: JobStatus) -> bool:
    """True when the job will not run again without a new row."""
    return status in _FINAL


def may_retry(status: JobStatus, attempt: int, max_attempts: int) -> bool:
    """Whether a failed job has another attempt left.

    ``attempt`` counts attempts *already made*, so the comparison is strict:
    with ``max_attempts=3``, attempts 0/1/2 may retry and attempt 3 may not.
    Getting this off by one means either a job that never retries or one that
    retries forever, and both fail quietly.
    """
    if status not in (JobStatus.FAILED, JobStatus.ORPHANED):
        return False
    return attempt < max_attempts


def next_status_after_failure(attempt: int, max_attempts: int) -> JobStatus:
    """Where a failing job lands: back in the queue, or finished as FAILED."""
    return (
        JobStatus.QUEUED
        if may_retry(JobStatus.FAILED, attempt, max_attempts)
        else JobStatus.FAILED
    )
