"""Card scenes, rendered locally (M4-02, from §1.0.3).

A card is a scene whose beat *is* the words — "Step 5", "1962", "3× more
likely". The reference videos intercut them with generated artwork, and they
have no business going to an image provider: there is nothing to draw, the
output would cost money, and it would come back slightly different every time,
which is exactly the drift R7 is about.

**Byte-identical for identical input**, which is what makes a card an ordinary
content-addressed artifact like every other frame. Two consequences worth
stating, because both are properties of *how* this is written rather than
happy accidents:

* the font size is found by stepping a fixed ladder and taking the first size
  that fits, never by solving for a fractional size — floating-point search
  would land on 47.9998 on one machine and 48.0001 on another, and the PNG
  bytes would differ;
* nothing here reads a clock, a locale, or a random seed. Pillow writes no
  ``tIME`` chunk unless asked, so the same text and the same palette produce
  the same digest.

**Colours come from the series style**, not from constants, so a card and an
illustration in the same episode still look like one series. The palette is a
list of hex colours in ``series_style.fields``; the lightest becomes paper and
the darkest becomes ink. Deriving it that way rather than adding two new style
fields means an existing approved style already renders cards correctly, with
no editing and no re-approval.

**The font is the honest gap.** The reference uses a marker/handwritten face
and the container ships DejaVu Sans Bold — that is what ``fonts-dejavu-core``
guarantees and what the M0-02 build check asserts. The layout is right and the
lettering is not; dropping a marker TTF into the image and pointing
``VIDEOFORGE_CARD_FONT`` at it is the whole fix, which is why this reads a
path rather than hardcoding one.
"""

from __future__ import annotations

import io
import os
import re
from dataclasses import dataclass
from typing import Any

from PIL import Image, ImageDraw, ImageFont

__all__ = [
    "CardPalette",
    "DEFAULT_FONT_PATHS",
    "draw_text_block",
    "fit_text",
    "load_font",
    "measure_width",
    "palette_from_style",
    "render_card",
    "text_block_height",
]

#: Searched in order; the first that exists wins. ``VIDEOFORGE_CARD_FONT``
#: overrides the list entirely, which is the seam for a real marker face.
DEFAULT_FONT_PATHS: tuple[str, ...] = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)

#: Cream paper and near-black ink — the reference's own values, and also the
#: demo style's palette, so an unconfigured series still renders a card that
#: looks deliberate rather than like a bug.
_FALLBACK_PAPER = "#F4EDE4"
_FALLBACK_INK = "#141414"

#: A hex triplet anywhere in a palette entry — see :func:`_parse_hex`.
_HEX = re.compile(r"#([0-9a-fA-F]{6})\b")

#: Point sizes tried largest-first. A ladder rather than a solve: see the
#: module docstring — a fractional size search is not reproducible across
#: machines, and reproducibility is the whole claim this module makes.
_SIZE_LADDER: tuple[int, ...] = (
    260,
    240,
    220,
    200,
    180,
    160,
    140,
    120,
    104,
    88,
    72,
    60,
    48,
)

#: The box around the text, as fractions of the frame.
_BOX_WIDTH = 0.84
_BOX_HEIGHT = 0.34
_BOX_RADIUS = 0.05
_BOX_STROKE = 0.006

#: Fractions of the frame the text may occupy — **strictly inside the box**.
#: The first version had the text bound (0.46 of height) larger than the box
#: (0.30), so "Step 5" was laid out correctly and then drawn straight through
#: the outline that was supposed to contain it. Padding is the difference
#: between these and the box constants above, so changing one cannot silently
#: un-pad the other.
_TEXT_WIDTH = _BOX_WIDTH - 0.12
_TEXT_HEIGHT = _BOX_HEIGHT - 0.10


@dataclass(frozen=True, slots=True)
class CardPalette:
    """The two colours a card needs, resolved from a series style."""

    paper: str
    ink: str


def palette_from_style(fields: Any) -> CardPalette:
    """Pick paper and ink out of ``series_style.fields``.

    Lightest and darkest of the style's palette, by luminance. Never raises:
    a style whose palette is prose, empty, or missing degrades to the
    fallbacks, because the alternative is a series whose cards cannot render
    until someone edits a field they were never asked to fill in.
    """
    swatches = []
    if isinstance(fields, dict):
        raw = fields.get("palette")
        candidates = raw if isinstance(raw, list) else [raw]
        for value in candidates:
            parsed = _parse_hex(value)
            if parsed is not None:
                swatches.append(parsed)

    if len(swatches) < 2:
        return CardPalette(paper=_FALLBACK_PAPER, ink=_FALLBACK_INK)

    swatches.sort(key=_luminance)
    return CardPalette(paper=_to_hex(swatches[-1]), ink=_to_hex(swatches[0]))


def render_card(
    text: str,
    *,
    width: int,
    height: int,
    palette: CardPalette,
    font_path: str | None = None,
) -> bytes:
    """Render one card to PNG bytes, deterministically.

    ``text`` is drawn as-is apart from wrapping — it is the reviewer-approved
    words from ``scene.card_text``, and a renderer that re-cased or re-punctuated
    them would be editing approved content.
    """
    words = text.split()
    if not words:
        # Guarded rather than trusted. The CHECK constraint on ``scene`` makes
        # this unreachable through the stage, but this function is also the
        # thing a future title card or stat callout will call, and an empty
        # card is a blank frame in a finished video.
        raise ValueError("a card needs text")

    image = Image.new("RGB", (width, height), palette.paper)
    draw = ImageDraw.Draw(image)

    _draw_box(draw, width=width, height=height, ink=palette.ink)

    font, lines = fit_text(
        draw,
        words,
        max_width=width * _TEXT_WIDTH,
        max_height=height * _TEXT_HEIGHT,
        font_path=font_path,
    )
    draw_text_block(
        draw, lines, font=font, centre=(width / 2, height / 2), ink=palette.ink
    )

    buffer = io.BytesIO()
    # ``optimize`` is deterministic in Pillow and shaves roughly a third off a
    # flat two-colour frame. No ``tIME`` chunk is written, which is what keeps
    # the digest stable between runs.
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _draw_box(draw: ImageDraw.ImageDraw, *, width: int, height: int, ink: str) -> None:
    """The rounded outline the reference frames its cards with."""
    box_w = width * _BOX_WIDTH
    box_h = height * _BOX_HEIGHT
    left = (width - box_w) / 2
    top = (height - box_h) / 2
    draw.rounded_rectangle(
        (left, top, left + box_w, top + box_h),
        radius=int(width * _BOX_RADIUS),
        outline=ink,
        width=max(1, int(width * _BOX_STROKE)),
    )


def fit_text(
    draw: ImageDraw.ImageDraw,
    words: list[str],
    *,
    max_width: float,
    max_height: float,
    font_path: str | None,
) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """Largest ladder size that fits **on the fewest lines the text allows**.

    Two rules, and the second one is the whole reason this is not a one-liner.
    Taking the largest size that merely *fits* favours enormous text that
    wraps: "Step 5" came out as "Step" over "5" at 260pt, because two stacked
    lines fitted the box while one line at 200pt would have read far better.
    So the minimum achievable line count is measured first — at the smallest
    size on the ladder — and only sizes that achieve it are considered.

    Falls back to the smallest size rather than raising. 60 characters at 48pt
    across three lines is legible, and a card that renders a little cramped
    beats a job that fails on text a human already approved.

    **Takes a box rather than a frame** (M5-02). It used to derive the text
    area from the card's own proportions, which was right while cards were the
    only caller; the Reels cover typesets into the safe square of a photograph
    instead. The measuring rules are identical and there should be exactly one
    of them.
    """
    path = _font_path(font_path)
    max_w = max_width
    max_h = max_height

    smallest = _load(path, _SIZE_LADDER[-1])
    floor = _wrap(draw, words, smallest, max_w)
    target_lines = len(floor)

    for size in _SIZE_LADDER:
        candidate = _load(path, size)
        wrapped = _wrap(draw, words, candidate, max_w)
        if len(wrapped) != target_lines:
            continue
        if _block_height(draw, wrapped, candidate) <= max_h and all(
            _text_width(draw, line, candidate) <= max_w for line in wrapped
        ):
            return candidate, wrapped
    return smallest, floor


def _wrap(
    draw: ImageDraw.ImageDraw,
    words: list[str],
    font: ImageFont.FreeTypeFont,
    max_w: float,
) -> list[str]:
    """Greedy wrap. A single word wider than the box gets its own line and
    overflows — breaking inside a word would turn "responsible" into "respon-
    sible" on a frame read in under a second."""
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and _text_width(draw, candidate, font) > max_w:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def draw_text_block(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    *,
    font: ImageFont.FreeTypeFont,
    centre: tuple[float, float],
    ink: str,
) -> None:
    """Centred as a block on ``centre``, each line centred within it.

    Takes a centre point rather than a frame for the same reason
    :func:`fit_text` takes a box: the cover's text sits low in the safe square,
    not in the middle of the picture.
    """
    line_h = _line_height(draw, font)
    block_h = line_h * len(lines)
    centre_x, centre_y = centre
    y = centre_y - block_h / 2
    for line in lines:
        draw.text(
            (centre_x, y + line_h / 2),
            line,
            font=font,
            fill=ink,
            anchor="mm",
        )
        y += line_h


def text_block_height(
    draw: ImageDraw.ImageDraw, lines: list[str], font: ImageFont.FreeTypeFont
) -> float:
    """How tall :func:`draw_text_block` will render ``lines``.

    Public because the cover has to draw a scrim *behind* the text and cannot
    size one without knowing this first.
    """
    return _block_height(draw, lines, font)


def _text_width(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont
) -> float:
    box = draw.textbbox((0, 0), text, font=font)
    return float(box[2] - box[0])


def _line_height(draw: ImageDraw.ImageDraw, font: ImageFont.FreeTypeFont) -> float:
    """From a fixed probe string, not from the text being drawn.

    Measuring the real lines would make line spacing depend on whether a line
    happens to contain a descender, so "Step 5" and "Step 4" could lay out
    differently. The probe spans ascender to descender.
    """
    box = draw.textbbox((0, 0), "Hgjq", font=font)
    return (box[3] - box[1]) * 1.35


def _block_height(
    draw: ImageDraw.ImageDraw, lines: list[str], font: ImageFont.FreeTypeFont
) -> float:
    return _line_height(draw, font) * len(lines)


def _font_path(override: str | None) -> str:
    """The override, the environment, then the bundled fallbacks."""
    for candidate in (override, os.environ.get("VIDEOFORGE_CARD_FONT")):
        if candidate:
            return candidate
    for path in DEFAULT_FONT_PATHS:
        if os.path.exists(path):
            return path
    raise RuntimeError(
        "no card font found; install fonts-dejavu-core or set "
        "VIDEOFORGE_CARD_FONT to a TTF path"
    )


def load_font(size: int, *, font_path: str | None = None) -> ImageFont.FreeTypeFont:
    """The card face at ``size``, resolving the same font search as everything
    else here. Public so the cover can shrink below the ladder — see
    ``cover._typeset`` for why only it needs to."""
    return _load(_font_path(font_path), size)


def measure_width(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont
) -> float:
    """How wide ``text`` renders. Public for the same reason as
    :func:`load_font`: a second implementation of "does this fit" is a second
    thing to keep true."""
    return _text_width(draw, text, font)


def _load(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def _parse_hex(value: Any) -> tuple[int, int, int] | None:
    """Find a ``#RRGGBB`` **anywhere in the string**. No match → ``None``.

    A search rather than a whole-string match, because that is what real
    styles contain. The approved style on 2026-08-09 read
    ``["#2B2A28 charcoal", "#F2EBDF warm off-white", "#C4622D muted
    terracotta"]`` — hex *plus the human name*, which is exactly right for a
    field written to be read by an image model and by a person. The strict
    version of this function rejected all three and fell back to defaults, so
    the series' own colours never reached a card and nothing said so.

    Still silent on no-match: a palette entry may be pure prose ("warm cream
    paper"), which is a good instruction to a model and a useless one here.
    """
    if not isinstance(value, str):
        return None
    match = _HEX.search(value)
    if match is None:
        return None
    text = match.group(1)
    return (int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16))


def _luminance(rgb: tuple[int, int, int]) -> float:
    """Rec. 601 luma. Good enough to answer "which of these is the paper?",
    which is the only question asked of it."""
    r, g, b = rgb
    return 0.299 * r + 0.587 * g + 0.114 * b


def _to_hex(rgb: tuple[int, int, int]) -> str:
    return "#{:02X}{:02X}{:02X}".format(*rgb)
