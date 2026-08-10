"""Review actions: approve, reject, edit, comment (SADD §17).

The human gate. Everything here writes a ``review_decision`` or a new
``artifact_version`` plus the transition, audit event and outbox event that
explain it — in one transaction, per §10.3 rule 6.

The FSM decides what is allowed; this module never checks states by hand. A
service that wrote ``if artifact.state == "AWAITING_APPROVAL"`` would be a
second, drifting copy of the rules the UI renders its buttons from.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass

from videoforge_domain.artifact_lifecycle import (
    ArtifactEvent,
    IllegalTransitionError,
    Transition,
    apply_event,
)
from videoforge_persistence.models import Artifact, ArtifactVersion
from videoforge_persistence.projection import refresh_project_state
from videoforge_persistence.uow import UnitOfWork
from videoforge_shared.enums import (
    ArtifactKind,
    ArtifactState,
    ReviewDecisionKind,
    SubjectType,
    VersionOrigin,
)
from videoforge_shared.hashing import sha256_bytes

logger = logging.getLogger(__name__)

__all__ = [
    "BatchOutcome",
    "ReviewService",
    "SkippedApproval",
    "StaleVersionError",
]


class StaleVersionError(RuntimeError):
    """The reviewer acted on a version that is no longer the current one.

    SADD §19.1 puts ``expected_version_no`` in the review request for this
    reason: two tabs, or a regeneration that landed while the reviewer was
    reading, must not let an approval silently apply to content nobody looked
    at. Mapped to 409 Conflict.
    """

    def __init__(self, expected: int, actual: int) -> None:
        super().__init__(
            f"expected version {expected}, but the artifact is at {actual}"
        )
        self.expected = expected
        self.actual = actual


@dataclass(frozen=True, slots=True)
class ReviewOutcome:
    artifact: Artifact
    version: ArtifactVersion


@dataclass(frozen=True, slots=True)
class SkippedApproval:
    """One version a batch could not approve, and why.

    Carried rather than raised: see :meth:`ReviewService.approve_many`.
    """

    version_id: str
    reason: str


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    approved: tuple[ReviewOutcome, ...]
    skipped: tuple[SkippedApproval, ...]


class ReviewService:
    def __init__(self, uow: UnitOfWork) -> None:
        self._uow = uow

    def _load(
        self, version_id: str, expected_version_no: int | None
    ) -> tuple[Artifact, ArtifactVersion]:
        version = self._uow.versions.get(version_id)
        if version is None:
            raise LookupError(f"artifact version {version_id} not found")
        artifact = self._uow.artifacts.get(version.artifact_id)
        if artifact is None:  # pragma: no cover - FK makes this unreachable
            raise LookupError(f"artifact {version.artifact_id} not found")
        if (
            expected_version_no is not None
            and expected_version_no != version.version_no
        ):
            raise StaleVersionError(expected_version_no, version.version_no)
        return artifact, version

    def approve(
        self,
        version_id: str,
        *,
        actor_id: str | None = None,
        comment: str | None = None,
        expected_version_no: int | None = None,
    ) -> ReviewOutcome:
        """Approve one specific version.

        Approval always targets an explicit version id, which is what makes
        §12.5's rollback work with no special case: approving an older version
        is simply the newest APPROVE, and the status view follows.
        """
        artifact, version = self._load(version_id, expected_version_no)
        transition = apply_event(ArtifactState(artifact.state), ArtifactEvent.APPROVED)

        self._uow.reviews.record(
            artifact_version_id=version.id,
            decision=ReviewDecisionKind.APPROVE,
            reviewer_id=actor_id,
            comment=comment,
        )
        artifact.state = transition.to_state
        self._uow.artifacts.clear_stale(artifact.id)
        self._record(artifact, version, transition, actor_id, "artifact.approved")
        # The pointer cache follows the decision; the view remains the
        # authority on what "approved" means (B1).
        self._uow.projects.set_active_pointer(
            artifact.project_id, artifact.kind.value, version.id
        )
        # An approval is the one transition that invalidates other artifacts
        # (finding S2), so it is the one that passes ``approved_kind``.
        refresh_project_state(
            self._uow, artifact.project_id, approved_kind=ArtifactKind(artifact.kind)
        )
        return ReviewOutcome(artifact=artifact, version=version)

    def approve_many(
        self,
        version_ids: Sequence[str],
        *,
        actor_id: str | None = None,
        comment: str | None = None,
    ) -> BatchOutcome:
        """Approve a set of versions in one transaction (M3-09, risk R9).

        **Why this exists.** Twenty scene images means twenty approvals, and at
        that point the human gate is the bottleneck on the very first video —
        R9 predicted it and the image fan-out delivered it. A reviewer scanning
        a contact sheet decides *once*, about a set.

        **Explicit version ids, not "everything pending".** The caller sends the
        versions it actually put on screen. A server-side "approve whatever is
        currently awaiting" would sweep up a scene that regenerated while the
        reviewer was scrolling — the exact failure ``expected_version_no``
        exists to prevent on the single-item path, reintroduced twenty at a
        time.

        **Partial success is the honest outcome**, so this returns rather than
        raises. A version that raced ahead is skipped with its reason and the
        other nineteen still land; failing the batch would make one stale tile
        cost the reviewer the whole pass. Skips are per item and named, so the
        UI can say which ones need another look.

        Everything is in the caller's transaction, so a failure that *does*
        escape rolls back the lot.
        """
        approved: list[ReviewOutcome] = []
        skipped: list[SkippedApproval] = []

        for version_id in version_ids:
            try:
                approved.append(
                    self.approve(version_id, actor_id=actor_id, comment=comment)
                )
            except LookupError:
                skipped.append(SkippedApproval(version_id, "not found"))
            except IllegalTransitionError as exc:
                # The FSM refusing is the ordinary case here, not an error: the
                # tile was already approved, or a regeneration moved it back to
                # GENERATING. Its own words are the most useful message.
                skipped.append(SkippedApproval(version_id, str(exc)))

        logger.info(
            "batch approval",
            extra={"approved": len(approved), "skipped": len(skipped)},
        )
        return BatchOutcome(approved=tuple(approved), skipped=tuple(skipped))

    def reject(
        self,
        version_id: str,
        *,
        actor_id: str | None = None,
        comment: str | None = None,
        reasons: Sequence[str] | None = None,
        expected_version_no: int | None = None,
    ) -> ReviewOutcome:
        """Reject a version, optionally saying **why** in structured form.

        ``reasons`` is what makes the next attempt different (M3-10). Stored
        verbatim; the vocabulary is the domain's
        (:class:`videoforge_domain.rejection.RejectionReason`) and validation
        happens at the API boundary, so a worker reading an older row is never
        broken by a reason this build has retired.
        """
        artifact, version = self._load(version_id, expected_version_no)
        transition = apply_event(ArtifactState(artifact.state), ArtifactEvent.REJECTED)

        self._uow.reviews.record(
            artifact_version_id=version.id,
            decision=ReviewDecisionKind.REJECT,
            reviewer_id=actor_id,
            comment=comment,
            reasons=reasons,
        )
        artifact.state = transition.to_state
        self._record(artifact, version, transition, actor_id, "artifact.rejected")
        # No cascade: rejecting invalidates nothing downstream, because nothing
        # downstream was ever built on a version that was never approved. The
        # phase still moves — §12.4's "rollback is just rejecting a version".
        refresh_project_state(self._uow, artifact.project_id)
        return ReviewOutcome(artifact=artifact, version=version)

    def edit(
        self,
        artifact_id: str,
        content: dict[str, object],
        *,
        actor_id: str | None = None,
    ) -> ReviewOutcome:
        """A human writes the content themselves → a new version.

        ``origin=human_edit`` is the *only* difference from a generated
        version: same table, same transitions, same review gate. That is the
        point — the pipeline must not care who typed the words, while the
        audit trail must be able to say.

        It lands in AWAITING_APPROVAL rather than APPROVED. Writing something
        is not the same as signing off on it, and collapsing the two would let
        an edit bypass the gate entirely.
        """
        artifact = self._uow.artifacts.get(artifact_id)
        if artifact is None:
            raise LookupError(f"artifact {artifact_id} not found")

        transition = apply_event(
            ArtifactState(artifact.state), ArtifactEvent.HUMAN_EDITED
        )
        canonical = json.dumps(content, sort_keys=True, separators=(",", ":"))
        version = self._uow.versions.add_version(
            artifact,
            origin=VersionOrigin.HUMAN_EDIT,
            content_hash=sha256_bytes(canonical.encode()),
            inline_content=dict(content),
            created_by=actor_id,
        )
        artifact.state = transition.to_state
        self._record(artifact, version, transition, actor_id, "artifact.edited")
        refresh_project_state(self._uow, artifact.project_id)
        return ReviewOutcome(artifact=artifact, version=version)

    def comment(
        self,
        version_id: str,
        body: str,
        *,
        actor_id: str | None = None,
        anchor: dict[str, object] | None = None,
    ) -> None:
        """A note that decides nothing — no transition, no outbox event."""
        self._uow.comments.add(
            artifact_version_id=version_id,
            body=body,
            author_id=actor_id,
            anchor=dict(anchor) if anchor else None,
        )
        self._uow.audit.record_event(
            event_type="artifact.commented",
            subject_type=SubjectType.ARTIFACT,
            subject_id=version_id,
            actor_id=actor_id,
        )

    def _record(
        self,
        artifact: Artifact,
        version: ArtifactVersion,
        transition: Transition,
        actor_id: str | None,
        event_type: str,
    ) -> None:
        """Transition + audit + outbox, together. One helper so no review
        action can accidentally write two of the three."""
        payload = {
            "project_id": artifact.project_id,
            "artifact_id": artifact.id,
            "version_id": version.id,
            "version_no": version.version_no,
            "kind": artifact.kind.value,
        }
        self._uow.audit.record_transition(
            subject_type=SubjectType.ARTIFACT,
            subject_id=artifact.id,
            from_state=transition.from_state.value,
            to_state=transition.to_state.value,
            cause=transition.cause,
            actor_id=actor_id,
        )
        self._uow.audit.record_event(
            event_type=event_type,
            subject_type=SubjectType.ARTIFACT,
            subject_id=artifact.id,
            actor_id=actor_id,
            payload=payload,
        )
        self._uow.outbox.enqueue(event_type=event_type, payload=payload)
