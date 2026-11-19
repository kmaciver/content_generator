"""``voice.generate`` (M3-12) — one narration for the whole script.

**One call, not twenty** (finding B3 revised). TTS reading a single sentence in
isolation produces a complete intonation contour, terminal fall included, and
twenty of those concatenated read as a list of statements rather than a
narration. In a format where voice-over carries the entire piece, prosody
continuity is the single biggest lever on perceived quality — so this stage is
deliberately **not** per-scene, unlike ``image.generate``, and the pipeline DAG
says so too (``voice`` requires ``scene_set``, not ``prompt``).

The cost of that choice is that the pipeline gets back one audio file and has
to work out where each scene sits inside it. That derivation is pure and lives
in ``videoforge_domain.timing``; this module supplies it the provider's
alignment and stores what comes out.

**What this artifact carries.** The audio itself is a ``storage_key`` — a
narration is megabytes and has no business in a jsonb column. Everything the
renderer and the review player need is in ``meta``: the per-scene spans, and
the per-word timings inside each. M4's timeline compiler turns those into a
concat list and an ASS caption file; nothing further needs the provider.

**Emotion tags (ADR-019).** The text sent is the narration with the approved
voice direction's tags inserted — ``[whispers] Nobody knew…`` — and the tags
are removed from the returned alignment *before* words are grouped, so captions
and scene spans see exactly the clean narration they always did.

**What it gives up, explicitly.** Surgical per-scene retakes. §17's
"per-segment retake" becomes a comment anchored to a scene that regenerates the
*whole* voice artifact — acceptable, because that is one cheap call, and the
new version is a normal artifact version with full lineage.
"""

from __future__ import annotations

import logging

from videoforge_domain.timing import scene_spans, words_from_characters
from videoforge_domain.voice_direction import (
    InvalidDirectionError,
    SceneText,
    compose,
    directed_scenes,
    strip_tag_characters,
)
from videoforge_providers.models import VoiceRequest
from videoforge_providers.protocols import VoiceProvider
from videoforge_providers.registry import build_voice_provider
from videoforge_shared.enums import ArtifactKind, SceneKind
from videoforge_shared.settings import load_worker_settings
from videoforge_shared.storage import StorageClient, storage_client_from_settings
from videoforge_shared.tasks import VOICE_GENERATE
from videoforge_workers.skeleton import JobContext, videoforge_task
from videoforge_workers.stages import (
    complete_stored_generation,
    content_language,
    load_artifact,
    require_approved_content,
    require_budget,
)

logger = logging.getLogger(__name__)

__all__ = ["generate_voice", "storage", "voice_body"]


def voice_body(ctx: JobContext) -> None:
    """Synthesise the whole script and derive per-scene spans."""
    artifact = load_artifact(ctx)
    project_id = artifact.project_id

    scenes = ctx.uow.scenes.for_approved_set(project_id)
    if not scenes:
        raise RuntimeError(f"no approved scene set for project {project_id}")

    # The script as spoken is the concatenation of the scene narrations, in
    # order — which is exactly what makes `scene_spans` a counting problem
    # rather than a matching one. Joined here, once, so the text sent and the
    # text the boundaries are derived against cannot diverge.
    narrations = [scene.narration_text.strip() for scene in scenes]

    # ADR-019: the approved direction, re-checked against these exact scenes.
    # A direction approved before the scene set moved on would put tags on the
    # wrong sentences, so it fails here rather than being half-applied.
    direction = require_approved_content(ctx, project_id, ArtifactKind.VOICE_DIRECTION)
    try:
        directed = directed_scenes(
            direction,
            [
                SceneText(scene.id, scene.index, narration)
                for scene, narration in zip(scenes, narrations, strict=True)
            ],
        )
    except InvalidDirectionError as exc:
        raise RuntimeError(
            f"the approved voice direction does not fit the approved scenes: {exc}"
        ) from exc
    composed = compose(directed)

    provider = _provider()
    limit = provider.capabilities().max_characters
    if limit and len(composed.text) > limit:
        # Refused before spending. B3 requires one synthesis for the whole
        # script, so splitting is not an option — the script is too long for
        # this model, and the fix is upstream.
        raise RuntimeError(
            f"the narration is {len(composed.text)} characters with tags; the "
            f"voice model accepts {limit} in one call"
        )

    require_budget(ctx)
    settings = load_worker_settings()
    result = provider.synthesise(
        VoiceRequest(
            text=composed.text,
            voice_id=settings.providers.voice.voice_id,
            # The *voice* still has to be one that speaks this language —
            # `language_code` tells the model which language the text is, not
            # which accent to fake. A Brazilian narration needs a Brazilian
            # voice id in PROVIDERS__VOICE__VOICE_ID; this only stops the model
            # guessing wrong about the text it was handed.
            language_code=content_language().voice_code,
        )
    )

    # Written alignment, never normalised. §1.0.2 found the reference captions
    # a bare numeral as its own frame, and a probe on 2026-08-09 put `762` at
    # 0.743-1.533s as one written token; the normalised stream would make it
    # four words nobody wrote. The adapter only ever returns the written one.
    characters, starts, ends, strip_method = strip_tag_characters(
        composed,
        result.characters,
        result.character_starts_s,
        result.character_ends_s,
    )
    if strip_method == "bracket_scan":
        logger.warning(
            "voice alignment was not one-to-one with the request; tags were "
            "removed by scanning for brackets (ADR-019)",
            extra={"project_id": project_id},
        )
    words = words_from_characters(characters, starts, ends)
    spans = scene_spans(words, narrations)

    # Loud rather than silent. A scene with no words is one the renderer would
    # hold for zero milliseconds — a frame that flashes past — and the cause is
    # a truncated synthesis, which is worth failing on while the audio is still
    # cheap to redo.
    if empty := [span.scene_index for span in spans if not span.words]:
        raise RuntimeError(
            f"narration ran out before scenes {empty} for project {project_id}; "
            "the synthesis was truncated and the video would have silent scenes"
        )

    stored = storage().put_bytes(
        settings.minio.bucket_assets,
        result.audio,
        f"{project_id}-narration{_extension(result.mime_type)}",
    )

    complete_stored_generation(
        ctx,
        artifact,
        storage_key=stored.key,
        content_hash=stored.sha256,
        result=result,
        prompt_ref="voice@1",
        operation="voice.synthesise",
        meta={
            "mime_type": result.mime_type,
            "duration_ms": result.duration_ms,
            "word_count": len(words),
            "scene_count": len(scenes),
            # ADR-019. How many tags were performed, and how they were taken
            # back out of the timings — evidence if captions ever drift.
            "tag_count": composed.tag_count,
            "tag_strip_method": strip_method,
            # Everything M4 needs, and everything the review player needs.
            # Stored rather than re-derived: the derivation is deterministic,
            # but re-running it later against a different build's grouping
            # rules would silently re-time an approved narration.
            "spans": [
                {
                    "scene_index": span.scene_index,
                    "scene_id": scene.id,
                    # Carried so a *reader* of these spans can group captions
                    # the way the compiler will: it skips cards, because a card
                    # is already text on screen. Without this the review
                    # player would preview a caption the render never burns.
                    "kind": SceneKind(scene.kind).value,
                    "start_ms": span.start_ms,
                    "end_ms": span.end_ms,
                    "words": [
                        {"text": w.text, "start_ms": w.start_ms, "end_ms": w.end_ms}
                        for w in span.words
                    ],
                }
                for span, scene in zip(spans, scenes, strict=True)
            ],
            "provider_meta": result.provider_meta,
        },
    )

    logger.info(
        "narration generated",
        extra={
            "project_id": project_id,
            "duration_ms": result.duration_ms,
            "words": len(words),
            "scenes": len(scenes),
        },
    )


def storage() -> StorageClient:
    """The object store, as a seam — see ``references.storage``."""
    return storage_client_from_settings(load_worker_settings().minio)


def _provider() -> VoiceProvider:
    """Built per job. One narration per project ever; a cached client would be
    held open for a worker's lifetime to save nothing."""
    settings = load_worker_settings()
    return build_voice_provider(settings.providers, settings.provider_keys)


def _extension(mime_type: str) -> str:
    """Content type decides the extension. The measured ElevenLabs response is
    **MP3**, not WAV, so hardcoding either would mislabel the object."""
    return {"audio/mpeg": ".mp3", "audio/wav": ".wav", "audio/ogg": ".ogg"}.get(
        mime_type, ".bin"
    )


@videoforge_task(name=VOICE_GENERATE.name, queue=VOICE_GENERATE.queue, job_bearing=True)
def generate_voice(ctx: JobContext) -> None:
    voice_body(ctx)
