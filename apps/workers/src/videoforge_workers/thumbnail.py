"""``thumbnail.generate`` — the Reels cover (M5-02).

Composes an approved scene image with the approved caption's hook. The picture
itself is :mod:`videoforge_workers.cover`, which is pure; this module is the
part that knows about storage, artifacts and which scene to use.

**A reviewable artifact, not a byproduct of packaging.** The cover is the first
thing a person sees and the only part of the output that is never watched — if
it is wrong, the video does not get opened. That is exactly the kind of thing
this pipeline puts a human gate in front of.

**Costs nothing and calls nobody.** It runs on the ``image`` queue because that
worker already carries the fonts and Pillow that M4-02 put there, not because
it spends money — so no budget check, and a usage row of zero. Regenerating a
cover is free, which is what makes "try scene 4 instead" a reasonable thing for
a reviewer to ask for.
"""

from __future__ import annotations

import logging

from videoforge_providers.models import ImageResult, Usage
from videoforge_shared.enums import ArtifactKind, SceneKind
from videoforge_shared.settings import load_worker_settings
from videoforge_shared.storage import StorageClient, storage_client_from_settings
from videoforge_shared.tasks import THUMBNAIL_GENERATE
from videoforge_workers.cover import render_cover
from videoforge_workers.skeleton import JobContext, videoforge_task
from videoforge_workers.stages import (
    complete_stored_generation,
    load_artifact,
    require_approved_content,
)

logger = logging.getLogger(__name__)

__all__ = ["COVER_REF", "generate_thumbnail", "storage", "thumbnail_body"]

#: Pinned onto the version like any prompt ref. A cover produced by a different
#: layout is a different picture, and §10.3 rule 4 wants that recoverable.
COVER_REF = "cover@1"


def thumbnail_body(ctx: JobContext) -> None:
    """Render one cover. Runs inside the skeleton's transaction."""
    artifact = load_artifact(ctx)
    project_id = artifact.project_id

    caption = require_approved_content(ctx, project_id, ArtifactKind.CAPTION)
    hook = str(caption.get("hook") or "").strip()

    scene_id, storage_key = _source_frame(ctx, project_id)
    settings = load_worker_settings()
    client = storage()

    background = client.get_bytes_verified(settings.minio.bucket_assets, storage_key)
    cover = render_cover(
        background,
        hook,
        width=settings.render.width,
        height=settings.render.height,
    )

    stored = client.put_bytes(
        settings.minio.bucket_assets, cover, f"{project_id}-cover.png"
    )

    complete_stored_generation(
        ctx,
        artifact,
        storage_key=stored.key,
        content_hash=stored.sha256,
        # A real zero rather than no row: a gap in `provider_usage` reads like a
        # missing record, and this stage genuinely spends nothing.
        result=ImageResult(usage=Usage(images=0, unit_cost_estimate=0.0)),
        prompt_ref=COVER_REF,
        operation="thumbnail.generate",
        meta={
            "mime_type": "image/png",
            "hook": hook,
            # Which scene the cover came from, so the review screen can say so
            # and a reviewer asking for a different one knows what they have.
            "scene_id": scene_id,
            "source_key": storage_key,
        },
    )

    logger.info(
        "cover rendered",
        extra={"project_id": project_id, "scene_id": scene_id, "hook": hook},
    )


def _source_frame(ctx: JobContext, project_id: str) -> tuple[str, str]:
    """The scene image the cover is built on: ``(scene_id, storage_key)``.

    **The first illustration**, not simply the first scene. A card (M4-01) is
    typography on flat paper — putting the hook over one gives a cover that is
    text on text, and the card's own words would be fighting the hook's. Scene
    order is the hook order, so the earliest illustration is both the opening
    image and the one chosen to represent the video.

    Raises rather than falling back to a card if no illustration has an
    approved image: a video with no artwork at all is a real anomaly, and a
    cover assembled from whatever happened to be lying around would hide it.
    """
    scenes = ctx.uow.scenes.for_approved_set(project_id)
    for scene in scenes:
        if SceneKind(scene.kind) is not SceneKind.ILLUSTRATION:
            continue
        artifact = ctx.uow.artifacts.find(project_id, ArtifactKind.IMAGE, scene.id)
        if artifact is None:
            continue
        approved = ctx.uow.versions.approved_version(artifact.id)
        if approved is None:
            continue
        version = ctx.uow.versions.get(approved.artifact_version_id)
        if version is not None and version.storage_key:
            return scene.id, version.storage_key

    raise RuntimeError(
        f"project {project_id} has no approved illustration to build a cover "
        "from; every scene is a card or its image is not approved"
    )


def storage() -> StorageClient:
    """The object store, as a seam — see ``references.storage``."""
    return storage_client_from_settings(load_worker_settings().minio)


@videoforge_task(
    name=THUMBNAIL_GENERATE.name, queue=THUMBNAIL_GENERATE.queue, job_bearing=True
)
def generate_thumbnail(ctx: JobContext) -> None:
    """Celery entry point. The work is in :func:`thumbnail_body`."""
    thumbnail_body(ctx)
