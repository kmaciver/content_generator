"""The rules for Instagram post copy — the caption artifact's content shape.

Shared by the caption worker (which *trims* what a model returned) and the
review service (which *validates* what a human typed). The two apply the same
limits with opposite policies on purpose: a model's overrun is cheap to fix
quietly, while a person who edits the copy wants to approve exactly the words
they wrote — so a human edit that breaks a limit is refused with a reason, never
silently rewritten.
"""

from __future__ import annotations

import re
from typing import Any

__all__ = [
    "MAX_CAPTION_CHARACTERS",
    "MAX_HASHTAGS",
    "MAX_HOOK_CHARACTERS",
    "MAX_HUMAN_HOOK_CHARACTERS",
    "MAX_HUMAN_HASHTAGS",
    "PREVIEW_CHARACTERS",
    "TAG_DISALLOWED",
    "InvalidCopyError",
    "preview_of",
    "validate_edited_caption",
]

#: Instagram's hard caption limit; a longer caption is rejected on posting.
MAX_CAPTION_CHARACTERS = 2_200

#: What shows before the "more" link.
PREVIEW_CHARACTERS = 125

#: The ceiling applied to *generated* tags — a judgement about spam signals,
#: not a platform limit. See ``videoforge_workers.caption``.
MAX_HASHTAGS = 8

#: Instagram's own limit, applied to a human edit. A reviewer who deliberately
#: adds a ninth tag has made a choice; only the platform's refusal is a reason
#: to stop them.
MAX_HUMAN_HASHTAGS = 30

#: The cover line. 40 characters is the width the cover renderer can set large
#: enough to read on a phone, so a *generated* hook is trimmed to it.
MAX_HOOK_CHARACTERS = 40

#: The ceiling for a hook a *reviewer* types. Not a rendering limit — the cover
#: wraps and shrinks the font to fit. Measured on a 1080x1920 cover: 40
#: characters set at 72px on 2 lines, ~95 at 60px on 4 lines (still inside the
#: text box), and ~140 overflows it. 100 keeps a margin below that. Between 40
#: and this the editor warns rather than refuses: smaller text is a choice a
#: reviewer is entitled to make.
MAX_HUMAN_HOOK_CHARACTERS = 100

#: Anything that is not a letter, digit or underscore ends a hashtag on
#: Instagram. Unicode-aware, so ``#educação`` is a valid tag.
TAG_DISALLOWED = re.compile(r"\W", re.UNICODE)


class InvalidCopyError(ValueError):
    """A human edit the platform would not accept as written."""


def preview_of(caption: str) -> str:
    return caption[:PREVIEW_CHARACTERS]


def validate_edited_caption(content: dict[str, Any]) -> dict[str, Any]:
    """A reviewer's caption edit, checked and with derived fields recomputed.

    Returns the content to store. The only changes made to what was typed are
    ones that cannot alter meaning: surrounding whitespace, a leading ``#`` on
    a tag (stored bare, the packager adds it) and an exact duplicate tag.
    Casing is kept — ``#EducacaoFinanceira`` is a legitimate, more readable
    spelling of the same tag. ``preview`` is always recomputed: it is derived
    from the caption, and storing the old one is how an edit came to look as if
    it had not saved.
    """
    caption = str(content.get("caption") or "").strip()
    if not caption:
        raise InvalidCopyError("the caption cannot be empty")
    if len(caption) > MAX_CAPTION_CHARACTERS:
        raise InvalidCopyError(
            f"the caption is {len(caption)} characters; Instagram allows "
            f"{MAX_CAPTION_CHARACTERS}"
        )

    hook = str(content.get("hook") or "").strip()
    if len(hook) > MAX_HUMAN_HOOK_CHARACTERS:
        raise InvalidCopyError(
            f"the cover hook is {len(hook)} characters; the cover fits "
            f"{MAX_HUMAN_HOOK_CHARACTERS}"
        )

    raw_tags = content.get("hashtags") or []
    if not isinstance(raw_tags, list):
        raise InvalidCopyError("hashtags must be a list")
    tags: list[str] = []
    seen: set[str] = set()
    for entry in raw_tags:
        tag = str(entry).strip().lstrip("#")
        if not tag:
            continue
        if TAG_DISALLOWED.search(tag):
            raise InvalidCopyError(
                f"#{tag} contains characters Instagram ends a tag at; use only "
                "letters, digits and underscores"
            )
        if tag.lower() in seen:
            continue
        seen.add(tag.lower())
        tags.append(tag)
    if len(tags) > MAX_HUMAN_HASHTAGS:
        raise InvalidCopyError(
            f"{len(tags)} hashtags; Instagram allows {MAX_HUMAN_HASHTAGS}"
        )

    return {
        **content,
        "hook": hook,
        "caption": caption,
        "hashtags": tags,
        "preview": preview_of(caption),
    }
