"""Versioned prompt templates (SADD §8, §10.3 rule 4).

Prompt text is a **behavioural input** to this system, not decoration. Change a
template and every artifact generated afterwards changes, so "why does this
script read like that?" is unanswerable unless each version records the exact
prompt that produced it. That is what ``artifact_version.prompt_template_ref``
is for — and until now it held the literal string ``"script/v1"``, a value that
stayed identical no matter how the prompt was edited.

**A reference pins content, not just a name.** A ref looks like::

    script@1+2f9c1ab4

``name@version`` is what a human reads; the trailing digest is the first eight
hex characters of the sha256 of the template source. Editing a template without
bumping its version therefore still changes the ref, and two artifacts made
from genuinely different prompts can never claim the same provenance. A bare
``script/v1`` makes the audit trail *look* complete while silently merging
every edit ever made.

**Rendering is pure.** Templates are files, loaded once and cached; rendering
takes a context and returns text. No provider, no clock, no database.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, StrictUndefined

__all__ = [
    "BLOCK_TEMPLATES",
    "TEMPLATES_DIR",
    "PromptTemplate",
    "RenderedBlock",
    "RenderedPrompt",
    "UnknownTemplateError",
    "available",
    "render",
    "render_block",
    "template_ref",
]

TEMPLATES_DIR = Path(__file__).parent / "templates"

#: Templates rendered by :func:`render_block` rather than :func:`render`.
#:
#: Chat prompts have a system/user split; an image prompt is one string,
#: because that is what a diffusion provider takes. Declaring which is which
#: here rather than sniffing for the separator means a template that is
#: *accidentally* missing its split is a failure rather than a reclassification
#: — the whole point of the invariant.
BLOCK_TEMPLATES: frozenset[str] = frozenset({"image"})

#: Separates the two halves of a template file. Everything before it is the
#: system prompt, everything after is the user turn — one file per prompt
#: rather than two, because a system prompt and the user turn it was written
#: against drift the moment they can be edited separately.
_SPLIT = "\n---\n"


class UnknownTemplateError(KeyError):
    """Raised at render time, naming what does exist.

    A typo'd template name is a stage that can never run, and the failure
    should say which names are valid rather than surfacing as a ``KeyError``
    carrying one word.
    """


@dataclass(frozen=True, slots=True)
class RenderedPrompt:
    """One prompt, ready to become ``LLMMessage``s, plus its provenance."""

    system: str
    user: str
    #: Goes straight into ``artifact_version.prompt_template_ref``.
    ref: str


@dataclass(frozen=True, slots=True)
class RenderedBlock:
    """A single-section template, rendered (M3-03).

    Image prompts have no system/user split — a diffusion provider takes one
    string. They still need provenance, so they are templates like everything
    else and carry the same content-pinned ``ref``.
    """

    text: str
    ref: str


@dataclass(frozen=True, slots=True)
class PromptTemplate:
    name: str
    version: int
    source: str

    @property
    def digest(self) -> str:
        """Content fingerprint.

        Short on purpose: this is read by humans in an audit trail, and eight
        hex characters distinguish every edit one project will ever make.
        """
        return hashlib.sha256(self.source.encode()).hexdigest()[:8]

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.version}+{self.digest}"


@lru_cache(maxsize=1)
def _environment() -> Environment:
    """``StrictUndefined`` is the point of this function.

    Jinja renders a missing variable as an empty string by default, so a
    template that lost its topic becomes "Write a script about: " and the model
    cheerfully invents one. The artifact then looks entirely normal and is
    about nothing in particular. An undefined variable has to be an error.

    ``autoescape`` stays off because the output is a prompt, not HTML —
    escaping quotes into ``&#34;`` would corrupt every instruction that
    contains one.
    """
    return Environment(
        undefined=StrictUndefined,
        autoescape=False,
        keep_trailing_newline=True,
    )


@lru_cache(maxsize=1)
def _templates() -> dict[str, PromptTemplate]:
    """Load every template once per process.

    The version is parsed from the filename (``script.v1.jinja``) rather than
    from front matter, so it is visible in a directory listing and in the
    header of every diff that touches the file.
    """
    found: dict[str, PromptTemplate] = {}
    for path in sorted(TEMPLATES_DIR.glob("*.v*.jinja")):
        name, raw_version = path.name.split(".")[:2]
        found[name] = PromptTemplate(
            name=name,
            version=int(raw_version.lstrip("v")),
            source=path.read_text(encoding="utf-8"),
        )
    return found


def available() -> tuple[str, ...]:
    return tuple(sorted(_templates()))


def template_ref(name: str) -> str:
    """The provenance string for ``name``, without rendering it."""
    return _get(name).ref


def render(name: str, /, **context: Any) -> RenderedPrompt:
    """Render ``name`` against ``context``.

    Raises on an unknown template or an undefined variable. Both are bugs that
    otherwise produce a plausible-looking prompt about nothing.
    """
    template = _get(name)
    text = _environment().from_string(template.source).render(**context)

    system, separator, user = text.partition(_SPLIT)
    if not separator or not user.strip():
        raise ValueError(
            f"template {name!r} has no user section; expected a {_SPLIT!r} separator"
        )
    return RenderedPrompt(system=system.strip(), user=user.strip(), ref=template.ref)


def render_block(name: str, /, **context: Any) -> RenderedBlock:
    """Render a template that has no system/user split.

    Deliberately a separate function rather than making the split optional in
    :func:`render`. The split is a *contract* for chat prompts — a template
    that lost its user section is a bug, and a lenient ``render`` would return
    an empty user turn and let the model answer a system prompt on its own.
    Two functions keep both contracts strict.
    """
    template = _get(name)
    text = _environment().from_string(template.source).render(**context)
    if _SPLIT in text:
        raise ValueError(
            f"template {name!r} contains a {_SPLIT!r} separator; it is a chat "
            "prompt and should be rendered with render(), not render_block()"
        )
    return RenderedBlock(text=text.strip(), ref=template.ref)


def _get(name: str) -> PromptTemplate:
    try:
        return _templates()[name]
    except KeyError:
        raise UnknownTemplateError(
            f"no prompt template {name!r}; available: {', '.join(available())}"
        ) from None
