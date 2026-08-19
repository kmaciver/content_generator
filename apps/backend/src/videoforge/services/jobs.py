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

from videoforge.services.admission import check_admission, resolve_branding
from videoforge.services.dispatch import TaskDispatcher
from videoforge_domain.artifact_lifecycle import ArtifactEvent, apply_event
from videoforge_domain.job_lifecycle import (
    IllegalJobTransitionError,
    JobEvent,
    apply_job_event,
)
from videoforge_persistence.models import Artifact, GenerationJob
from videoforge_persistence.projection import refresh_project_state
from videoforge_persistence.repositories import ReservedJob
from videoforge_persistence.uow import UnitOfWork
from videoforge_shared.enums import (
    ArtifactKind,
    ArtifactState,
    JobStatus,
    SubjectType,
    TransitionCause,
)
from videoforge_shared.tasks import STAGE_TASKS, TaskSpec

logger = logging.getLogger(__name__)

__all__ = ["JobRequest", "JobService", "idempotency_key"]

#: Kinds whose first job fixes the project's branding versions (ADR-016).
#: Images only, matching ``admission._NEEDS_BRANDING`` — a project that never
#: generates an image never acquires a pin, and has nothing to protect.
_PINS_BRANDING = frozenset({ArtifactKind.IMAGE})


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
        input_extra: dict[str, Any] | None = None,
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

        # Admission first, before anything is written (M3-06). Two checks live
        # here: the stage's pipeline inputs must be approved, and an image needs
        # its series branding. Both raise ``AdmissionError`` → 409.
        #
        # Ahead of the idempotency reservation on purpose. A request that must
        # not run should not consume a key — a rejected attempt would otherwise
        # park the key on a live job row and make the *legitimate* retry, once
        # the script is approved, look like a duplicate of the failure.
        check_admission(uow, project_id, kind)

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
                # Stage-specific inputs first, so the four fields every job
                # needs are written last and a caller cannot displace them.
                **(input_extra or {}),
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

        # Pin the branding this project builds against, on the first image job
        # and never again (ADR-016, M3-06). ``pin_branding`` is write-once in
        # SQL, so this is safe to call on every image request — a project that
        # already has a pin is unaffected, which is exactly what protects an
        # episode already half-generated against a character approved since.
        if kind in _PINS_BRANDING:
            branding = resolve_branding(uow, project_id)
            if not branding.pinned:
                uow.projects.pin_branding(
                    project_id,
                    character_version_id=branding.character.id,
                    style_version_id=branding.style.id,
                )
                logger.info(
                    "branding pinned",
                    extra={
                        "project_id": project_id,
                        "character_version_id": branding.character.id,
                        "style_version_id": branding.style.id,
                    },
                )

        # The artifact just moved into GENERATING; the project's phase
        # follows. Inside the caller's transaction, like everything else
        # here — only the broker message waits for the commit.
        refresh_project_state(uow, project_id)

        self._pending.append((spec, {"job_id": reserved.job.id}))
        return JobRequest(job=reserved.job, artifact=artifact, created=True)

    def request_series_job(
        self,
        *,
        series_id: str,
        spec: TaskSpec,
        idempotency_key_suffix: str,
        input_snapshot: dict[str, Any] | None = None,
        actor_id: str | None = None,
    ) -> ReservedJob:
        """Reserve a job that belongs to a **series**, not a project (M3-04b).

        Reference-sheet generation is the only user: it produces branding every
        episode consumes, so there is no project whose phase should move and no
        artifact to put into GENERATING. What it *does* need is everything else
        a job carries — idempotency, the audit trail, orphan recovery, and
        above all ``provider_usage`` rows, since a candidate run is the most
        expensive thing in the system and must land inside the S10 cap.

        Deliberately a separate method rather than ``request`` with optional
        arguments. The two share a reservation and nothing else: this one has
        no artifact FSM to advance, no pipeline admission to check, and no
        phase to recompute — three of the four things ``request`` exists to do.
        Folding them together would mean a method whose body is mostly
        ``if project_id is not None``.
        """
        uow = self._uow
        # The caller supplies the suffix because *it* knows what makes two
        # requests the same intent — for references, the character version the
        # sheets are being generated for.
        key = f"{spec.name}:series:{series_id}:{idempotency_key_suffix}"
        reserved = uow.jobs.reserve(
            series_id=series_id,
            task_name=spec.name,
            queue=spec.queue,
            idempotency_key=key,
            input_snapshot=input_snapshot or {},
        )

        if not reserved.created:
            logger.info(
                "duplicate series job request ignored",
                extra={"idempotency_key": key, "job_id": reserved.job.id},
            )
            return reserved

        uow.audit.record_event(
            event_type="job.requested",
            subject_type=SubjectType.JOB,
            subject_id=reserved.job.id,
            actor_id=actor_id,
            payload={
                "task": spec.name,
                "queue": spec.queue,
                "series_id": series_id,
                **(input_snapshot or {}),
            },
        )
        uow.outbox.enqueue(
            event_type="job.requested",
            payload={"job_id": reserved.job.id, "series_id": series_id},
        )

        self._pending.append((spec, {"job_id": reserved.job.id}))
        return reserved

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

    def release(self, artifact: Artifact, *, actor_id: str | None = None) -> bool:
        """Free a stage whose job is never going to finish (M5-05).

        **The failure this exists for, observed in M4.** A worker discarded a
        queued message — in that case because its image had been rebuilt and no
        longer registered the task name. The ``generation_job`` row stayed
        ``QUEUED``, which is a *live* status, so it kept holding
        ``uq_generation_job_live_idempotency_key``. That key is
        ``task:artifact:v{next}``, derived from state the parked job never
        advanced, so every retry produced the same key and deduplicated onto
        the corpse. The stage could not be run again at all, and the only cure
        was psql.

        ``cancel`` already fixed half of that and nothing could reach it. This
        is the whole move: kill the job, which releases the key, **and** let
        the artifact out of ``GENERATING``, which it does not do on its own —
        an artifact stuck generating still refuses a new job.

        **``ORPHANED``, not ``GENERATION_FAILED``.** The domain has carried
        that event since M1-02, described as "the reconciler's verdict on a job
        whose worker vanished", and never invoked it. That is exactly this
        verdict, made by hand instead of by a sweep — so the transition records
        ``cause=reconciler``, and the reconciler proper (M6) will use this same
        path rather than a second one. Lands in ``FAILED`` rather than back in
        ``PENDING`` deliberately: the operator should see that something broke,
        and ``FAILED`` is retryable.

        Returns False when there is nothing to release, so the caller can say
        "not stuck" rather than inventing a transition.
        """
        if ArtifactState(artifact.state) is not ArtifactState.GENERATING:
            return False

        uow = self._uow
        # The *same* key `request` would compute, found through the same
        # index — rather than a new "live job for this artifact" query, which
        # would be a second definition of which job belongs to a stage.
        spec = STAGE_TASKS.get(ArtifactKind(artifact.kind))
        job = None
        if spec is not None:
            job = uow.jobs.live_by_idempotency_key(
                idempotency_key(spec.name, artifact.id, artifact.current_version_no + 1)
            )
        if job is not None:
            self.cancel(job.id, actor_id=actor_id)

        transition = apply_event(ArtifactState(artifact.state), ArtifactEvent.ORPHANED)
        artifact.state = transition.to_state
        uow.audit.record_transition(
            subject_type=SubjectType.ARTIFACT,
            subject_id=artifact.id,
            from_state=transition.from_state.value,
            to_state=transition.to_state.value,
            cause=transition.cause,
            actor_id=actor_id,
            job_id=job.id if job is not None else None,
        )
        uow.audit.record_event(
            event_type="artifact.released",
            subject_type=SubjectType.ARTIFACT,
            subject_id=artifact.id,
            actor_id=actor_id,
        )
        uow.flush()
        refresh_project_state(uow, artifact.project_id)

        logger.info(
            "stage released",
            extra={
                "artifact_id": artifact.id,
                "kind": artifact.kind,
                "job_id": job.id if job is not None else None,
            },
        )
        return True

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
