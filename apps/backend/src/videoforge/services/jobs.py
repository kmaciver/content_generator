"""The job service — where "a user asked for something" becomes durable.

SADD's architectural rule: **the API never generates.** A request creates a
``generation_job`` row and returns; a worker does the work. This service is
that boundary, and the two properties it must hold are:

1. The job, the artifact state change, the audit trail and the outbox event
   land in **one transaction** (§10.3 rule 6). Anything less and a crash can
   leave a job with no history, or history for a job that does not exist.
2. Asking twice produces **one** job (§14.3). Users double-click, browsers
   retry, and at-least-once delivery means the broker will re-present work on
   its own.

The dispatch happens *after* commit, deliberately — see :meth:`request`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from videoforge.services.dispatch import TaskDispatcher
from videoforge_domain.artifact_lifecycle import ArtifactEvent, apply_event
from videoforge_domain.job_lifecycle import (
    IllegalJobTransitionError,
    JobEvent,
    apply_job_event,
)
from videoforge_persistence.models import Artifact, GenerationJob
from videoforge_persistence.projection import refresh_project_state
from videoforge_persistence.uow import UnitOfWork
from videoforge_shared.enums import (
    ArtifactKind,
    ArtifactState,
    JobStatus,
    SubjectType,
    TransitionCause,
)
from videoforge_shared.tasks import TaskSpec

logger = logging.getLogger(__name__)

__all__ = ["JobRequest", "JobService", "idempotency_key"]


def idempotency_key(task_name: str, artifact_id: str, next_version_no: int) -> str:
    """The key that makes "ask twice, get one job" true.

    Derived from **the state the job will advance from**, not from a random
    token or a timestamp. Two clicks on Generate while the artifact is still
    at version 2 produce the same key and therefore one job; a legitimate
    regeneration after version 3 exists produces a different key and is
    correctly a second job.

    A client-supplied token would work too, but only for clients that
    remember to send one — and the duplicate deliveries that matter most come
    from the broker, which has no opinion about tokens.
    """
    return f"{task_name}:{artifact_id}:v{next_version_no}"


@dataclass(frozen=True, slots=True)
class JobRequest:
    """The outcome of :meth:`JobService.request`.

    ``created`` distinguishes "this call made the job" from "this call found
    the job an earlier duplicate made". Callers must not dispatch on False —
    that is the double-enqueue this whole design exists to prevent.
    """

    job: GenerationJob
    artifact: Artifact
    created: bool


class JobService:
    """Creates generation jobs. Does not run them, and cannot."""

    def __init__(self, uow: UnitOfWork, dispatcher: TaskDispatcher) -> None:
        self._uow = uow
        self._dispatcher = dispatcher
        self._pending: list[tuple[TaskSpec, dict[str, Any]]] = []

    def request(
        self,
        *,
        project_id: str,
        kind: ArtifactKind,
        spec: TaskSpec,
        scene_ref: str | None = None,
        actor_id: str | None = None,
        regenerate: bool = False,
    ) -> JobRequest:
        """Reserve a job and move the artifact into GENERATING.

        Everything here happens inside the caller's transaction and **nothing
        is dispatched**. The broker message is queued in memory and sent by
        :meth:`dispatch_pending` after the caller commits — because a message
        published inside the transaction describes work that may still roll
        back, and a worker is entirely capable of picking it up before the
        commit lands and finding no job row at all.
        """
        uow = self._uow

        artifact = uow.artifacts.find(project_id, kind, scene_ref)
        if artifact is None:
            artifact = uow.artifacts.create(project_id, kind, scene_ref)
            uow.flush()

        # Idempotency is checked BEFORE the FSM, and the order is load-bearing.
        #
        # The reverse order looks natural and is wrong: a duplicate request
        # arrives while the artifact is already GENERATING (this request put it
        # there), so the FSM correctly refuses GENERATION_STARTED and raises —
        # turning "you clicked twice" into a 409 instead of a no-op. The FSM is
        # answering honestly; it is simply the wrong question to ask first.
        #
        # Asking the idempotency key first separates the two cases cleanly:
        # a repeat of *this* intent is a duplicate, and only a genuinely new
        # intent is put to the FSM. Because the key is derived from
        # ``current_version_no``, "regenerate while still generating" also
        # resolves as a duplicate — which is the correct answer, since the
        # version that regeneration would be based on does not exist yet.
        key = idempotency_key(spec.name, artifact.id, artifact.current_version_no + 1)
        reserved = uow.jobs.reserve(
            project_id=project_id,
            task_name=spec.name,
            queue=spec.queue,
            idempotency_key=key,
            artifact_id=artifact.id,
            input_snapshot={
                "artifact_id": artifact.id,
                "kind": kind.value,
                "scene_ref": scene_ref,
                "from_version_no": artifact.current_version_no,
            },
        )

        if not reserved.created:
            # A duplicate. The artifact is already GENERATING and the job is
            # already queued; doing any of the below again would write a second
            # transition and a second broker message for one user intent.
            logger.info(
                "duplicate job request ignored",
                extra={"idempotency_key": key, "job_id": reserved.job.id},
            )
            return JobRequest(job=reserved.job, artifact=artifact, created=False)

        # A genuinely new intent. Now the FSM's answer is the one that matters:
        # a request the artifact's state forbids raises IllegalTransitionError,
        # which the API maps to 409 Conflict.
        event = (
            ArtifactEvent.REGENERATE_REQUESTED
            if regenerate
            else ArtifactEvent.GENERATION_STARTED
        )
        transition = apply_event(ArtifactState(artifact.state), event)

        artifact.state = transition.to_state
        uow.audit.record_transition(
            subject_type=SubjectType.ARTIFACT,
            subject_id=artifact.id,
            from_state=transition.from_state.value,
            to_state=transition.to_state.value,
            cause=transition.cause,
            actor_id=actor_id,
            job_id=reserved.job.id,
        )
        uow.audit.record_event(
            event_type="job.requested",
            subject_type=SubjectType.JOB,
            subject_id=reserved.job.id,
            actor_id=actor_id,
            payload={
                "task": spec.name,
                "queue": spec.queue,
                "artifact_id": artifact.id,
                "kind": kind.value,
                "regenerate": regenerate,
            },
        )
        uow.outbox.enqueue(
            event_type="job.requested",
            payload={
                "job_id": reserved.job.id,
                "project_id": project_id,
                "artifact_id": artifact.id,
                "kind": kind.value,
            },
        )

        # The artifact just moved into GENERATING; the project's phase
        # follows. Inside the caller's transaction, like everything else
        # here — only the broker message waits for the commit.
        refresh_project_state(uow, project_id)

        self._pending.append((spec, {"job_id": reserved.job.id}))
        return JobRequest(job=reserved.job, artifact=artifact, created=True)

    def dispatch_pending(self) -> None:
        """Publish the messages queued by :meth:`request`. **Call after commit.**

        Split from ``request`` so the ordering is impossible to get wrong by
        accident: there is no code path that publishes while the transaction
        is still open.

        A failure here leaves a QUEUED job with no broker message — recoverable,
        because the reconciler (§14.4) and an operator retry both work from the
        row. The reverse ordering leaves a message with no row, which is
        unrecoverable noise a worker will fail on.
        """
        pending, self._pending = self._pending, []
        for spec, kwargs in pending:
            self._dispatcher.send(spec, **kwargs)

    def cancel(self, job_id: str, *, actor_id: str | None = None) -> bool:
        """Cancel a job. Returns False when it is past cancelling.

        "Past cancelling" is the job FSM's judgement, not this method's — a
        SUCCEEDED job cannot be cancelled because the work is already done and
        its artifact version already exists.
        """
        job = self._uow.jobs.get(job_id)
        if job is None:
            return False

        try:
            transition = apply_job_event(JobStatus(job.status), JobEvent.CANCELLED)
        except IllegalJobTransitionError:
            return False

        job.status = transition.to_status
        self._uow.audit.record_transition(
            subject_type=SubjectType.JOB,
            subject_id=job.id,
            from_state=transition.from_status.value,
            to_state=transition.to_status.value,
            cause=TransitionCause.SYSTEM,
            actor_id=actor_id,
            job_id=job.id,
        )
        self._uow.audit.record_event(
            event_type="job.cancelled",
            subject_type=SubjectType.JOB,
            subject_id=job.id,
            actor_id=actor_id,
        )
        return True
