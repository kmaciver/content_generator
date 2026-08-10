"""Composing an image prompt from scene, character and style (M3-03).

Three sources, one prompt, and a rule: **scene text cannot override the
character's immutable traits.** Everything here exists to make that rule hold
by construction rather than by hoping the model behaves.

It is enforced in three layers, each doing a different job:

1. **Structural.** The character block is built from ``immutable_traits``
   alone. There is no code path by which scene text reaches it — not an
   ordering convention, an actual absence of a parameter. This is the layer a
   test can prove, and :func:`build_image_prompt` is deliberately shaped so
   that adding one would be an obvious change.
2. **Positional.** The immutable block is emitted *last*, after the scene, with
   explicit precedence language (``image.v1.jinja``). Models weight the end of
   a prompt more heavily, so the thing that must not drift is read last.
3. **Observable.** Scene text that mentions an immutable trait is recorded in
   ``conflicts`` and travels in the snapshot. Not a rejection: matching a word
   is not understanding a claim, and failing a job over a false positive would
   be worse than the drift it guards against. But when scene 14 comes back
   wrong, the reviewer can see that its brief said "hair" and hair is fixed.

**Deterministic.** The same inputs always produce byte-identical text. The
prompt is pinned into an image's snapshot (§10.3 rule 4), so a builder that
reordered traits between runs would make two identical generations look
different in the audit trail — and would break any hope of reproducing an image
from its record. Every mapping is walked in sorted key order for that reason,
never in whatever order the caller's dict happened to have.

Pure: strings and mappings in, a value object out. No database row, no ORM
type, no provider.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from videoforge_prompts import render_block
from videoforge_prompts.style import StyleSpec, compile_style_block

__all__ = [
    "IMAGE_TEMPLATE",
    "CharacterSpec",
    "ImagePrompt",
    "build_image_prompt",
]

IMAGE_TEMPLATE = "image"

#: Words too common to be evidence of anything. Without this, a brief
#: containing "the" would collide with a trait key named "the" — and more
#: realistically, trait keys like "colour" or "size" appear in almost every
#: scene description and would flag constantly, which trains a reviewer to
#: ignore the signal.
_UNREVEALING = frozenset(
    {
        "colour",
        "color",
        "size",
        "shape",
        "style",
        "look",
        "form",
        "detail",
        "type",
    }
)


@dataclass(frozen=True, slots=True)
class CharacterSpec:
    """One character version, as the builder needs it.

    Plain mappings rather than the ORM row: ``packages/prompts`` has no
    persistence dependency and must not gain one. The worker maps
    ``SeriesCharacter`` onto this.
    """

    name: str
    #: Traits no scene may contradict — the R7 consistency anchor.
    #:
    #: **Name a colour on every element that has one.** The style's ``palette``
    #: declares which colours may appear; it says nothing about *where*, and a
    #: model will happily reassign them per scene. Measured against Gemini on
    #: 2026-08-07: four scenes returned the same character's body as terracotta,
    #: black, terracotta and cream, with limbs shuffling independently — the
    #: palette obeyed exactly, the character unrecognisable across cuts. Palette
    #: says what, traits say where.
    #:
    #: **Refuse additions by name.** "no hair" is not enough on a close-up; the
    #: same run grew a thick ring around the head that reads as a hood. Close
    #: framing invites detail, so the traits have to name what must not appear
    #: rather than only what must.
    immutable: Mapping[str, Any] = field(default_factory=dict)
    #: Traits a scene is free to change: pose, expression, framing.
    variable: Mapping[str, Any] = field(default_factory=dict)
    #: Terms that must never appear, folded into the negative prompt.
    never: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ImagePrompt:
    """A composed prompt and everything needed to explain it later."""

    prompt: str
    negative_prompt: str
    #: Content fingerprint of the composed prompt *and* its negative. Eight hex
    #: characters, matching ``PromptTemplate.digest`` — long enough to
    #: distinguish every prompt one project will produce, short enough to read.
    digest: str
    #: Provenance of the frame template itself (``image@1+abc12345``).
    template_ref: str
    #: Immutable trait keys mentioned by the scene text. Empty in the normal
    #: case; non-empty is a hint for whoever reviews a bad image, not an error.
    conflicts: tuple[str, ...] = ()

    def snapshot(self, **extra: Any) -> dict[str, Any]:
        """The immutable record M3-07 stores alongside a generated image.

        Everything needed to answer "why does this image look like this?"
        without re-deriving anything: the exact strings sent, the template that
        framed them, and whatever version ids the caller pins on top. Callers
        pass ``character_version_id`` and ``style_version_id`` here — this
        module never sees them, because it never sees a database.
        """
        return {
            "prompt": self.prompt,
            "negative_prompt": self.negative_prompt,
            "prompt_digest": self.digest,
            "template_ref": self.template_ref,
            "conflicts": list(self.conflicts),
            **extra,
        }


def build_image_prompt(
    *,
    scene: str,
    character: CharacterSpec | None = None,
    style_fields: Mapping[str, Any] | None = None,
    style: StyleSpec | None = None,
    scene_negative: str = "",
    correction: str = "",
) -> ImagePrompt:
    """Compose one image prompt.

    ``style`` and ``style_fields`` are alternatives: pass the compiled spec if
    the caller already has one (M3-07 compiles once per project and reuses it
    across twenty scenes), or the raw fields to have it compiled here.

    ``character`` is optional because M3-06's admission check — not this
    function — is what refuses to generate without approved branding. A builder
    that raised here would put the same rule in two places, and the two would
    disagree the first time one was relaxed.
    """
    spec = style if style is not None else compile_style_block(style_fields)

    scene_text = scene.strip()
    immutable_block = _traits_block(character.immutable if character else None)
    variable_block = _traits_inline(character.variable if character else None)

    prompt = render_block(
        IMAGE_TEMPLATE,
        style_block=spec.block,
        scene=scene_text,
        # The character block is composed from `character.immutable` and
        # nothing else. `scene_text` is not in scope for it. That absence is
        # layer 1 of the rule this module exists to enforce.
        character_block=(
            _named_block(character.name, immutable_block) if character else ""
        ),
        variable_block=variable_block,
        # From the *style*, but rendered here beside the character rather than
        # in the style block above. How a figure is constructed is a drawing
        # convention (hence a style field), but it has to be read late or the
        # scene's "a parent" outranks it — the same positional argument that
        # puts the character block last.
        cast_block=spec.cast,
        # **Last of all** (M3-10). A correction is about *this* attempt and
        # must outrank everything it corrects, including the character block —
        # not because traits stopped mattering, but because a reviewer saying
        # "the head is wrong again" is pointing at exactly those traits and
        # asking for them harder.
        correction_block=correction.strip(),
    )

    negative = _negative(spec, character, scene_negative)
    conflicts = _conflicts(scene_text, character)

    return ImagePrompt(
        prompt=prompt.text,
        negative_prompt=negative,
        digest=_digest(prompt.text, negative),
        template_ref=prompt.ref,
        conflicts=conflicts,
    )


def _named_block(name: str, traits: str) -> str:
    if not traits:
        return ""
    return f"{name.strip()} —\n{traits}" if name.strip() else traits


def _traits_block(traits: Mapping[str, Any] | None) -> str:
    """One trait per line, **sorted by key**.

    Sorted rather than in the mapping's own order: the same traits arriving
    from a jsonb round-trip, an editor form and a test literal must produce one
    string, or the digest changes for reasons that have nothing to do with the
    character.
    """
    if not traits:
        return ""
    lines = [
        f"- {_label(key)}: {value}"
        for key in sorted(traits)
        if (value := _flatten(traits[key]))
    ]
    return "\n".join(lines)


def _traits_inline(traits: Mapping[str, Any] | None) -> str:
    """Variable traits, comma-joined — they are hints, not a specification."""
    if not traits:
        return ""
    parts = [
        f"{_label(key)} {value}"
        for key in sorted(traits)
        if (value := _flatten(traits[key]))
    ]
    return ", ".join(parts)


def _negative(
    style: StyleSpec, character: CharacterSpec | None, scene_negative: str
) -> str:
    """Merge negative terms, deduplicated, most-authoritative first.

    Character prohibitions lead, then style, then the scene's own. Order
    matters because some providers weight a negative prompt positionally, and
    "never draw this character with a hat" outranks a scene's passing
    preference.
    """
    terms: dict[str, None] = {}
    for term in character.never if character else ():
        if cleaned := term.strip():
            terms.setdefault(cleaned, None)
    for term in style.avoid:
        terms.setdefault(term, None)
    for term in scene_negative.split(","):
        if cleaned := term.strip():
            terms.setdefault(cleaned, None)
    return ", ".join(terms)


def _conflicts(scene: str, character: CharacterSpec | None) -> tuple[str, ...]:
    """Immutable trait keys the scene text appears to talk about.

    Word-boundary matching on the trait *key*, which is a blunt instrument and
    is meant to be. Detecting that "she pushes her hair back" contradicts
    ``hair: none — a smooth pale dome`` needs a model, not a regex; what this
    can do cheaply is tell a reviewer where to look. Hence recorded, never
    enforced.
    """
    if not character or not character.immutable:
        return ()
    lowered = scene.lower()
    found = [
        key
        for key in sorted(character.immutable)
        if key.lower() not in _UNREVEALING
        and re.search(rf"\b{re.escape(key.lower().replace('_', ' '))}\b", lowered)
    ]
    return tuple(found)


def _digest(prompt: str, negative: str) -> str:
    """Fingerprint of what was actually sent.

    Both halves, because two generations with the same positive prompt and
    different negatives are different generations, and a digest that ignored
    the negative would claim they were the same.
    """
    blob = json.dumps({"p": prompt, "n": negative}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:8]


def _flatten(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list | tuple):
        return ", ".join(part for part in (_flatten(item) for item in value) if part)
    return str(value).strip()


def _label(key: str) -> str:
    return key.replace("_", " ").strip()
