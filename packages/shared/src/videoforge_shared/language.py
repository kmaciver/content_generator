"""What language the *content* is written and spoken in.

Distinct from anything about the UI: this is the language of the research, the
script, the scene narration, the burned-in captions and the Instagram copy —
the words an audience hears and reads. The application's own interface is
English and stays that way.

**One deployment, one content language.** A channel speaks to an audience, and
an audience has a language; mixing two inside one deployment would mean a
per-project selector, a per-series voice, and a reviewer who has to check which
they are looking at. When a second language is genuinely wanted, the honest
shape is a second deployment or a series-scoped override — not a field nobody
notices until a Portuguese script comes back in English.

**The image prompt is deliberately exempt.** Diffusion models are trained
overwhelmingly on English captions, and prompting them in Portuguese measurably
degrades what comes back. Image prompts are never seen by an audience, so they
stay English regardless of this setting — see ``prompt.v1.jinja``, which is
explicit about receiving non-English input and answering in English anyway.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["Language", "UnknownLanguageError", "VERIFIED", "resolve"]

#: BCP-47 shape: a 2–3 letter primary subtag, then optional subtags.
#:
#: Validated rather than trusted because the two consumers fail very
#: differently on a typo. An LLM told to "write in pt_BR" writes Portuguese
#: anyway and nobody notices the setting is malformed; ElevenLabs rejects the
#: same string outright. Failing at load time means one clear error instead of
#: a working script stage and a broken voice stage.
_TAG = re.compile(r"^[a-z]{2,3}(-[A-Za-z0-9]{2,8})*$")


class UnknownLanguageError(ValueError):
    """The configured content language is not a well-formed BCP-47 tag."""


@dataclass(frozen=True, slots=True)
class Language:
    """A content language, in the three forms its consumers need."""

    #: BCP-47, as configured: ``pt-BR``.
    tag: str
    #: What the model is *told*: "Brazilian Portuguese".
    #:
    #: A name, not the tag, and the difference is not cosmetic. Asked to write
    #: in "pt-BR" a model complies but drifts toward European Portuguese, which
    #: a Brazilian audience hears immediately — different pronouns, different
    #: verb forms, a different register.
    name: str
    #: ISO-639-1 primary subtag, which is what ElevenLabs' ``language_code``
    #: takes: ``pt``. Regional variety is carried by the *voice*, not by this.
    voice_code: str


#: Languages whose display name has been chosen deliberately.
#:
#: Not a whitelist — any well-formed tag works, deriving its name from itself.
#: This table exists for the cases where the tag alone would mislead a model,
#: which is every case where a region changes the register rather than just the
#: spelling.
VERIFIED: dict[str, str] = {
    "en": "English",
    "en-US": "American English",
    "en-GB": "British English",
    "pt-BR": "Brazilian Portuguese",
    "pt-PT": "European Portuguese",
    "es": "Spanish",
    "es-419": "Latin American Spanish",
    "fr": "French",
    "de": "German",
    "it": "Italian",
    "ja": "Japanese",
}


def resolve(tag: str) -> Language:
    """Parse a configured tag, or raise naming what is wrong with it.

    An unlisted-but-valid tag is fine and resolves to itself: refusing to run
    in Dutch because nobody has written "Dutch" into a table would be a
    gratuitous limit. What is refused is a tag that is not a tag.
    """
    cleaned = tag.strip()
    if not _TAG.match(cleaned):
        raise UnknownLanguageError(
            f"CONTENT_LANGUAGE={tag!r} is not a BCP-47 tag. Expected a form "
            f"like 'pt-BR', 'en' or 'es-419'; known names: "
            f"{', '.join(sorted(VERIFIED))}"
        )
    return Language(
        tag=cleaned,
        name=VERIFIED.get(cleaned, cleaned),
        voice_code=cleaned.split("-")[0].lower(),
    )
