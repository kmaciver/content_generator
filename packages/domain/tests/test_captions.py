"""M4-04 — grouping word timings into caption cues.

The numbers in these tests are measured, not chosen. They come from the real
narration generated on 2026-08-09: 235 words over 98 seconds, median word
279 ms, 43% of words shorter than 250 ms.
"""

from __future__ import annotations

from videoforge_domain.captions import Cue, group_into_cues
from videoforge_domain.timing import WordTiming

#: The opening of the real narration, with its real timings.
_REAL: tuple[tuple[str, int, int], ...] = (
    ("You", 0, 174),
    ("give", 174, 336),
    ("your", 336, 511),
    ("kid", 511, 731),
    ("five", 801, 1080),
    ("bucks", 1080, 1289),
    ("for", 1289, 1486),
    ("doing", 1567, 1798),
    ("the", 1798, 1904),
    ("dishes.", 1904, 2612),
    ("It", 2937, 3146),
    ("feels", 3146, 3413),
    ("responsible.", 3413, 4319),
)


def _words(
    raw: tuple[tuple[str, int, int], ...] = _REAL,
) -> tuple[WordTiming, ...]:
    return tuple(
        WordTiming(text=text, start_ms=start, end_ms=end, offset=index)
        for index, (text, start, end) in enumerate(raw)
    )


class TestAgainstRealNarration:
    def test_it_groups_into_readable_phrases(self) -> None:
        """The measured result, pinned.

        One word at a time gave 13 cues here with a 209 ms median. These are
        the phrase breaks the defaults produce, and every one lands on a
        phrase or sentence boundary rather than mid-thought.
        """
        assert [cue.text for cue in group_into_cues(_words())] == [
            "You give your kid",
            "five bucks for",
            "doing the dishes.",
            "It feels responsible.",
        ]

    def test_the_dwell_is_long_enough_to_read(self) -> None:
        """The problem being solved. A caption that changes every 279 ms is
        not read, it is glimpsed."""
        cues = group_into_cues(_words())
        assert min(cue.duration_ms for cue in cues) >= 600

    def test_no_cue_is_a_single_word_orphan(self) -> None:
        """Tighter settings (14 characters) produce four of them — "Financial",
        "mistake" — which look like the grouping broke rather than chose."""
        assert all(" " in cue.text for cue in group_into_cues(_words()))


class TestBreakRules:
    def test_a_sentence_end_always_breaks(self) -> None:
        """A caption straddling "…dishes. It…" reads as one thought and is
        two. This was a real defect in the first version of the grouping: the
        width cap flushed the cue and the sentence-end check was skipped for
        the word that started the next one.
        """
        cues = group_into_cues(_words())
        assert not any(
            "." in cue.text[:-1] for cue in cues
        ), "a cue carries text after a sentence end"

    def test_a_clause_end_breaks_once_the_cue_has_earned_its_dwell(self) -> None:
        words = _words(
            (
                ("Pay", 0, 300),
                ("yourself", 300, 700),
                ("first,", 700, 1000),
                ("every", 1000, 1300),
                ("month", 1300, 1700),
            )
        )
        assert [cue.text for cue in group_into_cues(words)] == [
            "Pay yourself first,",
            "every month",
        ]

    def test_a_very_short_clause_does_not_get_its_own_frame(self) -> None:
        """ "So," at 120 ms would otherwise flash by on its own."""
        words = _words(
            (
                ("So,", 0, 120),
                ("here", 120, 400),
                ("is", 400, 520),
                ("why.", 520, 900),
            )
        )
        assert [cue.text for cue in group_into_cues(words)] == ["So, here is why."]

    def test_a_long_run_without_punctuation_still_breaks(self) -> None:
        words = _words(tuple((f"w{n}", n * 400, n * 400 + 380) for n in range(8)))
        cues = group_into_cues(words)
        assert len(cues) > 1
        assert all(len(cue.text) <= 22 for cue in cues)


class TestBounds:
    def test_the_width_cap_is_never_exceeded(self) -> None:
        words = _words(tuple((f"word{n}", n * 100, n * 100 + 90) for n in range(20)))
        assert all(len(cue.text) <= 22 for cue in group_into_cues(words))

    def test_many_tiny_words_are_capped_by_count(self) -> None:
        """Single letters fit the width cap forever and burn no time; without
        ``MAX_WORDS`` this produced one cue of everything."""
        words = _words(tuple((f"{n % 10}", n * 20, n * 20 + 15) for n in range(30)))
        assert all(len(cue.text.split()) <= 6 for cue in group_into_cues(words))

    def test_a_word_wider_than_the_cap_gets_its_own_cue(self) -> None:
        words = _words((("supercalifragilisticexpialidocious", 0, 900),))
        assert group_into_cues(words) == (
            Cue(text="supercalifragilisticexpialidocious", start_ms=0, end_ms=900),
        )

    def test_no_words_is_no_cues(self) -> None:
        assert group_into_cues(()) == ()


class TestCueGeometry:
    def test_a_cue_spans_its_first_and_last_word(self) -> None:
        cue = group_into_cues(_words())[0]
        assert (cue.start_ms, cue.end_ms) == (0, 731)

    def test_cues_do_not_overlap(self) -> None:
        """The timeline refuses overlapping captions — libass would render
        both, stacked, without complaining."""
        cues = group_into_cues(_words())
        for earlier, later in zip(cues, cues[1:], strict=False):
            assert later.start_ms >= earlier.end_ms

    def test_a_pause_between_sentences_shows_no_caption(self) -> None:
        """ "dishes." ends at 2612 and "It" starts at 2937. Holding the phrase
        across that gap would put words on screen nobody is saying."""
        cues = group_into_cues(_words())
        assert cues[2].end_ms == 2612
        assert cues[3].start_ms == 2937
