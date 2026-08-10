"""Review decisions and comments (SADD §17)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa

from videoforge_persistence.models import ArtifactVersion, Comment, ReviewDecision
from videoforge_persistence.repositories.base import Repository, affected_rows
from videoforge_shared.enums import ReviewDecisionKind
from videoforge_shared.ids import new_ulid

__all__ = ["CommentRepository", "ReviewRepository"]


class ReviewRepository(Repository):
    """Append-only. There is no ``update`` here and there cannot be.

    Changing your mind is a new row, which is what makes "approve an older
    version" (§12.5 rollback) work with no special case — it is simply the
    newest APPROVE, and the ``artifact_version_status`` view follows.
    """

    def record(
        self,
        *,
        artifact_version_id: str,
        decision: ReviewDecisionKind,
        reviewer_id: str | None = None,
        comment: str | None = None,
        reasons: Sequence[str] | None = None,
    ) -> ReviewDecision:
        row = ReviewDecision(
            id=new_ulid(),
            artifact_version_id=artifact_version_id,
            decision=decision,
            reviewer_id=reviewer_id,
            comment=comment,
            reasons=list(reasons or []),
        )
        self.session.add(row)
        return row

    def last_rejection(self, artifact_id: str) -> ReviewDecision | None:
        """The most recent REJECT across every version of an artifact (M3-10).

        **Across versions, not for one version.** A regeneration produces a new
        version, and the rejection that prompted it belongs to the previous
        one — so a per-version lookup would find nothing at exactly the moment
        the correction is needed.

        Newest first by ``decided_at`` then ``id``, matching the ordering the
        status view uses, so "the last rejection" means the same thing here as
        it does there.
        """
        stmt = (
            sa.select(ReviewDecision)
            .join(
                ArtifactVersion,
                ArtifactVersion.id == ReviewDecision.artifact_version_id,
            )
            .where(
                ArtifactVersion.artifact_id == artifact_id,
                ReviewDecision.decision == ReviewDecisionKind.REJECT,
            )
            .order_by(ReviewDecision.decided_at.desc(), ReviewDecision.id.desc())
            .limit(1)
        )
        return self.session.scalars(stmt).one_or_none()

    def for_version(self, artifact_version_id: str) -> list[ReviewDecision]:
        """Newest first. Every decision, not just the effective one — the
        review UI shows "rejected, then approved" as history."""
        stmt = (
            sa.select(ReviewDecision)
            .where(ReviewDecision.artifact_version_id == artifact_version_id)
            .order_by(ReviewDecision.decided_at.desc(), ReviewDecision.id.desc())
        )
        return list(self.session.scalars(stmt))

    def latest_for_version(self, artifact_version_id: str) -> ReviewDecision | None:
        """The decision the status view considers effective.

        Ordering matches the view's ``DISTINCT ON`` exactly — ``decided_at``
        then ``id``, both descending. If these two ever disagree, the API
        would report one thing and the view another.
        """
        stmt = (
            sa.select(ReviewDecision)
            .where(ReviewDecision.artifact_version_id == artifact_version_id)
            .order_by(ReviewDecision.decided_at.desc(), ReviewDecision.id.desc())
            .limit(1)
        )
        return self.session.scalars(stmt).one_or_none()


class CommentRepository(Repository):
    """Notes that decide nothing — mutable, unlike decisions."""

    def add(
        self,
        *,
        artifact_version_id: str,
        body: str,
        author_id: str | None = None,
        anchor: dict[str, Any] | None = None,
    ) -> Comment:
        comment = Comment(
            id=new_ulid(),
            artifact_version_id=artifact_version_id,
            body=body,
            author_id=author_id,
            anchor=anchor,
        )
        self.session.add(comment)
        return comment

    def for_version(self, artifact_version_id: str) -> list[Comment]:
        stmt = (
            sa.select(Comment)
            .where(Comment.artifact_version_id == artifact_version_id)
            .order_by(Comment.created_at, Comment.id)
        )
        return list(self.session.scalars(stmt))

    def edit(self, comment_id: str, body: str) -> bool:
        result = self.session.execute(
            sa.update(Comment).where(Comment.id == comment_id).values(body=body)
        )
        return affected_rows(result) == 1

    def delete(self, comment_id: str) -> bool:
        result = self.session.execute(
            sa.delete(Comment).where(Comment.id == comment_id)
        )
        return affected_rows(result) == 1
