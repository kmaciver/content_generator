"""Generate one image from the configured provider and write it to disk.

The narrowest possible end-to-end proof of the image chain: settings → key →
adapter → capability gate → prompt builder → real pixels. It exists because
every layer below it is covered by tests with an injected client, and the one
thing those *cannot* cover is whether the SDK call signature is right — which
this answers on the first run.

Deliberately not a test. It spends money, needs a key, and depends on a vendor
being reachable; a suite that did any of those would be a suite people learn to
skip.

Usage, from inside a worker container (the only place the key exists, NF8)::

    docker compose ... run --rm worker-llm python /app/scripts/image-smoke.py

Optional arguments::

    --series <id>   pull the approved character and style from this series
    --scene "..."   one scene to draw
    --scenes        several scenes, one image each — the consistency check.
                    Bare ``--scenes`` uses a built-in set that varies framing,
                    pose and context, which is what actually stresses character
                    consistency (R7). Files are numbered.
    --out <path>    where to write (default /w/image-smoke.jpg)

**Write under ``/w``, not ``/tmp``.** ``docker run --rm`` deletes the container
on exit, taking its filesystem with it, and there is then no container left to
``docker cp`` from. ``/w`` is the bind mount, so a file written there is a file
on the host. The extension is corrected to match the format the provider
actually returned — Gemini answers with JPEG, the mock with PNG.
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from videoforge_persistence.engine import create_engine_from_settings
from videoforge_persistence.uow import unit_of_work
from videoforge_prompts.image_prompt import CharacterSpec, build_image_prompt
from videoforge_prompts.style import compile_style_block
from videoforge_providers.models import ImageRequest
from videoforge_providers.registry import build_image_provider
from videoforge_shared.logging import configure_logging
from videoforge_shared.settings import load_worker_settings

logger = logging.getLogger("image-smoke")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--series", default=None)
    parser.add_argument(
        "--scene", default="standing beside a tall wave, looking up at it"
    )
    parser.add_argument(
        "--scenes",
        nargs="*",
        default=None,
        help=(
            "several scenes, one image each — the consistency check. "
            "Pass with no values to use the built-in set."
        ),
    )
    # Under the bind mount, not /tmp: a file written to the container's own
    # filesystem vanishes with `--rm`, and there is no container left to copy
    # it out of.
    parser.add_argument("--out", default="/w/image-smoke.jpg")
    args = parser.parse_args()

    scenes = [args.scene] if args.scenes is None else (args.scenes or _CONSISTENCY_SET)

    configure_logging(level="INFO", fmt="pretty")
    settings = load_worker_settings()

    logger.info(
        "configuration",
        extra={
            "mode": settings.providers.mode.value,
            "adapter": settings.providers.image.adapter,
            "model": settings.providers.image.model or "(adapter default)",
            # Presence only. The value never goes near a log line.
            "google_key_present": settings.provider_keys.google_api_key is not None,
        },
    )

    # Builds the provider *and* runs the ADR-016 capability gate. A provider
    # that cannot take reference images fails here rather than after spending.
    provider = build_image_provider(settings.providers, settings.provider_keys)
    caps = provider.capabilities()
    logger.info(
        "capabilities",
        extra={
            "max_reference_images": caps.max_reference_images,
            "supports_seed": caps.supports_seed,
            "aspect_ratios": list(caps.aspect_ratios),
        },
    )

    character, style_fields = _branding(args.series)

    # Compiled **once** and reused across every scene — the path M3-07 takes for
    # twenty scenes. Recompiling per scene would also be correct (compilation is
    # deterministic), but reusing it is what proves the `style=` seam works.
    style = compile_style_block(style_fields)

    if len(scenes) > 1:
        print(
            f"\n{len(scenes)} scenes, one call each — about "
            f"{len(scenes) * 6}s and {len(scenes)} images' worth of spend.\n"
        )

    spent = 0.0
    written: list[tuple[int, str, str]] = []
    failed: list[tuple[int, str]] = []

    for index, scene in enumerate(scenes, start=1):
        built = build_image_prompt(scene=scene, character=character, style=style)
        if len(scenes) == 1:
            print("\n--- prompt ---")
            print(built.prompt)
            print(f"\n--- negative ---\n{built.negative_prompt or '(none)'}")
            print(f"\n--- digest --- {built.digest}  template {built.template_ref}\n")

        try:
            result = provider.generate(
                ImageRequest(
                    prompt=built.prompt,
                    negative_prompt=built.negative_prompt,
                    aspect_ratio="9:16",
                )
            )
        except Exception as exc:
            # One bad scene must not cost the whole run. A safety block on
            # scene 3 should still leave 1, 2 and 4 on disk to look at —
            # otherwise the expensive part is thrown away over the cheap part.
            logger.error(
                "scene failed",
                extra={"index": index, "scene": scene, "error": str(exc)},
            )
            failed.append((index, scene))
            continue

        image = result.images[0]
        destination = _destination(
            _numbered(Path(args.out), index, len(scenes)), image.mime_type
        )
        destination.write_bytes(image.data)
        spent += result.usage.unit_cost_estimate
        written.append((index, destination.name, built.digest))
        logger.info(
            "image written",
            extra={
                "index": index,
                "path": str(destination),
                "bytes": len(image.data),
                "width": image.width,
                "height": image.height,
                "mime": image.mime_type,
                "latency_ms": result.latency_ms,
                "cost_estimate": result.usage.unit_cost_estimate,
            },
        )

    if len(scenes) > 1:
        print("\n--- open these side by side ---")
        for index, name, digest in written:
            print(f"  {index}. {name}   digest {digest}   {scenes[index - 1]}")
        for index, scene in failed:
            print(f"  {index}. FAILED   {scene}")
        # An estimate built from an unverified price table — say so at the point
        # someone might otherwise read it as a bill.
        print(f"\nestimated spend: {spent:.2f} (unverified price table)\n")

    return 1 if failed and not written else 0


#: Scenes chosen to *stress* character consistency rather than to look nice.
#:
#: They vary the three things most likely to make a model redraw a character
#: from scratch: **framing** (close vs wide), **pose**, and **context**. Four
#: near-identical scenes would return four near-identical images and prove
#: nothing — the question R7 asks is whether Pip survives being drawn small,
#: large, from behind, and in an empty frame.
_CONSISTENCY_SET: list[str] = [
    "a close-up, filling most of the frame, facing the viewer",
    "very small in a wide empty landscape, seen from a distance",
    "mid-shot, walking to the left, seen from the side",
    "sitting down with knees drawn up, seen from slightly above",
]


def _numbered(path: Path, index: int, total: int) -> Path:
    """``pip.jpg`` → ``pip-2.jpg``, but only when there is more than one.

    A single-scene run keeps the exact filename the caller asked for; adding
    ``-1`` to it would break the obvious ``--out pip.jpg`` expectation to serve
    a case that is not happening.
    """
    if total == 1:
        return path
    return path.with_name(f"{path.stem}-{index}{path.suffix}")


def _destination(path: Path, mime_type: str) -> Path:
    """Correct the extension to match what the provider actually returned.

    Gemini returns **JPEG**, so a default of ``.png`` writes a file whose name
    lies about its contents — which is exactly the confusion this script exists
    to remove. The mock returns PNG, so neither extension can be hardcoded;
    the mime type is the only honest source.
    """
    suffix = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(
        mime_type
    )
    if suffix is None or path.suffix.lower() == suffix:
        return path
    corrected = path.with_suffix(suffix)
    logger.info(
        "renaming output to match the returned format",
        extra={"requested": path.name, "written": corrected.name, "mime": mime_type},
    )
    return corrected


def _branding(series_id: str | None) -> tuple[CharacterSpec | None, dict[str, object]]:
    """The approved character and style, or a built-in fallback.

    Falls back rather than failing so the script still proves the *provider*
    works on a database with no branding — which is the more common state while
    M3-13b's editor does not exist. The fallback is deliberately the reductive
    convention R7 recommends, so what comes back is a fair test of the approach
    rather than of an elaborate prompt.
    """
    if series_id is None:
        return _FALLBACK_CHARACTER, dict(_FALLBACK_STYLE)

    settings = load_worker_settings()
    with unit_of_work(create_engine_from_settings(settings.postgres)) as uow:
        character = uow.branding.approved_character(series_id)
        style = uow.branding.approved_style(series_id)
        if character is None or style is None:
            logger.warning(
                "series has no approved branding; using the fallback",
                extra={"series_id": series_id},
            )
            return _FALLBACK_CHARACTER, dict(_FALLBACK_STYLE)
        return (
            CharacterSpec(
                name=character.name,
                immutable=dict(character.immutable_traits or {}),
                variable=dict(character.variable_traits or {}),
            ),
            dict(style.fields or {}),
        )


#: Kept deliberately identical to ``database/seed/demo.py``'s branding, so a
#: run with no ``--series`` and a run against the seeded series are comparing
#: the same character. Two versions of "Pip" that quietly differed would make
#: every consistency observation ambiguous.
#:
#: **Every element names its own colour.** Measured 2026-08-07: with colours
#: only in the style palette, four scenes returned the body as terracotta,
#: black, terracotta and cream, with the limbs shuffling independently. The
#: palette was obeyed exactly — only its colours appeared — but nothing said
#: which element got which, so the model reassigned them per scene. The palette
#: says what may be used; the traits say where.
_FALLBACK_CHARACTER = CharacterSpec(
    name="Pip",
    immutable={
        "head": (
            "a smooth cream #F4EDE4 circle, no hair, no ears, "
            "no ring or band around it"
        ),
        "eyes": "two small black #141414 dots, no whites, no eyebrows",
        "body": "one rounded terracotta #D96A4E shape, always terracotta",
        "limbs": "thin black #141414 sticks",
        "scale": "the head is one third of total height",
    },
    variable={"pose": "varies with the scene"},
    # ``hair`` and ``hood`` by name: the close-up grew a thick black ring around
    # the head that read as one. Close framing invites detail, and both the
    # traits and the negative prompt have to refuse it explicitly.
    never=("facial detail", "photorealism", "hair", "hood", "outline around the head"),
)

_FALLBACK_STYLE: dict[str, object] = {
    "medium": "flat vector illustration",
    "palette": ["#141414", "#F4EDE4", "#D96A4E"],
    "line": "no outlines",
    "shading": "flat fills, no gradients",
    # Names the colour. "A single flat colour" was obeyed literally and
    # correctly, and gave two cream backgrounds then two black ones — a strobe
    # across hard cuts rather than a style.
    "background": "flat cream #F4EDE4, the same colour in every scene, no scenery",
    "composition": (
        "the subject fills the middle of the frame; keep the lower fifth "
        "visually quiet"
    ),
    "detail": "radically reductive; shapes only",
    "avoid": ["photorealism", "3d render", "text", "gradients"],
}


if __name__ == "__main__":
    sys.exit(main())
