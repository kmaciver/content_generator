"""Image Studio: free-form generations outside any project or series (ADR-018).

**Why not artifacts.** The same reason as branding (ADR-016): ``artifact.
project_id`` is NOT NULL, and a studio image belongs to no project. It also has
nothing an artifact exists to carry — no FSM, no review, no staleness, no
pipeline position. Two plain tables that nothing else references keep the
studio from leaking into any rule the pipeline depends on.

**Why a job anyway.** Everything else a job carries is wanted: the API never
generates (only workers hold provider keys, NF8), and ``provider_usage.job_id``
is NOT NULL, so a job row is what puts studio spend inside the daily cap.

Status is **not stored** here. It is the job's status, read through
``generation_job.studio_generation_id``; a copy on this table would be a second
source of truth that a crashed worker could leave disagreeing with the first.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from videoforge_persistence.base import Base
from videoforge_persistence.columns import ULIDType, created_at_col, ulid_pk

__all__ = ["StudioGeneration", "StudioImage"]


class StudioGeneration(Base):
    """One request: a prompt, its settings, and the references it was given.

    Immutable in practice — nothing updates a request after it is made, which
    is why there is no ``updated_at``.
    """

    __tablename__ = "studio_generation"

    id: Mapped[str] = ulid_pk()
    #: Exactly what reaches the provider, after trimming. No template wraps it.
    prompt: Mapped[str] = mapped_column(sa.Text, nullable=False)
    model: Mapped[str] = mapped_column(sa.Text, nullable=False)
    aspect_ratio: Mapped[str] = mapped_column(sa.Text, nullable=False)
    count: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    #: Storage keys in the assets bucket, in the order they were attached.
    #: Keys rather than bytes (ADR-004); a list rather than a child table
    #: because nothing ever queries *by* reference.
    references: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=sa.text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (
        sa.CheckConstraint("count > 0", name="count_positive"),
        # The gallery's only query: newest first, paged by id.
        sa.Index("ix_studio_generation_created_at", "created_at"),
    )


class StudioImage(Base):
    """One generated picture. The bytes are in the object store (ADR-004)."""

    __tablename__ = "studio_image"

    id: Mapped[str] = ulid_pk()
    generation_id: Mapped[str] = mapped_column(
        ULIDType,
        sa.ForeignKey("studio_generation.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: 1-based position within the request.
    index: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    storage_key: Mapped[str] = mapped_column(sa.Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    width: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    height: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    #: Provider metadata (model actually used, finish reason), for explaining a
    #: picture without re-running it.
    provider_meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (
        sa.UniqueConstraint("generation_id", "index"),
        sa.Index("ix_studio_image_generation_id", "generation_id"),
    )
