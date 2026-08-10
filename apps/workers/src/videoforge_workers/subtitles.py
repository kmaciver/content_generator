"""Timeline cues → an ASS subtitle track (M4-05).

**Deliberately not in ``packages/timeline``.** ASS is libass's format, and the
timeline is the renderer-neutral contract (§2.5) — the schema carries caption
text *unescaped* precisely so that one engine's override syntax never leaks
into it. This module is where the timeline stops being neutral, which is why
it sits beside the renderer that consumes it.

It generalises M0-09's ``ass_document`` from one burned caption to a whole
track. That spike is worth more than it looks: the caption it produced was
extracted from a real MP4 and inspected, so the band position, the fill and
the outline model below are measured rather than guessed, and the container
already asserts at build time that libass can resolve the font.

**Grouping is not decided here.** ``videoforge_domain.captions`` owns it and
the timeline carries the result, so this writer, the compiler, and the review
player all render the same cues. That is the drift S8 was withdrawn over —
avoided by having one implementation rather than by generating two.

Three format details that are easy to get wrong and expensive to discover in
a render:

* **Colours are ``&HAABBGGRR``** — alpha, then blue-green-red. An RGB literal
  pasted here renders in the wrong colour and looks like a style choice.
* **Timestamps are centiseconds**, ``H:MM:SS.cc``. Cues are milliseconds, so
  the last digit is lost. Both ends are *floored* rather than rounded, because
  flooring is monotonic: two cues that did not overlap in the timeline cannot
  be made to overlap by the conversion.
* **Text is escaped**, and narration is model-written. See :func:`escape`.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

__all__ = [
    "AssStyle",
    "CaptionLine",
    "ass_document",
    "ass_timestamp",
    "escape",
]

#: Fraction of frame height the caption sits at, measured from the reference
#: videos (§1.0.2) and already burned and inspected once in M0-09.
CAPTION_BAND = 0.57


@dataclass(frozen=True, slots=True)
class CaptionLine:
    """One cue, as this writer needs it. Structurally the timeline's
    ``CaptionCue``; taken as a plain dataclass so the module stays importable
    without the timeline package and testable without building one."""

    text: str
    start_ms: int
    end_ms: int


@dataclass(frozen=True, slots=True)
class AssStyle:
    """The caption's look.

    ``font_size`` is the one value here that is a real constraint rather than
    a preference: at 1080 wide, a 22-character phrase in bold DejaVu needs
    roughly 70px to fit the text area. M0-09's 110px was sized for a *single
    word* and would run off the frame now that cues are phrases.
    """

    font_name: str = "DejaVu Sans"
    font_size: int = 72
    #: Fill and outline, as RGB — converted to ASS's BGR order on the way out
    #: so nobody has to write &H00FFFFFF and hope.
    fill: str = "#FFFFFF"
    outline: str = "#000000"
    #: Heavy, per the reference: an outline rather than a box, so the caption
    #: reads over any frame without a slab of colour across the image.
    outline_width: int = 6
    #: Left and right margins in pixels. libass wraps within these, which is
    #: the safety net for a cue wider than the grouping expected.
    margin: int = 80


def ass_document(
    cues: Sequence[CaptionLine] | Iterable[CaptionLine],
    *,
    width: int,
    height: int,
    style: AssStyle | None = None,
) -> str:
    """Render a whole caption track.

    Cues are emitted in the order given. Ordering and non-overlap are the
    timeline's invariants, asserted there against the compiled artifact — this
    writer would have to re-derive the same rules to check them again, and a
    second implementation of an invariant is a second thing to keep true.
    """
    chosen = style or AssStyle()
    x = width // 2
    y = int(height * CAPTION_BAND)

    lines = [
        "[Script Info]",
        "ScriptType: v4.00+",
        f"PlayResX: {width}",
        f"PlayResY: {height}",
        # 0 = smart wrapping, balanced lines. Only reached by a cue wider than
        # the grouping's own cap, but a caption running off the frame is worse
        # than one on two lines.
        "WrapStyle: 0",
        # Without this libass scales borders and shadows by the *display*
        # resolution rather than PlayRes, so the outline thins out whenever the
        # video is scaled — including in the review player.
        "ScaledBorderAndShadow: yes",
        "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        # BorderStyle=1 + Outline is outline-only; BorderStyle=3 would draw an
        # opaque box, which the reference does not use. Alignment 5 is
        # middle-centre, and every line carries \pos anyway.
        "Style: Caption,{font},{size},{fill},{fill},{outline},&H00000000,"
        "1,0,0,0,100,100,0,0,1,{border},0,5,{margin},{margin},0,1".format(
            font=chosen.font_name,
            size=chosen.font_size,
            fill=_ass_colour(chosen.fill),
            outline=_ass_colour(chosen.outline),
            border=chosen.outline_width,
            margin=chosen.margin,
        ),
        "",
        "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Text",
    ]

    for cue in cues:
        lines.append(
            f"Dialogue: 0,{ass_timestamp(cue.start_ms)},{ass_timestamp(cue.end_ms)},"
            f"Caption,,0,0,0,{{\\pos({x},{y})}}{escape(cue.text)}"
        )

    lines.append("")
    return "\n".join(lines)


def ass_timestamp(milliseconds: int) -> str:
    """``H:MM:SS.cc`` — ASS carries centiseconds, not milliseconds.

    **Floored, not rounded.** Rounding is not monotonic at the boundary: a cue
    ending at 2615 ms and the next starting at 2617 ms would round to 2.62 and
    2.62 — still fine — but a cue ending at 2616 and the next starting at 2617
    round to 2.62 and 2.62 as well, and the general case of rounding one end up
    and the other down can make two cues overlap that did not. Flooring both
    ends preserves ordering exactly, at a cost of up to 9 ms of display time
    that no viewer can perceive.
    """
    if milliseconds < 0:
        raise ValueError(f"a caption cannot start at {milliseconds}ms")
    centiseconds = milliseconds // 10
    seconds, centiseconds = divmod(centiseconds, 100)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}.{centiseconds:02d}"


def escape(text: str) -> str:
    """Make model-written narration safe to put in a Dialogue line.

    Three characters matter, and the order below is load-bearing.

    ``\\`` goes first. In ASS a backslash introduces an override (``\\N``,
    ``\\h``) and libass simply drops one it does not recognise — so a stray
    backslash silently deletes the character after it. Removing them here
    makes that visible in the source rather than surprising in the frame, and
    doing it *first* means the escapes added below are not themselves eaten.

    ``{`` and ``}`` open and close an override block. Unescaped, ``{i}`` in
    narration would not appear on screen at all; worse, a model writing
    something like ``{\\fscx200}`` would be *executed*. ``\\{`` and ``\\}`` are
    the documented literals.

    Newlines become ``\\N`` — a hard line break. A raw newline would end the
    Dialogue line and turn the rest of the caption into a malformed event.
    """
    return (
        text.replace("\\", "")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .replace("\r\n", "\n")
        .replace("\n", "\\N")
    )


def _ass_colour(rgb: str) -> str:
    """``#RRGGBB`` → ``&HAABBGGRR``. Alpha is always opaque here.

    The reversal is the whole point of this function existing: ASS stores blue
    first, and a hand-written ``&H00FF0000`` meaning "red" is actually blue.
    """
    text = rgb.strip().lstrip("#")
    if len(text) != 6:
        raise ValueError(f"expected #RRGGBB, got {rgb!r}")
    red, green, blue = text[0:2], text[2:4], text[4:6]
    return f"&H00{blue}{green}{red}".upper()
