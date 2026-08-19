"""``caption.generate`` — the Instagram post copy (M5-01).

The last text the pipeline writes, and the only text a viewer reads outside the
video. F10 puts caption text and hashtags in the publishing package; this is
where they come from.

**A stage, not a step inside the packager.** Writing this on the way past while
assembling the zip would make the review unit a zip file — which nobody can
usefully review — and would make "reword that opening line" mean rebuilding the
archive. It is also the artifact most likely to be edited by hand, because it
is what actually gets published.

**Instagram, singly.** The SADD names Reels, TikTok and Shorts together, and v1
targets Instagram: one caption, one hashtag set, one cover line. The shape below
is structured rather than a blob of text so a per-platform formatter can be
added later without regenerating anybody's copy — N2 already reserves
``PublishingProvider`` as the seam for that.

**The limits are enforced here, not asked for in the prompt.** A model told
"under 2,200 characters" is usually under 2,200 characters, which is not the
same as always. Every bound below is applied to what came back, and each one is
a *product* claim with a reason attached — see the constants.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from videoforge_prompts import render
from videoforge_shared.enums import ArtifactKind
from videoforge_shared.tasks import CAPTION_GENERATE
from videoforge_workers.skeleton import JobContext, videoforge_task
from videoforge_workers.stages import (
    complete_generation,
    llm_complete,
    load_artifact,
    require_approved_content,
)

logger = logging.getLogger(__name__)

__all__ = ["CAPTION_TEMPLATE", "caption_body", "generate_caption", "normalise"]

CAPTION_TEMPLATE = "caption"

#: Instagram's hard caption limit. A caption over it is rejected on posting, so
#: this is the one bound here that is a platform fact rather than a judgement.
MAX_CAPTION_CHARACTERS = 2_200

#: What shows before the "more" link. Not enforced — truncating here would cut
#: a sentence in half — but the prompt is written around it and the stage logs
#: when the first sentence overruns, because that is a copy problem a reviewer
#: should see rather than a validation failure.
PREVIEW_CHARACTERS = 125

#: Instagram permits 30. Eight is the ceiling, and the reason is not the limit:
#: past roughly ten, tags stop being discovery and start being a signal
#: platforms read as spam. Extra tags are dropped rather than failing the
#: stage — the copy is fine, the model was just enthusiastic.
MAX_HASHTAGS = 8

#: The cover line (M5-02 typesets it). Six words at thumbnail size is already
#: optimistic; 40 characters is the width the card renderer can set large
#: enough to read on a phone.
MAX_HOOK_CHARACTERS = 40

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "hook": {"type": "string"},
        "caption": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["hook", "caption", "hashtags"],
}

#: What survives in a hashtag. Instagram allows letters, digits and underscore;
#: everything else silently ends the tag, so ``#dental-care`` is really
#: ``#dental``. Stripped here so the stored tag is the tag that will exist.
_TAG_ALLOWED = re.compile(r"[^a-z0-9_]")


def caption_body(ctx: JobContext) -> None:
    """Generate one caption version. Runs inside the skeleton's transaction."""
    artifact = load_artifact(ctx)
    project = ctx.uow.projects.get(artifact.project_id)
    if project is None:
        raise RuntimeError(f"project {artifact.project_id} vanished")

    # The script, not the render. The copy is about what the video *says*, and
    # the render is a dependency for ordering reasons the pipeline file
    # explains — not because the pixels are an input here.
    script = require_approved_content(ctx, project.id, ArtifactKind.SCRIPT)

    prompt = render(
        CAPTION_TEMPLATE,
        topic=project.topic,
        title=script.get("title") or "",
        script=str(script.get("script") or ""),
    )
    result = llm_complete(ctx, prompt, _RESPONSE_SCHEMA)

    content = normalise(result.parsed or {}, fallback_hook=project.topic)
    complete_generation(
        ctx, artifact, content=content, result=result, prompt_ref=prompt.ref
    )

    logger.info(
        "caption generated",
        extra={
            "project_id": project.id,
            "caption_characters": len(content["caption"]),
            "hashtags": len(content["hashtags"]),
        },
    )


def normalise(raw: dict[str, Any], *, fallback_hook: str) -> dict[str, Any]:
    """Whatever the model returned, reduced to what Instagram accepts.

    **Trims rather than raises, with one exception.** A caption forty characters
    over the limit is good copy and a bad count; failing the stage would throw
    away a completion that has already been paid for, and the reviewer who
    would have fixed it in five seconds never sees it. An *empty* caption is
    different — there is nothing to review and nothing to publish — so that one
    fails loudly.
    """
    caption = str(raw.get("caption") or "").strip()
    if not caption:
        raise RuntimeError(
            "the caption stage returned no caption text; there is nothing to "
            "review and nothing to publish"
        )
    if len(caption) > MAX_CAPTION_CHARACTERS:
        logger.warning(
            "caption over Instagram's limit; trimming",
            extra={"characters": len(caption), "limit": MAX_CAPTION_CHARACTERS},
        )
        caption = _trim_to_sentence(caption, MAX_CAPTION_CHARACTERS)

    hook = str(raw.get("hook") or "").strip() or fallback_hook
    if len(hook) > MAX_HOOK_CHARACTERS:
        # Cut on a word, never mid-syllable: this is the line that goes on the
        # cover, and "The surprising reason yo" is worse than a shorter hook.
        hook = _trim_to_word(hook, MAX_HOOK_CHARACTERS)

    return {
        "hook": hook,
        "caption": caption,
        "hashtags": _hashtags(raw.get("hashtags")),
        # Stored so the review screen can show what a scroller sees before the
        # "more" link, without reimplementing the rule in TypeScript.
        "preview": caption[:PREVIEW_CHARACTERS],
    }


def _hashtags(raw: Any) -> list[str]:
    """Clean, de-duplicate and cap. Stored **without** the leading ``#``.

    The packager adds it. Storing tags bare means the value is the thing a
    person would type into a search box, and it makes de-duplication work when
    a model returns ``#Budget`` and ``budget`` in one list — which it does.
    """
    if not isinstance(raw, list):
        return []

    tags: list[str] = []
    for entry in raw:
        tag = _TAG_ALLOWED.sub("", str(entry).strip().lstrip("#").lower())
        if tag and tag not in tags:
            tags.append(tag)
    if len(tags) > MAX_HASHTAGS:
        logger.info(
            "dropping hashtags past the cap",
            extra={"returned": len(tags), "kept": MAX_HASHTAGS},
        )
    return tags[:MAX_HASHTAGS]


def _trim_to_sentence(text: str, limit: int) -> str:
    """Cut at the last sentence end inside ``limit``, or the last word.

    A caption that ends mid-thought reads as broken; one that ends a paragraph
    early reads as edited.
    """
    window = text[:limit]
    for ending in (". ", "! ", "? ", ".\n", "!\n", "?\n"):
        cut = window.rfind(ending)
        if cut > limit // 2:
            return window[: cut + 1].strip()
    return _trim_to_word(window, limit)


def _trim_to_word(text: str, limit: int) -> str:
    window = text[:limit]
    cut = window.rfind(" ")
    return (window[:cut] if cut > 0 else window).strip()


@videoforge_task(
    name=CAPTION_GENERATE.name, queue=CAPTION_GENERATE.queue, job_bearing=True
)
def generate_caption(ctx: JobContext) -> None:
    """Celery entry point. The work is in :func:`caption_body`, which the tests
    call directly — a stage's logic should be testable without a broker."""
    caption_body(ctx)
