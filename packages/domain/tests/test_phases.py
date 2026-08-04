"""M2-03: phase derivation.

Pure, so these run with dict literals and no database. The cases that matter
are the ones where "the obvious rule" gives the wrong answer — parallel stages
especially.
"""

from __future__ import annotations

from typing import Any

import pytest

from videoforge_domain.phases import derive_phase, has_failures
from videoforge_domain.pipeline import Pipeline
from videoforge_shared.enums import ArtifactKind, ArtifactState, ProjectPhase
from videoforge_shared.pipeline_file import load_pipeline_mapping

APPROVED = ArtifactState.APPROVED
GENERATING = ArtifactState.GENERATING
AWAITING = ArtifactState.AWAITING_APPROVAL


@pytest.fixture(scope="module")
def pipeline() -> Pipeline:
    """The real declaration — phase derivation is only meaningful against it."""
    return Pipeline.from_mapping(load_pipeline_mapping())


def _states(**kwargs: Any) -> dict[ArtifactKind, ArtifactState]:
    return {ArtifactKind(k): v for k, v in kwargs.items()}


class TestDerivePhase:
    def test_a_project_with_no_artifacts_is_a_draft(self, pipeline: Pipeline) -> None:
        assert derive_phase(pipeline, {}) is ProjectPhase.DRAFT

    def test_generating_reports_the_generating_phase(self, pipeline: Pipeline) -> None:
        assert (
            derive_phase(pipeline, _states(research=GENERATING))
            is ProjectPhase.RESEARCHING
        )

    def test_awaiting_review_reports_the_review_phase(self, pipeline: Pipeline) -> None:
        assert (
            derive_phase(pipeline, _states(research=AWAITING))
            is ProjectPhase.RESEARCH_REVIEW
        )

    def test_a_rejection_stays_in_review(self, pipeline: Pipeline) -> None:
        """§12.4: rollback is just rejecting a version. The project does not
        advance, and it does not need a phase of its own to say so."""
        assert (
            derive_phase(
                pipeline, _states(research=APPROVED, script=ArtifactState.REJECTED)
            )
            is ProjectPhase.SCRIPT_REVIEW
        )

    def test_an_approved_stage_hands_over_to_the_next(self, pipeline: Pipeline) -> None:
        """Research approved, script not started — the project is at the script
        stage even though nothing is running yet."""
        assert (
            derive_phase(pipeline, _states(research=APPROVED)) is ProjectPhase.SCRIPTING
        )

    def test_generation_beats_review_across_parallel_stages(
        self, pipeline: Pipeline
    ) -> None:
        """The case the obvious rule gets wrong.

        Images and voice run concurrently (ADR-009). With images awaiting
        review while the voice track is still synthesising, "the earliest
        unfinished stage" would report MEDIA_REVIEW — inviting a reviewer to a
        stage that cannot complete, because timeline needs both.
        """
        states = _states(
            research=APPROVED,
            script=APPROVED,
            scene_set=APPROVED,
            prompt=APPROVED,
            image=AWAITING,
            voice=GENERATING,
        )
        assert derive_phase(pipeline, states) is ProjectPhase.MEDIA_GENERATION

    def test_a_fully_approved_pipeline_is_ready_to_publish(
        self, pipeline: Pipeline
    ) -> None:
        states = {stage.produces: APPROVED for stage in pipeline.stages}
        assert derive_phase(pipeline, states) is ProjectPhase.READY_TO_PUBLISH

    def test_published_is_never_derived(self, pipeline: Pipeline) -> None:
        """Publishing is an act, not a consequence of approvals. If derivation
        could produce PUBLISHED, a project would claim to be live because a zip
        file was approved."""
        every = [
            derive_phase(pipeline, {stage.produces: state for stage in pipeline.stages})
            for state in ArtifactState
        ]
        assert ProjectPhase.PUBLISHED not in every


class TestHasFailures:
    def test_failure_is_a_flag_not_a_phase(self, pipeline: Pipeline) -> None:
        """§12.4 calls HAS_FAILURES orthogonal. One failed image out of twenty
        must not drag the whole project backwards."""
        states = _states(
            research=APPROVED,
            script=APPROVED,
            scene_set=APPROVED,
            prompt=APPROVED,
            voice=APPROVED,
            image=ArtifactState.FAILED,
        )
        assert has_failures(states) is True
        assert derive_phase(pipeline, states) is ProjectPhase.MEDIA_REVIEW

    def test_a_healthy_project_has_no_failures(self) -> None:
        assert has_failures(_states(script=APPROVED)) is False
