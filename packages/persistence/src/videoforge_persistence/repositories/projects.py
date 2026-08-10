"""Workspace, series, and project queries."""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa

from videoforge_persistence.models import Series, VideoProject, Workspace
from videoforge_persistence.repositories.base import Repository, affected_rows
from videoforge_shared.enums import ProjectPhase
from videoforge_shared.ids import new_ulid

__all__ = ["ProjectRepository", "SeriesRepository", "WorkspaceRepository"]


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
