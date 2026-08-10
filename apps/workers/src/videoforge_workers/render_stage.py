"""``render.generate`` (M4-09) — the approved timeline becomes an MP4.

The milestone's payoff, and structurally the least surprising part of it:
every decision was made upstream. The compiler resolved the timing, the
caption grouping resolved the cues, the mix resolved the gain. This task
fetches, spells the graph, runs one subprocess, and checks its own work.

Reuses M0-09's helpers rather than reimplementing them — ``_run`` (list argv,
``shell=False``, timeout), ``_probe``, ``moov_before_mdat``. That spike existed
to prove this path and it is cheaper to extend than to redo.

**Self-checking, not trusting.** ffmpeg exits 0 on plenty of videos nobody
wants. Before anything is uploaded this task verifies, on its own product:

* the **duration** matches the timeline it was compiled from, within a frame —
  the single number that catches an offset arithmetic error, which otherwise
  produces a video that plays fine and drifts out of sync;
* both **streams** are present and are h264/aac;
* **``moov`` precedes ``mdat``** on the actual bytes, so the review screen can
  start playing before the file finishes arriving (M0-10's path);
* ffmpeg's stderr carries no **libass font failure**, because a silent
  fallback to tofu boxes is a successful encode of an unusable video.

**Inputs come through ``get_bytes_verified``.** A corrupted frame fails the
job with an integrity error rather than becoming a garbage frame discovered at
review — the rule M0-09 set and the reason the render reads from storage
rather than from a path someone passed in.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from videoforge_providers.models import ImageResult, Usage
from videoforge_shared.enums import ArtifactKind
from videoforge_shared.settings import load_worker_settings
from videoforge_shared.storage import StorageClient, storage_client_from_settings
from videoforge_shared.tasks import RENDER_GENERATE
from videoforge_timeline import Timeline
from videoforge_workers.render import FfmpegError, _probe, _run, moov_before_mdat
from videoforge_workers.rendering import (
    render_command,
    scene_marks,
    video_filter_graph,
)
from videoforge_workers.skeleton import JobContext, videoforge_task
from videoforge_workers.stages import complete_stored_generation, load_artifact
from videoforge_workers.subtitles import CaptionLine, ass_document

logger = logging.getLogger(__name__)

__all__ = ["RENDERER_REF", "render_body", "storage"]

#: Pinned onto every render, like a prompt template ref (§10.3 rule 4).
RENDERER_REF = "render@1"

#: A 20-scene 1080×1920 encode at ``preset medium`` runs well under this on a
#: laptop. Generous rather than tight: the cost of a too-short timeout is a
#: failed job on a video that was nearly finished, and the soft task limit
#: (540s) is the real backstop.
_TIMEOUT_S = 480

#: How far the finished file may differ from the timeline before it is a bug
#: rather than rounding. One frame at 30fps is 33ms; 100ms allows for the
#: encoder's final-GOP behaviour without hiding a real offset error.
_DURATION_TOLERANCE_MS = 100

#: libass says these on stderr when it cannot resolve a font, and then renders
#: boxes. The encode succeeds; the video is unusable.
_FONT_FAILURES = ("fontselect: failed", "Glyph 0x", "No usable fonts")


def render_body(ctx: JobContext) -> None:
    """Encode the project's approved timeline."""
    artifact = load_artifact(ctx)
    project_id = artifact.project_id

    timeline = _approved_timeline(ctx, project_id)
    settings = load_worker_settings()
    client = storage()

    with tempfile.TemporaryDirectory(prefix="videoforge-render-") as tmp:
        root = Path(tmp)

        frame_paths: list[str] = []
        for clip in timeline.clips:
            path = root / f"frame{clip.scene_index:03d}"
            path.write_bytes(
                client.get_bytes_verified(
                    settings.minio.bucket_assets, clip.storage_key
                )
            )
            frame_paths.append(str(path))

        audio_paths: list[str] = []
        for index, track in enumerate(timeline.audio):
            path = root / f"audio{index}"
            path.write_bytes(
                client.get_bytes_verified(
                    settings.minio.bucket_assets, track.storage_key
                )
            )
            audio_paths.append(str(path))

        ass_path = root / "captions.ass"
        ass_path.write_text(
            ass_document(
                [
                    CaptionLine(text=cue.text, start_ms=cue.start_ms, end_ms=cue.end_ms)
                    for cue in timeline.captions
                ],
                width=timeline.video.width,
                height=timeline.video.height,
            ),
            encoding="utf-8",
        )

        out_path = root / "render.mp4"
        graph = video_filter_graph(timeline, ass_path=str(ass_path))
        stderr = _run(
            render_command(
                timeline,
                frame_paths=frame_paths,
                audio_paths=audio_paths,
                graph=graph,
                out_path=str(out_path),
            ),
            timeout_s=_TIMEOUT_S,
        )

        mp4 = out_path.read_bytes()
        probe = _verify(mp4, stderr, str(out_path), timeline)

        stored = client.put_bytes(
            settings.minio.bucket_artifacts, mp4, f"{project_id}-render.mp4"
        )

    version = complete_stored_generation(
        ctx,
        artifact,
        storage_key=stored.key,
        content_hash=stored.sha256,
        # A real zero: the encode is local and costs nothing, and a gap in
        # `provider_usage` reads like a missing record (M4-02's argument).
        result=ImageResult(
            usage=Usage(images=0, unit_cost_estimate=0.0),
            provider_meta={"provider": "local", "model": RENDERER_REF},
        ),
        prompt_ref=RENDERER_REF,
        operation="render.encode",
        meta={
            "mime_type": "video/mp4",
            "duration_ms": probe["duration_ms"],
            "size_bytes": len(mp4),
            "width": timeline.video.width,
            "height": timeline.video.height,
            "fps": timeline.video.fps,
            "scene_count": len(timeline.clips),
            "caption_count": len(timeline.captions),
            # Where each scene sits in the finished file, for the review
            # player (M4-11). Stored rather than re-derived, exactly as M3-12
            # stores voice spans: the player and the encoder must agree about
            # where scene 7 is, and they agree by reading one number.
            "scene_marks": scene_marks(timeline),
            # The graph is the single best artifact for debugging a render that
            # looks wrong, and it is a few hundred bytes.
            "filter_graph": graph,
            "renderer_ref": RENDERER_REF,
        },
    )

    logger.info(
        "render complete",
        extra={
            "project_id": project_id,
            "version_id": version.id,
            "duration_ms": probe["duration_ms"],
            "size_bytes": len(mp4),
        },
    )


def _approved_timeline(ctx: JobContext, project_id: str) -> Timeline:
    """The approved timeline, validated on the way in.

    Re-parsed through the model rather than read as a dict: the artifact was
    written by this codebase, but a version that predates a schema change would
    otherwise reach the graph builder as a plausible-looking mapping and fail
    somewhere in ffmpeg's argv.
    """
    artifact = ctx.uow.artifacts.find(project_id, ArtifactKind.TIMELINE)
    if artifact is None:
        raise RuntimeError(f"no timeline artifact for project {project_id}")
    approved = ctx.uow.versions.approved_version(artifact.id)
    if approved is None:
        raise RuntimeError(f"timeline has no approved version for {project_id}")
    version = ctx.uow.versions.get(approved.artifact_version_id)
    if version is None or version.inline_content is None:
        raise RuntimeError(f"approved timeline for {project_id} has no content")
    return Timeline.model_validate(version.inline_content)


def _verify(mp4: bytes, stderr: str, path: str, timeline: Timeline) -> dict[str, int]:
    """Check the product, not the exit code."""
    for red_flag in _FONT_FAILURES:
        if red_flag in stderr:
            raise FfmpegError(
                f"caption font problem: {red_flag!r} in the ffmpeg log — the "
                "encode succeeded and the captions are boxes"
            )

    probe = _probe(path)
    codecs = sorted(stream.get("codec_name", "?") for stream in probe["streams"])
    if codecs != ["aac", "h264"]:
        raise FfmpegError(f"expected h264 + aac, got {codecs}")

    duration_ms = int(round(float(probe["format"]["duration"]) * 1000))
    drift = abs(duration_ms - timeline.total_ms)
    if drift > _DURATION_TOLERANCE_MS:
        raise FfmpegError(
            f"rendered {duration_ms}ms but the timeline says {timeline.total_ms}ms "
            f"({drift}ms out) — the offsets and the video disagree, which plays "
            "fine and drifts out of sync"
        )

    if not moov_before_mdat(mp4):
        raise FfmpegError(
            "moov does not precede mdat; the review screen cannot start playing "
            "until the whole file arrives"
        )
    return {"duration_ms": duration_ms}


def storage() -> StorageClient:
    """The object store, as a seam — see ``references.storage``."""
    return storage_client_from_settings(load_worker_settings().minio)


@videoforge_task(
    name=RENDER_GENERATE.name, queue=RENDER_GENERATE.queue, job_bearing=True
)
def generate_render(ctx: JobContext) -> None:
    render_body(ctx)
