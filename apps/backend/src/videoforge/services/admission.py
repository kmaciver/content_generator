"""Whether a stage may run at all (M3-06).

Two questions, asked before any job row exists:

1. **Are this stage's pipeline inputs approved?** (ADR-009)
2. **Does this project have the branding an image needs?** (ADR-016)

They look alike and are not. The first is an *edge* — the previous stage is in
progress, and waiting is the answer. The second is an *admission check* — the
project has no character, nothing happening in this pipeline will produce one,
and the fix is on another screen entirely. ADR-016 works through the four axes
on which they differ and concludes the branding rule must not be a DAG edge.
This module is where both are asked, in that order.

**The first check is new here, and was missing entirely.** M2-13 computed
``unmet`` in the DTO so the UI could disable a button, and its docstring said
the service "will then independently enforce" it. The service did not: nothing
in ``JobService.request`` consulted the pipeline. A direct
``POST /projects/{id}/generations`` for ``scene_set`` on a project with no
approved script returned 202, burned a job, and failed in the worker at
``require_approved_content`` — whose own comment said arriving there meant the
guard had failed. Both sides believed the other was checking.

Nothing was corrupted by that (the worker fails loudly and the artifact goes
FAILED), but it inverts the principle the UI is built on: the server decides
what may happen, and the client renders that decision. Here it trusted the
client to have obeyed a decision the server never made.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from videoforge_persistence.models import SeriesCharacter, SeriesStyle
from videoforge_persistence.projection import get_pipeline
from videoforge_persistence.uow import UnitOfWork
from videoforge_shared.enums import ArtifactKind, ArtifactState

logger = logging.getLogger(__name__)

__all__ = ["AdmissionError", "Branding", "check_admission", "resolve_branding"]

#: Kinds whose generation needs an approved series character and style.
#:
#: Images only. The prompt stage (M2-12) describes what is in frame and
#: deliberately names no style — ``prompt.v1.jinja`` says so, because a style
#: named there would fight the series block and win unpredictably. Voice and
#: timeline consume no visual branding at all.
_NEEDS_BRANDING = frozenset({ArtifactKind.IMAGE})


class AdmissionError(RuntimeError):
    """The request is well-formed but the world is not ready for it.

    Mapped to **409**, not 400: nothing about the request is wrong, and
    repeating it later may well succeed. That is the same distinction
    ``IllegalTransitionError`` already carries, which is why this sits beside
    it rather than inheriting from a validation error.
    """


@dataclass(frozen=True, slots=True)
class Branding:
    """The branding a project generates against — pinned, or about to be."""

    character: SeriesCharacter
    style: SeriesStyle
    #: True when these came from ``video_project``'s pin rather than from the
    #: series' current approvals. A pinned project keeps using a superseded
    #: character forever, and that is the point (ADR-016).
    pinned: bool


def check_admission(uow: UnitOfWork, project_id: str, kind: ArtifactKind) -> None:
    """Raise :class:`AdmissionError` if ``kind`` may not be generated now.

    Order matters. Pipeline prerequisites are checked first because they are
    the far more common failure and produce the more actionable message —
    "waiting on: script" tells an operator to go and approve a script, whereas
    a branding error sends them to a different screen for a reason that would
    still be true after they did.
    """
    _check_pipeline(uow, project_id, kind)
    if kind in _NEEDS_BRANDING:
        resolve_branding(uow, project_id)


def _check_pipeline(uow: UnitOfWork, project_id: str, kind: ArtifactKind) -> None:
    """Every ``requires`` of this stage must be APPROVED.

    Approval is read from ``artifact.state``, not from
    ``video_project.active_pointers``: the pointer column is a cache (B1) and
    this is a write path.

    Per-scene artifacts collapse to the **least advanced** of their kind — the
    same rule phase derivation uses. Nineteen approved images and one still
    generating is not an approved image stage, and a downstream stage that
    started on that basis would consume a hole.
    """
    pipeline = get_pipeline()
    if not pipeline.has_stage(kind):
        # Not a pipeline stage at all. Nothing to check rather than an error:
        # the caller already validated the kind against `STAGE_TASKS`, and
        # duplicating that rejection here would give two different messages
        # for one mistake.
        return

    worst: dict[ArtifactKind, ArtifactState] = {}
    for artifact in uow.artifacts.for_project(project_id):
        artifact_kind = ArtifactKind(artifact.kind)
        state = ArtifactState(artifact.state)
        current = worst.get(artifact_kind)
        if current is None or _rank(state) < _rank(current):
            worst[artifact_kind] = state

    approved = {k for k, state in worst.items() if state is ArtifactState.APPROVED}
    unmet = sorted(k.value for k in pipeline.unmet(kind, approved))
    if unmet:
        raise AdmissionError(
            f"{kind.value} cannot run yet; waiting on: {', '.join(unmet)}"
        )


def resolve_branding(uow: UnitOfWork, project_id: str) -> Branding:
    """The character and style this project must generate against.

    Returns the **pinned** versions when the project has them, even if the
    series has moved on — that is the whole point of pinning, and reading the
    series' current approvals here instead would make an episode's later scenes
    disagree with its earlier ones.

    Raises :class:`AdmissionError` when the project has no series, or the
    series has no approved character or style. ADR-016 chose this over a
    workspace-level default: more machinery for a case with no user yet.
    """
    project = uow.projects.get(project_id)
    if project is None:
        raise AdmissionError(f"no project {project_id}")

    if project.character_version_id and project.style_version_id:
        character = uow.branding.character(project.character_version_id)
        style = uow.branding.style(project.style_version_id)
        if character is not None and style is not None:
            return Branding(character=character, style=style, pinned=True)
        # The pin points at rows that no longer exist — only reachable by
        # deleting the series, which cascades the branding away and nulls
        # `series_id`. Saying so beats falling back to the series' current
        # branding, which would silently re-brand an existing video.
        raise AdmissionError(
            f"project {project_id} is pinned to branding that no longer exists; "
            "its series was deleted"
        )

    if project.series_id is None:
        raise AdmissionError(
            "image generation needs a series with an approved character and "
            "style (ADR-016); this project belongs to no series"
        )

    character = uow.branding.approved_character(project.series_id)
    style = uow.branding.approved_style(project.series_id)
    missing = [
        name
        for name, value in (("character", character), ("style", style))
        if value is None
    ]
    if missing or character is None or style is None:
        raise AdmissionError(
            f"image generation needs an approved {' and '.join(missing)} on "
            f"this project's series; set it up in series settings"
        )

    return Branding(character=character, style=style, pinned=False)


#: Least-advanced-wins ordering, matching ``projection._states``. Duplicated
#: rather than imported because that function is private to the data layer and
#: takes a unit of work; extracting a shared helper for six lines would put a
#: read-model detail into the domain package to save nothing.
_RANK: dict[ArtifactState, int] = {
    ArtifactState.FAILED: 0,
    ArtifactState.PENDING: 1,
    ArtifactState.GENERATING: 2,
    ArtifactState.REJECTED: 3,
    ArtifactState.AWAITING_APPROVAL: 4,
    ArtifactState.APPROVED: 5,
}


def _rank(state: ArtifactState) -> int:
    return _RANK[state]
