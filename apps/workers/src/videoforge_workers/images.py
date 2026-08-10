"""``image.generate`` (M3-07) — one image per scene, against pinned branding.

The stage the whole of M3 was built for. Everything upstream produces text; this
is where a project first produces something a viewer would recognise, and where
risk R7 — twenty scenes that do not look like one show — is either solved or
not.

**Three inputs, and the reason each is where it is.**

* The **prompt artifact** for the scene says what is in frame. It deliberately
  names no style (``prompt.v1.jinja``), because a style named there would fight
  the series block and win unpredictably.
* The **pinned** character and style say how everything looks. Pinned, not
  current: an episode half-generated against character v3 must finish against
  v3, or its later scenes will not match its earlier ones. ``JobService``
  writes that pin on the first image request and never again.
* The **approved reference sheets** show the model what the character actually
  is. This is the payoff for M3-04b — the first stage that consumes them.

**Why the pin is read here rather than resolved.** ``resolve_branding`` lives
in ``apps/backend``, and a worker importing from another app would break the
rule that apps depend on packages and never sideways. It is also unnecessary:
by the time this task runs, the pin is a fact the dispatching service already
established. Reading it — and failing loudly when it is absent — is both
architecturally correct and a stronger check, because an unpinned project
arriving here means the admission path was bypassed.

**One job, N images — for now.** ``prompts_stage`` promises that image
generation fans out into N jobs so that losing one of twenty images does not
re-run the other nineteen, and that remains the right shape. This ticket does
the batched version first: it makes the stage testable end to end, and the
fan-out is a change to how jobs are *created*, not to the body below. The cost
of the interim is stated rather than hidden — a failure on scene 4 fails the
job, and a retry regenerates scenes 1 to 3 as well.
"""

from __future__ import annotations

import logging
from typing import Any

from videoforge_domain.rejection import Correction, build_correction
from videoforge_persistence.models import (
    Artifact,
    CharacterReference,
    Scene,
    SeriesCharacter,
    SeriesStyle,
)
from videoforge_prompts.image_prompt import CharacterSpec, build_image_prompt
from videoforge_prompts.style import compile_style_block
from videoforge_providers.models import (
    ImageReference,
    ImageRequest,
    ImageResult,
    LLMResult,
    Usage,
)
from videoforge_providers.protocols import ImageProvider
from videoforge_providers.registry import build_image_provider
from videoforge_shared.enums import ArtifactKind, ArtifactState, SceneKind
from videoforge_shared.settings import load_worker_settings
from videoforge_shared.storage import StorageClient, storage_client_from_settings
from videoforge_shared.tasks import IMAGES_GENERATE
from videoforge_workers.cards import palette_from_style, render_card
from videoforge_workers.imaging import normalise
from videoforge_workers.skeleton import JobContext, videoforge_task
from videoforge_workers.stages import (
    approve_without_human,
    complete_generation,
    complete_stored_generation,
    load_artifact,
    require_budget,
)

logger = logging.getLogger(__name__)

__all__ = [
    "generate_images",
    "images_body",
    "normalise",
    "storage",
]

#: Version of the local card renderer, pinned onto every card version the same
#: way a prompt template ref is (§10.3 rule 4). A layout change here must bump
#: it, or "why does episode 4's card look different?" has no answer.
_CARD_RENDERER_REF = "card@1"

#: Which reference sheets to send, in preference order.
#:
#: Front and three-quarter first because they carry the most identifying
#: information; the back view is nearly content-free for a character defined by
#: its face and goes last, so a provider with a small reference budget spends it
#: on the views that discriminate.
_REFERENCE_PRIORITY: tuple[str, ...] = ("front", "three-quarter", "side", "back")

#: Negative terms every scene image carries, on top of the style's ``avoid``.
#:
#: These are properties of *being a video frame*, not of this series' look, so
#: they belong here rather than in an operator-editable style: a 9:16 frame
#: divided into panels is unusable however the series is drawn, and no style
#: should have to remember to forbid it.
#:
#: Measured on 2026-08-08, first live run. Two of five scenes came back as
#: split panels and one carried mirror-written text — and in every case the
#: *positive* prompt had asked for it, because the prompt stage wrote
#: "Split-screen composition divided by a vertical line" and "a notebook with
#: 'budget' written on the cover". The real fix is upstream in
#: ``prompt.v1.jinja``; this is the second line of defence, for a hand-written
#: or already-approved prompt that still asks. The nose experiment showed the
#: negative channel bites where prose refusals do not.
_SCENE_NEGATIVE = (
    "split screen, diptych, triptych, multiple panels, comic strip, "
    "storyboard grid, before and after, divided frame, vertical dividing line, "
    "picture-in-picture, inset, "
    "border, framed border, picture frame, drawn frame, matte, passepartout, "
    "margin, white margin, letterbox, vignette, rounded corners, "
    "text, letters, numbers, words, writing, handwriting, captions, subtitles, "
    "labels, signage, watermark, logo, signature"
)


def images_body(ctx: JobContext) -> None:
    """Generate one image artifact per scene with an approved prompt."""
    trigger = load_artifact(ctx)
    project_id = trigger.project_id

    scenes = ctx.uow.scenes.for_approved_set(project_id)
    if not scenes:
        raise RuntimeError(f"no approved scene set for project {project_id}")

    character, style = _pinned_branding(ctx, project_id)
    style_spec = compile_style_block(style.fields)
    character_spec = CharacterSpec(
        name=character.name,
        immutable=dict(character.immutable_traits or {}),
        variable=dict(character.variable_traits or {}),
    )

    provider = _provider()
    references = _references(ctx, character, provider)
    settings = load_worker_settings()
    aspect = settings.render.aspect_ratio
    target = (settings.render.width, settings.render.height)

    # **One scene, or all of them.** A request naming a ``scene_ref`` is the
    # contact sheet's per-tile Regenerate (M3-09): the one that missed should
    # not cost a re-run of the nineteen that landed.
    #
    # This also decides who closes the job's own artifact. For a whole-set run
    # the trigger is the project-wide `image` row and needs a manifest; for a
    # single scene the trigger *is* that scene's artifact, and the loop below
    # already completes it. Completing it twice raises "cannot apply
    # 'generation_succeeded' to an artifact in state 'AWAITING_APPROVAL'" —
    # which is exactly what the first per-tile regenerate did.
    scene_ref = ctx.input.get("scene_ref")
    if scene_ref:
        selected = [scene for scene in scenes if scene.id == str(scene_ref)]
        if not selected:
            raise RuntimeError(
                f"scene {scene_ref} is not in the approved scene set for "
                f"project {project_id}"
            )
    else:
        # A testing affordance, carried in the job's ``input_snapshot`` rather
        # than in configuration, so the record of *this run* says it was
        # limited. A cap in the environment would make an eight-image project
        # and a truncated twenty-image one indistinguishable afterwards.
        limit = int(ctx.input.get("max_scenes") or 0)
        selected = scenes[:limit] if limit > 0 else scenes

    # **Cards never reach a provider** (M4-01, §1.0.3). Split here rather than
    # in ``for_approved_set`` so a ``scene_ref`` naming a card renders that one
    # card — the query-level filter would report "not in the approved scene
    # set", which is true, useless, and sends the reader looking for a missing
    # row.
    cards = [scene for scene in selected if scene.kind is SceneKind.CARD]
    selected = [scene for scene in selected if scene.kind is not SceneKind.CARD]

    manifest: list[dict[str, Any]] = []

    # Cards first, and deliberately before the budget-checked loop below: they
    # cost nothing, so a run that stops at the daily cap should still have
    # produced every frame that was free. Ordering the manifest by scene index
    # afterwards keeps the contact sheet in scene order regardless.
    for scene in cards:
        manifest.append(_render_card(ctx, project_id, scene, style, target))
        ctx.uow.flush()
    for scene in selected:
        # Before **each** image, not once per run. Images are where the daily
        # cap is measured in real money, and a run that starts just under it
        # should stop at the boundary rather than sail past by four images.
        require_budget(ctx)

        brief = _scene_brief(ctx, project_id, scene.id, scene.visual_brief)
        artifact = _image_artifact(ctx, project_id, scene.id)
        # What the reviewer said was wrong last time (M3-10). Without this, a
        # regeneration runs against exactly the prompt that just failed and the
        # reviewer's judgement reaches nothing.
        correction = _correction(ctx, artifact.id)
        built = build_image_prompt(
            scene=brief,
            character=character_spec,
            style=style_spec,
            scene_negative=", ".join((_SCENE_NEGATIVE, *correction.avoid)),
            correction=correction.guidance,
        )
        result = provider.generate(
            ImageRequest(
                prompt=built.prompt,
                negative_prompt=built.negative_prompt,
                aspect_ratio=aspect,
                references=references,
            )
        )
        if not result.images:
            raise RuntimeError(f"provider returned no image for scene {scene.index}")
        image = result.images[0]

        # **Both objects, on one version** (B2). The provider's original is
        # stored untouched — content-addressed like every other binary — and
        # the version points at the normalised derivative, because that is the
        # frame the renderer composites and therefore the frame a reviewer must
        # be approving. Keeping the original costs a few hundred kilobytes and
        # means a future re-crop, or a series that changes to 16:9, never needs
        # a paid regeneration.
        original = storage().put_bytes(
            settings.minio.bucket_assets,
            image.data,
            f"{project_id}-scene{scene.index:03d}-src{_extension(image.mime_type)}",
        )
        frame = normalise(
            image.data,
            mime_type=image.mime_type,
            width=image.width,
            height=image.height,
            target=target,
        )
        stored = storage().put_bytes(
            settings.minio.bucket_assets,
            frame.data,
            f"{project_id}-scene{scene.index:03d}{_extension(frame.mime_type)}",
        )

        version = complete_stored_generation(
            ctx,
            artifact,
            storage_key=stored.key,
            content_hash=stored.sha256,
            result=result,
            prompt_ref=built.template_ref,
            # §10.3 rule 4: enough to explain any frame without re-deriving it,
            # including which branding versions it was drawn against and what
            # the provider actually returned — the real pixel dimensions matter
            # because they are not necessarily what was asked for (B2).
            meta=built.snapshot(
                character_version_id=character.id,
                style_version_id=style.id,
                scene_index=scene.index,
                aspect_ratio=aspect,
                width=frame.width,
                height=frame.height,
                mime_type=frame.mime_type,
                # What the provider actually gave, and what closing the gap
                # cost. ``discarded`` is the number to look at when a frame
                # comes back oddly cropped.
                source_storage_key=original.key,
                source_width=image.width,
                source_height=image.height,
                source_mime_type=image.mime_type,
                discarded=round(frame.plan.discarded, 4),
                reference_count=len(references),
                provider_meta=result.provider_meta,
            ),
        )
        manifest.append(
            {
                "scene_index": scene.index,
                "scene_id": scene.id,
                "artifact_id": artifact.id,
                "version_id": version.id,
                "storage_key": stored.key,
            }
        )
        ctx.uow.flush()

    # Scene order, not generation order. Cards were rendered first and would
    # otherwise sit at the top of a contact sheet that is meant to read as the
    # video does.
    manifest.sort(key=lambda entry: int(entry["scene_index"]))

    if not scene_ref:
        # Only the whole-set run closes the project-wide artifact. A per-scene
        # run's trigger was completed inside the loop above.
        _complete_trigger(ctx, trigger, manifest, len(scenes), len(cards))

    logger.info(
        "image fan-out complete",
        extra={
            "project_id": project_id,
            "generated": len(manifest),
            "scene_count": len(scenes),
            # Logged rather than inferred from the difference. "Twenty scenes,
            # seventeen images" has two explanations — three cards, or three
            # scenes that silently failed — and only one of them is fine.
            "cards_skipped": len(cards),
            "scene_ref": scene_ref,
            "references": len(references),
        },
    )


def _pinned_branding(
    ctx: JobContext, project_id: str
) -> tuple[SeriesCharacter, SeriesStyle]:
    """The character and style version this project is pinned to.

    Raises rather than falling back to the series' current approvals. A project
    that reaches this task without a pin means ``JobService.request`` did not
    run its admission path, and silently re-branding an episode mid-generation
    is a far worse outcome than a failed job.
    """
    project = ctx.uow.projects.get(project_id)
    if project is None:
        raise RuntimeError(f"project {project_id} vanished before generation")
    if not project.character_version_id or not project.style_version_id:
        raise RuntimeError(
            f"project {project_id} has no pinned branding; image generation "
            "must not choose it here"
        )

    character = ctx.uow.branding.character(project.character_version_id)
    style = ctx.uow.branding.style(project.style_version_id)
    if character is None or style is None:
        raise RuntimeError(
            f"project {project_id} is pinned to branding that no longer exists"
        )
    return character, style


def _references(
    ctx: JobContext, character: SeriesCharacter, provider: ImageProvider
) -> tuple[ImageReference, ...]:
    """The approved reference sheets, as bytes, within the provider's budget.

    Empty when the character was approved without choosing a group — which is a
    supported state, not an error: text alone already produces a recognisable
    character, and refusing to generate would make reference sheets mandatory
    for a benefit that is measurable rather than assumed.

    Ordered by :data:`_REFERENCE_PRIORITY` and truncated to
    ``max_reference_images`` so that a provider accepting three gets the three
    most informative views rather than whichever three were written first.
    """
    group_id = character.approved_reference_group_id
    if not group_id:
        logger.info(
            "generating without reference images",
            extra={"character_id": character.id, "reason": "no approved group"},
        )
        return ()

    rows = ctx.uow.branding.references(group_id)
    budget = provider.capabilities().max_reference_images
    if budget <= 0:
        return ()

    ordered = sorted(rows, key=_reference_rank)[:budget]
    client = storage()
    bucket = load_worker_settings().minio.bucket_assets
    return tuple(
        ImageReference(
            # Verified, not merely fetched: a reference sheet is the definition
            # of the character, and a silently corrupted one would drift every
            # scene in the episode rather than break loudly.
            data=client.get_bytes_verified(bucket, row.storage_key),
            mime_type=row.mime_type,
            role=f"{row.angle} view, {row.shot_type}".strip(", "),
        )
        for row in ordered
    )


def _reference_rank(row: CharacterReference) -> tuple[int, int]:
    """Priority order, with the row's own index breaking ties deterministically."""
    angle = (row.angle or "").strip().lower()
    position = (
        _REFERENCE_PRIORITY.index(angle)
        if angle in _REFERENCE_PRIORITY
        else len(_REFERENCE_PRIORITY)
    )
    return position, row.index


def _scene_brief(ctx: JobContext, project_id: str, scene_id: str, fallback: str) -> str:
    """The approved prompt for this scene, or the scene's own visual brief.

    The prompt artifact is what M2-12 generated *for* this purpose and is the
    right input. The fallback exists because a scene set can legitimately be
    approved while an individual scene's prompt is not, and drawing that scene
    from its brief is a better answer than failing the whole run — the brief is
    the same content, one refinement earlier.
    """
    artifact = ctx.uow.artifacts.find(project_id, ArtifactKind.PROMPT, scene_id)
    if artifact is not None:
        approved = ctx.uow.versions.approved_version(artifact.id)
        if approved is not None:
            version = ctx.uow.versions.get(approved.artifact_version_id)
            if version is not None and version.inline_content:
                text = str(version.inline_content.get("prompt_text") or "").strip()
                if text:
                    return text
    return fallback


def _correction(ctx: JobContext, artifact_id: str) -> Correction:
    """The last rejection of this artifact, as guidance for the next attempt.

    Empty when nothing was ever rejected — the ordinary first-generation case,
    which must cost no extra prompt text at all.

    Read from the **artifact**, not from a version: the rejection that prompted
    this run belongs to the previous version, so a per-version lookup would
    find nothing at exactly the moment the correction is needed.

    Not cleared after use. A version regenerated twice against the same
    complaint should be told twice; the reviewer approving is what ends it,
    because the next rejection is then a newer row than this one.
    """
    rejection = ctx.uow.reviews.last_rejection(artifact_id)
    if rejection is None:
        return Correction(guidance="")
    return build_correction(rejection.reasons, rejection.comment)


def _image_artifact(ctx: JobContext, project_id: str, scene_id: str) -> Artifact:
    """The image artifact for one scene, created on first use.

    ``find`` before ``create`` for the reason ``prompts_stage`` gives: a
    regeneration targets scenes that already have artifacts, and creating a
    duplicate would hit S1's uniqueness constraint rather than produce a second
    one.
    """
    artifact = ctx.uow.artifacts.find(project_id, ArtifactKind.IMAGE, scene_id)
    if artifact is None:
        artifact = ctx.uow.artifacts.create(
            project_id,
            ArtifactKind.IMAGE,
            scene_id,
            state=ArtifactState.GENERATING,
        )
        ctx.uow.flush()
    else:
        artifact.state = ArtifactState.GENERATING
    return artifact


def _render_card(
    ctx: JobContext,
    project_id: str,
    scene: Scene,
    style: SeriesStyle,
    target: tuple[int, int],
) -> dict[str, Any]:
    """One card scene → an ordinary approved image artifact (M4-02).

    **Why it becomes an image artifact at all.** The alternative was to leave
    cards out of the media stage and let M4's timeline compiler render them at
    compile time. That would have made the compiler do I/O — the one property
    M4-04 is built around is that it is pure and golden-file testable — and it
    would have given the renderer a second kind of input to understand. Here,
    a card is a PNG in the same bucket as every other frame, and everything
    downstream stays uniform (§1.0 D4: "the renderer never learns what a card
    is").

    **Why it is approved without a human.** §1.0.3: a card is the deterministic
    rendering of ``card_text``, which a reviewer already approved at the
    scene-set gate. There is no second judgement to make — the reviewer would
    be re-reading their own words — and leaving twenty cards in the approval
    queue is exactly the R9 bottleneck the contact sheet exists to remove.
    The frame is still visible in the contact sheet; it just arrives approved.

    **No budget check.** ``require_budget`` guards spend, and this spends
    nothing. Calling it anyway would let a run that hit the daily cap fail on
    a frame that costs zero.
    """
    if not scene.card_text:
        # Unreachable through the CHECK constraint; asserted because the cost
        # of being wrong is a blank frame in a finished video.
        raise RuntimeError(f"card scene {scene.index} has no text")

    palette = palette_from_style(style.fields)
    width, height = target
    png = render_card(scene.card_text, width=width, height=height, palette=palette)

    artifact = _image_artifact(ctx, project_id, scene.id)
    settings = load_worker_settings()
    stored = storage().put_bytes(
        settings.minio.bucket_assets,
        png,
        f"{project_id}-scene{scene.index:03d}-card.png",
    )

    # A real usage row with zero cost, rather than none. The audit trail says
    # "this frame was produced by ``local``" instead of leaving a gap that
    # reads like a missing record, and the S10 cap sums a genuine zero.
    result = ImageResult(
        usage=Usage(images=0, unit_cost_estimate=0.0),
        provider_meta={"provider": "local", "model": _CARD_RENDERER_REF},
    )
    version = complete_stored_generation(
        ctx,
        artifact,
        storage_key=stored.key,
        content_hash=stored.sha256,
        result=result,
        prompt_ref=_CARD_RENDERER_REF,
        operation="card.render",
        meta={
            "scene_index": scene.index,
            "kind": SceneKind.CARD.value,
            "card_text": scene.card_text,
            "width": width,
            "height": height,
            "mime_type": "image/png",
            # The two colours actually used, not the style they came from. A
            # style edited later must not change what this frame is recorded
            # as having been (§10.3 rule 4).
            "paper": palette.paper,
            "ink": palette.ink,
            "style_version_id": style.id,
        },
    )

    # ``complete_stored_generation`` may already have approved this if the
    # series opted `image` out of review. Approving twice would raise, so the
    # state is asked rather than assumed.
    if ArtifactState(artifact.state) is ArtifactState.AWAITING_APPROVAL:
        approve_without_human(
            ctx,
            artifact,
            version.id,
            comment="card: deterministic rendering of approved scene text",
        )

    return {
        "scene_index": scene.index,
        "scene_id": scene.id,
        "artifact_id": artifact.id,
        "version_id": version.id,
        "storage_key": stored.key,
        "card": True,
    }


def _complete_trigger(
    ctx: JobContext,
    trigger: Artifact,
    manifest: list[dict[str, Any]],
    scene_count: int,
    card_count: int,
) -> None:
    """Close the project-wide ``image`` artifact with a manifest.

    Without this the artifact ``JobService.request`` created stays GENERATING
    forever: nothing else completes it, and phase derivation takes the *least
    advanced* artifact of a kind, so the project would sit in MEDIA_GENERATION
    with no error anywhere.

    A zero-usage result, deliberately — every real call was already metered
    against its own scene above, and re-recording one here would double-count
    in the row the S10 cap reads.
    """
    complete_generation(
        ctx,
        trigger,
        content={
            "scene_count": scene_count,
            "generated": len(manifest),
            # So the manifest accounts for every scene. A reviewer looking at
            # seventeen tiles for a twenty-scene project needs the third number
            # to know nothing went missing (M4-01).
            "cards": card_count,
            "images": manifest,
        },
        result=LLMResult(text="", provider_meta={"provider": "batch", "model": "none"}),
        prompt_ref="image@1",
    )


def storage() -> StorageClient:
    """The object store, as a seam — see ``references.storage``."""
    return storage_client_from_settings(load_worker_settings().minio)


def _provider() -> ImageProvider:
    """Built per job rather than cached, matching ``references._provider``."""
    settings = load_worker_settings()
    return build_image_provider(settings.providers, settings.provider_keys)


def _extension(mime_type: str) -> str:
    """Content type decides the extension, not the other way round."""
    return {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(
        mime_type, ".bin"
    )


@videoforge_task(
    name=IMAGES_GENERATE.name, queue=IMAGES_GENERATE.queue, job_bearing=True
)
def generate_images(ctx: JobContext) -> None:
    images_body(ctx)
