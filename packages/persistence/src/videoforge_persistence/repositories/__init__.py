"""Repositories — the only code that touches the session (SADD §10.1).

They live in ``packages/persistence`` rather than under the backend for the
same reason the ORM does: workers write artifact versions, jobs, transitions
and outbox rows in the same transaction as their outputs (§13), and workers
must never import the backend.

Each exposes *intent* rather than a query builder — ``latest_for(project,
kind)``, ``claim_orphans(older_than)``, ``reserve(idempotency_key=...)``. A
repository that returned a query object would put query shape back at the call
sites, which is the thing this layer exists to prevent.

None of them commit. The unit of work is one transaction per use case, opened
and closed by the service.
"""

from videoforge_persistence.repositories.artifacts import (
    ArtifactRepository,
    ArtifactVersionRepository,
    VersionStatusRow,
)
from videoforge_persistence.repositories.audit import (
    AuditRepository,
    OutboxRepository,
)
from videoforge_persistence.repositories.base import Repository
from videoforge_persistence.repositories.branding import BrandingRepository
from videoforge_persistence.repositories.jobs import (
    JobRepository,
    ProviderUsageRepository,
    ReservedJob,
)
from videoforge_persistence.repositories.projects import (
    ProjectRepository,
    SeriesRepository,
    WorkspaceRepository,
)
from videoforge_persistence.repositories.reviews import (
    CommentRepository,
    ReviewRepository,
)
from videoforge_persistence.repositories.scenes import SceneRepository

__all__ = [
    "ArtifactRepository",
    "ArtifactVersionRepository",
    "AuditRepository",
    "BrandingRepository",
    "CommentRepository",
    "JobRepository",
    "OutboxRepository",
    "ProjectRepository",
    "ProviderUsageRepository",
    "Repository",
    "SceneRepository",
    "ReservedJob",
    "ReviewRepository",
    "SeriesRepository",
    "VersionStatusRow",
    "WorkspaceRepository",
]
