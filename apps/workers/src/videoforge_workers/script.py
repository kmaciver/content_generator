"""``script.generate`` — the first real stage, now with an upstream (M2-09).

M1 built this against the project topic alone, because research did not exist.
§13 always said otherwise: script "consumes approved research version id from
job input_snapshot". That is now true, and it is the change worth understanding
here — a working, tested stage gained a dependency.

**The research is read at execution time, not pinned at enqueue.** The DAG
(M2-02) will not dispatch this stage until research is APPROVED, and approving
a *new* research version marks the script stale (M2-04) rather than mutating a
running job's inputs. Reading it here through the status view rather than
through ``active_pointers`` keeps the write path off the cache (B1).

Everything after the provider call lives in ``stages.py`` — see that module for
why.
"""

from __future__ import annotations

import logging
from typing import Any

from videoforge_domain.duration import target_duration_ms
from videoforge_prompts import render
from videoforge_shared.enums import ArtifactKind
from videoforge_shared.tasks import SCRIPT_GENERATE
from videoforge_workers.skeleton import JobContext, videoforge_task
from videoforge_workers.stages import (
    complete_generation,
    llm_complete,
    load_artifact,
    require_approved_content,
)

logger = logging.getLogger(__name__)

__all__ = ["SCRIPT_TEMPLATE", "generate_script", "script_body"]

#: The template this stage renders. Its *ref* — name, version and a digest of
#: the source — is pinned onto every version produced (§10.3 rule 4).
SCRIPT_TEMPLATE = "script"

#: JSON mode. Asking for a structured object rather than parsing prose is what
#: keeps the scenes stage from having to guess where the title ends.
_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "script": {"type": "string"}},
    "required": ["title", "script"],
}


def script_body(ctx: JobContext) -> None:
    """Generate one script version. Runs inside the skeleton's transaction."""
    artifact = load_artifact(ctx)
    project = ctx.uow.projects.get(artifact.project_id)
    if project is None:
        raise RuntimeError(f"project {artifact.project_id} vanished")

    research = require_approved_content(ctx, project.id, ArtifactKind.RESEARCH)
    target_ms = target_duration_ms(project.settings)

    prompt = render(
        SCRIPT_TEMPLATE,
        topic=project.topic,
        target_seconds=round(target_ms / 1000),
        research=_as_notes(research),
    )
    result = llm_complete(ctx, prompt, _RESPONSE_SCHEMA)

    content = result.parsed or {"title": project.topic, "script": result.text}
    complete_generation(
        ctx, artifact, content=content, result=result, prompt_ref=prompt.ref
    )


def _as_notes(research: dict[str, Any]) -> str:
    """Flatten the research artifact into prompt text.

    Done here rather than in the template because the shape belongs to the
    research stage's schema: if that schema grows a field, this is where the
    decision to include it should be made and reviewed, not buried in Jinja
    where a missing key would render as an empty line.
    """
    lines: list[str] = []
    if summary := research.get("summary"):
        lines.append(str(summary))

    # Type-checked before iterating, not inside the comprehension. A model that
    # returns a *string* where the schema asked for an array would otherwise be
    # iterated character by character — producing a bullet per letter, which
    # looks like a prompt-engineering problem rather than a parsing one.
    facts = research.get("key_facts")
    if isinstance(facts, list):
        lines.extend(f"- {fact}" for fact in facts)

    if angle := research.get("surprising_angle"):
        lines.append(f"Most surprising: {angle}")
    if misconception := research.get("misconception"):
        lines.append(f"Common misconception: {misconception}")

    uncertain = research.get("uncertainties")
    if isinstance(uncertain, list) and uncertain:
        # Carried through deliberately. A scriptwriter told a claim is shaky
        # can work around it; one who is not will assert it on camera.
        lines.append("Treat as uncertain: " + "; ".join(str(u) for u in uncertain))
    return "\n".join(lines)


@videoforge_task(
    name=SCRIPT_GENERATE.name, queue=SCRIPT_GENERATE.queue, job_bearing=True
)
def generate_script(ctx: JobContext) -> None:
    """Celery entry point. All the work is in :func:`script_body`, which the
    tests call directly — a stage's logic should be testable without a broker."""
    script_body(ctx)
