"""Word timings → caption cues (M4-04).

``timing.py`` recovers *words* from the provider's character alignment. This
module decides how many of them share a frame.

**Why not one word at a time.** §1.0.2 measured the reference videos as
one-word captions, and that is what M3-12's review player shows. Against this
project's real narration that is 235 cues in 98 seconds with a **median word
duration of 279 ms**, and 43% of words shorter than 250 ms. Two things follow:
it is genuinely hard to read, and it demands timing precision that nothing
downstream can honour — a browser's ``timeupdate`` fires about four times a
second, so nearly half the words could never get their own frame in a preview
at all.

Grouping into short phrases removes both problems at once. Measured on the
first two scenes of the real narration, the settings below turn 23 words into
8 cues with a median dwell of 882 ms, no single-word orphans, and every break
landing on a phrase or sentence boundary.

**The rules, in priority order.** A sentence end is a hard break — a caption
straddling "…dishes. It…" reads as one thought and is two. A clause end
(comma, semicolon, colon) breaks too, once the cue has earned at least half
its target dwell, so short parentheticals do not each get a frame. Otherwise
the cue grows until it reaches the dwell target or the width cap.

**Width is the real knob, not dwell.** Above about 22 characters the character
cap fires before the dwell target and the two stop interacting — measured, a
30-character cap produced output identical to 22. Widening this therefore does
nothing on its own; the dwell target has to move with it.

Pure, and deliberately separate from the ASS writer (M4-05) and from the
review player: all three read *these* cues, so a preview and a burn cannot
disagree about where a caption starts.
"""

from __future__ import annotations

from dataclasses import dataclass

from videoforge_domain.timing import WordTiming

__all__ = [
    "CLAUSE_ENDINGS",
    "Cue",
    "MAX_CHARACTERS",
    "MAX_WORDS",
    "SENTENCE_ENDINGS",
    "TARGET_DWELL_MS",
    "group_into_cues",
]

#: Characters the caption may hold before it must break. See the module
#: docstring: this is the knob that actually decides grouping.
MAX_CHARACTERS = 22

#: How long a cue should ideally stay up. Only bites below the width cap —
#: on short words, which is exactly where one-word captions were unreadable.
TARGET_DWELL_MS = 600

#: A hard ceiling for the pathological case of many very short words
#: ("a", "to", "of") that fit the width cap and burn no time.
MAX_WORDS = 6

#: Extra characters the orphan merge may spend that the grouping loop may not.
#:
#: Measured: of 14 single-word cues in the real 98-second narration, most were
#: **one-word sentences** — "Save." "Spend." "Give." "Budgeting." — which are
#: rhetorical beats and belong alone. Two were not: "That's the whole" /
#: "lesson." (24 characters joined) and "So what actually" / "works?" (23),
#: blocked by one and two characters.
#:
#: The width cap is a legibility guideline, unlike the card renderer's, which
#: is a real box with real edges. An orphan on screen costs more than three
#: characters of overflow, so the merge is allowed them and the grouping loop
#: is not — a merge is repairing a defect, and greed is what caused it.
MERGE_SLACK = 3

SENTENCE_ENDINGS = (".", "!", "?", "…")
CLAUSE_ENDINGS = (",", ";", ":", "—")


@dataclass(frozen=True, slots=True)
class Cue:
    """One caption: the words that share a frame, and when it is up."""

    text: str
    start_ms: int
    end_ms: int

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


def group_into_cues(
    words: list[WordTiming] | tuple[WordTiming, ...],
    *,
    max_characters: int = MAX_CHARACTERS,
    target_dwell_ms: int = TARGET_DWELL_MS,
    max_words: int = MAX_WORDS,
) -> tuple[Cue, ...]:
    """Group one scene's words into caption cues.

    **One scene's words, never a whole narration.** A cue that spanned a scene
    boundary would stay on screen while the image changed underneath it, which
    is both wrong and unrenderable — the caller passes per-scene words and the
    boundary is enforced by construction rather than by a rule this function
    would have to be told about.

    A cue ends at its last word's end, so a pause between sentences shows no
    caption. That is the honest rendering of "the caption shows what is being
    said now"; holding the previous phrase over a silence would put words on
    screen that nobody is saying.
    """
    grouped: list[list[WordTiming]] = []
    current: list[WordTiming] = []

    def flush() -> None:
        if current:
            grouped.append(list(current))
            current.clear()

    for word in words:
        # Width is checked *before* appending, so the cap is a real bound on
        # the emitted text rather than a threshold it is allowed to cross once.
        if current and _too_wide(current, word, max_characters, max_words):
            flush()

        current.append(word)
        if _should_break(
            word, current[-1].end_ms - current[0].start_ms, target_dwell_ms
        ):
            flush()

    flush()
    _absorb_orphans(grouped, max_characters + MERGE_SLACK, max_words)
    return tuple(
        Cue(
            text=" ".join(word.text for word in group),
            start_ms=group[0].start_ms,
            end_ms=group[-1].end_ms,
        )
        for group in grouped
    )


def _should_break(word: WordTiming, dwell_ms: int, target_dwell_ms: int) -> bool:
    """The three break rules, in priority order.

    A function rather than a chain of ``elif`` in the loop: ruff's autofix
    collapses adjacent branches into one ``or`` expression, which is
    semantically identical and reads as a single condition rather than as
    three rules with an order. The order is the design.
    """
    tail = word.text[-1:]
    if tail in SENTENCE_ENDINGS:
        # Hard. "…dishes. It…" in one caption reads as one thought and is two.
        return True
    if tail in CLAUSE_ENDINGS:
        # Once the cue has earned half its dwell, so a two-word parenthetical
        # does not get a frame of its own.
        return dwell_ms >= target_dwell_ms // 2
    return dwell_ms >= target_dwell_ms


def _absorb_orphans(
    grouped: list[list[WordTiming]], max_characters: int, max_words: int
) -> None:
    """Fold single-word cues back into a neighbour where they fit.

    The grouping above is greedy, and greedy leaves orphans: "Pay yourself" /
    **"first,"** / "every month", because the dwell target fired one word
    before the comma that should have ended the phrase. A lone word on screen
    reads as the grouping having broken rather than having chosen — and it
    reintroduces exactly the flash-by problem this module exists to remove.

    A post-pass rather than lookahead in the loop. Lookahead means asking "is
    the *next* word a clause end, and would it still fit?" at every branch,
    which multiplies the rules that already interact; this asks one question
    once, after all of them have had their say.

    Backwards by preference, because a phrase reads better completed than
    anticipated — except across a sentence end, where joining would produce
    the "…dishes. It…" straddle the hard break exists to prevent.
    """
    position = 1
    while position < len(grouped):
        group = grouped[position]
        if len(group) > 1:
            position += 1
            continue

        previous = grouped[position - 1]
        if not _ends_sentence(previous) and _fits(
            previous, group, max_characters, max_words
        ):
            previous.extend(group)
            del grouped[position]
            continue

        following = grouped[position + 1] if position + 1 < len(grouped) else None
        if (
            following is not None
            and not _ends_sentence(group)
            and _fits(group, following, max_characters, max_words)
        ):
            group.extend(following)
            del grouped[position + 1]
            continue

        # Nowhere to go: a word wider than the cap, or bounded by sentence
        # ends on both sides. Left alone rather than forced.
        position += 1

    # The first cue can be an orphan too, and has no predecessor to fold into.
    if (
        len(grouped) > 1
        and len(grouped[0]) == 1
        and not _ends_sentence(grouped[0])
        and _fits(grouped[0], grouped[1], max_characters, max_words)
    ):
        grouped[0].extend(grouped[1])
        del grouped[1]


def _ends_sentence(group: list[WordTiming]) -> bool:
    return group[-1].text[-1:] in SENTENCE_ENDINGS


def _fits(
    left: list[WordTiming],
    right: list[WordTiming],
    max_characters: int,
    max_words: int,
) -> bool:
    if len(left) + len(right) > max_words:
        return False
    return _width(left + right) <= max_characters


def _width(group: list[WordTiming]) -> int:
    return sum(len(word.text) for word in group) + len(group) - 1


def _too_wide(
    current: list[WordTiming], word: WordTiming, max_characters: int, max_words: int
) -> bool:
    if len(current) + 1 > max_words:
        return True
    return _width([*current, word]) > max_characters
