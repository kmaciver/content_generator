"""``voice_direction.generate`` (ADR-019) — suggested emotion tags.

One completion over the whole approved narration. The model returns each
scene's narration with tags inserted; :func:`parse_tagged_narration` turns that
back into word positions and refuses any scene whose *words* changed. A refused
scene is kept untagged rather than failing the stage: the reviewer can tag it by
hand, and the completion has already been paid for.

The suggestion is a starting point, not a decision — it lands in review like
every other stage, and the voice stage reads only an *approved* direction.
"""

from __future__ import annotations

import logging

from videoforge_domain.voice_direction import (
    AUDIO_TAGS,
    MAX_GENERATED_TAGS_PER_SCENE,
    DirectedScene,
    SceneText,
    direction_content,
    parse_tagged_narration,
)
from videoforge_prompts import render
from videoforge_shared.enums import ArtifactKind
from videoforge_shared.tasks import VOICE_DIRECTION_GENERATE
from videoforge_workers.skeleton import JobContext, videoforge_task
from videoforge_workers.stages import (
    complete_generation,
    content_language,
    llm_complete,
    load_artifact,
)

logger = logging.getLogger(__name__)

__all__ = [
    "VOICE_DIRECTION_TEMPLATE",
    "generate_voice_direction",
    "voice_direction_body",
]

VOICE_DIRECTION_TEMPLATE = "voice_direction"

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "scenes": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "scene_index": {"type": "integer"},
                    "narration": {"type": "string"},
                },
                "required": ["scene_index", "narration"],
            },
        }
    },
    "required": ["scenes"],
}


def voice_direction_body(ctx: JobContext) -> None:
    artifact = load_artifact(ctx)
    project = ctx.uow.projects.get(artifact.project_id)
    if project is None:
        raise RuntimeError(f"project {artifact.project_id} vanished")

    scenes = [
        SceneText(scene.id, scene.index, scene.narration_text.strip())
        for scene in ctx.uow.scenes.for_approved_set(project.id)
    ]
    if not scenes:
        raise RuntimeError(f"no approved scene set for project {project.id}")

    prompt = render(
        VOICE_DIRECTION_TEMPLATE,
        topic=project.topic,
        language=content_language().name,
        tags=AUDIO_TAGS,
        max_per_scene=MAX_GENERATED_TAGS_PER_SCENE,
        scenes=[{"index": s.scene_index, "narration": s.narration} for s in scenes],
    )
    result = llm_complete(ctx, prompt, _RESPONSE_SCHEMA)

    suggested = _by_index((result.parsed or {}).get("scenes"))
    directed: list[DirectedScene] = []
    refused: list[int] = []
    for scene in scenes:
        tagged = suggested.get(scene.scene_index)
        tags = parse_tagged_narration(scene.narration, tagged) if tagged else ()
        if tags is None:
            refused.append(scene.scene_index)
            tags = ()
        directed.append(DirectedScene(scene, tags[:MAX_GENERATED_TAGS_PER_SCENE]))

    if refused:
        # Not an error: the scenes are kept untagged and the reviewer can tag
        # them by hand. Logged because a model that rewrites most scenes is a
        # prompt problem worth knowing about.
        logger.warning(
            "voice direction changed the words of some scenes; left untagged",
            extra={"project_id": project.id, "scenes": refused},
        )

    scene_set = ctx.uow.artifacts.find(project.id, ArtifactKind.SCENE_SET)
    approved = ctx.uow.versions.approved_version(scene_set.id) if scene_set else None
    content = direction_content(
        directed, approved.artifact_version_id if approved else None
    )
    content["untagged_by_mismatch"] = refused
    complete_generation(
        ctx, artifact, content=content, result=result, prompt_ref=prompt.ref
    )

    logger.info(
        "voice direction generated",
        extra={"project_id": project.id, "tags": content["tag_count"]},
    )


def _by_index(raw: object) -> dict[int, str]:
    """The model's scenes keyed by index. A missing or malformed entry simply
    leaves that scene untagged."""
    if not isinstance(raw, list):
        return {}
    found: dict[int, str] = {}
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        index, text = entry.get("scene_index"), entry.get("narration")
        if isinstance(index, int) and isinstance(text, str):
            found.setdefault(index, text)
    return found


@videoforge_task(
    name=VOICE_DIRECTION_GENERATE.name,
    queue=VOICE_DIRECTION_GENERATE.queue,
    job_bearing=True,
)
def generate_voice_direction(ctx: JobContext) -> None:
    voice_direction_body(ctx)
