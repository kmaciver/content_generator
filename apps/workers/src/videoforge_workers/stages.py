"""What every generation stage does around its provider call (M2, §13).

``script.py``'s docstring said it: *"every later stage is this shape with a
different provider call in the middle."* Batch 3 adds three more stages, and
copying its hundred-line completion tail four times is precisely how four
copies of the same rule drift into four slightly different rules — the version
that forgets the outbox row, the one that skips the phase recompute, the one
whose auto-approve check runs before the transition instead of after.

So the tail lives here once. A stage supplies what is genuinely its own:

* the prompt it renders and the schema it wants back,
* how a provider result becomes artifact content,
* anything extra to write in the same transaction (``scene`` rows).

Everything else — the version row, the usage row, the FSM transition, the audit
event, the outbox event, auto-approval and the phase recompute — is identical
by construction rather than by review.

**The whole of :func:`complete_generation` runs inside the skeleton's
transaction** (§10.3 rule 6). Nothing here commits.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from functools import lru_cache
from typing import Any

from videoforge_domain.approval_policy import ApprovalPolicy
from videoforge_domain.artifact_lifecycle import ArtifactEvent, apply_event
from videoforge_domain.budget import check_budget
from videoforge_persistence.models import Artifact, ArtifactVersion
from videoforge_persistence.projection import refresh_project_state
from videoforge_prompts import RenderedPrompt
from videoforge_providers.models import (
    ImageResult,
    LLMMessage,
    LLMRequest,
    LLMResult,
    VoiceResult,
)
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
from videoforge_shared.settings import get_worker_settings, load_worker_settings
from videoforge_workers.skeleton import JobContext

logger = logging.getLogger(__name__)

__all__ = [
    "AfterVersion",
    "complete_generation",
    "complete_stored_generation",
    "llm_complete",
    "load_artifact",
    "provider",
    "require_approved_content",
    "require_budget",
]

#: Hook for a stage that must write more rows in the same transaction — the
#: scene set's ``scene`` rows are the only user today. Takes the version it
#: hangs off, because that is what those rows reference.
AfterVersion = Callable[[JobContext, ArtifactVersion], None]


@lru_cache(maxsize=1)
def provider() -> LLMProvider:
    """The configured LLM provider, built once per worker process.

    ``load_worker_settings`` — not ``get_app_settings`` — because only
    ``WorkerSettings`` carries the provider plane. That asymmetry *is* the NF8
    boundary: the API cannot build a provider because it cannot construct the
    settings a provider needs.
    """
    settings = load_worker_settings()
    return build_llm_provider(settings.providers, settings.provider_keys)


def require_budget(ctx: JobContext) -> None:
    """Refuse to spend when the day's estimated cap is reached (S10, M3-11).

    **Before the call, not after.** The cap exists to stop a regeneration loop,
    and a loop is only stoppable before the next request; checking afterwards
    produces a very well-documented bill.

    The window is the **UTC day**, computed database-side so that five
    containers with five slightly different clocks agree on when "today"
    started — the same reasoning ``claim_orphans`` uses for its cutoff.

    Scope note: ``provider_usage`` has no workspace column, so this is a
    deployment-wide total rather than the per-workspace one §21.4 describes.
    Identical today, because v1 seeds exactly one workspace. When a second
    arrives this needs a join through ``generation_job → video_project →
    series``, and the total will otherwise silently cap the wrong thing.
    """
    # The *cached* getter, not ``load_worker_settings``: this runs once per
    # provider call, and a twenty-scene prompt fan-out would otherwise re-read
    # the environment and rebuild every settings model twenty times. Config is
    # immutable for a process's lifetime, which is what the cache encodes.
    settings = get_worker_settings()
    limit = settings.core.daily_cost_limit
    if limit <= 0:
        # Checked before the query, so "no cap" costs no round-trip at all.
        return
    spent = ctx.uow.usage.spend_today()
    check_budget(spent, limit, currency=settings.core.cost_currency)


def llm_complete(
    ctx: JobContext, prompt: RenderedPrompt, schema: dict[str, Any]
) -> LLMResult:
    """One completion from a rendered prompt, inside the budget.

    Always with a schema: every stage in this pipeline consumes structure, and
    a stage that parsed prose would be one provider mood away from producing
    unusable artifacts.

    ``ctx`` is taken purely so the budget check cannot be forgotten. It could
    have been a separate call at the top of each stage body, which is exactly
    how three of four stages would eventually end up without one — the same
    argument that put the completion tail in ``complete_generation``.
    """
    require_budget(ctx)
    return provider().complete(
        LLMRequest(
            messages=(
                LLMMessage(role="system", content=prompt.system),
                LLMMessage(role="user", content=prompt.user),
            ),
            response_schema=schema,
        )
    )


def load_artifact(ctx: JobContext) -> Artifact:
    """The artifact this job was created against.

    From ``input_snapshot``, never re-derived: a job queued three minutes ago
    must run against what it was asked to do.
    """
    artifact_id = str(ctx.input["artifact_id"])
    artifact = ctx.uow.artifacts.get(artifact_id)
    if artifact is None:
        raise RuntimeError(f"artifact {artifact_id} vanished before generation")
    return artifact


def require_approved_content(
    ctx: JobContext, project_id: str, kind: ArtifactKind
) -> dict[str, Any]:
    """The approved content of an upstream stage, or a clear failure.

    Reads the ``artifact_version_status`` view rather than
    ``video_project.active_pointers``: the pointer column is a cache (B1), and
    a *write* path that trusted a cache could generate against a version that
    is no longer the approved one.

    Raising here rather than degrading is deliberate. A scenes job that ran
    without its script would produce scenes about nothing, and they would look
    entirely plausible — the pipeline DAG (M2-02) is supposed to make this
    unreachable, so arriving here means the guard failed and the job should
    stop loudly.
    """
    artifact = ctx.uow.artifacts.find(project_id, kind)
    if artifact is None:
        raise RuntimeError(f"{kind.value} artifact does not exist for {project_id}")

    approved = ctx.uow.versions.approved_version(artifact.id)
    if approved is None:
        raise RuntimeError(f"{kind.value} has no approved version for {project_id}")

    version = ctx.uow.versions.get(approved.artifact_version_id)
    if version is None or version.inline_content is None:
        raise RuntimeError(f"approved {kind.value} version has no inline content")
    return dict(version.inline_content)


def complete_generation(
    ctx: JobContext,
    artifact: Artifact,
    *,
    content: dict[str, Any],
    result: LLMResult,
    prompt_ref: str,
    after_version: AfterVersion | None = None,
) -> ArtifactVersion:
    """Write one generated version and everything that must accompany it.

    Order matters and is the reason this is one function: the version exists
    before anything references it, the transition follows the write it
    describes, and the phase is recomputed last — after auto-approval, so a
    policy approval and the phase it implies land in a single recomputation
    rather than two that disagree in between.
    """
    # Hash the canonical serialisation, not the dict: key order would otherwise
    # change the hash for identical content, and the hash is what the timeline's
    # input_snapshot pins for reproducibility (§10.3 rule 4).
    canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))

    version = ctx.uow.versions.add_version(
        artifact,
        origin=VersionOrigin.GENERATED,
        content_hash=sha256_bytes(canonical.encode()),
        # Inline: these artifacts are kilobytes, and a round-trip to object
        # storage would buy nothing. Media artifacts (M3) use storage_key
        # instead — the CHECK constraint enforces exactly one.
        inline_content=content,
        generation_job_id=ctx.job.id,
        prompt_template_ref=prompt_ref,
        provider_ref=result.provider_meta.get("provider"),
        meta={
            "model": result.provider_meta.get("model"),
            "provider_meta": result.provider_meta,
            "latency_ms": result.latency_ms,
        },
    )

    if after_version is not None:
        # Before the transition, so a stage whose extra rows fail leaves the
        # artifact still GENERATING rather than AWAITING_APPROVAL with nothing
        # to review. The whole body is one transaction, so it rolls back
        # either way — this only decides what a partially-written state would
        # have looked like, and "still generating" is the honest one.
        after_version(ctx, version)

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

    _finalise(ctx, artifact, version)
    return version


def complete_stored_generation(
    ctx: JobContext,
    artifact: Artifact,
    *,
    storage_key: str,
    content_hash: str,
    result: ImageResult | VoiceResult,
    prompt_ref: str,
    meta: dict[str, Any],
    operation: str = "image.generate",
) -> ArtifactVersion:
    """The binary-content equivalent of :func:`complete_generation` (M3-07).

    Two things genuinely differ from the text stages, and everything after them
    is shared through :func:`_finalise`:

    * the version carries a ``storage_key`` rather than ``inline_content`` —
      the CHECK constraint permits exactly one, and a megabyte of JPEG has no
      business in a jsonb column;
    * the usage row records ``images`` rather than tokens, under a different
      ``operation``, so the S10 cap sums a real number instead of counting
      zero tokens for the most expensive call in the system.

    ``operation`` is a parameter rather than a constant because M3-12 reuses
    this for narration: an audio artifact stores a key exactly like an image
    does, and hardcoding ``image.generate`` here would file every voice call
    under image spend in the one table the daily cap reads.

    Deliberately a sibling rather than a flag on ``complete_generation``. The
    two callers share a *tail*, not a signature, and folding them together
    would produce a function whose body is mostly deciding which half of its
    own parameters to ignore.
    """
    version = ctx.uow.versions.add_version(
        artifact,
        origin=VersionOrigin.GENERATED,
        # The digest of the bytes themselves, matching the storage key's own
        # content addressing (ADR-004) — so an artifact version and the object
        # it points at can be shown to be the same thing.
        content_hash=content_hash,
        storage_key=storage_key,
        generation_job_id=ctx.job.id,
        prompt_template_ref=prompt_ref,
        provider_ref=result.provider_meta.get("provider"),
        meta=meta,
    )

    ctx.uow.usage.record(
        job_id=ctx.job.id,
        provider=str(result.provider_meta.get("provider", "unknown")),
        model=str(result.provider_meta.get("model", "unknown")),
        operation=operation,
        latency_ms=result.latency_ms,
        unit_cost_estimate=result.usage.unit_cost_estimate,
        images=result.usage.images,
        raw_meta=result.provider_meta,
    )

    _finalise(ctx, artifact, version)
    return version


def _finalise(ctx: JobContext, artifact: Artifact, version: ArtifactVersion) -> None:
    """Everything that must accompany a generated version, whatever it holds.

    Order matters and is the reason this is one function: the transition
    follows the write it describes, and the phase is recomputed last — after
    auto-approval, so a policy approval and the phase it implies land in a
    single recomputation rather than two that disagree in between.
    """
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
    payload = {
        "version_id": version.id,
        "version_no": version.version_no,
        "kind": artifact.kind.value,
    }
    ctx.uow.audit.record_event(
        event_type="artifact.version_created",
        subject_type=SubjectType.ARTIFACT,
        subject_id=artifact.id,
        payload=payload,
    )
    ctx.uow.outbox.enqueue(
        event_type="artifact.version_created",
        payload={"project_id": artifact.project_id, "artifact_id": artifact.id}
        | payload,
    )

    _maybe_auto_approve(ctx, artifact, version.id)
    refresh_project_state(ctx.uow, artifact.project_id)


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

    approve_without_human(
        ctx, artifact, version_id, comment="auto-approved by series policy"
    )


def approve_without_human(
    ctx: JobContext, artifact: Artifact, version_id: str, *, comment: str
) -> None:
    """Approve a version with no reviewer attached.

    Extracted for M4-02, which needs the same mechanism for a different
    reason. Two distinct grounds reach this function and it is worth keeping
    them distinguishable in the audit trail rather than in the code:

    * a **series policy** opted a whole stage out of review (D3's seam,
      defaulting closed);
    * a **card scene** (§1.0.3) is the deterministic rendering of text a human
      already approved at the scene-set gate, so there is no second judgement
      to make — approving it again would be asking someone to re-read words
      they wrote.

    ``comment`` is what separates them afterwards, which is why it has no
    default: a caller that has not said why it is skipping the human gate has
    not thought about whether it should.
    """
    ctx.uow.reviews.record(
        artifact_version_id=version_id,
        decision=ReviewDecisionKind.APPROVE,
        # reviewer_id stays NULL: nobody reviewed this. The audit trail must
        # not attribute an automatic approval to a person.
        reviewer_id=None,
        comment=comment,
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
    # Per-scene artifacts do not own the project-wide pointer for their kind:
    # twenty image artifacts cannot each be "the" image. Only the project-wide
    # row sets it (finding S1's `scene_ref IS NULL` is the same distinction).
    if artifact.scene_ref is None:
        ctx.uow.projects.set_active_pointer(
            artifact.project_id, ArtifactKind(artifact.kind).value, version_id
        )
