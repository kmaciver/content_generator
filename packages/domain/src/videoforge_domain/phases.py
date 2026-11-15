"""Project phase, derived (SADD §12.4, ADR-001).

``video_project.phase`` is a **cache**, never a source of truth. It exists so
listing fifty projects does not mean fifty graph walks; it is recomputed from
artifact states after every transition, and because it is derived it can never
disagree with the artifacts. "Rollback" therefore needs no machinery at all —
reject a version and the phase falls back on its own.

Pure: a pipeline and a mapping in, an enum out. No session, no clock, no I/O.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum

from videoforge_domain.pipeline import Pipeline
from videoforge_shared.enums import ArtifactKind, ArtifactState, ProjectPhase

__all__ = ["WaitingOn", "derive_phase", "has_failures", "waiting_on"]


class WaitingOn(StrEnum):
    """Who has to act next for a project to move (M5-08).

    Not a phase and not stored: it is a *reading* of one, and the dashboard
    is the only thing that wants it.
    """

    #: A person: approve, reject, retry a failure, or press Generate.
    HUMAN = "human"
    #: A worker is running. Nothing a person does now makes it faster.
    MACHINE = "machine"
    #: Finished. The package is built and there is nothing left to do here.
    NOBODY = "nobody"


def derive_phase(
    pipeline: Pipeline, states: Mapping[ArtifactKind, ArtifactState]
) -> ProjectPhase:
    """Where the project is, from what its artifacts say.

    Three rules, in order:

    1. **No artifacts at all is ``DRAFT``** — a topic and nothing else.
    2. **Anything actively generating wins.** Among incomplete stages this is
       the most actionable fact for the operator, and it is the one case where
       the answer is not simply "the earliest unfinished thing". Images and
       voice run concurrently (ADR-009): with an image awaiting review while
       the voice track is still synthesising, reporting ``MEDIA_REVIEW`` would
       invite a reviewer to a stage that cannot complete.
    3. **Otherwise the earliest stage that is not APPROVED** decides, because
       that is what the project is actually blocked on.

    An all-approved pipeline lands on the final stage's review phase —
    ``READY_TO_PUBLISH``. ``PUBLISHED`` is never derived: publishing is an act,
    not a consequence of approvals, and M5 sets it explicitly.
    """
    if not states:
        return ProjectPhase.DRAFT

    for stage in pipeline.stages:
        if states.get(stage.produces) is ArtifactState.GENERATING:
            return stage.phase_generating

    for stage in pipeline.stages:
        state = states.get(stage.produces)
        if state is ArtifactState.APPROVED:
            continue
        if state is None or state is ArtifactState.PENDING:
            # The stage is next up but has produced nothing yet. Its own
            # "generating" phase is the closest honest label — §12.4 has no
            # separate "ready to run" phase, and inventing one here would put
            # a value in the column that the enum does not carry.
            return stage.phase_generating
        return stage.phase_review

    return pipeline.stages[-1].phase_review


def has_failures(states: Mapping[ArtifactKind, ArtifactState]) -> bool:
    """§12.4's orthogonal ``HAS_FAILURES`` flag.

    Deliberately *not* a phase. A failed image does not move the project
    backwards — the other nineteen are still fine — so it is a badge on top of
    whatever phase the project is otherwise in.
    """
    return any(state is ArtifactState.FAILED for state in states.values())


#: Phases in which the pipeline has nothing left to produce.
#:
#: ``PUBLISHED`` is never derived (publishing is an act, not a consequence of
#: approvals) but a project that reached it is just as finished.
_DONE = frozenset({ProjectPhase.READY_TO_PUBLISH, ProjectPhase.PUBLISHED})


def waiting_on(phase: ProjectPhase, *, generating: bool) -> WaitingOn:
    """Who the project is blocked on — the dashboard's only question (M5-08).

    **Derived from the artifacts, not read off the phase**, and the difference
    is not academic. ``derive_phase`` returns a stage's *generating* phase both
    when that stage is actually running and when it has produced nothing yet
    (rule 3 — §12.4 has no "ready to run" phase to return). So a project whose
    research is approved and whose script nobody has started reports
    ``SCRIPTING``, which reads as "a machine is working on it" and means
    exactly the opposite: it is sitting there waiting for someone to press a
    button. A dashboard built on the phase string alone would file the one
    project needing attention under "working".

    The rule that *is* true is simpler than the phases suggest: **if no worker
    is running and the pipeline is not finished, only a person can move it.**
    Approve, reject, retry, or generate the next stage — every remaining door
    is opened by a human. So one boolean over the artifact states decides it,
    and the phase is consulted only for "is this done".
    """
    if generating:
        return WaitingOn.MACHINE
    if phase in _DONE:
        return WaitingOn.NOBODY
    return WaitingOn.HUMAN
