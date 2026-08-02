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
from dataclasses import dataclass

from videoforge_domain.artifact_lifecycle import (
    ArtifactEvent,
    Transition,
    apply_event,
)

from videoforge_persistence.models import Artifact, ArtifactVersion
from videoforge_persistence.uow import UnitOfWork
from videoforge_shared.enums import (
    ArtifactState,
    ReviewDecisionKind,
    SubjectType,
    VersionOrigin,
)
from videoforge_shared.hashing import sha256_bytes

logger = logging.getLogger(__name__)

__all__ = ["ReviewService", "StaleVersionError"]


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
        return ReviewOutcome(artifact=artifact, version=version)

    def reject(
        self,
        version_id: str,
        *,
        actor_id: str | None = None,
        comment: str | None = None,
        expected_version_no: int | None = None,
    ) -> ReviewOutcome:
        artifact, version = self._load(version_id, expected_version_no)
        transition = apply_event(ArtifactState(artifact.state), ArtifactEvent.REJECTED)

        self._uow.reviews.record(
            artifact_version_id=version.id,
            decision=ReviewDecisionKind.REJECT,
            reviewer_id=actor_id,
            comment=comment,
        )
        artifact.state = transition.to_state
        self._record(artifact, version, transition, actor_id, "artifact.rejected")
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
