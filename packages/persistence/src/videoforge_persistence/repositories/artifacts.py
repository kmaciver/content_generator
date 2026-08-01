"""Artifact and artifact-version queries, including the B1 status view."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa

from videoforge_persistence.models import Artifact, ArtifactVersion
from videoforge_persistence.repositories.base import Repository, affected_rows
from videoforge_shared.enums import (
    ArtifactKind,
    ArtifactState,
    VersionOrigin,
    VersionStatus,
)
from videoforge_shared.ids import new_ulid

__all__ = ["ArtifactRepository", "ArtifactVersionRepository", "VersionStatusRow"]


@dataclass(frozen=True, slots=True)
class VersionStatusRow:
    """One row of ``artifact_version_status`` (finding B1).

    A read model, not a mapped entity: the view has no primary key and nothing
    writes to it. Mapping it as an ORM class would imply both.
    """

    artifact_version_id: str
    artifact_id: str
    version_no: int
    status: VersionStatus
    decided_at: datetime | None


class ArtifactRepository(Repository):
    def get(self, artifact_id: str) -> Artifact | None:
        return self.session.get(Artifact, artifact_id)

    def for_project(self, project_id: str) -> list[Artifact]:
        stmt = (
            sa.select(Artifact)
            .where(Artifact.project_id == project_id)
            .order_by(Artifact.kind, Artifact.scene_ref, Artifact.created_at)
        )
        return list(self.session.scalars(stmt))

    def find(
        self,
        project_id: str,
        kind: ArtifactKind,
        scene_ref: str | None = None,
    ) -> Artifact | None:
        """The lookup the S1 unique constraint makes unambiguous.

        ``scene_ref IS NULL`` rather than ``= NULL`` — SQLAlchemy renders
        ``is_()`` correctly, but writing it as an equality here would silently
        match nothing for every project-wide artifact, which is most of them.
        """
        stmt = sa.select(Artifact).where(
            Artifact.project_id == project_id,
            Artifact.kind == kind,
            (
                Artifact.scene_ref.is_(scene_ref)
                if scene_ref is None
                else Artifact.scene_ref == scene_ref
            ),
        )
        return self.session.scalars(stmt).one_or_none()

    def create(
        self,
        project_id: str,
        kind: ArtifactKind,
        scene_ref: str | None = None,
        state: ArtifactState = ArtifactState.PENDING,
    ) -> Artifact:
        artifact = Artifact(
            id=new_ulid(),
            project_id=project_id,
            kind=kind,
            scene_ref=scene_ref,
            state=state,
        )
        self.session.add(artifact)
        return artifact

    def mark_stale(
        self, artifact_ids: list[str], *, since: datetime | None = None
    ) -> int:
        """Finding S2: the staleness cascade (§12.4).

        ``stale_since IS NULL`` in the WHERE clause makes this idempotent —
        re-running a cascade must not reset the timestamp, or "stale since
        when?" answers "just now" forever and the UI can never show how long
        something has been out of date.
        """
        if not artifact_ids:
            return 0
        stmt = (
            sa.update(Artifact)
            .where(Artifact.id.in_(artifact_ids), Artifact.stale_since.is_(None))
            .values(stale_since=since or sa.func.now())
        )
        return affected_rows(self.session.execute(stmt))

    def clear_stale(self, artifact_id: str) -> None:
        self.session.execute(
            sa.update(Artifact)
            .where(Artifact.id == artifact_id)
            .values(stale_since=None)
        )


class ArtifactVersionRepository(Repository):
    def get(self, version_id: str) -> ArtifactVersion | None:
        return self.session.get(ArtifactVersion, version_id)

    def history(self, artifact_id: str) -> list[ArtifactVersion]:
        """Newest first — the version switcher's order."""
        stmt = (
            sa.select(ArtifactVersion)
            .where(ArtifactVersion.artifact_id == artifact_id)
            .order_by(ArtifactVersion.version_no.desc())
        )
        return list(self.session.scalars(stmt))

    def latest(self, artifact_id: str) -> ArtifactVersion | None:
        stmt = (
            sa.select(ArtifactVersion)
            .where(ArtifactVersion.artifact_id == artifact_id)
            .order_by(ArtifactVersion.version_no.desc())
            .limit(1)
        )
        return self.session.scalars(stmt).one_or_none()

    def latest_for(
        self, project_id: str, kind: ArtifactKind, scene_ref: str | None = None
    ) -> ArtifactVersion | None:
        """SADD §10.1's named example, spelled as one query rather than two.

        A caller doing ``find()`` then ``latest()`` would work, but this is the
        hot path for the review screen and the extra round-trip is per
        artifact — twenty of them on the image grid.
        """
        stmt = (
            sa.select(ArtifactVersion)
            .join(Artifact, Artifact.id == ArtifactVersion.artifact_id)
            .where(
                Artifact.project_id == project_id,
                Artifact.kind == kind,
                (
                    Artifact.scene_ref.is_(scene_ref)
                    if scene_ref is None
                    else Artifact.scene_ref == scene_ref
                ),
            )
            .order_by(ArtifactVersion.version_no.desc())
            .limit(1)
        )
        return self.session.scalars(stmt).one_or_none()

    def add_version(
        self,
        artifact: Artifact,
        *,
        origin: VersionOrigin,
        content_hash: str,
        storage_key: str | None = None,
        inline_content: dict[str, Any] | None = None,
        generation_job_id: str | None = None,
        parent_version_id: str | None = None,
        prompt_template_ref: str | None = None,
        provider_ref: str | None = None,
        created_by: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> ArtifactVersion:
        """Append the next version, advancing the artifact's counter.

        The version number comes from ``current_version_no + 1`` computed in
        SQL against the row this transaction holds, not from a Python read —
        two workers racing would otherwise both read N and both write N+1, and
        the loser would hit ``uq_artifact_version_artifact_id_version_no``.
        The unique constraint remains the real guarantee; this just stops it
        firing in the ordinary case.

        Parent defaults to the current latest version, which is what makes the
        lineage chain in §10.3 rule 2 a chain rather than a set of orphans.
        """
        next_no = self.session.execute(
            sa.update(Artifact)
            .where(Artifact.id == artifact.id)
            .values(current_version_no=Artifact.current_version_no + 1)
            .returning(Artifact.current_version_no)
        ).scalar_one()

        if parent_version_id is None and next_no > 1:
            previous = self.session.scalars(
                sa.select(ArtifactVersion.id)
                .where(
                    ArtifactVersion.artifact_id == artifact.id,
                    ArtifactVersion.version_no == next_no - 1,
                )
                .limit(1)
            ).one_or_none()
            parent_version_id = previous

        version = ArtifactVersion(
            id=new_ulid(),
            artifact_id=artifact.id,
            version_no=next_no,
            origin=origin,
            content_hash=content_hash,
            storage_key=storage_key,
            inline_content=inline_content,
            generation_job_id=generation_job_id,
            parent_version_id=parent_version_id,
            prompt_template_ref=prompt_template_ref,
            provider_ref=provider_ref,
            created_by=created_by,
            meta=meta or {},
        )
        self.session.add(version)
        # Flush so the caller sees the row (and any constraint violation)
        # inside its own transaction rather than at commit, where the stack
        # trace no longer points at the code that caused it.
        self.session.flush()
        # The counter was bumped by a Core UPDATE, which the identity map knows
        # nothing about — without this, a caller holding `artifact` would keep
        # reading the pre-increment value and, reasonably, conclude the version
        # was never written.
        self.session.expire(artifact, ["current_version_no"])
        return version

    # --- the B1 view -----------------------------------------------------

    def _status_rows(
        self, whereclause: sa.ColumnElement[bool]
    ) -> list[VersionStatusRow]:
        view = sa.table(
            "artifact_version_status",
            sa.column("artifact_version_id", sa.String),
            sa.column("artifact_id", sa.String),
            sa.column("version_no", sa.Integer),
            sa.column("status", sa.String),
            sa.column("decided_at", sa.TIMESTAMP(timezone=True)),
        )
        stmt = (
            sa.select(view).where(whereclause).order_by(sa.column("version_no").desc())
        )
        return [
            VersionStatusRow(
                artifact_version_id=row.artifact_version_id,
                artifact_id=row.artifact_id,
                version_no=row.version_no,
                status=VersionStatus(row.status),
                decided_at=row.decided_at,
            )
            for row in self.session.execute(stmt)
        ]

    def statuses_for_artifact(self, artifact_id: str) -> list[VersionStatusRow]:
        return self._status_rows(sa.column("artifact_id") == artifact_id)

    def status_of(self, version_id: str) -> VersionStatusRow | None:
        rows = self._status_rows(sa.column("artifact_version_id") == version_id)
        return rows[0] if rows else None

    def approved_version(self, artifact_id: str) -> VersionStatusRow | None:
        """The single approved version, or None.

        Reads the view rather than ``video_project.active_pointers``: the
        pointer column is a cache (B1), and a write path that trusted the
        cache could approve against a stale value. The cache is for display.
        """
        for row in self.statuses_for_artifact(artifact_id):
            if row.status is VersionStatus.APPROVED:
                return row
        return None
