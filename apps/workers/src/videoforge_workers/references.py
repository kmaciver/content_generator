"""``references.generate`` — candidate reference sheets for a character (M3-04b).

The first job in the system that belongs to a **series** rather than a project,
and the first that writes images. Both follow from ADR-016: branding is
generated once and consumed by every episode.

**Candidates, not a version.** One run produces 4–8 images of the same
character in different poses, and the operator approves the *group* as the
canonical sheet. That is not "one artifact, many versions, one approved" — the
unit of approval is a set — which is exactly why ADR-016 kept branding out of
the artifact tables.

**Nothing is auto-selected.** The run leaves the character where it found it;
approving a group is a separate, explicit act (``POST /characters/{id}/approve``
with a ``reference_group_id``). A run that quietly promoted its own output would
make the review gate decorative for the one asset every episode depends on.

**Why the sheets are generated from text and not from each other.** Feeding
candidate 1 back as a reference for candidate 2 would produce a tighter set —
and a set that has already collapsed onto whatever candidate 1 happened to be,
before a human has said it is right. The point of showing several is that they
differ.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from videoforge_domain.budget import check_budget
from videoforge_persistence.models import SeriesCharacter
from videoforge_prompts.image_prompt import CharacterSpec, build_image_prompt
from videoforge_prompts.style import StyleSpec, compile_style_block
from videoforge_providers.models import ImageRequest
from videoforge_providers.protocols import ImageProvider
from videoforge_providers.registry import build_image_provider
from videoforge_shared.enums import BrandingStatus
from videoforge_shared.settings import get_worker_settings, load_worker_settings
from videoforge_shared.storage import StorageClient, storage_client_from_settings
from videoforge_shared.tasks import REFERENCES_GENERATE
from videoforge_workers.skeleton import JobContext, videoforge_task

logger = logging.getLogger(__name__)

__all__ = [
    "REFERENCE_POSES",
    "generate_references",
    "references_body",
    "storage",
]

#: What each candidate shows, and the axis M3-07 later selects a reference by.
#:
#: Chosen to *span* the character rather than to flatter it: a sheet of four
#: near-identical front views tells a reviewer nothing about whether the
#: convention survives being turned, and gives the scene stage nothing to pick
#: between. Front and three-quarter are the workhorses; the back view is the
#: one that exposes a character defined only by its face.
REFERENCE_POSES: tuple[dict[str, str], ...] = (
    {
        "pose": "standing, arms at sides",
        "angle": "front",
        "expression": "neutral",
        "shot_type": "full body",
        "scene": "standing straight, facing the viewer, full body visible",
    },
    {
        "pose": "standing, arms at sides",
        "angle": "three-quarter",
        "expression": "neutral",
        "shot_type": "full body",
        "scene": "standing, turned three-quarters to the left, full body visible",
    },
    {
        "pose": "standing, arms at sides",
        "angle": "side",
        "expression": "neutral",
        "shot_type": "full body",
        "scene": "standing in exact profile, seen from the side, full body visible",
    },
    {
        "pose": "standing, arms at sides",
        "angle": "back",
        "expression": "neutral",
        "shot_type": "full body",
        "scene": "standing with the back to the viewer, full body visible",
    },
)


#: What a reference sheet's background must be, whatever the series style says.
#:
#: The style's ``background`` field describes what **scenes** look like, and a
#: series that wants a suggested setting per scene says so there. Measured on
#: 2026-08-08: a style reading "at most one simple suggested element" put a tuft
#: of grass beside the character in the back view. In a scene that is correct;
#: in a reference sheet it is contamination, because the sheet is the visual
#: definition of the character and every mark in it reads as a claim about who
#: they are.
#:
#: Overridden for the same reason :func:`_character_spec` drops ``variable``: a
#: sheet is the *canonical* depiction, so the things a scene is free to vary are
#: exactly the things the sheet must pin.
_REFERENCE_BACKGROUND = (
    "a single flat field of plain warm off-white paper, entirely empty; "
    "no scenery, no props, no ground line, no horizon"
)

#: Negative terms for reference sheets only, passed per call rather than added
#: to the series style.
#:
#: A style-level refusal of "scenery" would strip it from all twenty scenes too,
#: which is the opposite of what a style is for. ``build_image_prompt`` takes
#: ``scene_negative`` precisely so a caller can refuse something here without
#: legislating for every image the series will ever produce.
#:
#: ``multiple views, split panels`` is not cosmetic: asked for a *reference*, a
#: model will readily answer with a contact sheet of small views in one frame,
#: which is unusable as a single-pose reference for M3-07.
_REFERENCE_NEGATIVE = (
    "scenery, grass, plants, props, furniture, background objects, "
    "horizon line, ground line, floor, multiple views, split panels"
)


def _reference_style(style_fields: Mapping[str, Any] | None) -> StyleSpec:
    """The series style with its background replaced for sheet generation.

    A copy, never a mutation: ``style.fields`` is the persisted jsonb of an
    approved version, and editing it in place would rewrite an immutable record
    as a side effect of drawing a picture.
    """
    fields = dict(style_fields or {})
    fields["background"] = _REFERENCE_BACKGROUND
    return compile_style_block(fields)


def references_body(ctx: JobContext) -> None:
    """Generate one candidate group for a character version."""
    character_id = str(ctx.input["character_id"])
    group_id = str(ctx.input["group_id"])

    character = ctx.uow.branding.character(character_id)
    if character is None:
        raise RuntimeError(f"character {character_id} vanished before generation")

    style = ctx.uow.branding.approved_style(character.series_id)
    if style is None:
        # Not a degradation. A reference sheet drawn without the series style is
        # a sheet the scene images will not match, which is worse than no sheet
        # at all — and the admission check in the API is supposed to have made
        # this unreachable.
        raise RuntimeError(
            f"series {character.series_id} has no approved style; "
            "reference sheets would not match the scenes they anchor"
        )

    provider = _provider()
    spec = _character_spec(character)
    style_spec = _reference_style(style.fields)
    settings = load_worker_settings()

    written = 0
    for index, pose in enumerate(REFERENCE_POSES, start=1):
        # Checked before **each** image, not once per run. A four-image run
        # that starts just under the cap should stop at the boundary rather
        # than sail past it by three images, and images are where that
        # difference is measured in real money.
        _require_budget(ctx)

        built = build_image_prompt(
            scene=pose["scene"],
            character=spec,
            style=style_spec,
            scene_negative=_REFERENCE_NEGATIVE,
        )
        result = provider.generate(
            ImageRequest(
                prompt=built.prompt,
                negative_prompt=built.negative_prompt,
                aspect_ratio="1:1",
            )
        )
        image = result.images[0]

        # Content-addressed, like every other binary (ADR-004). The *original*
        # provider bytes, never a re-encode: these are fed back as references
        # for scene generation, and Gemini answers in JPEG, so re-encoding at
        # each hop would compound generational loss on the one asset the whole
        # series' consistency rests on.
        stored = storage().put_bytes(
            settings.minio.bucket_assets,
            image.data,
            f"{character_id}-{group_id}-{index}{_extension(image.mime_type)}",
        )

        ctx.uow.usage.record(
            job_id=ctx.job.id,
            provider=str(result.provider_meta.get("provider", "unknown")),
            model=str(result.provider_meta.get("model", "unknown")),
            operation="image.generate",
            latency_ms=result.latency_ms,
            unit_cost_estimate=result.usage.unit_cost_estimate,
            images=result.usage.images,
            raw_meta=result.provider_meta,
        )
        ctx.uow.branding.add_reference(
            character_id,
            group_id=group_id,
            index=index,
            storage_key=stored.key,
            content_hash=stored.sha256,
            mime_type=image.mime_type,
            width=image.width,
            height=image.height,
            pose=pose["pose"],
            angle=pose["angle"],
            expression=pose["expression"],
            shot_type=pose["shot_type"],
            generation_job_id=ctx.job.id,
            # §10.3 rule 4: the exact prompt, its digest and the template that
            # framed it, plus which character and style version it was drawn
            # against. Enough to explain any sheet without re-deriving it.
            generation_snapshot=built.snapshot(
                character_version_id=character.id,
                style_version_id=style.id,
                aspect_ratio="1:1",
                provider_meta=result.provider_meta,
            ),
        )
        ctx.uow.flush()
        written += 1

    # The character becomes reviewable, not approved. Approving is a separate
    # explicit act that also names *which* group won — a run cannot promote its
    # own output.
    if character.status is BrandingStatus.PENDING:
        character.status = BrandingStatus.AWAITING_APPROVAL

    logger.info(
        "reference group generated",
        extra={
            "character_id": character_id,
            "group_id": group_id,
            "images": written,
        },
    )


def _character_spec(character: SeriesCharacter) -> CharacterSpec:
    """The ORM row as the prompt builder wants it.

    ``variable`` is deliberately dropped: a reference sheet is the *canonical*
    depiction, and the traits a scene may vary are exactly the ones it should
    not. The pose comes from ``REFERENCE_POSES`` instead, which is what makes
    the sheet span the character rather than repeat one arbitrary stance.
    """
    return CharacterSpec(
        name=character.name,
        immutable=dict(character.immutable_traits or {}),
        variable={},
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
    """The object store, as a **seam**.

    A module-level function rather than a direct
    ``storage_client_from_settings`` call at the use site, for the same reason
    ``worker_db.get_session_factory`` is one: the integration harness stands up
    a real PostgreSQL because the database is where the interesting properties
    live, and standing up a real MinIO alongside it would buy nothing — the
    property under test here is "four rows with the right provenance", not
    "boto3 can PUT". Tests substitute an in-memory client through this name.

    Not cached: ``put_bytes`` is called a handful of times per series, and a
    cached S3 client held for a worker's lifetime is a connection to babysit
    for no measurable gain.
    """
    return storage_client_from_settings(load_worker_settings().minio)


def _provider() -> ImageProvider:
    """Built per call rather than cached.

    ``stages.provider`` caches the LLM provider because every stage uses it on
    every job. Reference generation is rare — a handful of times per series
    ever — so a cache would hold a client open for the life of a worker to save
    microseconds a few times a month.
    """
    settings = load_worker_settings()
    return build_image_provider(settings.providers, settings.provider_keys)


def _extension(mime_type: str) -> str:
    """Content type decides the extension, not the other way round.

    The key is content-addressed and the extension is only a hint for the
    asset-serving path's content type (ADR-011). Gemini returns JPEG and the
    mock returns PNG, so hardcoding either would mislabel half the objects.
    """
    return {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(
        mime_type, ".bin"
    )


@videoforge_task(
    name=REFERENCES_GENERATE.name, queue=REFERENCES_GENERATE.queue, job_bearing=True
)
def generate_references(ctx: JobContext) -> None:
    references_body(ctx)
