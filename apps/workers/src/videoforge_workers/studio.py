"""Image Studio generation (ADR-018).

Free-form images: the person's prompt goes to the provider **verbatim** — no
``build_image_prompt``, no series character, no style block, no negative
prompt. Everything a reference-sheet run does *around* the provider call is
kept: the budget check before each image, content-addressed storage of the
original bytes (ADR-004), and a ``provider_usage`` row per call so studio spend
is inside the daily cap.

One transaction for the whole run, like reference sheets: if image three of
four fails, the job fails and none of the rows land. The objects already
written to the store stay (content-addressed, so a retry that draws the same
bytes reuses them) but are unreferenced.
"""

from __future__ import annotations

import logging
import mimetypes

from videoforge_domain.budget import check_budget
from videoforge_providers.models import ImageReference, ImageRequest
from videoforge_providers.protocols import ImageProvider
from videoforge_providers.registry import build_image_provider
from videoforge_shared.settings import get_worker_settings, load_worker_settings
from videoforge_shared.storage import StorageClient, storage_client_from_settings
from videoforge_shared.tasks import STUDIO_GENERATE
from videoforge_workers.skeleton import JobContext, videoforge_task

logger = logging.getLogger(__name__)

__all__ = ["generate_studio_images", "studio_body"]

_EXTENSIONS = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def studio_body(ctx: JobContext) -> None:
    """Generate every image one studio request asked for."""
    generation_id = str(ctx.input["generation_id"])
    generation = ctx.uow.studio.generation(generation_id)
    if generation is None:
        raise RuntimeError(f"studio generation {generation_id} vanished before running")

    settings = load_worker_settings()
    bucket = settings.minio.bucket_assets
    store = storage()

    # Read once, before any paid call: a missing reference should fail the job
    # for free rather than after three images have been billed.
    references = tuple(
        ImageReference(
            data=store.get_bytes(bucket, key),
            mime_type=mimetypes.guess_type(key)[0] or "image/png",
        )
        for key in generation.references
    )

    provider = _provider()
    for index in range(1, generation.count + 1):
        # Before **each** image, so a run that starts just under the cap stops
        # at it rather than finishing the batch past it.
        _require_budget(ctx)

        result = provider.generate(
            ImageRequest(
                prompt=generation.prompt,
                aspect_ratio=generation.aspect_ratio,
                references=references,
                model_hint=generation.model,
            )
        )
        image = result.images[0]
        stored = store.put_bytes(
            bucket,
            image.data,
            f"studio-{generation_id}-{index}{_EXTENSIONS.get(image.mime_type, '.bin')}",
        )
        ctx.uow.usage.record(
            job_id=ctx.job.id,
            provider=str(result.provider_meta.get("provider", "unknown")),
            model=str(result.provider_meta.get("model", generation.model)),
            operation="image.generate",
            latency_ms=result.latency_ms,
            unit_cost_estimate=result.usage.unit_cost_estimate,
            images=result.usage.images,
            raw_meta=result.provider_meta,
        )
        ctx.uow.studio.add_image(
            generation_id,
            index=index,
            storage_key=stored.key,
            content_hash=stored.sha256,
            mime_type=image.mime_type,
            width=image.width,
            height=image.height,
            provider_meta=dict(result.provider_meta),
        )
        ctx.uow.flush()

    logger.info(
        "studio generation complete",
        extra={
            "generation_id": generation_id,
            "images": generation.count,
            "model": generation.model,
            "references": len(references),
        },
    )


def _require_budget(ctx: JobContext) -> None:
    settings = get_worker_settings()
    limit = settings.core.daily_cost_limit
    if limit <= 0:
        return
    check_budget(
        ctx.uow.usage.spend_today(), limit, currency=settings.core.cost_currency
    )


def storage() -> StorageClient:
    """The object store, as a seam tests substitute (see ``references.storage``)."""
    return storage_client_from_settings(load_worker_settings().minio)


def _provider() -> ImageProvider:
    """Built per job, like reference generation: the studio is occasional."""
    settings = load_worker_settings()
    return build_image_provider(settings.providers, settings.provider_keys)


@videoforge_task(
    name=STUDIO_GENERATE.name, queue=STUDIO_GENERATE.queue, job_bearing=True
)
def generate_studio_images(ctx: JobContext) -> None:
    studio_body(ctx)
