"""Scene queries.

Everything downstream of the scene set — prompts, images, and eventually the
timeline — needs "the scenes of the approved scene set, in order". That query
was written inline in the prompts stage first; a second caller (the project DTO,
which the review UI reads) is the point at which it belongs here instead.

**Always joined through ``scene_set.artifact_version_id``, never read from the
scene-set artifact's inline JSON.** The rows are what ``artifact.scene_ref``
points at, so generating or reviewing against the JSON copy would let the two
drift apart in exactly the place a mismatch is invisible.
"""

from __future__ import annotations

import sqlalchemy as sa

from videoforge_persistence.models import Scene, SceneSet
from videoforge_persistence.repositories.base import Repository

__all__ = ["SceneRepository"]


class SceneRepository(Repository):
    def for_version(self, artifact_version_id: str) -> list[Scene]:
        """The scenes belonging to one scene-set version, by index."""
        return list(
            self.session.scalars(
                sa.select(Scene)
                .join(SceneSet, Scene.scene_set_id == SceneSet.id)
                .where(SceneSet.artifact_version_id == artifact_version_id)
                .order_by(Scene.index)
            )
        )

    def for_approved_set(self, project_id: str) -> list[Scene]:
        """The scenes of the project's *approved* scene set, or empty.

        Empty rather than raising: a project with no approved scene set is the
        ordinary early state, and callers render "not yet" from an empty list
        more gracefully than from an exception.

        Reads the ``artifact_version_status`` view through
        ``ArtifactVersionRepository.approved_version`` rather than
        ``video_project.active_pointers`` — the pointer column is a cache (B1).
        """
        # Imported here rather than at module scope: the artifact repositories
        # import nothing from this module today, and a mutual import between
        # two repository modules is a cycle waiting for its third caller.
        from videoforge_persistence.repositories.artifacts import (
            ArtifactRepository,
            ArtifactVersionRepository,
        )
        from videoforge_shared.enums import ArtifactKind

        artifact = ArtifactRepository(self.session).find(
            project_id, ArtifactKind.SCENE_SET
        )
        if artifact is None:
            return []
        approved = ArtifactVersionRepository(self.session).approved_version(artifact.id)
        if approved is None:
            return []
        return self.for_version(approved.artifact_version_id)
