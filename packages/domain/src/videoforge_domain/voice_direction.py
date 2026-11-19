"""Voice direction: emotion tags layered over the approved narration (ADR-019).

ElevenLabs' ``eleven_v3`` reads inline *audio tags* — ``[whispers]``,
``[excited]`` — as performance direction rather than as words. This module owns
everything about those tags that is not I/O: which ones exist, where they sit,
how a model's suggestion is turned into positions, how a reviewer's edit is
checked, how the text sent to the provider is built, and how the tags are taken
back *out* of the timings the provider returns.

**Tags are positions, not text.** The narration lives in ``scene.narration_text``
and is never rewritten. A direction is a list of ``(word index, tag)`` pairs per
scene, so the words cannot drift from the scene set by construction: a tag can
be put somewhere, but it cannot change what is said. Every other consumer of
``narration_text`` — captions, scene spans, the timeline, post copy — keeps
reading clean words and needs no change at all.

**A tag goes before a word, and has no end.** v3 has no closing tag; a tag
colours what follows it, roughly to the end of the sentence. So a tag's
position is the index of the word it precedes, and ``len(words)`` is also a
legal position — a ``[laughs]`` after the last word.

**Why tags are stripped by offset.** The adapter returns the alignment of the
text *as written*, one entry per character of the request. The composer knows
exactly which character ranges it inserted, so it removes those indices and
nothing else — no pattern matching against narration that might itself contain
a bracket. A response that is *not* one-to-one with the request (the probe
decides whether v3 ever does that) falls back to removing bracketed runs, which
is only wrong for narration that contains literal brackets.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "AUDIO_TAGS",
    "MAX_GENERATED_TAGS_PER_SCENE",
    "MAX_TAGS_PER_SCENE",
    "ComposedNarration",
    "DirectedScene",
    "InvalidDirectionError",
    "SceneText",
    "Tag",
    "compose",
    "direction_content",
    "directed_scenes",
    "parse_tagged_narration",
    "strip_tag_characters",
    "validate_direction",
    "words_of",
]

#: The tags a reviewer can pick and a model may suggest. **Closed on purpose.**
#: v3 accepts free text in brackets, and a misspelt one is not an error — it is
#: read aloud, or silently ignored. A fixed list means every tag that reaches
#: the provider is one somebody has heard work. Order is the order the picker
#: shows them: delivery first, then non-verbal sounds, then timing.
AUDIO_TAGS: tuple[str, ...] = (
    "excited",
    "curious",
    "serious",
    "sad",
    "sarcastic",
    "whispers",
    "shouts",
    "laughs",
    "sighs",
    "short pause",
)

_AUDIO_TAG_SET = frozenset(AUDIO_TAGS)

#: A generated direction is trimmed to this many tags per scene. A model asked
#: to "add emotion" tags every sentence, and a narration where every line is a
#: performance has no contrast left — the tag that matters stops landing.
MAX_GENERATED_TAGS_PER_SCENE = 2

#: The ceiling for a reviewer. Not a judgement about taste — a person adding a
#: fifth tag to one scene has made a choice — just a bound that stops a
#: malformed request from posting thousands.
MAX_TAGS_PER_SCENE = 12

#: A bracketed run inside generated text: ``[whispers]``, ``[short pause]``.
#: Bounded so a stray ``[`` cannot swallow the rest of a scene.
_BRACKETED = re.compile(r"\[([^\[\]\n]{1,40})\]")
_WORD = re.compile(r"\S+")


class InvalidDirectionError(ValueError):
    """A voice direction that cannot be applied to the approved narration."""


@dataclass(frozen=True, slots=True, order=True)
class Tag:
    """One audio tag, placed before the ``word``-th word of its scene."""

    word: int
    tag: str

    def as_dict(self) -> dict[str, Any]:
        return {"word": self.word, "tag": self.tag}


@dataclass(frozen=True, slots=True)
class SceneText:
    """The bit of a scene this module needs: who it is and what it says."""

    scene_id: str
    scene_index: int
    narration: str


@dataclass(frozen=True, slots=True)
class DirectedScene:
    scene: SceneText
    tags: tuple[Tag, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "scene_id": self.scene.scene_id,
            "scene_index": self.scene.scene_index,
            "narration": self.scene.narration,
            "tags": [tag.as_dict() for tag in self.tags],
        }


@dataclass(frozen=True, slots=True)
class ComposedNarration:
    """The text sent to the provider, and where the tags sit inside it."""

    text: str
    #: Half-open ``[start, end)`` character ranges of every inserted tag,
    #: brackets included, in ascending order. The trailing space after a tag is
    #: *not* included: it is whitespace, and word grouping already ignores it.
    tag_spans: tuple[tuple[int, int], ...]

    @property
    def tag_count(self) -> int:
        return len(self.tag_spans)


def words_of(narration: str) -> list[str]:
    """Words exactly as ``scene_spans`` counts them: whitespace-separated."""
    return narration.split()


# --------------------------------------------------------------------------- #
# Generated suggestions
# --------------------------------------------------------------------------- #


def parse_tagged_narration(narration: str, tagged: str) -> tuple[Tag, ...] | None:
    """Recover tag positions from a model's tagged copy of ``narration``.

    Returns ``None`` when the model changed the *words* — a reworded line, a
    dropped comma. The caller keeps the scene untagged rather than guessing:
    the narration was approved word for word, and a tag placed by fuzzy
    matching against a sentence the model rewrote is a tag in the wrong place.

    Unknown tags are dropped, not refused: a model inventing ``[mysterious]``
    has still placed the tags it got right.
    """
    tags: list[Tag] = []
    words: list[str] = []
    cursor = 0
    for match in _BRACKETED.finditer(tagged):
        words.extend(words_of(tagged[cursor : match.start()]))
        tag = _normalise_tag(match.group(1))
        if tag is not None:
            tags.append(Tag(word=len(words), tag=tag))
        cursor = match.end()
    words.extend(words_of(tagged[cursor:]))

    if words != words_of(narration):
        return None
    return tuple(_dedupe(tags))


def _normalise_tag(raw: str) -> str | None:
    tag = " ".join(raw.strip().lower().split())
    return tag if tag in _AUDIO_TAG_SET else None


def _dedupe(tags: Sequence[Tag]) -> list[Tag]:
    """Sorted by position, one of each tag per position.

    Stable within a position, so ``[sad][whispers]`` keeps its order — two
    tags on one word are read in sequence and the order is the author's.
    """
    seen: set[Tag] = set()
    kept: list[Tag] = []
    for tag in sorted(tags, key=lambda t: t.word):
        if tag not in seen:
            seen.add(tag)
            kept.append(tag)
    return kept


# --------------------------------------------------------------------------- #
# Human edits
# --------------------------------------------------------------------------- #


def validate_direction(
    content: Mapping[str, Any], scenes: Sequence[SceneText]
) -> dict[str, Any]:
    """A reviewer's direction, checked against the approved narration.

    Returns the canonical content to store. Refuses rather than repairs: a
    reviewer approves exactly the tags they placed, so a tag in a position that
    does not exist is reported back, never moved to the nearest one.

    The scene list must be *this* scene set's, in order. A direction written
    against a scene set that has since been replaced is the one mistake that
    would put tags on the wrong sentences, so it is refused by name.
    """
    raw_scenes = content.get("scenes")
    if not isinstance(raw_scenes, list):
        raise InvalidDirectionError("a voice direction needs a 'scenes' list")
    if len(raw_scenes) != len(scenes):
        raise InvalidDirectionError(
            f"the direction has {len(raw_scenes)} scenes but the approved scene "
            f"set has {len(scenes)}; regenerate the voice direction"
        )

    directed: list[DirectedScene] = []
    for raw, scene in zip(raw_scenes, scenes, strict=True):
        if not isinstance(raw, Mapping) or raw.get("scene_id") != scene.scene_id:
            raise InvalidDirectionError(
                f"scene {scene.scene_index} does not match the approved scene "
                "set; regenerate the voice direction"
            )
        directed.append(DirectedScene(scene, _tags(raw.get("tags"), scene)))

    return direction_content(directed, content.get("scene_set_version_id"))


def _tags(raw: Any, scene: SceneText) -> tuple[Tag, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise InvalidDirectionError(f"scene {scene.scene_index}: tags must be a list")
    if len(raw) > MAX_TAGS_PER_SCENE:
        raise InvalidDirectionError(
            f"scene {scene.scene_index} has {len(raw)} tags; at most "
            f"{MAX_TAGS_PER_SCENE} per scene"
        )

    word_count = len(words_of(scene.narration))
    tags: list[Tag] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            raise InvalidDirectionError(
                f"scene {scene.scene_index}: each tag needs a word and a tag"
            )
        name = _normalise_tag(str(entry.get("tag") or ""))
        if name is None:
            raise InvalidDirectionError(
                f"scene {scene.scene_index}: {entry.get('tag')!r} is not an "
                f"available tag; choose one of {', '.join(AUDIO_TAGS)}"
            )
        word = entry.get("word")
        # `bool` is an `int`; a JSON `true` as a position is a client bug.
        if not isinstance(word, int) or isinstance(word, bool):
            raise InvalidDirectionError(
                f"scene {scene.scene_index}: tag position must be a word number"
            )
        if not 0 <= word <= word_count:
            raise InvalidDirectionError(
                f"scene {scene.scene_index}: no word {word}; the narration has "
                f"{word_count}"
            )
        tags.append(Tag(word=word, tag=name))
    return tuple(_dedupe(tags))


def direction_content(
    scenes: Sequence[DirectedScene], scene_set_version_id: Any
) -> dict[str, Any]:
    """The stored shape, built in one place for the worker and the editor."""
    return {
        "scene_set_version_id": (
            str(scene_set_version_id) if scene_set_version_id else None
        ),
        "scenes": [scene.as_dict() for scene in scenes],
        "tag_count": sum(len(scene.tags) for scene in scenes),
    }


def directed_scenes(
    content: Mapping[str, Any], scenes: Sequence[SceneText]
) -> list[DirectedScene]:
    """Approved direction content, re-checked against the scenes about to be
    spoken. The voice stage's guard: :func:`validate_direction` already ran on
    every human edit, and running it again here costs nothing and catches a
    direction approved against a scene set that has since moved on."""
    canonical = validate_direction(content, scenes)
    return [
        DirectedScene(
            scene,
            tuple(Tag(word=t["word"], tag=t["tag"]) for t in raw["tags"]),
        )
        for raw, scene in zip(canonical["scenes"], scenes, strict=True)
    ]


# --------------------------------------------------------------------------- #
# Synthesis
# --------------------------------------------------------------------------- #


def compose(scenes: Sequence[DirectedScene]) -> ComposedNarration:
    """The text the provider reads: narrations joined, tags inserted.

    Joined with one space, exactly as the voice stage always has, and each
    narration's *own* whitespace preserved — tags are inserted at word starts
    rather than by re-joining split words, so a direction with no tags
    produces byte-for-byte the text an untagged synthesis would have sent.
    """
    parts: list[str] = []
    spans: list[tuple[int, int]] = []
    length = 0

    for position, directed in enumerate(scenes):
        narration = directed.scene.narration.strip()
        if position:
            parts.append(" ")
            length += 1

        starts = [m.start() for m in _WORD.finditer(narration)]
        by_word: dict[int, list[str]] = {}
        for tag in directed.tags:
            by_word.setdefault(tag.word, []).append(tag.tag)

        cursor = 0
        for word_index, start in enumerate([*starts, len(narration)]):
            names = by_word.get(word_index)
            if not names:
                continue
            chunk = narration[cursor:start]
            # A trailing tag sits after the last word, so it needs a space
            # before it rather than after.
            trailing = word_index == len(starts)
            if trailing and chunk and not chunk[-1].isspace():
                chunk += " "
            parts.append(chunk)
            length += len(chunk)
            for name in names:
                marker = f"[{name}]"
                spans.append((length, length + len(marker)))
                parts.append(marker)
                length += len(marker)
                if not trailing:
                    parts.append(" ")
                    length += 1
            cursor = start
        tail = narration[cursor:]
        parts.append(tail)
        length += len(tail)

    return ComposedNarration(text="".join(parts), tag_spans=tuple(spans))


def strip_tag_characters(
    composed: ComposedNarration,
    characters: Sequence[str],
    starts_s: Sequence[float],
    ends_s: Sequence[float],
) -> tuple[tuple[str, ...], tuple[float, ...], tuple[float, ...], str]:
    """Remove the inserted tags from a provider's per-character alignment.

    Returns the three arrays without the tag characters, plus which method was
    used — ``"none"``, ``"offset"`` or ``"bracket_scan"`` — so the voice stage
    can record it. The method is evidence: if production ever reports
    ``bracket_scan``, the one-to-one assumption ADR-019 rests on has stopped
    holding and someone should look.
    """
    count = min(len(characters), len(starts_s), len(ends_s))
    chars, starts, ends = characters[:count], starts_s[:count], ends_s[:count]

    if not composed.tag_spans:
        return tuple(chars), tuple(starts), tuple(ends), "none"

    if "".join(chars) == composed.text:
        drop: set[int] = set()
        for start, end in composed.tag_spans:
            drop.update(range(start, end))
        keep = [i for i in range(count) if i not in drop]
        method = "offset"
    else:
        keep = _outside_brackets(chars)
        method = "bracket_scan"

    return (
        tuple(chars[i] for i in keep),
        tuple(starts[i] for i in keep),
        tuple(ends[i] for i in keep),
        method,
    )


def _outside_brackets(chars: Sequence[str]) -> list[int]:
    """Indices not inside a ``[...]`` run. An unclosed ``[`` drops nothing —
    a stray bracket must not swallow the rest of the narration."""
    keep: list[int] = []
    index = 0
    while index < len(chars):
        if chars[index] == "[":
            close = _find_close(chars, index)
            if close is not None:
                index = close + 1
                continue
        keep.append(index)
        index += 1
    return keep


def _find_close(chars: Sequence[str], open_at: int) -> int | None:
    for index in range(open_at + 1, min(len(chars), open_at + 42)):
        if chars[index] == "]":
            return index
        if chars[index] in "[\n":
            return None
    return None
