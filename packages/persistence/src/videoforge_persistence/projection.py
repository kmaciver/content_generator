"""Maintaining the derived caches: ``video_project.phase`` and ``stale_since``.

Both are **caches over artifact truth**, never sources of it (SADD §12.4,
finding S2). The rules that compute them are pure and live in
``videoforge_domain``; this module is the thing that runs them against a
session and writes the answers down. Keeping cache maintenance next to the rows
being cached is why it lives in the data layer rather than in either app —
workers and the API both cause artifact transitions, and neither may import the
other.

**Every artifact transition must end here.** A phase that is recomputed on some
paths and not others is worse than no phase at all: it looks authoritative and
is wrong, and the discrepancy only shows up in a listing screen nobody is
looking at when it happens.
"""

from __future__ import annotations

import logging
from functools import lru_cache

from videoforge_domain.phases import derive_phase
from videoforge_domain.pipeline import Pipeline
from videoforge_persistence.uow import UnitOfWork
from videoforge_shared.enums import (
    ArtifactKind,
    ArtifactState,
    ProjectPhase,
    SubjectType,
    TransitionCause,
)
from videoforge_shared.pipeline_file import load_pipeline_mapping

logger = logging.getLogger(__name__)

__all__ = [
    "cascade_staleness",
    "get_pipeline",
    "recompute_phase",
    "refresh_project_state",
]


@lru_cache(maxsize=1)
def get_pipeline() -> Pipeline:
    """The process-wide pipeline, read once.

    Cached because the declaration cannot change while a process runs — the
    file is baked into the image. Tests that need a different graph pass one
    explicitly rather than clearing this, so the cache never becomes a source
    of cross-test coupling.
    """
    return Pipeline.from_mapping(load_pipeline_mapping())


def _states(uow: UnitOfWork, project_id: str) -> dict[ArtifactKind, ArtifactState]:
    """Current state per artifact kind, ignoring per-scene multiplicity.

    Twenty image artifacts collapse to one entry, and the **least advanced**
    wins: nineteen approved images and one still generating means the image
    stage is not done. Taking the most advanced would report a project ready
    for a stage that would then fail on a missing input.
    """
    ranked = {
        ArtifactState.FAILED: 0,
        ArtifactState.PENDING: 1,
        ArtifactState.GENERATING: 2,
        ArtifactState.REJECTED: 3,
        ArtifactState.AWAITING_APPROVAL: 4,
        ArtifactState.APPROVED: 5,
    }
    worst: dict[ArtifactKind, ArtifactState] = {}
    for artifact in uow.artifacts.for_project(project_id):
        kind = ArtifactKind(artifact.kind)
        state = ArtifactState(artifact.state)
        current = worst.get(kind)
        if current is None or ranked[state] < ranked[current]:
            worst[kind] = state
    return worst


def recompute_phase(
    uow: UnitOfWork, project_id: str, *, pipeline: Pipeline | None = None
) -> ProjectPhase | None:
    """Recompute and store the project's phase. Returns it, or None if gone.

    Writes a ``state_transition`` when the phase actually moves — the subject
    is ``project_phase`` (§10.4), which is exactly what that subject type was
    reserved for. No transition is written when the phase is unchanged, so the
    audit trail records movement rather than heartbeat.
    """
    project = uow.projects.get(project_id)
    if project is None:
        return None

    graph = pipeline or get_pipeline()
    phase = derive_phase(graph, _states(uow, project_id))
    previous = ProjectPhase(project.phase)
    if phase is previous:
        return phase

    project.phase = phase
    uow.audit.record_transition(
        subject_type=SubjectType.PROJECT_PHASE,
        subject_id=project_id,
        from_state=previous.value,
        to_state=phase.value,
        cause=TransitionCause.SYSTEM,
    )
    return phase


def cascade_staleness(
    uow: UnitOfWork,
    project_id: str,
    approved_kind: ArtifactKind,
    *,
    pipeline: Pipeline | None = None,
) -> int:
    """Mark everything downstream of ``approved_kind`` stale. Returns the count.

    Finding S2, and the reason the DAG is data: the blast radius of approving a
    new script is "everything the graph says descends from script", computed
    rather than listed. A hardcoded list would be wrong the day a stage is
    added, and wrong silently.

    **Not a rejection.** Stale artifacts stay viewable, queryable and
    approvable; the operator is told *when* their inputs changed and decides
    whether to regenerate. Deleting or invalidating them would throw away work
    that is often still fine.

    Series-level supersession does **not** come through here (ADR-016).
    ``stale_since`` means within-project invalidation only; a project pinned to
    character v1 is not stale because the series moved to v2.
    """
    graph = pipeline or get_pipeline()
    if not graph.has_stage(approved_kind):
        return 0

    downstream = graph.descendants(approved_kind)
    if not downstream:
        return 0

    ids = [
        artifact.id
        for artifact in uow.artifacts.for_project(project_id)
        if ArtifactKind(artifact.kind) in downstream
    ]
    if not ids:
        return 0

    marked = uow.artifacts.mark_stale(ids)
    if marked:
        logger.info(
            "staleness cascaded",
            extra={
                "project_id": project_id,
                "approved_kind": approved_kind.value,
                "artifacts_marked": marked,
            },
        )
    return marked


def refresh_project_state(
    uow: UnitOfWork,
    project_id: str,
    *,
    approved_kind: ArtifactKind | None = None,
    pipeline: Pipeline | None = None,
) -> ProjectPhase | None:
    """The one call every transition path makes.

    ``approved_kind`` is passed only when *this* transition was an approval —
    it is what turns on the cascade. Order matters: staleness first, then the
    phase, because a phase derived before the cascade would describe a project
    state that existed for the length of one function call and never again.
    """
    graph = pipeline or get_pipeline()
    if approved_kind is not None:
        cascade_staleness(uow, project_id, approved_kind, pipeline=graph)
    return recompute_phase(uow, project_id, pipeline=graph)
