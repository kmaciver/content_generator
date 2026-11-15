"""Workspace, series, and project queries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa

from videoforge_persistence.models import Artifact, Series, VideoProject, Workspace
from videoforge_persistence.repositories.base import Repository, affected_rows
from videoforge_shared.enums import ArtifactState, ProjectPhase
from videoforge_shared.ids import new_ulid

__all__ = [
    "ProjectActivity",
    "ProjectRepository",
    "SeriesRepository",
    "WorkspaceRepository",
]


@dataclass(frozen=True, slots=True)
class ProjectActivity:
    """What a project's artifacts are doing, without loading them (M5-08).

    Two booleans, because two booleans is all the dashboard needs: who the
    project waits on, and whether anything under it has failed.
    """

    generating: bool
    failed: bool


#: What a project with no artifacts at all is doing: nothing.
IDLE = ProjectActivity(generating=False, failed=False)


class WorkspaceRepository(Repository):
    def get(self, workspace_id: str) -> Workspace | None:
        return self.session.get(Workspace, workspace_id)

    def sole(self) -> Workspace | None:
        """The single v1 workspace.

        Exists so call sites stop hardcoding a seeded id. When tenancy becomes
        real this is the one method that must fail loudly rather than quietly
        pick a row — hence ``one_or_none()`` over ``first()``: two workspaces
        raise instead of returning an arbitrary one.
        """
        return self.session.scalars(
            sa.select(Workspace).order_by(Workspace.created_at)
        ).one_or_none()


class SeriesRepository(Repository):
    def get(self, series_id: str) -> Series | None:
        return self.session.get(Series, series_id)

    def for_workspace(self, workspace_id: str) -> list[Series]:
        return list(
            self.session.scalars(
                sa.select(Series)
                .where(Series.workspace_id == workspace_id)
                .order_by(Series.created_at.desc())
            )
        )

    def create(
        self,
        *,
        workspace_id: str,
        title: str,
        voice_preset: dict[str, Any] | None = None,
        music_policy: dict[str, Any] | None = None,
        auto_approve_policy: dict[str, Any] | None = None,
        hashtag_template: str | None = None,
    ) -> Series:
        series = Series(
            id=new_ulid(),
            workspace_id=workspace_id,
            title=title,
            voice_preset=voice_preset or {},
            music_policy=music_policy or {},
            # Empty means all-manual — see ``ApprovalPolicy.from_jsonb``.
            auto_approve_policy=auto_approve_policy or {},
            hashtag_template=hashtag_template,
        )
        self.session.add(series)
        return series


class ProjectRepository(Repository):
    def get(self, project_id: str) -> VideoProject | None:
        return self.session.get(VideoProject, project_id)

    def for_workspace(
        self, workspace_id: str, *, limit: int = 50, offset: int = 0
    ) -> list[VideoProject]:
        """Newest first — served by ``ix_video_project_workspace_id_created_at``."""
        return list(
            self.session.scalars(
                sa.select(VideoProject)
                .where(VideoProject.workspace_id == workspace_id)
                .order_by(VideoProject.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )

    def activity_for(self, project_ids: list[str]) -> dict[str, ProjectActivity]:
        """One grouped scan for a whole page of projects (M5-08).

        The dashboard needs to know, per project, whether a worker is running
        and whether anything failed. Reading it from the ``phase`` column is
        not an option — ``derive_phase`` reports a stage's *generating* phase
        both when it is running and when it has produced nothing yet, so the
        column cannot distinguish "a machine has this" from "nobody has
        started it" (see ``videoforge_domain.phases.waiting_on``). Only the
        artifact states can, and the honest way to get them for fifty projects
        is not fifty queries.

        ``FAILED`` is the other half, and it is invisible today for a specific
        reason: a failed image among twenty does **not** move the phase
        (§12.4's ``HAS_FAILURES`` is deliberately orthogonal, since the other
        nineteen are fine), so a project can sit on the list looking healthy
        with a broken scene inside it. This is the query that surfaces it.

        Served by ``ix_artifact_project_id_state``. Projects with no artifacts
        return no row at all, which is why callers default to ``IDLE`` rather
        than expecting a key.
        """
        if not project_ids:
            return {}
        rows = self.session.execute(
            sa.select(
                Artifact.project_id,
                sa.func.bool_or(Artifact.state == ArtifactState.GENERATING),
                sa.func.bool_or(Artifact.state == ArtifactState.FAILED),
            )
            .where(Artifact.project_id.in_(project_ids))
            .group_by(Artifact.project_id)
        ).all()
        return {
            project_id: ProjectActivity(
                generating=bool(generating), failed=bool(failed)
            )
            for project_id, generating, failed in rows
        }

    def create(
        self,
        *,
        workspace_id: str,
        topic: str,
        series_id: str | None = None,
        title: str | None = None,
        settings: dict[str, Any] | None = None,
    ) -> VideoProject:
        project = VideoProject(
            id=new_ulid(),
            workspace_id=workspace_id,
            series_id=series_id,
            topic=topic,
            title=title,
            phase=ProjectPhase.DRAFT,
            settings=settings or {},
        )
        self.session.add(project)
        return project

    def delete(self, project: VideoProject) -> None:
        """Remove a project and everything the schema hangs off it.

        **The FKs do the work, and they already reach everything that is
        *about* this project**: artifacts → versions → comments, reviews,
        scene sets → scenes, publishing packages; jobs → usage rows and their
        transitions. All ``ON DELETE CASCADE``, verified against the live
        schema rather than assumed.

        Two things it deliberately does not take.

        **The audit trail stays.** ``audit_event``, ``outbox_event`` and the
        review-caused ``state_transition`` rows address their subject
        polymorphically (``subject_type`` + ``subject_id``), so no FK reaches
        them and nothing here goes looking. That is the right outcome, not an
        oversight: §10.3 makes the trail immutable so "what happened to that
        video?" stays answerable, and a deletion is the moment the question
        gets asked. The rows are small and name a subject that no longer
        exists, which is exactly what a tombstone is.

        **The bytes stay.** Storage is content-addressed (ADR-004) and
        deduplicated — ``put_bytes`` reports when a key already existed — so
        two projects can and do share an object. Deleting this project's keys
        would silently break another project's frames. Reclaiming them needs a
        mark-and-sweep against every surviving version, which is its own job
        and not a side effect of a click.
        """
        self.session.delete(project)

    def set_phase(self, project_id: str, phase: ProjectPhase) -> bool:
        """Write the derived phase cache (§12.4).

        ``phase != :phase`` in the WHERE clause so a recompute that lands on
        the same value is a no-op: without it, every reconciliation pass would
        bump ``phase_updated_at`` and the UI would show constant churn on a
        project where nothing happened.
        """
        result = self.session.execute(
            sa.update(VideoProject)
            .where(VideoProject.id == project_id, VideoProject.phase != phase)
            .values(phase=phase, phase_updated_at=sa.func.now())
        )
        return affected_rows(result) == 1

    def set_active_pointer(
        self, project_id: str, kind: str, artifact_version_id: str | None
    ) -> None:
        """Update one key of the ``active_pointers`` cache.

        A JSONB merge in SQL rather than read-modify-write in Python: two
        stages approving concurrently would otherwise each read the whole
        object and write it back, and the later write would erase the earlier
        one's key. ``||`` merges server-side, so both survive.

        This is a *cache* (B1). The authority is ``artifact_version_status``,
        and it is always recomputable from it.
        """
        if artifact_version_id is None:
            # Postgres spells "remove this key" as ``jsonb - text``. Written
            # with ``op('-')`` because SQLAlchemy's JSONB type reserves ``-``
            # for Python-side subtraction and offers no named equivalent.
            self.session.execute(
                sa.update(VideoProject)
                .where(VideoProject.id == project_id)
                .values(
                    active_pointers=VideoProject.active_pointers.op("-")(
                        sa.cast(kind, sa.Text)
                    )
                )
            )
            return
        self.session.execute(
            sa.update(VideoProject)
            .where(VideoProject.id == project_id)
            .values(
                active_pointers=VideoProject.active_pointers.concat(
                    sa.func.jsonb_build_object(kind, artifact_version_id)
                )
            )
        )

    def pin_branding(
        self,
        project_id: str,
        *,
        character_version_id: str,
        style_version_id: str,
    ) -> bool:
        """Record which branding versions this project generates against (M3-06).

        **Write-once.** The guard is in the WHERE clause — ``character_version_id
        IS NULL`` — so a second call changes zero rows and returns False rather
        than moving a pin. That matters because the pin is what protects the
        back catalogue (ADR-016): a project whose pin could move would start
        producing images against a character its earlier scenes never saw, and
        the mismatch would be invisible until someone watched the video.

        Guarded in SQL rather than by reading first, for the reason
        ``JobRepository.claim`` gives: two concurrent image jobs on a fresh
        project both find NULL, and only the statement itself can decide which
        one wins.
        """
        result = self.session.execute(
            sa.update(VideoProject)
            .where(
                VideoProject.id == project_id,
                VideoProject.character_version_id.is_(None),
            )
            .values(
                character_version_id=character_version_id,
                style_version_id=style_version_id,
            )
        )
        return affected_rows(result) == 1
