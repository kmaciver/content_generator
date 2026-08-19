"""M5-02 — the Reels cover.

Two properties matter and they are asserted against pixels rather than trusted.

**The hook survives the profile-grid crop.** Instagram shows a Reel's cover 9:16
in the feed and centre-cropped to a square on the grid, so text placed low in
the frame is invisible in the place people browse. This is the failure the
whole layout exists to avoid and it is not visible in a 9:16 preview — which is
precisely why it needs a test rather than an eyeball.

**Byte-identity for identical input**, like the card renderer, so a cover is an
ordinary content-addressed artifact and a re-run is free instead of a diff.

Pure — no storage, no database.
"""

from __future__ import annotations

import io

from PIL import Image

from videoforge_workers.cover import render_cover, safe_square

_WIDTH = 1080
_HEIGHT = 1920


def _background(colour: tuple[int, int, int] = (40, 90, 140)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (_WIDTH, _HEIGHT), colour).save(buffer, format="PNG")
    return buffer.getvalue()


def _open(png: bytes) -> Image.Image:
    return Image.open(io.BytesIO(png)).convert("RGB")


def _luminance(image: Image.Image) -> list[int]:
    """Every pixel's brightness.

    ``list(...)`` rather than iterating ``getdata()`` directly: the sequence it
    returns is an ``ImagingCore``, which supports indexing but is not typed as
    iterable.
    """
    return list(image.convert("L").getdata())


def _bright_pixels(image: Image.Image) -> int:
    """Roughly, how much white text is on screen. The backgrounds here are
    mid-tone by construction, so near-white is the hook and nothing else."""
    return sum(1 for value in _luminance(image) if value > 200)


class TestGeometry:
    def test_the_cover_is_the_requested_size(self) -> None:
        assert _open(render_cover(_background(), "Why budgets fail")).size == (
            _WIDTH,
            _HEIGHT,
        )

    def test_the_safe_square_is_the_middle_of_the_frame(self) -> None:
        """1080x1920 → y 420..1500. The number the layout is built on."""
        assert safe_square(_WIDTH, _HEIGHT) == (420, 1080)

    def test_an_odd_background_is_cropped_not_letterboxed(self) -> None:
        """Every stored image has been through M3-08's normalisation and is
        already 9:16, so this only fires on a stray input — where a silent
        letterbox would be a defect visible only after publishing."""
        wide = io.BytesIO()
        Image.new("RGB", (1920, 1080), (200, 30, 30)).save(wide, format="PNG")

        cover = _open(render_cover(wide.getvalue(), ""))
        assert cover.size == (_WIDTH, _HEIGHT)
        # No black bars: every corner is still the source colour.
        for point in ((2, 2), (_WIDTH - 3, 2), (2, _HEIGHT - 3)):
            assert cover.getpixel(point) == (200, 30, 30)


class TestHook:
    def test_the_hook_lands_inside_the_profile_crop(self) -> None:
        """**The failure this ticket exists to avoid.** A hook typeset low in
        the 9:16 frame looks fine in the feed and is cropped away entirely on
        the grid, which is where people browse."""
        cover = _open(render_cover(_background(), "Why budgets fail"))
        top, side = safe_square(_WIDTH, _HEIGHT)

        square = cover.crop((0, top, side, top + side))
        assert _bright_pixels(square) > 0, "the hook is outside the profile crop"

        # And all of it, not merely some: nothing bright outside the square.
        above = cover.crop((0, 0, _WIDTH, top))
        below = cover.crop((0, top + side, _WIDTH, _HEIGHT))
        assert _bright_pixels(above) == 0
        assert _bright_pixels(below) == 0

    def test_an_empty_hook_renders_the_picture_alone(self) -> None:
        """A real outcome, not a failure. Refusing would mean a caption whose
        hook a reviewer cleared could never produce a thumbnail."""
        plain = _open(render_cover(_background(), ""))
        assert _bright_pixels(plain) == 0

    def test_the_hook_is_readable_over_a_pale_picture(self) -> None:
        """White text on cream is the case the scrim exists for. Without it the
        hook is present in the file and invisible on a phone."""
        pale = _background((242, 238, 230))
        cover = _open(render_cover(pale, "Why budgets fail"))
        top, side = safe_square(_WIDTH, _HEIGHT)
        square = cover.crop((0, top, side, top + side))

        dark = sum(1 for value in _luminance(square) if value < 120)
        assert dark > 0, "no scrim was drawn behind the hook"

    def test_a_long_hook_still_fits_the_text_box(self) -> None:
        """Wrapped and shrunk by the shared fitter, not overflowed. 40
        characters is the caption stage's cap, so this is its worst case."""
        cover = _open(render_cover(_background(), "x" * 40))
        top, side = safe_square(_WIDTH, _HEIGHT)
        square = cover.crop((0, top, side, top + side))
        box = square.point(lambda v: 255 if v > 200 else 0).convert("L").getbbox()
        assert box is not None
        assert box[0] > 0 and box[2] < side, "the hook ran off the safe square"


class TestDeterminism:
    def test_the_same_inputs_give_the_same_bytes(self) -> None:
        """What makes a cover an ordinary content-addressed artifact (ADR-004),
        and a regeneration free rather than a new version."""
        first = render_cover(_background(), "Why budgets fail")
        second = render_cover(_background(), "Why budgets fail")
        assert first == second

    def test_a_different_hook_gives_different_bytes(self) -> None:
        """The other half: identity that ignored its input would make every
        cover in the workspace the same object."""
        assert render_cover(_background(), "One") != render_cover(_background(), "Two")
