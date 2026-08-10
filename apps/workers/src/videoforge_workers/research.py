"""``research.generate`` (M2-08) — the pipeline's root stage.

The first stage added after the shared runner existed, and the measure of
whether that runner was worth extracting: everything below the provider call is
one function call. If a new stage ever needs more than this, the runner is
wrong rather than the stage being special.

Research has no upstream. It reads the project's topic and nothing else, which
is what makes it the DAG's only root (``requires: []`` in
``templates/pipeline.yaml``).
"""

from __future__ import annotations

import logging

from videoforge_domain.duration import target_duration_ms
from videoforge_prompts import render
from videoforge_shared.tasks import RESEARCH_GENERATE
from videoforge_workers.skeleton import JobContext, videoforge_task
from videoforge_workers.stages import (
    complete_generation,
    llm_complete,
    load_artifact,
)

logger = logging.getLogger(__name__)

__all__ = ["RESEARCH_TEMPLATE", "generate_research", "research_body"]

RESEARCH_TEMPLATE = "research"

#: Notes, not prose. ``uncertainties`` is the field that earns its place: a
#: scriptwriter who knows a claim is shaky can work around it, and one who does
#: not will assert it on camera.
_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "key_facts": {"type": "array", "items": {"type": "string"}},
        "surprising_angle": {"type": "string"},
        "misconception": {"type": "string"},
        "uncertainties": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["summary", "key_facts", "surprising_angle"],
}


def research_body(ctx: JobContext) -> None:
    """Gather research for one project. Runs inside the skeleton's transaction."""
    artifact = load_artifact(ctx)
    project = ctx.uow.projects.get(artifact.project_id)
    if project is None:
        raise RuntimeError(f"project {artifact.project_id} vanished")

    target_ms = target_duration_ms(project.settings)
    prompt = render(
        RESEARCH_TEMPLATE,
        topic=project.topic,
        target_seconds=round(target_ms / 1000),
    )
    result = llm_complete(ctx, prompt, _RESPONSE_SCHEMA)

    content = result.parsed or {
        "summary": result.text,
        "key_facts": [],
        "surprising_angle": "",
    }
    complete_generation(
        ctx, artifact, content=content, result=result, prompt_ref=prompt.ref
    )


@videoforge_task(
    name=RESEARCH_GENERATE.name, queue=RESEARCH_GENERATE.queue, job_bearing=True
)
def generate_research(ctx: JobContext) -> None:
    research_body(ctx)
