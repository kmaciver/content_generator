"""Normalising a generated frame to the render's exact pixels (M3-08, B2).

**The finding.** ``size: 1080x1920`` was never a valid request for any of the
named providers, so ``ImageRequest`` carries an aspect *ratio* and the adapter
maps it to whatever the provider actually offers. Measured against Gemini on
2026-08-07: asking for ``9:16`` returned **768×1376**, which is 24:43 — close
to 9:16 but not equal to it. So the gap between "what the provider drew" and
"what the renderer composites" is a real crop, not a scale, and something has
to close it.

**Owned by the worker, not the adapter.** Adapters stay dumb: they report the
size they produced and nothing more. A normalisation living in the adapter
would have to be written once per vendor and would silently differ between
them.

**Cover, then centre-crop — never pad.** Padding a frame to fit produces bars,
and bars in a full-bleed vertical video are worse than losing a few pixels of
floor. For the measured case the loss is tiny: 768×1376 scales to 1080×1935
and 15 rows come off, 0.8% of the image. A provider returning something far
from the requested ratio would lose much more, so :attr:`CropPlan.discarded`
records the fraction and the caller logs a warning past a threshold — visible
rather than silent.

**ffmpeg, not Pillow.** The finding said Pillow; ffmpeg is what this repo
already has. It is installed in the app image, already the project's imaging
and video tool, and already invoked the same way by ``render.py`` — argv list,
``shell=False``. Adding Pillow would mean a second imaging dependency, in every
worker image, to centre-crop a rectangle.

The geometry is a pure function so it can be tested without ffmpeg at all; only
:func:`normalise` shells out.
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
from dataclasses import dataclass
from math import ceil
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "CropPlan",
    "NormalisedImage",
    "crop_plan",
    "normalise",
    "png_dimensions",
]

#: Past this fraction of the source area, a crop stops being a tidy-up and
#: starts being a composition decision nobody made. Not an error — the frame is
#: still the best available — but it is logged, because the alternative is a
#: silently half-cropped episode.
LOUD_CROP = 0.10

#: PNG for the derivative, deliberately. It is re-encoded exactly once here and
#: then read by the renderer, and this art style — thin charcoal lines on flat
#: cream — is precisely where JPEG ringing shows. The provider's original is
#: kept alongside it, so nothing is lost either way.
_DERIVATIVE_MIME = "image/png"


@dataclass(frozen=True, slots=True)
class CropPlan:
    """How to get from a provider's pixels to the render's pixels."""

    source_width: int
    source_height: int
    #: Scale to *cover* the target — both dimensions at least the target's.
    scale_width: int
    scale_height: int
    #: Top-left of the centred crop window within the scaled image.
    offset_x: int
    offset_y: int
    target_width: int
    target_height: int

    @property
    def is_identity(self) -> bool:
        """True when the provider already gave exactly what the render wants.

        Worth asking: re-encoding a frame that needs no change would spend CPU
        and a generation of quality to produce the same picture.
        """
        return (
            self.source_width == self.target_width
            and self.source_height == self.target_height
        )

    @property
    def discarded(self) -> float:
        """Fraction of the source image thrown away, 0.0–1.0.

        Computed in the *scaled* space, where the crop actually happens, then
        expressed as a proportion of area — which is what a reviewer means by
        "how much did we lose".
        """
        scaled = self.scale_width * self.scale_height
        if scaled <= 0:
            return 0.0
        kept = self.target_width * self.target_height
        return max(0.0, 1.0 - kept / scaled)


@dataclass(frozen=True, slots=True)
class NormalisedImage:
    data: bytes
    mime_type: str
    width: int
    height: int
    plan: CropPlan


def crop_plan(
    source_width: int, source_height: int, target_width: int, target_height: int
) -> CropPlan:
    """Cover-and-centre-crop geometry. Pure — no image, no ffmpeg.

    Scales by whichever axis needs the *larger* factor, so the scaled image
    covers the target on both axes and the crop only ever removes pixels. The
    opposite choice (fit) would leave gaps that have to be padded.

    Rounding is up (ceiling), because a scaled dimension that lands a pixel
    short of the target makes the crop impossible and ffmpeg fail — a rounding
    error must cost one wasted row, never a failed job.
    """
    if source_width <= 0 or source_height <= 0:
        raise ValueError(f"source has no area: {source_width}×{source_height}")
    if target_width <= 0 or target_height <= 0:
        raise ValueError(f"target has no area: {target_width}×{target_height}")

    scale = max(target_width / source_width, target_height / source_height)
    scale_width = max(target_width, ceil(source_width * scale))
    scale_height = max(target_height, ceil(source_height * scale))

    return CropPlan(
        source_width=source_width,
        source_height=source_height,
        scale_width=scale_width,
        scale_height=scale_height,
        # Centred. The subject is centred by the style's `composition` field,
        # so the middle is where the character is; a top-anchored crop would
        # favour heads at the cost of feet, and this convention draws both.
        offset_x=(scale_width - target_width) // 2,
        offset_y=(scale_height - target_height) // 2,
        target_width=target_width,
        target_height=target_height,
    )


def normalise(
    data: bytes, *, mime_type: str, width: int, height: int, target: tuple[int, int]
) -> NormalisedImage:
    """Scale-and-crop ``data`` to exactly ``target``.

    Returns the input untouched when it already matches, so the common future
    case — a provider that natively offers 1080×1920 — costs nothing.

    Raises on an ffmpeg failure or a mis-sized result rather than returning a
    frame of the wrong shape: the renderer composites these 1:1, and a frame
    that is quietly 1079 wide becomes a broken video discovered much later.
    """
    plan = crop_plan(width, height, *target)
    if plan.is_identity:
        return NormalisedImage(
            data=data, mime_type=mime_type, width=width, height=height, plan=plan
        )

    if plan.discarded > LOUD_CROP:
        logger.warning(
            "large crop normalising frame",
            extra={
                "source": f"{width}x{height}",
                "target": f"{plan.target_width}x{plan.target_height}",
                "discarded": round(plan.discarded, 3),
            },
        )

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source = root / f"in{_extension(mime_type)}"
        destination = root / "out.png"
        source.write_bytes(data)

        # argv list, shell=False — a filter graph is full of shell
        # metacharacters, and `render.py` states the same boundary.
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                str(source),
                "-vf",
                (
                    f"scale={plan.scale_width}:{plan.scale_height}:flags=lanczos,"
                    f"crop={plan.target_width}:{plan.target_height}:"
                    f"{plan.offset_x}:{plan.offset_y}"
                ),
                "-frames:v",
                "1",
                str(destination),
            ],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0 or not destination.exists():
            raise RuntimeError(
                f"ffmpeg failed normalising {width}x{height} → "
                f"{plan.target_width}x{plan.target_height}: "
                f"{result.stderr.decode(errors='replace')[-500:]}"
            )
        out = destination.read_bytes()

    # Self-checked before it can become an artifact version, for the reason
    # `render.py` gives about its own output: a bad frame should fail the job,
    # not be found at review.
    actual = png_dimensions(out)
    if actual != (plan.target_width, plan.target_height):
        raise RuntimeError(
            f"normalised frame is {actual}, expected "
            f"{(plan.target_width, plan.target_height)}"
        )

    return NormalisedImage(
        data=out,
        mime_type=_DERIVATIVE_MIME,
        width=plan.target_width,
        height=plan.target_height,
        plan=plan,
    )


def png_dimensions(data: bytes) -> tuple[int, int]:
    """Width and height from a PNG's IHDR. Pure, and cheaper than ffprobe.

    IHDR is mandated to be the first chunk, so the numbers are always at a
    fixed offset — no scanning, unlike JPEG's segment walk.
    """
    if len(data) < 24 or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    return (
        int.from_bytes(data[16:20], "big"),
        int.from_bytes(data[20:24], "big"),
    )


def _extension(mime_type: str) -> str:
    """ffmpeg infers the demuxer from content, but a sensible suffix keeps the
    temp file readable in a stack trace."""
    return {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}.get(
        mime_type, ".bin"
    )
