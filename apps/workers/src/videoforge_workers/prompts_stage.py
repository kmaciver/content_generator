"""``prompts.generate`` (M2-12) — one job, N artifacts.

Every stage so far wrote one artifact version. This one writes a prompt
artifact **per scene** (§13: "batched: one job, N prompt artifacts"), and it is
the ticket that proves finding S1 was worth installing: the constraint

    UNIQUE (project_id, kind, scene_ref) NULLS NOT DISTINCT

is what makes twenty artifacts of kind ``prompt`` unambiguous. Without it,
"the prompt for scene 4" would be a query with an ordering guess.

**Why batched rather than fanned out into N jobs.** Image generation fans out
per scene because each call is slow, expensive and independently retryable —
losing one of twenty images should not re-run the other nineteen. Prompts are
cheap text calls against one already-loaded script; twenty jobs would mean
twenty transactions, twenty rows of overhead and a partially-prompted scene set
that no reviewer can act on. The unit a human reviews is the whole set.

The module is named ``prompts_stage`` rather than ``prompts`` so it cannot
shadow ``videoforge_prompts`` for a careless reader — the same reasoning as
``anthropic_adapter``.
"""

from __future__ import annotations

import logging
from typing import Any

from videoforge_persistence.models import Artifact
from videoforge_prompts import render, template_ref
from videoforge_providers.models import LLMResult
from videoforge_shared.enums import ArtifactKind, ArtifactState
from videoforge_shared.settings import load_worker_settings
from videoforge_shared.tasks import PROMPTS_GENERATE
from videoforge_workers.skeleton import JobContext, videoforge_task
from videoforge_workers.stages import complete_generation, llm_complete, load_artifact

logger = logging.getLogger(__name__)

__all__ = ["PROMPT_TEMPLATE", "generate_prompts", "prompts_body"]

PROMPT_TEMPLATE = "prompt"

_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "prompt_text": {"type": "string"},
        "negative_prompt": {"type": "string"},
    },
    "required": ["prompt_text"],
}


def prompts_body(ctx: JobContext) -> None:
    """Generate one prompt artifact per scene of the approved scene set."""
    trigger = load_artifact(ctx)
    project_id = trigger.project_id
    scenes = ctx.uow.scenes.for_approved_set(project_id)
    if not scenes:
        raise RuntimeError(f"no approved scene set for project {project_id}")

    render_settings = load_worker_settings().render

    manifest: list[dict[str, Any]] = []
    for scene in scenes:
        artifact = _prompt_artifact(ctx, project_id, scene.id)
        prompt = render(
            PROMPT_TEMPLATE,
            index=scene.index,
            total=len(scenes),
            visual_brief=scene.visual_brief,
            narration=scene.narration_text,
            # The frame's shape, from the render settings that will actually
            # encode it. A brief written without knowing the frame is tall
            # produces square compositions — a flat lay, a centred icon — and
            # the image model boxes those inside a drawn border rather than
            # invent content for the space left over (measured 2026-08-08).
            aspect=render_settings.aspect_ratio,
            orientation=render_settings.orientation,
        )
        result = llm_complete(ctx, prompt, _RESPONSE_SCHEMA)
        content: dict[str, Any] = result.parsed or {"prompt_text": result.text}
        content["scene_index"] = scene.index

        version = complete_generation(
            ctx, artifact, content=content, result=result, prompt_ref=prompt.ref
        )
        manifest.append(
            {
                "scene_index": scene.index,
                "scene_id": scene.id,
                "artifact_id": artifact.id,
                "version_id": version.id,
            }
        )

    # The job's own artifact — the project-wide `prompt` row that
    # `JobService.request` created — gets a **manifest** version.
    #
    # Without it that artifact stays GENERATING forever: nothing else ever
    # completes it, and phase derivation takes the *least advanced* artifact of
    # a kind, so the project would sit in SCENING for good with no error
    # anywhere. It also gives the reviewer the thing the module docstring says
    # they review: the whole set, not twenty separate decisions.
    #
    # A zero-usage result, deliberately. Every real call was already metered
    # against its own scene above, and re-recording one of them here would
    # double-count tokens in the row the S10 spend cap reads.
    complete_generation(
        ctx,
        trigger,
        content={"scene_count": len(scenes), "prompts": manifest},
        result=LLMResult(text="", provider_meta={"provider": "batch", "model": "none"}),
        prompt_ref=template_ref(PROMPT_TEMPLATE),
    )

    logger.info(
        "prompt fan-out complete",
        extra={"project_id": project_id, "scene_count": len(scenes)},
    )


def _prompt_artifact(ctx: JobContext, project_id: str, scene_id: str) -> Artifact:
    """The prompt artifact for one scene, created on first use.

    ``find`` before ``create`` because a regeneration targets scenes that
    already have artifacts — and because creating a duplicate would hit S1's
    constraint rather than producing a second one, which is the constraint
    doing its job but a poor way to discover it.

    The artifact starts GENERATING rather than PENDING: it is being generated
    right now, and ``complete_generation`` applies ``GENERATION_SUCCEEDED``,
    which the FSM only accepts from GENERATING.
    """
    artifact = ctx.uow.artifacts.find(project_id, ArtifactKind.PROMPT, scene_id)
    if artifact is None:
        artifact = ctx.uow.artifacts.create(
            project_id,
            ArtifactKind.PROMPT,
            scene_id,
            state=ArtifactState.GENERATING,
        )
        ctx.uow.flush()
    else:
        artifact.state = ArtifactState.GENERATING
    return artifact


@videoforge_task(
    name=PROMPTS_GENERATE.name, queue=PROMPTS_GENERATE.queue, job_bearing=True
)
def generate_prompts(ctx: JobContext) -> None:
    prompts_body(ctx)
