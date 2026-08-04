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

from videoforge_domain.pipeline import Pipeline
from videoforge_shared.enums import ArtifactKind, ArtifactState, ProjectPhase

__all__ = ["derive_phase", "has_failures"]


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
