"""``script.generate`` — the first real stage, and the template for the rest.

Every later stage (research, scenes, prompts, images, voice, timeline, render)
is this shape with a different provider call in the middle. What it
demonstrates, and what M2 onward must not deviate from:

* the job guard is the skeleton's, not the task's;
* **one transaction** holds the artifact version, the state transition, the
  audit event, the outbox event and the provider usage row (§10.3 rule 6);
* the provider is reached through the registry, never constructed here;
* every version records what produced it — provider, model, prompt reference —
  so the reproducibility chain of §10.3 rule 4 holds by construction.

The task body writes no ``generation_job`` status at all. That belongs to the
skeleton, and a stage task that touched it would be able to mark itself
succeeded without producing anything.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache

from videoforge_domain.approval_policy import ApprovalPolicy
from videoforge_domain.artifact_lifecycle import ArtifactEvent, apply_event

from videoforge_persistence.models import Artifact
from videoforge_providers.models import LLMMessage, LLMRequest
from videoforge_providers.protocols import LLMProvider
from videoforge_providers.registry import build_llm_provider
from videoforge_shared.enums import (
    ArtifactKind,
    ArtifactState,
    ReviewDecisionKind,
    SubjectType,
    VersionOrigin,
)
from videoforge_shared.hashing import sha256_bytes
from videoforge_shared.settings import load_worker_settings
from videoforge_shared.tasks import SCRIPT_GENERATE
from videoforge_workers.skeleton import JobContext, videoforge_task

logger = logging.getLogger(__name__)

__all__ = ["SCRIPT_PROMPT_REF", "generate_script", "script_body"]

#: Pinned onto every version it produces (§10.3 rule 4). A literal until
#: ``packages/prompts`` owns real templates in M2; the *column* is populated
#: from M1 so that no version ever exists without provenance.
SCRIPT_PROMPT_REF = "script/v1"

_SYSTEM_PROMPT = (
    "You write short-form educational video scripts: 45-60 seconds, spoken "
    "aloud, no headings or stage directions. Open with a hook, build in clear "
    "beats, and close with the payoff."
)

#: JSON mode. Asking for a structured object rather than parsing prose is what
#: keeps the scene splitter (M2) from having to guess where the title ends.
_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "script": {"type": "string"}},
    "required": ["title", "script"],
}


@lru_cache(maxsize=1)
def _provider() -> LLMProvider:
    """The configured LLM provider, built once per worker process.

    ``load_worker_settings`` — not ``get_app_settings`` — because only
    ``WorkerSettings`` carries the provider plane. That asymmetry *is* the NF8
    boundary: the API cannot build a provider because it cannot construct the
    settings a provider needs.
    """
    settings = load_worker_settings()
    return build_llm_provider(settings.providers, settings.provider_keys)


def script_body(ctx: JobContext) -> None:
    """Generate one script version. Runs inside the skeleton's transaction."""
    artifact_id = str(ctx.input["artifact_id"])
    artifact = ctx.uow.artifacts.get(artifact_id)
    if artifact is None:
        raise RuntimeError(f"artifact {artifact_id} vanished before generation")

    project = ctx.uow.projects.get(artifact.project_id)
    if project is None:
        raise RuntimeError(f"project {artifact.project_id} vanished")

    result = _provider().complete(
        LLMRequest(
            messages=(
                LLMMessage(role="system", content=_SYSTEM_PROMPT),
                LLMMessage(
                    role="user",
                    content=(
                        "Write a short educational video script about: "
                        f"{project.topic}"
                    ),
                ),
            ),
            response_schema=_RESPONSE_SCHEMA,
        )
    )

    content = result.parsed or {"title": project.topic, "script": result.text}
    # Hash the canonical serialisation, not the dict: key order would otherwise
    # change the hash for identical content, and the hash is what the timeline's
    # input_snapshot pins for reproducibility.
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))

    version = ctx.uow.versions.add_version(
        artifact,
        origin=VersionOrigin.GENERATED,
        content_hash=sha256_bytes(canonical.encode()),
        # Inline: a script is a few kilobytes, and a round-trip to object
        # storage for that would buy nothing. Media artifacts (M3) use
        # storage_key instead — the CHECK constraint enforces exactly one.
        inline_content=content,
        generation_job_id=ctx.job.id,
        prompt_template_ref=SCRIPT_PROMPT_REF,
        provider_ref=result.provider_meta.get("provider"),
        meta={
            "model": result.provider_meta.get("model"),
            "provider_meta": result.provider_meta,
            "latency_ms": result.latency_ms,
        },
    )

    ctx.uow.usage.record(
        job_id=ctx.job.id,
        provider=str(result.provider_meta.get("provider", "unknown")),
        model=str(result.provider_meta.get("model", "unknown")),
        operation="llm.complete",
        latency_ms=result.latency_ms,
        unit_cost_estimate=result.usage.unit_cost_estimate,
        input_tokens=result.usage.input_tokens,
        output_tokens=result.usage.output_tokens,
        raw_meta=result.provider_meta,
    )

    transition = apply_event(
        ArtifactState(artifact.state), ArtifactEvent.GENERATION_SUCCEEDED
    )
    artifact.state = transition.to_state
    ctx.uow.audit.record_transition(
        subject_type=SubjectType.ARTIFACT,
        subject_id=artifact.id,
        from_state=transition.from_state.value,
        to_state=transition.to_state.value,
        cause=transition.cause,
        job_id=ctx.job.id,
    )
    ctx.uow.audit.record_event(
        event_type="artifact.version_created",
        subject_type=SubjectType.ARTIFACT,
        subject_id=artifact.id,
        payload={
            "version_id": version.id,
            "version_no": version.version_no,
            "kind": artifact.kind.value,
        },
    )
    ctx.uow.outbox.enqueue(
        event_type="artifact.version_created",
        payload={
            "project_id": artifact.project_id,
            "artifact_id": artifact.id,
            "version_id": version.id,
            "version_no": version.version_no,
            "kind": artifact.kind.value,
        },
    )

    _maybe_auto_approve(ctx, artifact, version.id)


def _maybe_auto_approve(ctx: JobContext, artifact: Artifact, version_id: str) -> None:
    """Apply the series' approval policy (SADD §11).

    Defaults to all-manual, so this is a no-op unless a series has explicitly
    opted a stage out of human review. Auto-approval still writes a real
    ``review_decision`` row rather than special-casing the status view — the
    view's definition of APPROVED stays the only one, and the audit trail shows
    both that the approval happened and that no human made it.
    """
    project = ctx.uow.projects.get(artifact.project_id)
    if project is None or project.series_id is None:
        return
    series = ctx.uow.series.get(project.series_id)
    if series is None:
        return

    policy = ApprovalPolicy.from_jsonb(series.auto_approve_policy)
    kind = ArtifactKind(artifact.kind)
    if policy.requires_human(kind):
        return

    ctx.uow.reviews.record(
        artifact_version_id=version_id,
        decision=ReviewDecisionKind.APPROVE,
        # reviewer_id stays NULL: nobody reviewed this. The audit trail must
        # not attribute an automatic approval to a person.
        reviewer_id=None,
        comment="auto-approved by series policy",
    )
    transition = apply_event(ArtifactState(artifact.state), ArtifactEvent.APPROVED)
    artifact.state = transition.to_state
    ctx.uow.audit.record_transition(
        subject_type=SubjectType.ARTIFACT,
        subject_id=artifact.id,
        from_state=transition.from_state.value,
        to_state=transition.to_state.value,
        cause=transition.cause,
        job_id=ctx.job.id,
    )
    ctx.uow.projects.set_active_pointer(project.id, kind.value, version_id)


@videoforge_task(
    name=SCRIPT_GENERATE.name, queue=SCRIPT_GENERATE.queue, job_bearing=True
)
def generate_script(ctx: JobContext) -> None:
    """Celery entry point. All the work is in :func:`script_body`, which the
    tests call directly — a stage's logic should be testable without a broker."""
    script_body(ctx)
