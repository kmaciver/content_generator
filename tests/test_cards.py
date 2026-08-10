"""M4-02 — the local card renderer (§1.0.3).

The claim this module makes is **byte-identity for identical input**, and that
is what most of these assert. It is not a nicety: it is what lets a card be an
ordinary content-addressed artifact, what makes a re-run free instead of a
diff, and what removes card scenes from R7's drift surface entirely.

Everything here is pure — no database, no provider, no object store. A card
that needed any of those would not be a card.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image

from videoforge_workers.cards import (
    CardPalette,
    palette_from_style,
    render_card,
)

_WIDTH = 1080
_HEIGHT = 1920
_PALETTE = CardPalette(paper="#F4EDE4", ink="#141414")


def _render(text: str, palette: CardPalette = _PALETTE) -> bytes:
    return render_card(text, width=_WIDTH, height=_HEIGHT, palette=palette)


def _ink_count(image: Image.Image) -> int:
    """How many dark pixels a region holds."""
    return sum(1 for value in list(image.convert("L").getdata()) if value < 100)


def _ink_bbox(png: bytes) -> tuple[int, int, int, int]:
    """Bounding box of the *text*, in crop coordinates.

    Cropped well inside the drawn outline first, because the outline is ink
    too — measuring the whole frame would return the box's bounding box on
    every input and the assertions built on it would all pass vacuously.
    """
    image = Image.open(io.BytesIO(png)).convert("L")
    crop = image.crop(
        (
            int(_WIDTH * 0.11),
            int(_HEIGHT * (0.5 - 0.34 / 2) + 12),
            int(_WIDTH * 0.89),
            int(_HEIGHT * (0.5 + 0.34 / 2) - 12),
        )
    )
    box = crop.point(lambda v: 255 if v < 100 else 0).getbbox()
    assert box is not None, "no text found inside the card's box"
    return box


class TestDeterminism:
    def test_same_text_renders_the_same_bytes(self) -> None:
        """The whole ticket in one assertion.

        If this ever fails, cards stop being free: every re-run produces a new
        content hash, a new artifact version, and a frame a reviewer has to
        look at again.
        """
        assert _render("Step 5") == _render("Step 5")

    def test_different_text_renders_different_bytes(self) -> None:
        """The control. A renderer that returned a constant would pass the
        test above and nothing else."""
        assert _render("Step 5") != _render("Step 6")

    def test_palette_changes_the_output(self) -> None:
        other = CardPalette(paper="#FFFFFF", ink="#000000")
        assert _render("Step 5", other) != _render("Step 5")


class TestGeometry:
    def test_output_is_a_png_at_the_requested_size(self) -> None:
        image = Image.open(io.BytesIO(_render("Step 5")))
        assert image.format == "PNG"
        assert image.size == (1080, 1920)

    def test_the_paper_colour_reaches_the_corners(self) -> None:
        """A card fills its frame. A renderer that left a default-black border
        would composite as a black bar in the finished video."""
        image = Image.open(io.BytesIO(_render("Step 5"))).convert("RGB")
        assert image.getpixel((2, 2)) == (0xF4, 0xED, 0xE4)
        assert image.getpixel((1077, 1917)) == (0xF4, 0xED, 0xE4)

    def test_the_text_is_actually_drawn(self) -> None:
        """Ink appears somewhere in the middle band.

        Weak on purpose — this is not a golden-frame test (that is M4-10) — but
        it catches the failure that matters most here: a font that resolved to
        nothing, which produces a beautifully centred blank card.
        """
        image = Image.open(io.BytesIO(_render("Step 5"))).convert("RGB")
        band = image.crop((200, 850, 880, 1070))
        assert (0x14, 0x14, 0x14) in list(band.getdata())

    def test_short_text_stays_on_one_line(self) -> None:
        """**Regression.** Taking the largest size that merely *fits* rendered
        "Step 5" as "Step" over "5" at 260pt, because two stacked lines fitted
        the box while one line at 200pt would have read far better.

        Asserted through the ink's bounding box rather than by exposing the
        line count: a single line of "Step 5" is much wider than it is tall,
        and a stacked one is not.
        """
        box = _ink_bbox(_render("Step 5"))
        width, height = box[2] - box[0], box[3] - box[1]
        assert width > height * 2

    def test_text_stays_inside_the_drawn_box(self) -> None:
        """**Regression.** The text bound was 0.46 of the frame height while
        the box was 0.34, so the layout was computed correctly and then drawn
        straight through the outline meant to contain it.

        Asserted by looking *outside* the box rather than inside it: any ink
        above or below the outline is text that escaped.
        """
        image = Image.open(
            io.BytesIO(_render("Pay yourself first, every month"))
        ).convert("RGB")
        top = int(image.height * (0.5 - 0.34 / 2))
        bottom = int(image.height * (0.5 + 0.34 / 2))
        assert _ink_count(image.crop((0, 0, image.width, top - 8))) == 0
        assert _ink_count(image.crop((0, bottom + 8, image.width, image.height))) == 0

    def test_long_text_still_fits_the_frame(self) -> None:
        """60 characters is the column's bound, so it is the case that must
        not overflow. Renders, wraps, and stays the requested size."""
        image = Image.open(io.BytesIO(_render("x" * 60)))
        assert image.size == (1080, 1920)

    def test_a_single_very_long_word_does_not_raise(self) -> None:
        """Greedy wrapping cannot break inside a word, so this one overflows
        its box by design. It must still produce a frame — a job that failed
        here would fail on text a human already approved."""
        assert _render("supercalifragilisticexpialidocious")


class TestEmptyText:
    def test_empty_text_is_refused(self) -> None:
        """Unreachable through the CHECK constraint on ``scene``; guarded
        because this function is also what a future title card will call, and
        an empty card is a blank frame in a finished video."""
        with pytest.raises(ValueError):
            _render("   ")


class TestPaletteFromStyle:
    def test_lightest_is_paper_and_darkest_is_ink(self) -> None:
        palette = palette_from_style({"palette": ["#141414", "#F4EDE4"]})
        assert palette == CardPalette(paper="#F4EDE4", ink="#141414")

    def test_order_in_the_style_does_not_matter(self) -> None:
        """Sorted by luminance, not by position. A style editor that reorders
        swatches must not invert every card in the back catalogue."""
        assert palette_from_style({"palette": ["#F4EDE4", "#141414"]}) == (
            palette_from_style({"palette": ["#141414", "#F4EDE4"]})
        )

    def test_a_hex_followed_by_its_name_is_read(self) -> None:
        """**Regression.** The approved style on 2026-08-09 read
        ``["#2B2A28 charcoal", "#F2EBDF warm off-white", "#C4622D muted
        terracotta"]`` — hex plus the human name, which is right for a field
        read by both a model and a person. Requiring the whole string to be
        hex rejected all three, so the series' own colours never reached a
        card and the fallback quietly stood in for them.
        """
        palette = palette_from_style(
            {
                "palette": [
                    "#2B2A28 charcoal",
                    "#F2EBDF warm off-white",
                    "#C4622D muted terracotta",
                ]
            }
        )
        assert palette == CardPalette(paper="#F2EBDF", ink="#2B2A28")

    def test_prose_swatches_are_ignored(self) -> None:
        """A palette is allowed to contain prose — "warm cream paper" is a good
        instruction to an image model and a useless one to a renderer."""
        palette = palette_from_style(
            {"palette": ["warm cream paper", "#141414", "#F4EDE4"]}
        )
        assert palette == CardPalette(paper="#F4EDE4", ink="#141414")

    def test_more_than_two_swatches_takes_the_extremes(self) -> None:
        palette = palette_from_style(
            {"palette": ["#808080", "#141414", "#F4EDE4", "#C04000"]}
        )
        assert palette == CardPalette(paper="#F4EDE4", ink="#141414")

    @pytest.mark.parametrize(
        "fields",
        [None, {}, {"palette": []}, {"palette": "cream and charcoal"}, "not a dict"],
    )
    def test_unusable_styles_fall_back_rather_than_raise(self, fields: object) -> None:
        """A series whose cards cannot render until someone fills in a field
        they were never asked for is the wrong failure mode."""
        assert palette_from_style(fields) == CardPalette(paper="#F4EDE4", ink="#141414")
