"""The task registry: names and queues, and nothing else.

This exists to solve one problem. The API creates jobs and must publish them
to the broker, but **the backend may never import the workers** (SADD §8) —
so it cannot reference the task functions it needs to enqueue. Celery's
``send_task`` publishes by *name*, which turns the problem into "where does
the name live so both sides agree on it".

Here. A plain dataclass with no Celery import, so ``videoforge_shared`` stays
dependency-free and both apps can read it. The producer sends
``SCRIPT_GENERATE.name`` to ``SCRIPT_GENERATE.queue``; the worker registers
the same constant through the task decorator. A typo becomes an import error
rather than a message published to a queue nobody consumes — which is the
failure mode the mandatory ``queue`` argument was already guarding against,
extended to the producing side.
"""

from __future__ import annotations

from dataclasses import dataclass

from videoforge_shared.enums import ArtifactKind

__all__ = [
    "DRAIN_OUTBOX",
    "PING",
    "PROMPTS_GENERATE",
    "RECONCILE_JOBS",
    "RESEARCH_GENERATE",
    "SCENES_GENERATE",
    "RENDER_HELLO",
    "SCRIPT_GENERATE",
    "STAGE_TASKS",
    "TaskSpec",
]


@dataclass(frozen=True, slots=True)
class TaskSpec:
    """A task's name and the queue it belongs on — inseparable by design.

    Passing these around as one value is what stops a caller supplying the
    right name with the wrong queue, which routes work to a consumer that
    never picks it up and fails silently.
    """

    name: str
    queue: str


#: Stage tasks. Queues mirror `templates/pipeline.yaml`, and a test asserts
#: they agree — a stage routed to a queue no worker consumes is a job that
#: sits in the broker forever with no error anywhere.
RESEARCH_GENERATE = TaskSpec("research.generate", "llm")
SCRIPT_GENERATE = TaskSpec("script.generate", "llm")
SCENES_GENERATE = TaskSpec("scenes.generate", "llm")
PROMPTS_GENERATE = TaskSpec("prompts.generate", "llm")

#: Which task produces which artifact kind.
#:
#: Here rather than in the API because two callers need it and neither may own
#: it: the endpoint that dispatches a stage, and the DTO that tells the UI
#: whether a stage is implemented yet. A dict rather than a naming convention
#: so an unimplemented stage is a 400 with a list, not a message published to a
#: queue nothing consumes. M3+ fill in the rest.
STAGE_TASKS: dict[ArtifactKind, TaskSpec] = {
    ArtifactKind.RESEARCH: RESEARCH_GENERATE,
    ArtifactKind.SCRIPT: SCRIPT_GENERATE,
    ArtifactKind.SCENE_SET: SCENES_GENERATE,
    ArtifactKind.PROMPT: PROMPTS_GENERATE,
}

#: Infrastructure tasks.
DRAIN_OUTBOX = TaskSpec("outbox.drain", "events")
RECONCILE_JOBS = TaskSpec("jobs.reconcile", "events")

#: M0 leftovers, still the cheapest liveness probes an operator has.
RENDER_HELLO = TaskSpec("render.hello", "render")


def PING(queue: str) -> TaskSpec:  # noqa: N802 - reads as a constant at call sites
    """One ping task per queue (M0-08)."""
    return TaskSpec(f"ping.{queue}", queue)
