"""M1-02: job mechanics and approval policy."""

from __future__ import annotations

import pytest
from videoforge_domain.approval_policy import AUTO_APPROVE_KEY, ApprovalPolicy
from videoforge_domain.job_lifecycle import (
    IllegalJobTransitionError,
    JobEvent,
    apply_job_event,
    is_finished,
    may_retry,
    next_status_after_failure,
)

from videoforge_shared.enums import ArtifactKind, JobStatus


class TestJobLifecycle:
    def test_queued_to_running_to_succeeded(self) -> None:
        status = JobStatus.QUEUED
        for event, expected in (
            (JobEvent.CLAIMED, JobStatus.RUNNING),
            (JobEvent.SUCCEEDED, JobStatus.SUCCEEDED),
        ):
            status = apply_job_event(status, event).to_status
            assert status is expected

    def test_succeeded_is_final(self) -> None:
        """The guard behind the double-delivery test (§14.3, M1-04).

        At-least-once delivery means a SUCCEEDED job *will* be redelivered.
        If it could re-run, the second delivery would produce a second
        artifact version — the exact bug idempotency exists to prevent.
        """
        assert is_finished(JobStatus.SUCCEEDED)
        for event in JobEvent:
            with pytest.raises(IllegalJobTransitionError):
                apply_job_event(JobStatus.SUCCEEDED, event)

    def test_orphaned_can_be_requeued(self) -> None:
        assert (
            apply_job_event(JobStatus.ORPHANED, JobEvent.REQUEUED).to_status
            is JobStatus.QUEUED
        )

    def test_cancelled_is_final(self) -> None:
        assert is_finished(JobStatus.CANCELLED)
        with pytest.raises(IllegalJobTransitionError):
            apply_job_event(JobStatus.CANCELLED, JobEvent.REQUEUED)

    @pytest.mark.parametrize(
        ("attempt", "expected"),
        [(0, True), (1, True), (2, True), (3, False), (4, False)],
    )
    def test_retry_budget_boundary(self, attempt: int, expected: bool) -> None:
        """``attempt`` counts attempts already made, so the check is strict.

        Off by one here is either a job that never retries or one that retries
        forever, and both fail quietly — which is why the boundary is
        enumerated rather than spot-checked.
        """
        assert may_retry(JobStatus.FAILED, attempt, max_attempts=3) is expected

    def test_running_job_may_not_retry(self) -> None:
        """Retry is a question about a *finished* attempt."""
        assert not may_retry(JobStatus.RUNNING, 0, max_attempts=3)

    def test_next_status_after_failure(self) -> None:
        assert next_status_after_failure(0, 3) is JobStatus.QUEUED
        assert next_status_after_failure(3, 3) is JobStatus.FAILED


class TestApprovalPolicy:
    def test_default_is_all_manual(self) -> None:
        """The decision recorded in M0: gates are opt-out, never opt-in."""
        policy = ApprovalPolicy.all_manual()
        assert all(policy.requires_human(kind) for kind in ArtifactKind)

    def test_missing_config_is_all_manual(self) -> None:
        for raw in (None, {}, {"unrelated": True}):
            assert ApprovalPolicy.from_jsonb(raw) == ApprovalPolicy.all_manual()

    def test_malformed_config_fails_safe(self) -> None:
        """A corrupt policy must get *stricter*, never looser.

        The failure mode being avoided: a malformed blob that parses as
        "auto-approve everything" would publish unreviewed video. An
        unnecessary click is the acceptable direction to fail in.
        """
        for raw in (
            {AUTO_APPROVE_KEY: "script"},  # string, not a list
            {AUTO_APPROVE_KEY: None},
            {AUTO_APPROVE_KEY: 42},
        ):
            assert ApprovalPolicy.from_jsonb(raw) == ApprovalPolicy.all_manual()

    def test_unknown_kind_is_ignored_not_fatal(self) -> None:
        """A renamed kind must not auto-approve, and must not crash the series.

        Crashing would make one stale config row take down every project in
        the series; auto-approving would be the unsafe direction. Dropping the
        entry keeps the gate and keeps the series working.
        """
        policy = ApprovalPolicy.from_jsonb(
            {AUTO_APPROVE_KEY: ["script", "no_such_kind"]}
        )
        assert policy.is_automatic(ArtifactKind.SCRIPT)
        assert policy.requires_human(ArtifactKind.IMAGE)
        assert len(policy.auto_approve) == 1

    def test_round_trips_through_jsonb(self) -> None:
        policy = ApprovalPolicy.all_manual().with_automatic(
            ArtifactKind.SCRIPT, ArtifactKind.RESEARCH
        )
        assert ApprovalPolicy.from_jsonb(policy.to_jsonb()) == policy

    def test_serialisation_is_stable(self) -> None:
        """Sorted output, so an unchanged policy produces no diff."""
        policy = ApprovalPolicy.all_manual().with_automatic(
            ArtifactKind.SCRIPT, ArtifactKind.RESEARCH
        )
        assert policy.to_jsonb() == {AUTO_APPROVE_KEY: ["research", "script"]}

    def test_policy_is_immutable(self) -> None:
        """Frozen: a policy cannot change between the check and the write."""
        base = ApprovalPolicy.all_manual()
        derived = base.with_automatic(ArtifactKind.SCRIPT)
        assert base == ApprovalPolicy.all_manual()
        assert derived.is_automatic(ArtifactKind.SCRIPT)
        assert base.requires_human(ArtifactKind.SCRIPT)

    def test_without_automatic_restores_the_gate(self) -> None:
        policy = ApprovalPolicy.all_manual().with_automatic(ArtifactKind.SCRIPT)
        assert policy.without_automatic(ArtifactKind.SCRIPT) == (
            ApprovalPolicy.all_manual()
        )
