"""Compiling a structured style preset into a reusable prompt block (M3-05).

An operator edits **fields** — medium, palette, line treatment, how backgrounds
are handled. Every image generated for the series is then prompted with the
same compiled text, which is what makes twenty scenes look like one show
(risk R7).

**Why fields rather than a free-text style prompt.** A textarea is easier to
build and produces a style nobody can diff. "Why did episode 4 look different?"
is answerable when the change is `palette: [...] → [...]`; it is not answerable
when the change is a reworded paragraph. Fields also let M3.5's assisted
extraction propose a *structured* diff for approval rather than a wall of prose.

**Compilation is deterministic and total.** The same fields always compile to
byte-identical text, because the compiled block is pinned into an image's
snapshot (§10.3 rule 4) and a block that reordered itself between runs would
make two identical generations look different in the audit trail. Unknown
fields are carried through rather than dropped — an operator who adds
``texture: risograph grain`` should see it reach the model, not discover it was
silently ignored.

Pure: a mapping in, a string out. No database, no provider, no clock.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

__all__ = [
    "CANONICAL_FIELDS",
    "StyleSpec",
    "compile_style_block",
]

#: The known style axes, **in the order they are emitted**.
#:
#: Order is fixed here rather than taken from the mapping: a dict's insertion
#: order depends on how it was built (an editor's form, a jsonb round-trip, a
#: test literal), and three sources producing three different block texts for
#: the same style is precisely the drift this module exists to prevent.
#:
#: The vocabulary is aimed at R7's finding: consistency comes from a *radically
#: reductive* convention that a diffusion model reproduces reliably, so the
#: axes that matter most are the ones that constrain — line, shading,
#: background, detail — rather than the ones that embellish.
CANONICAL_FIELDS: tuple[tuple[str, str], ...] = (
    ("medium", "Medium"),
    ("palette", "Palette"),
    ("line", "Line"),
    ("shading", "Shading"),
    ("background", "Background"),
    ("detail", "Detail"),
    ("composition", "Composition"),
    ("mood", "Mood"),
)

#: Fields that are *not* part of the positive block. ``avoid`` becomes the
#: negative prompt, and emitting it as "Avoid: X" inside the positive prompt is
#: a well-known way to get X — the model reads the noun, not the instruction.
_NEGATIVE_FIELD = "avoid"

#: How **any** figure in this world is constructed — extracted for *position*,
#: not for exclusion.
#:
#: The style block is emitted first, and ``image.v1.jinja`` puts the character
#: last because models weight the end of a prompt. A cast rule left in the
#: style block would therefore sit above the scene and lose to it. Measured on
#: 2026-08-08: a scene naming "a parent" and "two children" returned three
#: figures with human proportions, hair and drawn faces, correctly rendered in
#: the series' medium — the *rendering* transferred and the *construction* did
#: not, because nothing described it.
#:
#: A style field rather than a character trait, deliberately. "Everyone has an
#: oversized round head" is a drawing convention, the same kind of statement as
#: ``line`` or ``shading``, and it must outlive any one character version.
#: Pip's terracotta tunic is identity and stays on the character.
_CAST_FIELD = "cast"


@dataclass(frozen=True, slots=True)
class StyleSpec:
    """A compiled style, ready to be composed into an image prompt."""

    #: The positive block: how everything in frame is rendered.
    block: str
    #: Terms for the provider's negative prompt, deduplicated and ordered.
    avoid: tuple[str, ...]
    #: How every figure in this world is built. Empty when the series has not
    #: said, in which case secondary figures are whatever the model assumes —
    #: which measurement shows is "ordinary humans".
    cast: str = ""

    @property
    def is_empty(self) -> bool:
        """An unconfigured style.

        Worth asking about explicitly: M3-06's admission check refuses image
        generation without an *approved* style, and a style that is approved
        but says nothing would pass that check while contributing nothing to
        consistency.
        """
        return not self.block and not self.avoid and not self.cast


def compile_style_block(fields: Mapping[str, Any] | None) -> StyleSpec:
    """Turn ``series_style.fields`` into a prompt block plus negative terms.

    Never raises. A malformed field degrades to its string form rather than
    failing the job: the alternative is a series whose images cannot be
    generated because someone typed a number into a text field, and the
    operator is about to review the output anyway (SADD §17).
    """
    data = dict(fields or {})

    lines: list[str] = []
    for key, label in CANONICAL_FIELDS:
        value = _flatten(data.pop(key, None))
        if value:
            lines.append(f"{label}: {value}")

    avoid = _terms(data.pop(_NEGATIVE_FIELD, None))
    cast = _flatten(data.pop(_CAST_FIELD, None))

    # Anything the operator added that this module has never heard of. Sorted,
    # so an unknown field's position does not depend on dict ordering either.
    for key in sorted(data):
        value = _flatten(data[key])
        if value:
            lines.append(f"{_label(key)}: {value}")

    return StyleSpec(block="\n".join(lines), avoid=avoid, cast=cast)


def _flatten(value: Any) -> str:
    """One field's value as prompt text.

    Lists become comma-joined — a palette is naturally a list and reads as a
    list. Nested structures are stringified rather than walked: a style field
    deep enough to need recursion is a sign the vocabulary is wrong, and
    guessing at its shape would produce confident nonsense.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Sequence) and not isinstance(value, str | bytes):
        return ", ".join(part for part in (_flatten(item) for item in value) if part)
    return str(value).strip()


def _terms(value: Any) -> tuple[str, ...]:
    """Negative terms, deduplicated with first-seen order preserved.

    Order is preserved rather than sorted because a negative prompt is read by
    the provider as a weighted list in some implementations, and re-sorting an
    operator's deliberate ordering would quietly change its meaning.
    """
    if value is None:
        return ()
    raw = (
        [value]
        if isinstance(value, str)
        else list(value) if isinstance(value, Sequence) else [value]
    )
    seen: dict[str, None] = {}
    for item in raw:
        term = _flatten(item)
        if term:
            seen.setdefault(term, None)
    return tuple(seen)


def _label(key: str) -> str:
    """``line_weight`` → ``Line weight``. Presentation only."""
    return key.replace("_", " ").strip().capitalize()
