"""The video project — one topic in, one video out."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from videoforge_persistence.base import Base
from videoforge_persistence.columns import (
    TimestampType,
    ULIDType,
    created_at_col,
    jsonb_col,
    ulid_pk,
    updated_at_col,
)
from videoforge_persistence.enum_types import PROJECT_PHASE
from videoforge_shared.enums import ProjectPhase


class VideoProject(Base):
    """One video, from topic to publishing package.

    Two of these columns are **caches of derived truth**, and both are safe
    only because they are recomputable from the artifact tables at any moment:

    - ``phase`` (SADD §12.4) is computed from artifact states against the
      pipeline DAG. It is stored so that listing fifty projects does not mean
      fifty DAG evaluations. Because it is derived it can never *disagree*
      with the artifacts — a stale cache is a performance bug, not a
      correctness one, and rebuilding it is a single pass.
    - ``active_pointers`` maps artifact kind → the currently approved
      ``artifact_version.id``. Finding B1 makes the ``artifact_version_status``
      view the authority on what "approved" means; this column is a lookaside
      so the review UI can resolve twenty pointers without twenty view
      queries. **Never branch on it in a write path** — read the view.
    """

    __tablename__ = "video_project"

    id: Mapped[str] = ulid_pk()
    workspace_id: Mapped[str] = mapped_column(
        ULIDType, sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    # Nullable: a one-off video need not belong to a series. ``SET NULL`` so
    # deleting a series orphans rather than destroys its episodes.
    series_id: Mapped[str | None] = mapped_column(
        ULIDType, sa.ForeignKey("series.id", ondelete="SET NULL"), nullable=True
    )
    topic: Mapped[str] = mapped_column(sa.Text, nullable=False)
    title: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    phase: Mapped[ProjectPhase] = mapped_column(
        PROJECT_PHASE, nullable=False, default=ProjectPhase.DRAFT
    )
    phase_updated_at: Mapped[datetime] = mapped_column(
        TimestampType, nullable=False, server_default=sa.func.now()
    )
    active_pointers: Mapped[dict[str, Any]] = jsonb_col()
    # M3-06, and the load-bearing half of ADR-016: which branding versions this
    # project was generated against. Set once, on the first image generation,
    # and never moved.
    #
    # Without them, approving character v2 would retroactively invalidate every
    # episode built from v1 — a staleness cascade across the whole back
    # catalogue, triggered by an ordinary tweak. With them, superseding at the
    # series level affects *new* projects only.
    #
    # **Deliberately not foreign keys.** These are a provenance record and must
    # outlive their subjects: deleting a series cascades its branding rows away
    # while ``series_id`` here goes SET NULL, and a FK would then either block
    # the delete or (with SET NULL) erase the record of what the video was
    # actually made from. Same reasoning as ``state_transition.subject_id`` and
    # ``character_reference.generation_job_id`` — history does not hold
    # references, it holds ids.
    character_version_id: Mapped[str | None] = mapped_column(ULIDType, nullable=True)
    style_version_id: Mapped[str | None] = mapped_column(ULIDType, nullable=True)
    settings: Mapped[dict[str, Any]] = jsonb_col()
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    __table_args__ = (
        # The dashboard's only two queries: "this workspace, newest first"
        # and "this workspace, still in review".
        sa.Index(
            "ix_video_project_workspace_id_created_at", "workspace_id", "created_at"
        ),
        sa.Index("ix_video_project_workspace_id_phase", "workspace_id", "phase"),
        sa.Index("ix_video_project_series_id", "series_id"),
    )
