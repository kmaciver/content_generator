"""Turning a provider's timings into words, and words into scene spans (M3-12).

**Finding B3 revised** chose one synthesis call for the whole script over twenty
per-scene calls: TTS reading a single sentence in isolation produces a complete
intonation contour, terminal fall included, and twenty of those concatenated
read as a list of statements rather than a narration. The cost of that choice is
that the pipeline receives *one* audio file and must work out for itself where
each scene begins and ends. This module is that work.

**Two derivations, and only the second is B3's.**

1. Characters → words. Measured against ElevenLabs on 2026-08-09, the
   ``/with-timestamps`` endpoint returns **character**-level timings, not word
   -level: ``alignment.characters`` is a list of single characters with a start
   and end each. Word timings do not exist in the response; they are computed
   here.
2. Words → scene spans, by walking the list **sequentially**. This is the part
   B3 defended: the fragility it originally worried about was *fuzzy text
   matching*, and sequential consumption is a different and far more robust
   operation, because words arrive in input order and a cut point needs frame
   accuracy (~33 ms), not sample accuracy.

**Caption from the written text, never the normalised text.** The provider
returns two alignments — one for the characters as written, one for the
normalisation it actually spoke. §1.0.2 found the reference videos display a
bare numeral as its own caption frame (``762``), and a probe on 2026-08-09 put
that token at 0.743–1.533 s as a single written unit. Caption from the
normalised stream and ``762`` becomes four words nobody wrote. The caller passes
the written alignment; this module never sees the other one.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

__all__ = [
    "SceneSpan",
    "WordTiming",
    "scene_spans",
    "words_from_characters",
]


@dataclass(frozen=True, slots=True)
class WordTiming:
    """One written token and when it is spoken.

    ``text`` is exactly as it appears in the script — punctuation attached,
    numerals unexpanded — because it is what a caption frame displays.
    """

    text: str
    start_ms: int
    end_ms: int
    #: Index of the first character of this word in the original script, so a
    #: caller can map a word back to the text it came from without searching.
    offset: int

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)


@dataclass(frozen=True, slots=True)
class SceneSpan:
    """Where one scene's narration sits inside the single audio file."""

    scene_index: int
    start_ms: int
    end_ms: int
    #: The words belonging to this scene, in order. The caption track for the
    #: scene is built from these and nothing else.
    words: tuple[WordTiming, ...]

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)


def words_from_characters(
    characters: Sequence[str],
    starts_s: Sequence[float],
    ends_s: Sequence[float],
) -> tuple[WordTiming, ...]:
    """Group per-character timings into per-word timings.

    A word runs from the start of its first character to the end of its last.
    Whitespace separates words and belongs to neither: a caption frame shows a
    word, and stretching it over the pause after it would drift every
    subsequent frame later by the length of a space.

    Punctuation stays **attached** to the word it follows, because that is how
    the script reads and therefore how the caption must read — "times." is one
    frame, not a word frame and a full-stop frame.

    Tolerates the three malformations a real response can carry: lists of
    unequal length (truncated to the shortest, so a provider that pads one
    array cannot produce words with invented timings), non-monotonic times
    (clamped so ``end`` never precedes ``start``), and leading or trailing
    whitespace, which the measured response does add to its normalised
    alignment.
    """
    count = min(len(characters), len(starts_s), len(ends_s))
    words: list[WordTiming] = []

    current: list[str] = []
    start: float | None = None
    end: float = 0.0
    offset = 0

    for index in range(count):
        char = characters[index]
        if char.isspace():
            if current:
                words.append(_word(current, start, end, offset))
                current = []
                start = None
            continue
        if not current:
            start = starts_s[index]
            offset = index
        current.append(char)
        end = max(end, ends_s[index])

    if current:
        words.append(_word(current, start, end, offset))
    return tuple(words)


def _word(chars: list[str], start: float | None, end: float, offset: int) -> WordTiming:
    start_ms = _ms(start or 0.0)
    end_ms = _ms(end)
    return WordTiming(
        text="".join(chars),
        start_ms=start_ms,
        # Clamped, never trusted: a provider returning an end before its start
        # would otherwise produce a negative-length caption frame that the
        # renderer turns into an ASS event with a reversed time range.
        end_ms=max(start_ms, end_ms),
        offset=offset,
    )


def _ms(seconds: float) -> int:
    """Seconds to whole milliseconds.

    Rounded rather than truncated. The timeline compiler works in milliseconds
    and a systematic downward bias would accumulate across ~200 words into a
    caption track that drifts visibly early by the end of the video.
    """
    return int(round(seconds * 1000))


def scene_spans(
    words: Sequence[WordTiming], narrations: Sequence[str]
) -> tuple[SceneSpan, ...]:
    """Assign words to scenes by consuming the word list **in order**.

    Each scene claims as many words as its narration has, one after another.
    No matching, no search: the script that was synthesised is the
    concatenation of these narrations, so the Nth word of the audio is the Nth
    word of the script by construction. That is why B3 reversed its original
    objection — the fragile thing was fuzzy *matching*, and this is counting.

    A scene's span runs from its first word's start to its last word's end.
    The gap between scenes therefore belongs to neither, which is correct: the
    pause after a sentence is silence between two images, and giving it to
    either side would cut a scene early or hold it late by that pause.

    **Runs out rather than guesses.** If the words are exhausted before the
    scenes are — a truncated synthesis, or a script edited after the audio was
    made — the remaining scenes come back with no words and a zero-length span,
    and the caller can see exactly which ones. Inventing timings for them would
    produce a video whose last scenes are silently unnarrated.
    """
    spans: list[SceneSpan] = []
    cursor = 0

    for index, narration in enumerate(narrations, start=1):
        wanted = len(narration.split())
        claimed = tuple(words[cursor : cursor + wanted])
        cursor += len(claimed)
        spans.append(
            SceneSpan(
                scene_index=index,
                start_ms=claimed[0].start_ms if claimed else 0,
                end_ms=claimed[-1].end_ms if claimed else 0,
                words=claimed,
            )
        )

    return tuple(spans)
