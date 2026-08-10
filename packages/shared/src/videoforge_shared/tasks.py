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
    "IMAGES_GENERATE",
    "VOICE_GENERATE",
    "PING",
    "PROMPTS_GENERATE",
    "RECONCILE_JOBS",
    "REFERENCES_GENERATE",
    "RESEARCH_GENERATE",
    "SCENES_GENERATE",
    "RENDER_GENERATE",
    "RENDER_HELLO",
    "SCRIPT_GENERATE",
    "STAGE_TASKS",
    "TIMELINE_COMPILE",
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

#: M3-07. On the ``image`` queue with ``references.generate``, sharing the
#: concurrency limit that stops a slow, expensive modality starving the cheap
#: ones (§14.1).
IMAGES_GENERATE = TaskSpec("image.generate", "image")

#: M3-12. **Not** per-scene, unlike images: B3 revised requires one synthesis
#: call for the whole script, because twenty sentences read in isolation
#: concatenate into a list of statements rather than a narration.
VOICE_GENERATE = TaskSpec("voice.generate", "voice")

#: M4-08. On its own queue rather than ``llm``: the compile itself is pure and
#: takes milliseconds, but it must not queue behind a two-minute script
#: generation to tell a waiting reviewer that a scene has no approved frame.
TIMELINE_COMPILE = TaskSpec("timeline.compile", "timeline")

#: M4-09. The ``render`` queue exists so a two-minute encode cannot occupy the
#: worker that a cheap stage is waiting on — the concurrency argument of §14.1,
#: at its most extreme.
RENDER_GENERATE = TaskSpec("render.generate", "render")

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
    ArtifactKind.IMAGE: IMAGES_GENERATE,
    ArtifactKind.VOICE: VOICE_GENERATE,
    ArtifactKind.TIMELINE: TIMELINE_COMPILE,
    ArtifactKind.RENDER: RENDER_GENERATE,
}

#: Series-scoped branding (M3-04b). On the ``image`` queue because it is image
#: generation and must share the concurrency limit that keeps a slow, expensive
#: modality from starving the cheap ones (§14.1).
REFERENCES_GENERATE = TaskSpec("references.generate", "image")

#: Infrastructure tasks.
DRAIN_OUTBOX = TaskSpec("outbox.drain", "events")
RECONCILE_JOBS = TaskSpec("jobs.reconcile", "events")

#: M0 leftovers, still the cheapest liveness probes an operator has.
RENDER_HELLO = TaskSpec("render.hello", "render")


def PING(queue: str) -> TaskSpec:  # noqa: N802 - reads as a constant at call sites
    """One ping task per queue (M0-08)."""
    return TaskSpec(f"ping.{queue}", queue)
