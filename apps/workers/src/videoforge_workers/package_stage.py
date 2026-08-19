"""``package.assemble`` — the publishing package (M5-03, F10).

The last stage, and the hole that has been in the DAG since M2: ``package`` has
been an ``ArtifactKind`` and the graph's terminal node since M2-02 with no task,
no worker and no table behind it. ``test_api`` has been using it as *the* stage
genuinely absent from ``STAGE_TASKS`` — which was an accurate marker of where
the pipeline stopped.

What goes in, per F10: the video, the thumbnail, the caption text, the
hashtags, metadata, and the scene assets. What makes it useful is the manifest
— see :mod:`videoforge_workers.packaging` for why a per-entry sha256 is the
point rather than decoration.

**Everything it reads is already approved.** The stage resolves the *approved*
version of each input rather than the latest, which is the same rule
``timeline.compile`` follows and for the same reason: a package assembled from
a render nobody signed off is a file that looks finished and is not.

**Spends nothing.** No provider call, so a zero usage row — a real zero rather
than an absent one, because a gap in ``provider_usage`` reads like a missing
record and the S10 cap sums this column.
"""

from __future__ import annotations

import logging
from typing import Any

from videoforge_persistence.models import PublishingPackage
from videoforge_providers.models import ImageResult, Usage
from videoforge_shared.enums import ArtifactKind, SceneKind
from videoforge_shared.ids import new_ulid
from videoforge_shared.settings import load_worker_settings
from videoforge_shared.storage import StorageClient, storage_client_from_settings
from videoforge_shared.tasks import PACKAGE_ASSEMBLE
from videoforge_workers.packaging import PackageEntry, build_package, manifest_for
from videoforge_workers.skeleton import JobContext, videoforge_task
from videoforge_workers.stages import (
    complete_stored_generation,
    load_artifact,
    require_approved_content,
)

logger = logging.getLogger(__name__)

__all__ = ["PACKAGER_REF", "assemble_body", "assemble_package", "storage"]

#: Pinned onto the version. A package built by a different layout is a
#: different archive, and §10.3 rule 4 wants that recoverable.
PACKAGER_REF = "package@1"

#: Paths inside the archive. Flat and predictable, because the first thing
#: anyone does with a publishing package is drag a file out of it.
_VIDEO_PATH = "video.mp4"
_COVER_PATH = "cover.png"
_CAPTION_PATH = "caption.txt"
_HASHTAGS_PATH = "hashtags.txt"
_SCENES_DIR = "scenes"


def assemble_body(ctx: JobContext) -> None:
    """Build one package version. Runs inside the skeleton's transaction."""
    artifact = load_artifact(ctx)
    project_id = artifact.project_id
    project = ctx.uow.projects.get(project_id)
    if project is None:
        raise RuntimeError(f"project {project_id} vanished")

    caption = require_approved_content(ctx, project_id, ArtifactKind.CAPTION)
    settings = load_worker_settings()
    client = storage()

    render_key = _approved_key(ctx, project_id, ArtifactKind.RENDER)
    cover_key = _approved_key(ctx, project_id, ArtifactKind.THUMBNAIL)

    video = client.get_bytes_verified(settings.minio.bucket_artifacts, render_key)
    cover = client.get_bytes_verified(settings.minio.bucket_assets, cover_key)

    hashtags = [str(tag) for tag in caption.get("hashtags") or []]
    entries = [
        PackageEntry(_VIDEO_PATH, video),
        PackageEntry(_COVER_PATH, cover),
        # Text files, not JSON. This is the half of the package a person opens
        # to copy and paste into Instagram, and a caption wrapped in quotes
        # with escaped newlines is a caption they have to clean up first.
        PackageEntry(_CAPTION_PATH, str(caption.get("caption") or "").encode("utf-8")),
        PackageEntry(
            _HASHTAGS_PATH, ("\n".join(f"#{tag}" for tag in hashtags)).encode("utf-8")
        ),
        *_scene_entries(ctx, project_id, client, settings.minio.bucket_assets),
    ]

    manifest = manifest_for(
        entries,
        project={
            "id": project_id,
            "topic": project.topic,
            "title": project.title,
        },
        video=_video_facts(ctx, project_id),
        caption={
            "hook": caption.get("hook"),
            "characters": len(str(caption.get("caption") or "")),
            "hashtags": hashtags,
        },
    )

    stored = client.put_bytes(
        settings.minio.bucket_artifacts,
        build_package(entries, manifest=manifest),
        f"{project_id}-package.zip",
    )

    version = complete_stored_generation(
        ctx,
        artifact,
        storage_key=stored.key,
        content_hash=stored.sha256,
        result=ImageResult(usage=Usage(images=0, unit_cost_estimate=0.0)),
        prompt_ref=PACKAGER_REF,
        operation="package.assemble",
        meta={
            "mime_type": "application/zip",
            "bytes": stored.size,
            # The manifest is in `meta` as well as in the row, so the review
            # screen can list the contents from the ordinary version endpoint
            # rather than needing a second one. The row is what a support
            # question queries; this is what a reviewer reads.
            "manifest": manifest,
        },
    )

    ctx.uow.session.add(
        PublishingPackage(
            id=new_ulid(),
            artifact_version_id=version.id,
            zip_key=stored.key,
            manifest=manifest,
        )
    )

    logger.info(
        "package assembled",
        extra={
            "project_id": project_id,
            "files": len(manifest["files"]),
            "bytes": stored.size,
        },
    )


def _scene_entries(
    ctx: JobContext, project_id: str, client: StorageClient, bucket: str
) -> list[PackageEntry]:
    """Every scene's approved frame, named by index.

    **Zero-padded** (``scene-001.png``): a directory listing sorts lexically,
    and ``scene-10`` before ``scene-2`` is the kind of thing that makes someone
    assemble a video in the wrong order by hand.

    Cards are included. They are frames of the finished video like any other,
    and a package whose scene folder skipped three of twenty would look like
    three had failed.
    """
    entries: list[PackageEntry] = []
    for scene in ctx.uow.scenes.for_approved_set(project_id):
        artifact = ctx.uow.artifacts.find(project_id, ArtifactKind.IMAGE, scene.id)
        if artifact is None:
            continue
        approved = ctx.uow.versions.approved_version(artifact.id)
        if approved is None:
            continue
        version = ctx.uow.versions.get(approved.artifact_version_id)
        if version is None or not version.storage_key:
            continue
        # Cards are labelled in the filename. "Why is scene 7 just text?" has a
        # very different answer when scene 7 was never drawn, and the person
        # asking is looking at a folder rather than at this codebase.
        card = "-card" if SceneKind(scene.kind) is SceneKind.CARD else ""
        entries.append(
            PackageEntry(
                f"{_SCENES_DIR}/scene-{scene.index:03d}{card}.png",
                client.get_bytes_verified(bucket, version.storage_key),
            )
        )
    return entries


def _video_facts(ctx: JobContext, project_id: str) -> dict[str, Any]:
    """Duration, dimensions and scene marks, copied from the render version.

    Read rather than recomputed: M4-11 stores these on the render because the
    player and the encoder must agree about where scene 7 is, and a package
    that measured them again would be a third opinion.
    """
    artifact = ctx.uow.artifacts.find(project_id, ArtifactKind.RENDER)
    if artifact is None:
        return {}
    approved = ctx.uow.versions.approved_version(artifact.id)
    if approved is None:
        return {}
    version = ctx.uow.versions.get(approved.artifact_version_id)
    meta = dict(version.meta or {}) if version is not None else {}
    return {
        "duration_ms": meta.get("duration_ms"),
        "width": meta.get("width"),
        "height": meta.get("height"),
        "scenes": len(meta.get("scene_marks") or []),
    }


def _approved_key(ctx: JobContext, project_id: str, kind: ArtifactKind) -> str:
    """The storage key of ``kind``'s approved version, or a loud failure.

    Admission has already checked that the stage is approved, so reaching the
    raise here means the artifact is approved and has no bytes — a state the
    stages that write them cannot produce, and worth saying so rather than
    packaging a zip with a hole in it.
    """
    artifact = ctx.uow.artifacts.find(project_id, kind)
    if artifact is not None:
        approved = ctx.uow.versions.approved_version(artifact.id)
        if approved is not None:
            version = ctx.uow.versions.get(approved.artifact_version_id)
            if version is not None and version.storage_key:
                return str(version.storage_key)
    raise RuntimeError(
        f"project {project_id} has an approved {kind.value} with no stored "
        "bytes; the package would be missing a file it claims to contain"
    )


def storage() -> StorageClient:
    """The object store, as a seam — see ``references.storage``."""
    return storage_client_from_settings(load_worker_settings().minio)


@videoforge_task(
    name=PACKAGE_ASSEMBLE.name, queue=PACKAGE_ASSEMBLE.queue, job_bearing=True
)
def assemble_package(ctx: JobContext) -> None:
    """Celery entry point. The work is in :func:`assemble_body`."""
    assemble_body(ctx)
