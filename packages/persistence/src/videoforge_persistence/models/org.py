"""Ownership and configuration: workspace, user, series.

These three are the *stable* end of the schema — they change on human
timescales, carry no lifecycle state, and every other table roots in them.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from videoforge_persistence.base import Base
from videoforge_persistence.columns import (
    ULIDType,
    created_at_col,
    jsonb_col,
    ulid_pk,
    updated_at_col,
)
from videoforge_persistence.enum_types import USER_ROLE
from videoforge_shared.enums import UserRole


class Workspace(Base):
    """The tenancy root. v1 seeds exactly one.

    It exists in v1 despite being a single row because retrofitting a tenancy
    column onto twelve tables later is a migration with a lock on every one of
    them; carrying an unused foreign key now costs 26 bytes a row.
    """

    __tablename__ = "workspace"

    id: Mapped[str] = ulid_pk()
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    settings: Mapped[dict[str, Any]] = jsonb_col()
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()


class AppUser(Base):
    """A human actor.

    **Named ``app_user``, not ``user`` (finding B7).** ``USER`` is a reserved
    SQL keyword *and* a Postgres function returning the session user, so an
    unquoted ``select ... from user`` silently does something else entirely.
    Every hand-written query, every migration, and every psql session would
    have needed double quotes forever. The rename costs one word.
    """

    __tablename__ = "app_user"

    id: Mapped[str] = ulid_pk()
    workspace_id: Mapped[str] = mapped_column(
        ULIDType, sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(sa.Text, nullable=False)
    display_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    role: Mapped[UserRole] = mapped_column(
        USER_ROLE, nullable=False, default=UserRole.OWNER
    )
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    __table_args__ = (
        # Scoped to the workspace rather than global: the same person may
        # legitimately hold accounts in two workspaces once tenancy is real.
        sa.UniqueConstraint("workspace_id", "email"),
    )


class Series(Base):
    """A recurring show — the unit that owns *style*.

    Style consistency across a video's ~20 illustrations is what separates
    professional output from a slideshow (risk R7), and consistency across
    *episodes* is what makes a series recognisable. Both are configuration,
    so they live here and are inherited by every project in the series.

    ``auto_approve_policy`` is the ``ApprovalPolicy`` seam (SADD §11): six
    mandatory human gates per video is heavy at volume, so gates are
    configurable — defaulting, per the decision recorded in M0, to all-manual.
    """

    __tablename__ = "series"

    id: Mapped[str] = ulid_pk()
    workspace_id: Mapped[str] = mapped_column(
        ULIDType, sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    title: Mapped[str] = mapped_column(sa.Text, nullable=False)
    style_preset: Mapped[dict[str, Any]] = jsonb_col()
    voice_preset: Mapped[dict[str, Any]] = jsonb_col()
    music_policy: Mapped[dict[str, Any]] = jsonb_col()
    hashtag_template: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    auto_approve_policy: Mapped[dict[str, Any]] = jsonb_col()
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    __table_args__ = (sa.Index("ix_series_workspace_id", "workspace_id"),)
