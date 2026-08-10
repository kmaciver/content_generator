"""M3-12: character timings → words → scene spans.

Pure, so no provider and no database. The fixture below is the **measured**
ElevenLabs response for "The moon pulls 762 times." (2026-08-09), not an
invented one — the point of these tests is that the derivation survives the
shape a real provider actually returns.
"""

from __future__ import annotations

from videoforge_domain.timing import WordTiming, scene_spans, words_from_characters

#: Verbatim from the live probe. Character-level, because that is what the
#: endpoint returns — word timings do not exist in the response.
TEXT = "The moon pulls 762 times."
STARTS = [
    0.0,
    0.116,
    0.151,
    0.174,
    0.221,
    0.267,
    0.302,
    0.337,
    0.372,
    0.418,
    0.464,
    0.499,
    0.534,
    0.581,
    0.627,
    0.743,
    0.918,
    1.196,
    1.533,
    1.591,
    1.649,
    1.707,
    1.788,
    1.869,
    2.020,
]
ENDS = [
    0.116,
    0.151,
    0.174,
    0.221,
    0.267,
    0.302,
    0.337,
    0.372,
    0.418,
    0.464,
    0.499,
    0.534,
    0.581,
    0.627,
    0.743,
    0.918,
    1.196,
    1.533,
    1.591,
    1.649,
    1.707,
    1.788,
    1.869,
    2.020,
    2.136,
]


class TestWordsFromCharacters:
    def test_the_measured_response_yields_the_written_words(self) -> None:
        words = words_from_characters(list(TEXT), STARTS, ENDS)
        assert [w.text for w in words] == ["The", "moon", "pulls", "762", "times."]

    def test_a_numeral_stays_one_token(self) -> None:
        """**§1.0.2's decisive observation.**

        The reference videos display a bare numeral as its own caption frame.
        Caption from the provider's *normalised* alignment and ``762`` becomes
        "seven hundred sixty two" — four frames nobody wrote. This module is
        only ever given the written alignment, and this is what that buys.
        """
        words = words_from_characters(list(TEXT), STARTS, ENDS)
        numeral = next(w for w in words if w.text == "762")
        assert numeral.start_ms == 743
        assert numeral.end_ms == 1533

    def test_punctuation_stays_attached(self) -> None:
        """A caption frame shows a word as the script reads it — "times." is
        one frame, not a word frame followed by a full-stop frame."""
        words = words_from_characters(list(TEXT), STARTS, ENDS)
        assert words[-1].text == "times."

    def test_whitespace_belongs_to_no_word(self) -> None:
        """Stretching a word over the pause after it drifts every later frame
        by the length of a space."""
        words = words_from_characters(list(TEXT), STARTS, ENDS)
        # "The" ends where its own last character ends, not where "moon" starts.
        assert words[0].end_ms == 174
        assert words[1].start_ms == 221

    def test_offsets_point_back_into_the_script(self) -> None:
        words = words_from_characters(list(TEXT), STARTS, ENDS)
        for word in words:
            assert TEXT[word.offset : word.offset + len(word.text)] == word.text

    def test_surrounding_whitespace_is_ignored(self) -> None:
        """The measured ``normalized_alignment`` came back padded with spaces;
        a padded written alignment must not produce empty words."""
        padded = f" {TEXT} "
        starts = [0.0, *STARTS, 2.136]
        ends = [0.0, *ENDS, 2.2]
        words = words_from_characters(list(padded), starts, ends)
        assert [w.text for w in words] == ["The", "moon", "pulls", "762", "times."]

    def test_unequal_arrays_truncate_rather_than_invent(self) -> None:
        """A provider that pads one array must not be able to produce words
        with made-up timings."""
        words = words_from_characters(list("ab cd"), [0.0, 0.1], [0.1, 0.2])
        assert [w.text for w in words] == ["ab"]

    def test_a_reversed_range_is_clamped(self) -> None:
        """An end before its start becomes an ASS event with a reversed time
        range, which the renderer will not draw."""
        words = words_from_characters(list("hi"), [1.0, 1.0], [0.5, 0.5])
        assert words[0].end_ms >= words[0].start_ms

    def test_empty_input_is_empty_output(self) -> None:
        assert words_from_characters([], [], []) == ()


class TestSceneSpans:
    WORDS = (
        WordTiming("One", 0, 100, 0),
        WordTiming("two", 150, 250, 4),
        WordTiming("three.", 300, 500, 8),
        WordTiming("Four", 900, 1000, 15),
        WordTiming("five.", 1050, 1200, 20),
    )

    def test_words_are_claimed_in_order(self) -> None:
        spans = scene_spans(self.WORDS, ["One two three.", "Four five."])
        assert [w.text for w in spans[0].words] == ["One", "two", "three."]
        assert [w.text for w in spans[1].words] == ["Four", "five."]

    def test_a_span_runs_from_first_word_to_last(self) -> None:
        spans = scene_spans(self.WORDS, ["One two three.", "Four five."])
        assert (spans[0].start_ms, spans[0].end_ms) == (0, 500)
        assert (spans[1].start_ms, spans[1].end_ms) == (900, 1200)

    def test_the_pause_between_scenes_belongs_to_neither(self) -> None:
        """It is silence between two images. Giving it to either side cuts one
        scene early or holds the other late by exactly that pause."""
        spans = scene_spans(self.WORDS, ["One two three.", "Four five."])
        assert spans[0].end_ms < spans[1].start_ms

    def test_running_out_of_words_leaves_visible_holes(self) -> None:
        """A truncated synthesis, or a script edited after the audio was made.
        Inventing timings would produce a video whose last scenes are silently
        unnarrated."""
        spans = scene_spans(self.WORDS, ["One two three.", "Four five.", "Six."])
        assert spans[2].words == ()
        assert spans[2].duration_ms == 0

    def test_scene_indexes_are_one_based(self) -> None:
        spans = scene_spans(self.WORDS, ["One two three.", "Four five."])
        assert [s.scene_index for s in spans] == [1, 2]

    def test_no_scenes_consumes_nothing(self) -> None:
        assert scene_spans(self.WORDS, []) == ()
