"""M1-02: the artifact FSM, tested without a database.

That these run in milliseconds with no fixtures is the point of the layering
(SADD §10.1) — the workflow rules are where this system's real complexity
lives, and they should be the cheapest thing to test.
"""

from __future__ import annotations

import pytest
from videoforge_domain.artifact_lifecycle import (
    ArtifactEvent,
    IllegalTransitionError,
    apply_event,
    can_approve,
    can_edit,
    can_regenerate,
    can_reject,
    capabilities,
    is_terminal,
    legal_events,
)

from videoforge_shared.enums import ArtifactState, TransitionCause


class TestHappyPath:
    def test_generate_review_approve(self) -> None:
        """The spine of every stage: PENDING → GENERATING → review → approved."""
        state = ArtifactState.PENDING
        for event, expected in (
            (ArtifactEvent.GENERATION_STARTED, ArtifactState.GENERATING),
            (ArtifactEvent.GENERATION_SUCCEEDED, ArtifactState.AWAITING_APPROVAL),
            (ArtifactEvent.APPROVED, ArtifactState.APPROVED),
        ):
            transition = apply_event(state, event)
            assert transition.to_state is expected
            assert transition.from_state is state
            state = transition.to_state

    def test_reject_then_regenerate(self) -> None:
        """The M1 exit-test flow: a rejection must be recoverable."""
        transition = apply_event(
            ArtifactState.AWAITING_APPROVAL, ArtifactEvent.REJECTED
        )
        assert transition.to_state is ArtifactState.REJECTED
        assert transition.cause is TransitionCause.REVIEW

        retry = apply_event(ArtifactState.REJECTED, ArtifactEvent.REGENERATE_REQUESTED)
        assert retry.to_state is ArtifactState.GENERATING

    def test_human_edit_skips_generation(self) -> None:
        """§12.2: a human writing the content lands straight in review.

        Not in APPROVED — writing something is not the same as signing off on
        it, and collapsing the two would let an edit bypass the gate entirely.
        """
        transition = apply_event(ArtifactState.PENDING, ArtifactEvent.HUMAN_EDITED)
        assert transition.to_state is ArtifactState.AWAITING_APPROVAL
        assert transition.cause is TransitionCause.EDIT

    def test_regenerating_an_approved_artifact_is_allowed(self) -> None:
        """Approval is not a lock.

        Revising an approved script is ordinary — it is what drives §12.4's
        staleness cascade. If APPROVED were terminal, the only way to change
        a video would be to start a new project.
        """
        assert can_regenerate(ArtifactState.APPROVED)
        assert (
            apply_event(
                ArtifactState.APPROVED, ArtifactEvent.REGENERATE_REQUESTED
            ).to_state
            is ArtifactState.GENERATING
        )


class TestFailurePaths:
    def test_failed_job_is_retryable(self) -> None:
        transition = apply_event(
            ArtifactState.GENERATING, ArtifactEvent.GENERATION_FAILED
        )
        assert transition.to_state is ArtifactState.FAILED
        assert transition.cause is TransitionCause.JOB_FAILED
        assert can_regenerate(ArtifactState.FAILED)

    def test_orphaned_lands_in_failed_with_reconciler_cause(self) -> None:
        """§14.4: a vanished worker must surface, not silently reset.

        The cause matters as much as the state — an operator looking at a
        FAILED artifact needs to distinguish "the provider errored" from "the
        worker died", and the ``state_transition`` row is where that lives.
        """
        transition = apply_event(ArtifactState.GENERATING, ArtifactEvent.ORPHANED)
        assert transition.to_state is ArtifactState.FAILED
        assert transition.cause is TransitionCause.RECONCILER


class TestIllegalTransitionErrors:
    @pytest.mark.parametrize(
        ("state", "event"),
        [
            # Approving something nobody has generated.
            (ArtifactState.PENDING, ArtifactEvent.APPROVED),
            # Approving while a worker is still writing it.
            (ArtifactState.GENERATING, ArtifactEvent.APPROVED),
            # Approving twice — the double-click, and the double-delivery.
            (ArtifactState.APPROVED, ArtifactEvent.APPROVED),
            # Rejecting something already rejected.
            (ArtifactState.REJECTED, ArtifactEvent.REJECTED),
            # A success report for a job that was never running.
            (ArtifactState.PENDING, ArtifactEvent.GENERATION_SUCCEEDED),
            # Editing mid-generation: the worker would overwrite it.
            (ArtifactState.GENERATING, ArtifactEvent.HUMAN_EDITED),
        ],
    )
    def test_rejected(self, state: ArtifactState, event: ArtifactEvent) -> None:
        with pytest.raises(IllegalTransitionError) as excinfo:
            apply_event(state, event)
        assert excinfo.value.state is state
        assert excinfo.value.event is event

    def test_error_names_both_sides(self) -> None:
        """The message is what an operator reads at 2am; it must be specific."""
        with pytest.raises(IllegalTransitionError) as excinfo:
            apply_event(ArtifactState.GENERATING, ArtifactEvent.APPROVED)
        message = str(excinfo.value)
        assert "approved" in message
        assert "GENERATING" in message


class TestCapabilities:
    """The payload the UI renders buttons from (SADD §11)."""

    def test_awaiting_approval_offers_every_action(self) -> None:
        caps = capabilities(ArtifactState.AWAITING_APPROVAL)
        assert caps == {
            "can_approve": True,
            "can_reject": True,
            "can_regenerate": True,
            "can_edit": True,
        }

    def test_generating_offers_nothing(self) -> None:
        """While a worker holds it, every button is off."""
        assert capabilities(ArtifactState.GENERATING) == {
            "can_approve": False,
            "can_reject": False,
            "can_regenerate": False,
            "can_edit": False,
        }

    @pytest.mark.parametrize("state", list(ArtifactState))
    def test_capabilities_agree_with_the_table(self, state: ArtifactState) -> None:
        """The guards and the transition table are the same fact.

        This is the property that stops the UI lying: if a capability said
        True where ``apply_event`` raises, the user would get a button that
        409s. Checking every state means a future table edit cannot break the
        agreement silently.
        """
        events = legal_events(state)
        assert can_approve(state) is (ArtifactEvent.APPROVED in events)
        assert can_reject(state) is (ArtifactEvent.REJECTED in events)
        assert can_regenerate(state) is (ArtifactEvent.REGENERATE_REQUESTED in events)
        assert can_edit(state) is (ArtifactEvent.HUMAN_EDITED in events)

    @pytest.mark.parametrize("state", list(ArtifactState))
    def test_every_capability_is_actually_applicable(
        self, state: ArtifactState
    ) -> None:
        """A capability of True must mean ``apply_event`` succeeds."""
        for event in legal_events(state):
            assert apply_event(state, event).from_state is state


class TestReachability:
    def test_no_state_is_terminal_yet(self) -> None:
        """Documents a real property, and will fail loudly if it changes.

        Nothing locks today — even APPROVED accepts regeneration. When a
        PUBLISHED lock arrives this test is the reminder to decide
        deliberately rather than discover it.
        """
        assert not any(is_terminal(state) for state in ArtifactState)

    def test_every_state_is_reachable_from_pending(self) -> None:
        """A state nothing can reach is dead code in the schema's enum."""
        seen = {ArtifactState.PENDING}
        frontier = [ArtifactState.PENDING]
        while frontier:
            state = frontier.pop()
            for event in legal_events(state):
                nxt = apply_event(state, event).to_state
                if nxt not in seen:
                    seen.add(nxt)
                    frontier.append(nxt)
        assert seen == set(ArtifactState)
