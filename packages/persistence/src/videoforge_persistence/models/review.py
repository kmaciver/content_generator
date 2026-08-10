"""Human verdicts and human notes — deliberately two different tables.

A ``review_decision`` changes what the pipeline does and is immutable. A
``comment`` changes nothing and can be edited. Collapsing them into one table
with a nullable ``decision`` column would mean either making comments
immutable (annoying) or making decisions mutable (unauditable).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from videoforge_persistence.base import Base
from videoforge_persistence.columns import (
    TimestampType,
    ULIDType,
    created_at_col,
    nullable_jsonb_col,
    ulid_pk,
    updated_at_col,
)
from videoforge_persistence.enum_types import REVIEW_DECISION_KIND
from videoforge_shared.enums import ReviewDecisionKind


class ReviewDecision(Base):
    """A verdict on one specific version — immutable (SADD §10.2, §17).

    **This table is the source of truth for approval.** The
    ``artifact_version_status`` view computes ``APPROVED``/``REJECTED``/
    ``SUPERSEDED`` from these rows and nothing else (finding B1), and
    ``video_project.active_pointers`` is a cache rebuilt from them.

    Append-only means changing your mind is a *new row*, not an edit — which
    is exactly what makes "approve an older version" (§12.5 rollback) work
    without a special case: it is just the newest APPROVE.
    """

    __tablename__ = "review_decision"

    id: Mapped[str] = ulid_pk()
    artifact_version_id: Mapped[str] = mapped_column(
        ULIDType,
        sa.ForeignKey("artifact_version.id", ondelete="CASCADE"),
        nullable=False,
    )
    decision: Mapped[ReviewDecisionKind] = mapped_column(
        REVIEW_DECISION_KIND, nullable=False
    )
    comment: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    #: Structured rejection reasons (M3-10), from ``RejectionReason``.
    #:
    #: **A jsonb array of plain strings, not a Postgres enum array.** The
    #: vocabulary will change as new failure modes are found, and every change
    #: to a Postgres enum is an ``ALTER TYPE`` migration that cannot run inside
    #: a transaction with other DDL. The domain owns the vocabulary; unknown
    #: values read back are ignored rather than raising, so a reason retired in
    #: a later build never makes an old artifact impossible to regenerate.
    #:
    #: Empty for approvals. Nothing forbids reasons on an APPROVE — "approved,
    #: but the hands are odd" is a real thing to record — and the correction
    #: builder only ever reads them from a REJECT.
    reasons: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
    )
    #: **No foreign key** (finding M1-04a). Who approved a version is the most
    #: load-bearing fact in the audit trail; erasing a user must not erase it,
    #: and ``SET NULL`` was impossible anyway against this table's trigger.
    reviewer_id: Mapped[str | None] = mapped_column(ULIDType, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(
        TimestampType, nullable=False, server_default=sa.func.now()
    )
    created_at: Mapped[datetime] = created_at_col()
    # Immutable: no ``updated_at``, trigger raises on UPDATE.

    __table_args__ = (
        # The status view's driving index: latest decision per version.
        # DESC on both so the view's DISTINCT ON walks it in order.
        sa.Index(
            "ix_review_decision_version_decided_at",
            "artifact_version_id",
            sa.text("decided_at DESC"),
            sa.text("id DESC"),
        ),
    )


class Comment(Base):
    """A note on a version that decides nothing.

    Mutable, unlike everything else attached to a version: a typo in a note
    is not history worth preserving. ``anchor`` locates the comment inside the
    content (a character range in a script, a box on an image) so the review
    UI can pin it; NULL means it applies to the whole version.
    """

    __tablename__ = "comment"

    id: Mapped[str] = ulid_pk()
    artifact_version_id: Mapped[str] = mapped_column(
        ULIDType,
        sa.ForeignKey("artifact_version.id", ondelete="CASCADE"),
        nullable=False,
    )
    author_id: Mapped[str | None] = mapped_column(
        ULIDType, sa.ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
    body: Mapped[str] = mapped_column(sa.Text, nullable=False)
    anchor: Mapped[dict[str, Any] | None] = nullable_jsonb_col()
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    __table_args__ = (
        sa.Index("ix_comment_artifact_version_id", "artifact_version_id"),
    )
