"""The per-artifact state machine (SADD §12.2) — the workhorse.

Why a machine at all, rather than services setting ``state`` directly: the
review UI renders buttons from the same guards the services enforce
(``capabilities`` in §11), so "the button is enabled" and "the service will
accept it" are the same fact. Reimplementing the rules in TypeScript is how
they drift, and a disabled-looking Approve button that 409s is worse than no
button.

Everything here is a pure function of ``(state, event)``. No I/O, no session,
no clock.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum, auto

from videoforge_shared.enums import ArtifactState, TransitionCause

__all__ = [
    "ArtifactEvent",
    "IllegalTransitionError",
    "Transition",
    "apply_event",
    "can_approve",
    "can_edit",
    "can_regenerate",
    "can_reject",
    "capabilities",
    "is_terminal",
    "legal_events",
]


class ArtifactEvent(StrEnum):
    """The only things that can move an artifact (SADD §12.2).

    The set is closed on purpose: if an artifact changed state and the reason
    is not one of these, something wrote to the database outside a service.
    """

    #: A worker picked the job up.
    GENERATION_STARTED = auto()
    #: The job produced a version.
    GENERATION_SUCCEEDED = auto()
    #: The job failed (retryable or not — retry is a job concern, §12.3).
    GENERATION_FAILED = auto()
    #: A human approved the current version.
    APPROVED = auto()
    #: A human rejected the current version.
    REJECTED = auto()
    #: A human asked for another attempt.
    REGENERATE_REQUESTED = auto()
    #: A human wrote the content themselves — jumps straight to review.
    HUMAN_EDITED = auto()
    #: The reconciler found the RUNNING job orphaned (§14.4).
    ORPHANED = auto()


class IllegalTransitionError(RuntimeError):
    """Raised when an event does not apply to the current state.

    A distinct exception type rather than ``ValueError`` because the API maps
    it to **409 Conflict** — the request was well-formed, the world just moved
    (someone else approved it, a worker finished first). Callers that race are
    expected; callers that send nonsense are a different error.
    """

    def __init__(self, state: ArtifactState, event: ArtifactEvent) -> None:
        super().__init__(
            f"cannot apply {event.value!r} to an artifact in state {state.value!r}"
        )
        self.state = state
        self.event = event


@dataclass(frozen=True, slots=True)
class Transition:
    """The outcome of an event: where it lands, and what to record.

    ``cause`` is carried here rather than decided by the caller so that the
    ``state_transition`` row (§10.3 rule 6) cannot disagree with the machine
    about why something happened.
    """

    from_state: ArtifactState
    to_state: ArtifactState
    event: ArtifactEvent
    cause: TransitionCause

    @property
    def is_noop(self) -> bool:
        return self.from_state is self.to_state


#: ``(state, event) -> (next state, cause)``.
#:
#: Written as a table rather than a chain of ``if``s because the table *is*
#: the specification — §12.2's diagram transcribed, and every absent key is a
#: deliberate "no". Reviewing a diff against a diagram is possible; reviewing
#: a diff against nested conditionals is not.
_TABLE: dict[
    tuple[ArtifactState, ArtifactEvent], tuple[ArtifactState, TransitionCause]
] = {
    # PENDING — nothing generated yet.
    (ArtifactState.PENDING, ArtifactEvent.GENERATION_STARTED): (
        ArtifactState.GENERATING,
        TransitionCause.SYSTEM,
    ),
    # A human writing the content themselves skips generation entirely and
    # lands in review (§12.2: "human edit jumps PENDING→AWAITING_APPROVAL").
    (ArtifactState.PENDING, ArtifactEvent.HUMAN_EDITED): (
        ArtifactState.AWAITING_APPROVAL,
        TransitionCause.EDIT,
    ),
    # GENERATING — a worker holds it.
    (ArtifactState.GENERATING, ArtifactEvent.GENERATION_SUCCEEDED): (
        ArtifactState.AWAITING_APPROVAL,
        TransitionCause.JOB_SUCCEEDED,
    ),
    (ArtifactState.GENERATING, ArtifactEvent.GENERATION_FAILED): (
        ArtifactState.FAILED,
        TransitionCause.JOB_FAILED,
    ),
    # The reconciler's verdict on a job whose worker vanished. Lands in
    # FAILED, not back in PENDING: the operator should see that something
    # broke, and FAILED is retryable anyway.
    (ArtifactState.GENERATING, ArtifactEvent.ORPHANED): (
        ArtifactState.FAILED,
        TransitionCause.RECONCILER,
    ),
    # AWAITING_APPROVAL — a human holds it.
    (ArtifactState.AWAITING_APPROVAL, ArtifactEvent.APPROVED): (
        ArtifactState.APPROVED,
        TransitionCause.REVIEW,
    ),
    (ArtifactState.AWAITING_APPROVAL, ArtifactEvent.REJECTED): (
        ArtifactState.REJECTED,
        TransitionCause.REVIEW,
    ),
    (ArtifactState.AWAITING_APPROVAL, ArtifactEvent.REGENERATE_REQUESTED): (
        ArtifactState.GENERATING,
        TransitionCause.SYSTEM,
    ),
    (ArtifactState.AWAITING_APPROVAL, ArtifactEvent.HUMAN_EDITED): (
        ArtifactState.AWAITING_APPROVAL,
        TransitionCause.EDIT,
    ),
    # REJECTED — recoverable, and only by producing something new.
    (ArtifactState.REJECTED, ArtifactEvent.REGENERATE_REQUESTED): (
        ArtifactState.GENERATING,
        TransitionCause.SYSTEM,
    ),
    (ArtifactState.REJECTED, ArtifactEvent.HUMAN_EDITED): (
        ArtifactState.AWAITING_APPROVAL,
        TransitionCause.EDIT,
    ),
    # FAILED — retry is the same door as regenerate (§12.5: a retry never
    # reuses the version slot, it creates the next version).
    (ArtifactState.FAILED, ArtifactEvent.REGENERATE_REQUESTED): (
        ArtifactState.GENERATING,
        TransitionCause.SYSTEM,
    ),
    (ArtifactState.FAILED, ArtifactEvent.HUMAN_EDITED): (
        ArtifactState.AWAITING_APPROVAL,
        TransitionCause.EDIT,
    ),
    # APPROVED — not terminal. Regenerating an approved artifact is the
    # ordinary way to revise a video (§12.4's staleness cascade depends on
    # it), and an edit is the same move by hand.
    (ArtifactState.APPROVED, ArtifactEvent.REGENERATE_REQUESTED): (
        ArtifactState.GENERATING,
        TransitionCause.SYSTEM,
    ),
    (ArtifactState.APPROVED, ArtifactEvent.HUMAN_EDITED): (
        ArtifactState.AWAITING_APPROVAL,
        TransitionCause.EDIT,
    ),
}


def apply_event(state: ArtifactState, event: ArtifactEvent) -> Transition:
    """Resolve ``(state, event)`` or raise :class:`IllegalTransitionError`.

    Raises rather than returning ``None`` so that a service which forgets to
    check gets a loud failure instead of silently skipping a state change —
    the failure mode that produces a project stuck in GENERATING forever.
    """
    try:
        to_state, cause = _TABLE[(state, event)]
    except KeyError:
        raise IllegalTransitionError(state, event) from None
    return Transition(from_state=state, to_state=to_state, event=event, cause=cause)


def legal_events(state: ArtifactState) -> frozenset[ArtifactEvent]:
    """Every event that applies in ``state``."""
    return frozenset(event for (s, event) in _TABLE if s is state)


def is_terminal(state: ArtifactState) -> bool:
    """True when nothing can move the artifact.

    Nothing is terminal today — even APPROVED accepts regeneration. The
    function exists so callers ask the machine instead of hardcoding that
    assumption, which stops being true the moment a PUBLISHED lock is added.
    """
    return not legal_events(state)


def can_approve(state: ArtifactState) -> bool:
    return ArtifactEvent.APPROVED in legal_events(state)


def can_reject(state: ArtifactState) -> bool:
    return ArtifactEvent.REJECTED in legal_events(state)


def can_regenerate(state: ArtifactState) -> bool:
    return ArtifactEvent.REGENERATE_REQUESTED in legal_events(state)


def can_edit(state: ArtifactState) -> bool:
    return ArtifactEvent.HUMAN_EDITED in legal_events(state)


def capabilities(state: ArtifactState) -> dict[str, bool]:
    """The payload the API sends so the UI never renders a lying button.

    Derived from the same table the services enforce, so a capability can
    never disagree with what the service will actually accept (SADD §11).
    """
    return {
        "can_approve": can_approve(state),
        "can_reject": can_reject(state),
        "can_regenerate": can_regenerate(state),
        "can_edit": can_edit(state),
    }
