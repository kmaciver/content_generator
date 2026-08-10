"""``timeline.compile`` (M4-08) — the compiler, behind the task skeleton.

The first M4 stage that touches the database. Everything it decides was
decided in ``packages/timeline``; this module's job is to *gather* — approved
scenes, approved frames, the approved narration — and to store what comes back
as an ordinary reviewable artifact version.

**The gathering is the risk, not the compiling.** ``compile_timeline`` is pure
and has its own tests; what can go wrong here is reading the *wrong* versions
— a stale frame, a superseded narration — which produces a timeline that is
internally consistent and describes a video nobody approved. So every read
goes through ``versions.approved_version`` (the status view, B1) rather than
``video_project.active_pointers``, which is a cache.

**Inline, not stored.** The compiled timeline is tens of kilobytes of JSON —
ninety-odd caption cues and a clip per scene — so it lives in
``inline_content`` like the text stages, not behind a ``storage_key`` like the
media ones. It is also the artifact a human is most likely to want to read
directly when a video comes out wrong.

**No provider, no spend.** The usage row this writes is a real zero under
``timeline.compile``, for the reason M4-02 gives about cards: a gap in
``provider_usage`` reads like a missing record.
"""

from __future__ import annotations

import logging
from typing import Any

from videoforge_domain.timing import WordTiming
from videoforge_persistence.models import Scene
from videoforge_providers.models import LLMResult
from videoforge_shared.enums import ArtifactKind, SceneKind
from videoforge_shared.settings import load_worker_settings
from videoforge_shared.tasks import TIMELINE_COMPILE
from videoforge_timeline import (
    CompileOptions,
    Frame,
    Span,
    TimelineSource,
    compile_timeline,
)
from videoforge_workers.skeleton import JobContext, videoforge_task
from videoforge_workers.stages import complete_generation, load_artifact

logger = logging.getLogger(__name__)

__all__ = ["COMPILER_REF", "compile_body", "compile_timeline_task"]

#: Pinned onto every version, like a prompt template ref (§10.3 rule 4). A
#: change to the compiler's arithmetic must bump this, or "why is episode 4
#: cut differently?" has no answer.
COMPILER_REF = "timeline@1"


def compile_body(ctx: JobContext) -> None:
    """Compile the approved media of one project into a timeline."""
    artifact = load_artifact(ctx)
    project_id = artifact.project_id

    scenes = ctx.uow.scenes.for_approved_set(project_id)
    if not scenes:
        raise RuntimeError(f"no approved scene set for project {project_id}")

    voice_version_id, voice = _approved(ctx, project_id, ArtifactKind.VOICE)
    scene_set_version_id, _ = _approved(ctx, project_id, ArtifactKind.SCENE_SET)

    frames = _frames(ctx, project_id, scenes)
    spans = _spans(voice, {frame.scene_id for frame in frames})

    settings = load_worker_settings()
    timeline = compile_timeline(
        project_id=project_id,
        frames=frames,
        spans=spans,
        narration_storage_key=str(voice["storage_key"]),
        narration_duration_ms=int(voice["duration_ms"]),
        source=TimelineSource(
            scene_set_version_id=scene_set_version_id,
            voice_version_id=voice_version_id,
            image_version_ids={frame.scene_id: frame.version_id for frame in frames},
        ),
        options=CompileOptions(
            width=settings.render.width,
            height=settings.render.height,
            fps=settings.render.fps,
        ),
    )

    complete_generation(
        ctx,
        artifact,
        content=timeline.model_dump(mode="json"),
        # A real zero rather than no row: a gap in `provider_usage` reads like
        # a missing record, and this stage genuinely spends nothing.
        result=LLMResult(
            text="", provider_meta={"provider": "local", "model": COMPILER_REF}
        ),
        prompt_ref=COMPILER_REF,
    )

    logger.info(
        "timeline compiled",
        extra={
            "project_id": project_id,
            "total_ms": timeline.total_ms,
            "clips": len(timeline.clips),
            "captions": len(timeline.captions),
            "cards": sum(1 for clip in timeline.clips if clip.kind is SceneKind.CARD),
        },
    )


def _approved(
    ctx: JobContext, project_id: str, kind: ArtifactKind
) -> tuple[str, dict[str, Any]]:
    """The approved version id **and** content of a project-wide artifact.

    ``require_approved_content`` returns only the content, and this stage has
    to pin the version id into ``TimelineSource`` — without it, "why is scene 4
    three seconds long?" has no answer once the voice artifact moves on.
    """
    artifact = ctx.uow.artifacts.find(project_id, kind)
    if artifact is None:
        raise RuntimeError(f"{kind.value} artifact does not exist for {project_id}")
    approved = ctx.uow.versions.approved_version(artifact.id)
    if approved is None:
        raise RuntimeError(f"{kind.value} has no approved version for {project_id}")
    version = ctx.uow.versions.get(approved.artifact_version_id)
    if version is None:
        raise RuntimeError(f"approved {kind.value} version vanished for {project_id}")

    content: dict[str, Any] = dict(version.inline_content or {})
    if version.storage_key:
        # Media artifacts keep their bytes in object storage and everything
        # else in `meta`; the CHECK constraint permits exactly one of the two.
        content = dict(version.meta or {})
        content["storage_key"] = version.storage_key
    return version.id, content


def _frames(ctx: JobContext, project_id: str, scenes: list[Scene]) -> list[Frame]:
    """One approved frame per scene, in scene order.

    Fails naming **every** missing scene rather than the first. A reviewer who
    has to approve three stragglers wants to know that in one message, not to
    re-run the job three times.
    """
    frames: list[Frame] = []
    missing: list[int] = []

    for scene in scenes:
        artifact = ctx.uow.artifacts.find(project_id, ArtifactKind.IMAGE, scene.id)
        approved = (
            ctx.uow.versions.approved_version(artifact.id)
            if artifact is not None
            else None
        )
        version = (
            ctx.uow.versions.get(approved.artifact_version_id)
            if approved is not None
            else None
        )
        if version is None or not version.storage_key:
            missing.append(scene.index)
            continue
        frames.append(
            Frame(
                scene_id=scene.id,
                scene_index=scene.index,
                kind=scene.kind,
                storage_key=version.storage_key,
                version_id=version.id,
            )
        )

    if missing:
        raise RuntimeError(
            f"project {project_id} has no approved image for scenes {missing}; "
            "every scene needs a frame before a timeline can be compiled"
        )
    return frames


def _spans(voice: dict[str, Any], scene_ids: set[str]) -> list[Span]:
    """The stored spans, as the compiler's input type.

    ``meta['spans']`` is written by ``voice.generate`` and read here — the
    grouping is deliberately *not* re-derived. It is deterministic, but
    re-running it against a different build's rules would silently re-time a
    narration a human already approved (the reason M3-12 stored it at all).
    """
    raw = voice.get("spans")
    if not isinstance(raw, list) or not raw:
        raise RuntimeError("the approved voice artifact carries no scene spans")

    spans: list[Span] = []
    for entry in raw:
        scene_id = str(entry.get("scene_id") or "")
        if scene_id not in scene_ids:
            # The narration was synthesised against a different scene set.
            # Compiling anyway would put words over the wrong pictures.
            raise RuntimeError(
                f"the approved narration has a span for scene {scene_id}, which "
                "is not in the approved scene set; regenerate the voice"
            )
        spans.append(
            Span(
                scene_id=scene_id,
                start_ms=int(entry["start_ms"]),
                end_ms=int(entry["end_ms"]),
                words=tuple(
                    WordTiming(
                        text=str(word["text"]),
                        start_ms=int(word["start_ms"]),
                        end_ms=int(word["end_ms"]),
                        offset=index,
                    )
                    for index, word in enumerate(entry.get("words") or [])
                ),
            )
        )
    return spans


@videoforge_task(
    name=TIMELINE_COMPILE.name, queue=TIMELINE_COMPILE.queue, job_bearing=True
)
def compile_timeline_task(ctx: JobContext) -> None:
    compile_body(ctx)
