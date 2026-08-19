"""The Reels cover: an approved scene image with the hook typeset on it (M5-02).

Pure and deterministic — bytes in, bytes out, no storage and no database — for
the same reason ``cards.py`` is: the thing worth testing is what the picture
looks like, and a function that also fetched and saved could only be tested
against a stack.

**Why not a frame from the finished MP4.** The obvious source is the video
itself, and it is the wrong one: M4-05 burns the caption track into the picture
at 57% of frame height, so a lifted frame carries subtitle text straight across
the middle of the cover. Picking a moment with no cue would work until a video
had no gaps, which is most of them.

**Why not a separately generated image.** It costs a provider call per video,
it can drift from the style the video actually uses (R7 — the whole reason
character references exist), and it puts a picture nobody reviewed in front of
the content. The scene images have already been generated, normalised (B2) and
approved by a human.

**The 1:1 crop is the constraint that shapes everything here.** A Reel's cover
is displayed 9:16 in the feed and **centre-cropped to a square on the profile
grid**, so anything in the top or bottom sixth of the frame is invisible in the
place people browse. Text and scrim therefore live inside the middle square,
and the layout constants below are fractions *of that square*, not of the frame.
"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

from videoforge_workers.cards import (
    draw_text_block,
    fit_text,
    load_font,
    measure_width,
    text_block_height,
)

__all__ = ["render_cover", "safe_square"]

#: Where the hook's baseline block sits inside the safe square, as a fraction
#: of the square's height from its top. Low rather than centred: the artwork's
#: subject is usually mid-frame, and text over a face reads worse than text
#: under one.
_TEXT_CENTRE = 0.74

#: Text box inside the safe square. Generous side margins because this is read
#: at roughly 150px wide in a profile grid.
_TEXT_WIDTH = 0.84
_TEXT_HEIGHT = 0.34

#: The scrim behind the text. Without it a white hook over a pale illustration
#: is unreadable, and an outline heavy enough to fix that (M4-05's approach for
#: subtitles) looks like a subtitle rather than a cover.
_SCRIM_ALPHA = 150
_SCRIM_PAD_X = 0.05
_SCRIM_PAD_Y = 0.06
_SCRIM_RADIUS = 0.03

_INK = "#FFFFFF"


def safe_square(width: int, height: int) -> tuple[int, int]:
    """``(top, side)`` of the region Instagram's profile grid keeps.

    A Reel's cover shows 9:16 in the feed and **centre-cropped to a square** on
    the grid, so on a 1080x1920 cover the visible-everywhere region is y
    420..1500. Public because the tests assert against it and because a future
    "is the subject inside the crop?" check belongs on this number, not on a
    second copy of it.
    """
    side = min(width, height)
    return round((height - side) / 2), side


def render_cover(
    background: bytes,
    hook: str,
    *,
    width: int = 1080,
    height: int = 1920,
    font_path: str | None = None,
) -> bytes:
    """Compose the cover. Deterministic: same inputs, same bytes.

    ``background`` is an approved scene image. It is scaled and centre-cropped
    to the target rather than trusted to be the right size — every image the
    pipeline stores has been through M3-08's normalisation and is already
    1080x1920, but a cover that silently letterboxed a stray aspect ratio would
    be the kind of defect only visible after publishing.

    An empty ``hook`` renders the picture alone. That is a real outcome, not a
    failure: a cover with no text is a weaker cover and still a valid one, and
    refusing here would mean a caption whose hook a reviewer had cleared out
    could never produce a thumbnail.
    """
    picture = _fit_frame(Image.open(io.BytesIO(background)), width, height)

    words = hook.split()
    if words:
        _typeset(picture, words, width=width, height=height, font_path=font_path)

    buffer = io.BytesIO()
    # No ``tIME`` chunk, so the digest is stable between runs — the property
    # ADR-004's content addressing depends on and M4-02 already relies on.
    picture.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _fit_frame(source: Image.Image, width: int, height: int) -> Image.Image:
    """Cover-and-centre-crop to exactly ``width`` x ``height``.

    The same geometry ``imaging.crop_plan`` computes for scene images, done
    with Pillow directly because this module already holds a decoded image and
    round-tripping through that module's bytes-in/bytes-out contract would
    encode a PNG only to decode it again.
    """
    picture = source.convert("RGB")
    if picture.size == (width, height):
        return picture

    scale = max(width / picture.width, height / picture.height)
    resized = picture.resize(
        (max(1, round(picture.width * scale)), max(1, round(picture.height * scale))),
        Image.Resampling.LANCZOS,
    )
    left = (resized.width - width) // 2
    top = (resized.height - height) // 2
    return resized.crop((left, top, left + width, top + height))


#: Below this the hook is unreadable at grid size anyway, so a word that still
#: does not fit is left to overflow rather than shrunk into invisibility.
_MIN_FONT_SIZE = 24


def _shrink_to_fit(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    font: ImageFont.FreeTypeFont,
    box_width: float,
    font_path: str | None,
) -> ImageFont.FreeTypeFont:
    """Keep reducing the size until the widest line fits, or the floor is hit.

    **The cover needs this and a card does not**, which is why it lives here
    rather than in ``fit_text``. Greedy wrapping cannot break inside a word, so
    a single token wider than the box overflows — deliberate for a card, whose
    text is short and human-approved, and asserted as such in ``test_cards``.
    A hook is model-written and forty characters are allowed, so "Unterhaltungs-
    elektronik" is a cover with letters running off both edges. The one image
    that represents the whole video should not be able to do that.
    """
    size = font.size
    while size > _MIN_FONT_SIZE:
        if all(measure_width(draw, line, font) <= box_width for line in lines):
            return font
        size = max(_MIN_FONT_SIZE, int(size * 0.9))
        font = load_font(size, font_path=font_path)
    return font


def _typeset(
    picture: Image.Image,
    words: list[str],
    *,
    width: int,
    height: int,
    font_path: str | None,
) -> None:
    """Draw the scrim and the hook, in that order, inside the safe square."""
    square_top, square = safe_square(width, height)

    measure = ImageDraw.Draw(picture)
    box_width = square * _TEXT_WIDTH
    font, lines = fit_text(
        measure,
        words,
        max_width=box_width,
        max_height=square * _TEXT_HEIGHT,
        font_path=font_path,
    )
    font = _shrink_to_fit(measure, lines, font, box_width, font_path)

    centre_y = square_top + square * _TEXT_CENTRE
    block_h = text_block_height(measure, lines, font)
    block_w = max(
        (measure.textbbox((0, 0), line, font=font)[2] for line in lines), default=0.0
    )

    # Drawn on its own RGBA layer and composited: Pillow cannot draw a
    # translucent shape straight onto an RGB image — it would paint the alpha
    # as opacity-1 grey.
    scrim = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    pad_x = square * _SCRIM_PAD_X
    pad_y = square * _SCRIM_PAD_Y
    ImageDraw.Draw(scrim).rounded_rectangle(
        (
            width / 2 - block_w / 2 - pad_x,
            centre_y - block_h / 2 - pad_y,
            width / 2 + block_w / 2 + pad_x,
            centre_y + block_h / 2 + pad_y,
        ),
        radius=int(square * _SCRIM_RADIUS),
        fill=(0, 0, 0, _SCRIM_ALPHA),
    )
    picture.paste(Image.alpha_composite(picture.convert("RGBA"), scrim).convert("RGB"))

    draw_text_block(
        ImageDraw.Draw(picture),
        lines,
        font=font,
        centre=(width / 2, centre_y),
        ink=_INK,
    )
