"""M4-05 — the ASS caption writer.

Pure text generation, so these are cheap. What they are really guarding is the
class of failure that is invisible until a frame is extracted from a rendered
MP4: a caption in the wrong colour, in the wrong place, missing entirely, or —
worst — carrying an override the narration happened to contain.
"""

from __future__ import annotations

import pytest

from videoforge_workers.subtitles import (
    AssStyle,
    CaptionLine,
    ass_document,
    ass_timestamp,
    escape,
)

_CUES = (
    CaptionLine(text="You give your kid", start_ms=0, end_ms=731),
    CaptionLine(text="five bucks for", start_ms=801, end_ms=1486),
    CaptionLine(text="doing the dishes.", start_ms=1567, end_ms=2612),
)


def _document(**kwargs: object) -> str:
    return ass_document(_CUES, width=1080, height=1920, **kwargs)  # type: ignore[arg-type]


class TestTimestamps:
    @pytest.mark.parametrize(
        ("milliseconds", "expected"),
        [
            (0, "0:00:00.00"),
            (731, "0:00:00.73"),
            (2612, "0:00:02.61"),
            (60_000, "0:01:00.00"),
            (98_538, "0:01:38.53"),
            (3_661_000, "1:01:01.00"),
        ],
    )
    def test_it_formats_centiseconds(self, milliseconds: int, expected: str) -> None:
        assert ass_timestamp(milliseconds) == expected

    def test_it_floors_rather_than_rounds(self) -> None:
        """2612 ms is 2.612 s. Rounding would give 2.61 here and 2.62 for
        2615 — and the general case of rounding one cue's end up while the
        next cue's start rounds down makes two cues overlap that did not."""
        assert ass_timestamp(2619) == "0:00:02.61"

    def test_flooring_cannot_invert_two_cues(self) -> None:
        """The property, not the example. Flooring is monotonic, so a track
        that was ordered in milliseconds is ordered in centiseconds."""
        stamps = [ass_timestamp(ms) for ms in range(0, 5000, 7)]
        assert stamps == sorted(stamps)

    def test_a_negative_timestamp_is_refused(self) -> None:
        with pytest.raises(ValueError, match="cannot start at"):
            ass_timestamp(-1)


class TestEscaping:
    def test_braces_are_escaped_not_executed(self) -> None:
        """The dangerous one. Unescaped, ``{\\fscx200}`` in narration is an
        override libass *runs* — the caption silently doubles in width. This
        is model-written text."""
        assert escape("{\\fscx200}scale") == "\\{fscx200\\}scale"

    def test_a_stray_backslash_is_removed(self) -> None:
        """libass drops an override it does not recognise, taking the next
        character with it. Removing them here makes that visible in the source
        rather than surprising in the frame."""
        assert escape("back\\slash") == "backslash"

    def test_backslashes_are_stripped_before_braces_are_escaped(self) -> None:
        """Order is load-bearing: escaping first and stripping second would
        undo the escapes it had just added."""
        assert escape("{brace}") == "\\{brace\\}"

    def test_newlines_become_hard_breaks(self) -> None:
        """A raw newline would end the Dialogue line and turn the rest of the
        caption into a malformed event."""
        assert escape("two\nlines") == "two\\Nlines"
        assert escape("crlf\r\nlines") == "crlf\\Nlines"

    def test_ordinary_narration_is_untouched(self) -> None:
        assert escape("It feels responsible.") == "It feels responsible."


class TestDocument:
    def test_every_cue_becomes_a_dialogue_line(self) -> None:
        dialogue = [
            line for line in _document().splitlines() if line.startswith("Dialogue:")
        ]
        assert len(dialogue) == len(_CUES)

    def test_a_cue_carries_its_own_times_and_text(self) -> None:
        line = [
            line for line in _document().splitlines() if line.startswith("Dialogue:")
        ][2]
        assert "0:00:01.56" in line
        assert "0:00:02.61" in line
        assert line.endswith("doing the dishes.")

    def test_captions_sit_in_the_measured_band(self) -> None:
        """§1.0.2's 57%, at 1920 tall: 1094. M0-09 burned a caption there and
        the extracted frame was inspected, so this number is measured."""
        assert "\\pos(540,1094)" in _document()

    def test_the_play_resolution_matches_the_video(self) -> None:
        """libass positions against PlayRes. A mismatch puts every caption in
        the wrong place by a constant factor — which reads as a design choice
        rather than a bug."""
        document = _document()
        assert "PlayResX: 1080" in document
        assert "PlayResY: 1920" in document

    def test_colours_are_written_in_ass_byte_order(self) -> None:
        """``&HAABBGGRR``. A literal ``&H00FF0000`` meaning red is blue."""
        style = [
            line for line in _document().splitlines() if line.startswith("Style:")
        ][0]
        assert "&H00FFFFFF" in style  # white fill
        assert "&H00000000" in style  # black outline

    def test_a_non_grey_colour_is_actually_reversed(self) -> None:
        """White and black are palindromes, so the test above would pass on a
        writer that never reversed anything."""
        document = _document(style=AssStyle(fill="#C4622D"))
        assert "&H002D62C4" in document

    def test_the_outline_is_drawn_not_boxed(self) -> None:
        """BorderStyle 1 with an outline, not 3 with an opaque box: the
        reference has no slab of colour across the image."""
        style = [
            line for line in _document().splitlines() if line.startswith("Style:")
        ][0]
        # ..., BorderStyle, Outline, Shadow, Alignment, ...
        assert ",1,6,0,5," in style

    def test_borders_scale_with_the_play_resolution(self) -> None:
        """Without this the outline thins out whenever the video is displayed
        at anything other than its native size — including in the player."""
        assert "ScaledBorderAndShadow: yes" in _document()

    def test_an_empty_track_is_still_a_valid_document(self) -> None:
        """A project whose scenes are all cards has no captions. libass must
        get a well-formed file, not an empty one."""
        document = ass_document((), width=1080, height=1920)
        assert "[Events]" in document
        assert "Dialogue:" not in document

    def test_the_font_size_fits_a_phrase(self) -> None:
        """M0-09's 110px was sized for a single word. At 1080 wide a
        22-character phrase in bold needs roughly 70px, and a caption that
        runs off the frame is the failure this guards."""
        assert AssStyle().font_size <= 80

    def test_narration_containing_an_override_is_neutralised(self) -> None:
        """End to end: the escape reaches the Dialogue line."""
        document = ass_document(
            (CaptionLine(text="use {braces} here", start_ms=0, end_ms=500),),
            width=1080,
            height=1920,
        )
        assert "\\{braces\\}" in document
