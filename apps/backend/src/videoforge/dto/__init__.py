"""Pydantic v2 request/response models (SADD §10.1, §19).

**ORM objects never leak through these.** That rule is what makes the
repository layer's decision to return ORM models safe: the boundary is here,
enforced by construction, because a DTO is built from explicit fields rather
than by serialising whatever the ORM happens to be holding. Serialising a
model directly would emit lazy-load queries from inside the response and leak
schema shape into the API contract.

The other half of the contract is ``capabilities``: every artifact response
carries what the *domain FSM* says is currently allowed, so the UI renders
buttons from the same table the services enforce. No TypeScript reimplements
the rules (§11).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from videoforge_domain.artifact_lifecycle import can_regenerate, capabilities
from videoforge_persistence.models import (
    Artifact,
    ArtifactVersion,
    GenerationJob,
    VideoProject,
)
from videoforge_persistence.projection import get_pipeline
from videoforge_persistence.repositories import VersionStatusRow
from videoforge_shared.enums import ArtifactKind, ArtifactState
from videoforge_shared.tasks import STAGE_TASKS

__all__ = [
    "ArtifactDetail",
    "ArtifactSummary",
    "CommentRequest",
    "CreateProjectRequest",
    "EditContentRequest",
    "GenerateRequest",
    "JobResponse",
    "ProjectDetail",
    "ProjectSummary",
    "ReviewRequest",
    "VersionDetail",
    "VersionSummary",
]


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #


class CreateProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str = Field(min_length=1, max_length=500)
    series_id: str | None = None
    title: str | None = Field(default=None, max_length=200)


class GenerateRequest(BaseModel):
    """``POST /projects/{id}/generations`` (SADD §19.1)."""

    model_config = ConfigDict(extra="forbid")

    stage: ArtifactKind
    scene_id: str | None = None
    #: True when the caller knows a version exists and wants another. Drives
    #: which FSM event is applied, so the machine can refuse a regeneration
    #: the artifact's state does not allow.
    regenerate: bool = False


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    comment: str | None = Field(default=None, max_length=4000)
    #: Optimistic concurrency (§19.1). Two tabs, or a regeneration that landed
    #: while the reviewer was reading, must not let an approval apply to
    #: content nobody looked at. Optional so a script can omit it; the UI
    #: always sends it.
    expected_version_no: int | None = None


class EditContentRequest(BaseModel):
    """``PUT /artifacts/{id}/content`` — a human edit becomes a new version."""

    model_config = ConfigDict(extra="forbid")

    content: dict[str, Any]


class CommentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=4000)
    anchor: dict[str, Any] | None = None


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #


class VersionSummary(BaseModel):
    id: str
    version_no: int
    origin: str
    #: **Derived**, from ``artifact_version_status`` (finding B1) — never a
    #: stored column. Present on every version so the review UI can render a
    #: version switcher without a request per version.
    status: str
    created_at: datetime
    created_by: str | None = None
    prompt_template_ref: str | None = None
    provider_ref: str | None = None

    @classmethod
    def of(
        cls, version: ArtifactVersion, status: VersionStatusRow | None
    ) -> VersionSummary:
        return cls(
            id=version.id,
            version_no=version.version_no,
            origin=version.origin.value,
            status=status.status.value if status else "AWAITING_APPROVAL",
            created_at=version.created_at,
            created_by=version.created_by,
            prompt_template_ref=version.prompt_template_ref,
            provider_ref=version.provider_ref,
        )


class VersionDetail(VersionSummary):
    content: dict[str, Any] | None = None
    storage_key: str | None = None
    content_hash: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)
    parent_version_id: str | None = None

    @classmethod
    def of_detail(
        cls, version: ArtifactVersion, status: VersionStatusRow | None
    ) -> VersionDetail:
        summary = VersionSummary.of(version, status)
        return cls(
            **summary.model_dump(),
            content=version.inline_content,
            storage_key=version.storage_key,
            content_hash=version.content_hash,
            meta=dict(version.meta or {}),
            parent_version_id=version.parent_version_id,
        )


class ArtifactSummary(BaseModel):
    id: str
    kind: str
    scene_ref: str | None = None
    state: str
    current_version_no: int
    stale_since: datetime | None = None
    #: What the FSM permits right now. The UI renders buttons from this and
    #: never decides for itself (§11).
    capabilities: dict[str, bool]

    @classmethod
    def of(cls, artifact: Artifact) -> ArtifactSummary:
        return cls(
            id=artifact.id,
            kind=artifact.kind.value,
            scene_ref=artifact.scene_ref,
            state=artifact.state.value,
            current_version_no=artifact.current_version_no,
            stale_since=artifact.stale_since,
            capabilities=capabilities(ArtifactState(artifact.state)),
        )


class ArtifactDetail(ArtifactSummary):
    versions: list[VersionSummary] = Field(default_factory=list)

    @classmethod
    def of_detail(
        cls, artifact: Artifact, versions: list[VersionSummary]
    ) -> ArtifactDetail:
        return cls(**ArtifactSummary.of(artifact).model_dump(), versions=versions)


class ProjectSummary(BaseModel):
    id: str
    topic: str
    title: str | None = None
    phase: str
    created_at: datetime

    @classmethod
    def of(cls, project: VideoProject) -> ProjectSummary:
        return cls(
            id=project.id,
            topic=project.topic,
            title=project.title,
            phase=project.phase.value,
            created_at=project.created_at,
        )


class StageSummary(BaseModel):
    """One pipeline stage, as the UI needs to see it (M2-13).

    The DAG is server-side (ADR-009) and stays there. Without this, a client
    wanting to know whether "Generate scenes" should be enabled would have to
    reimplement the dependency graph in TypeScript — the same drift the
    ``capabilities`` payload exists to prevent, one level up.

    ``unmet`` is the reason, not just the fact. "Waiting on: script" is an
    answer; a disabled button is a puzzle.
    """

    kind: str
    queue: str
    state: str | None = None
    artifact_id: str | None = None
    stale_since: datetime | None = None
    #: Kinds that must be APPROVED before this stage may run.
    requires: list[str] = Field(default_factory=list)
    #: Which of those are not approved yet. Empty means runnable.
    unmet: list[str] = Field(default_factory=list)
    can_generate: bool = False


class ProjectDetail(ProjectSummary):
    series_id: str | None = None
    #: A cache of the status view (B1), exposed for convenience. Clients
    #: needing certainty read each version's ``status`` instead.
    active_pointers: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[ArtifactSummary] = Field(default_factory=list)
    #: The pipeline, in dependency order, with this project's progress on it.
    stages: list[StageSummary] = Field(default_factory=list)

    @classmethod
    def of_detail(
        cls, project: VideoProject, artifacts: list[Artifact]
    ) -> ProjectDetail:
        summary = ProjectSummary.of(project)
        return cls(
            **summary.model_dump(),
            series_id=project.series_id,
            active_pointers=dict(project.active_pointers or {}),
            artifacts=[ArtifactSummary.of(a) for a in artifacts],
            stages=_stages(artifacts),
        )


def _stages(artifacts: list[Artifact]) -> list[StageSummary]:
    """Project the pipeline graph onto one project's artifacts.

    Approval is read from ``artifact.state``, not from ``active_pointers``: the
    pointer column is a cache (B1) and this decides whether a button is
    enabled, which the service will then independently enforce.

    Per-scene artifacts collapse to the *least advanced* of their kind, the
    same rule phase derivation uses — nineteen approved images and one still
    generating is not an approved image stage.
    """
    pipeline = get_pipeline()
    by_kind: dict[ArtifactKind, Artifact] = {}
    for artifact in artifacts:
        kind = ArtifactKind(artifact.kind)
        current = by_kind.get(kind)
        if current is None or _rank(artifact) < _rank(current):
            by_kind[kind] = artifact

    approved = {
        kind
        for kind, artifact in by_kind.items()
        if ArtifactState(artifact.state) is ArtifactState.APPROVED
    }

    summaries: list[StageSummary] = []
    for stage in pipeline.stages:
        found = by_kind.get(stage.produces)
        unmet = sorted(k.value for k in pipeline.unmet(stage.produces, approved))
        state = ArtifactState(found.state) if found else None
        summaries.append(
            StageSummary(
                kind=stage.produces.value,
                queue=stage.queue,
                state=state.value if state else None,
                artifact_id=found.id if found else None,
                stale_since=found.stale_since if found else None,
                requires=sorted(k.value for k in stage.requires),
                unmet=unmet,
                # A stage is runnable when its inputs are approved AND the FSM
                # would accept the move — a stage mid-generation must not offer
                # a second Generate.
                can_generate=(
                    not unmet
                    and stage.produces in STAGE_TASKS
                    and (
                        state is None
                        or can_regenerate(state)
                        or state is ArtifactState.PENDING
                    )
                ),
            )
        )
    return summaries


def _rank(artifact: Artifact) -> int:
    order = {
        ArtifactState.FAILED: 0,
        ArtifactState.PENDING: 1,
        ArtifactState.GENERATING: 2,
        ArtifactState.REJECTED: 3,
        ArtifactState.AWAITING_APPROVAL: 4,
        ArtifactState.APPROVED: 5,
    }
    return order[ArtifactState(artifact.state)]


class JobResponse(BaseModel):
    id: str
    status: str
    task_name: str
    queue: str
    attempt: int
    max_attempts: int
    error: dict[str, Any] | None = None
    queued_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @classmethod
    def of(cls, job: GenerationJob) -> JobResponse:
        return cls(
            id=job.id,
            status=job.status.value,
            task_name=job.task_name,
            queue=job.queue,
            attempt=job.attempt,
            max_attempts=job.max_attempts,
            error=job.error,
            queued_at=job.queued_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
        )
