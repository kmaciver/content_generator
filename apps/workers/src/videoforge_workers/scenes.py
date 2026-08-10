"""``scenes.generate`` (M2-11) — the first stage whose output is rows.

Everything before this produces text. A scene set produces a ``scene_set`` row
and N ``scene`` rows *in the same transaction as its artifact version*, which
is why ``complete_generation`` grew an ``after_version`` hook rather than the
stage writing them itself afterwards: rows that reference a version must not be
able to exist without it.

**The narration is copied verbatim.** The voice track is synthesised from the
concatenation of these strings (§13, finding B3 revised), and captions come
from the word timestamps of that synthesis. A scene whose narration was tidied
in passing desynchronises the captions from the audio, and nothing downstream
can detect it.

Duration validation is a **warning, not a rejection**. See ``_check_total``.
"""

from __future__ import annotations

import logging
from typing import Any

from videoforge_domain.duration import duration_tolerance_ms, target_duration_ms
from videoforge_persistence.models import ArtifactVersion, Scene, SceneSet
from videoforge_prompts import render
from videoforge_shared.enums import ArtifactKind, SceneKind
from videoforge_shared.ids import new_ulid
from videoforge_shared.tasks import SCENES_GENERATE
from videoforge_workers.skeleton import JobContext, videoforge_task
from videoforge_workers.stages import (
    complete_generation,
    llm_complete,
    load_artifact,
    require_approved_content,
)

logger = logging.getLogger(__name__)

__all__ = ["SCENES_TEMPLATE", "generate_scenes", "scenes_body"]

SCENES_TEMPLATE = "scenes"

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "narration_text": {"type": "string"},
                    "visual_brief": {"type": "string"},
                    "target_duration_ms": {"type": "integer"},
                    # M4-01. Optional in the schema, defaulted in `_normalise`:
                    # a model that omits it produces illustrations, which is
                    # the pre-M4 behaviour and the safe direction to fail.
                    "kind": {"type": "string", "enum": ["illustration", "card"]},
                    "card_text": {"type": "string"},
                },
                "required": [
                    "narration_text",
                    "visual_brief",
                    "target_duration_ms",
                ],
            },
        }
    },
    "required": ["scenes"],
}


def scenes_body(ctx: JobContext) -> None:
    """Break the approved script into scenes."""
    artifact = load_artifact(ctx)
    project = ctx.uow.projects.get(artifact.project_id)
    if project is None:
        raise RuntimeError(f"project {artifact.project_id} vanished")

    script = require_approved_content(ctx, project.id, ArtifactKind.SCRIPT)
    target_ms = target_duration_ms(project.settings)

    prompt = render(
        SCENES_TEMPLATE,
        title=str(script.get("title", project.topic)),
        script=str(script.get("script", "")),
        target_ms=target_ms,
        tolerance_ms=duration_tolerance_ms(target_ms),
    )
    result = llm_complete(ctx, prompt, _RESPONSE_SCHEMA)

    scenes = _normalise(result.parsed or {})
    if not scenes:
        # Not a warning. A scene set with no scenes cannot be reviewed, cannot
        # be illustrated, and would advance the project's phase to "awaiting
        # approval" of nothing.
        raise RuntimeError("scenes stage returned no scenes")

    _check_total(scenes, target_ms, project_id=project.id)

    script_version_id = _approved_script_version_id(ctx, project.id)

    def write_rows(inner: JobContext, version: ArtifactVersion) -> None:
        # The id is minted here rather than read back after the insert: the
        # scenes reference it, and a SELECT to recover an id we just chose is
        # a round-trip that can only ever return what we already knew.
        scene_set_id = new_ulid()
        inner.uow.session.add(
            SceneSet(
                id=scene_set_id,
                artifact_version_id=version.id,
                script_version_id=script_version_id,
            )
        )
        # Flushed before the scenes so the FK has something to point at.
        inner.uow.flush()
        for index, scene in enumerate(scenes, start=1):
            inner.uow.session.add(
                Scene(
                    id=new_ulid(),
                    scene_set_id=scene_set_id,
                    index=index,
                    narration_text=scene["narration_text"],
                    visual_brief=scene["visual_brief"],
                    target_duration_ms=scene["target_duration_ms"],
                    kind=SceneKind(scene["kind"]),
                    card_text=scene["card_text"],
                )
            )
        inner.uow.flush()

    complete_generation(
        ctx,
        artifact,
        # The version's inline content mirrors the rows. Redundant on purpose:
        # the review UI reads one artifact version like every other stage, and
        # the rows are what the image and voice stages join against.
        content={"scenes": scenes},
        result=result,
        prompt_ref=prompt.ref,
        after_version=write_rows,
    )


def _normalise(parsed: dict[str, Any]) -> list[dict[str, Any]]:
    """Coerce the model's scenes into exactly what the columns accept.

    Every field is required by the schema, so this is about *types* rather than
    presence: a duration arriving as ``"4000"`` would fail the integer column
    with a message about SQL rather than about the model's output.
    """
    scenes: list[dict[str, Any]] = []
    for raw in parsed.get("scenes") or []:
        if not isinstance(raw, dict):
            continue
        narration = str(raw.get("narration_text", "")).strip()
        brief = str(raw.get("visual_brief", "")).strip()
        try:
            duration = int(raw.get("target_duration_ms", 0))
        except (TypeError, ValueError):
            duration = 0
        if not narration or not brief or duration <= 0:
            # The CHECK constraint would reject these anyway; dropping them
            # here means the failure names the model's output rather than a
            # constraint the operator has never heard of.
            logger.warning("dropping malformed scene", extra={"scene": raw})
            continue
        kind, card_text = _kind_of(raw)
        scenes.append(
            {
                "narration_text": narration,
                "visual_brief": brief,
                "target_duration_ms": duration,
                "kind": kind.value,
                "card_text": card_text,
            }
        )
    return scenes


def _kind_of(raw: dict[str, Any]) -> tuple[SceneKind, str | None]:
    """Decide illustration-or-card, and refuse to write a contradiction.

    **Demoted rather than dropped** (M4-01). The two CHECK constraints on
    ``scene`` reject a card with no text and an illustration carrying card
    text, so a model that claims ``card`` without usable text would fail the
    whole job — throwing away nineteen good scenes over one bad field. A card
    that cannot be rendered is exactly an illustration, which is the pre-M4
    behaviour and costs one image rather than the run.

    ``card_text`` is truncated, not rejected, for the same reason: the column
    caps at 60 characters because that is what stays legible at card size, and
    a model that wrote 64 got the scene right and the brevity wrong.
    """
    declared = str(raw.get("kind") or SceneKind.ILLUSTRATION.value).strip().lower()
    text = str(raw.get("card_text") or "").strip()

    if declared != SceneKind.CARD.value:
        if text:
            # Not silent: the model said "illustration" and then wrote words to
            # put on a card. One of the two was a mistake and we cannot tell
            # which, so the field that survives is the one it was asked for.
            logger.warning(
                "discarding card_text on an illustration scene",
                extra={"card_text": text},
            )
        return SceneKind.ILLUSTRATION, None

    if not text:
        logger.warning("card scene arrived with no text; treating as illustration")
        return SceneKind.ILLUSTRATION, None

    return SceneKind.CARD, text[:60]


def _check_total(
    scenes: list[dict[str, Any]], target_ms: int, *, project_id: str
) -> None:
    """Warn when the durations do not add up. **Deliberately not a failure.**

    §13 says this stage "validates durations sum ≈ target length", and the
    temptation is to reject. But these are the model's *estimates* of speaking
    time; the authoritative durations arrive later, from the voice clip. Failing
    a job over an estimate would throw away a scene breakdown that is very
    likely fine, and hand the reviewer nothing to look at.

    A human is about to review this anyway (SADD §17). Their job is easier with
    the numbers in front of them than with an error message instead of them.
    """
    total = sum(int(s["target_duration_ms"]) for s in scenes)
    tolerance = duration_tolerance_ms(target_ms)
    if abs(total - target_ms) > tolerance:
        logger.warning(
            "scene durations are outside the target window",
            extra={
                "project_id": project_id,
                "scene_count": len(scenes),
                "total_ms": total,
                "target_ms": target_ms,
                "tolerance_ms": tolerance,
            },
        )


def _approved_script_version_id(ctx: JobContext, project_id: str) -> str:
    """Which script version these scenes came from — §10.3 rule 4's pin."""
    artifact = ctx.uow.artifacts.find(project_id, ArtifactKind.SCRIPT)
    if artifact is None:
        raise RuntimeError("script artifact vanished between checks")
    approved = ctx.uow.versions.approved_version(artifact.id)
    if approved is None:
        raise RuntimeError("script lost its approval between checks")
    return str(approved.artifact_version_id)


@videoforge_task(
    name=SCENES_GENERATE.name, queue=SCENES_GENERATE.queue, job_bearing=True
)
def generate_scenes(ctx: JobContext) -> None:
    scenes_body(ctx)
