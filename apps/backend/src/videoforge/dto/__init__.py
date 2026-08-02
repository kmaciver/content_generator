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
from videoforge_domain.artifact_lifecycle import capabilities

from videoforge_persistence.models import (
    Artifact,
    ArtifactVersion,
    GenerationJob,
    VideoProject,
)
from videoforge_persistence.repositories import VersionStatusRow
from videoforge_shared.enums import ArtifactKind, ArtifactState

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


class ProjectDetail(ProjectSummary):
    series_id: str | None = None
    #: A cache of the status view (B1), exposed for convenience. Clients
    #: needing certainty read each version's ``status`` instead.
    active_pointers: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[ArtifactSummary] = Field(default_factory=list)

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
        )


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
