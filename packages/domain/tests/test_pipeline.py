"""M2-02: the pipeline graph.

Built from dict literals throughout — no fixture files, no filesystem. That is
the point of keeping the YAML read in ``videoforge_shared``: every rule here is
testable against three lines of data.

The shipped declaration gets its own test at the bottom, because a graph that
validates in the abstract and a graph that matches SADD §13 are different
claims.
"""

from __future__ import annotations

from typing import Any

import pytest

from videoforge_domain.pipeline import Pipeline, PipelineError
from videoforge_shared.enums import ArtifactKind
from videoforge_shared.pipeline_file import load_pipeline_mapping


def _stage(
    produces: str, requires: list[str] | None = None, **over: Any
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "produces": produces,
        "requires": requires or [],
        "queue": "llm",
        "phase_generating": "SCRIPTING",
        "phase_review": "SCRIPT_REVIEW",
    }
    base.update(over)
    return base


def _pipeline(*stages: dict[str, Any]) -> Pipeline:
    return Pipeline.from_mapping({"stages": list(stages)})


class TestValidation:
    """Every case here is a config mistake that is otherwise silent.

    The shared failure mode: a project that stops advancing, with no error in
    any log, because a stage can never become available. Failing at load time
    turns that into a message at boot.
    """

    def test_requires_must_be_an_artifact_kind(self) -> None:
        """ADR-016's homogeneity rule, enforced rather than remembered.

        ``character`` is the concrete thing this forbids: series-scoped
        branding is an admission check in the dispatch service, never an edge
        in this graph.
        """
        with pytest.raises(PipelineError) as excinfo:
            _pipeline(_stage("image", ["character"]))

        message = str(excinfo.value)
        assert "not an artifact kind" in message
        assert "ADR-016" in message
        # The message lists the alternatives — a validator that says "invalid"
        # and stops has made the operator's job harder, not easier.
        assert "scene_set" in message

    def test_requirements_must_be_produced_by_some_stage(self) -> None:
        with pytest.raises(PipelineError, match="which no stage produces"):
            _pipeline(_stage("script", ["research"]))

    def test_two_stages_cannot_produce_one_kind(self) -> None:
        """Otherwise ``active_pointers[kind]`` is ambiguous — finding S1 again,
        one level up: the constraint stops two *artifacts* of a kind, this
        stops two *stages* claiming to make it."""
        with pytest.raises(PipelineError, match="two stages both produce"):
            _pipeline(_stage("script"), _stage("script"))

    def test_cycles_are_rejected_and_named(self) -> None:
        with pytest.raises(PipelineError) as excinfo:
            _pipeline(
                _stage("script", ["timeline"]),
                _stage("timeline", ["script"]),
            )
        message = str(excinfo.value)
        assert "cycle" in message
        # Naming the members is the difference between a fixable error and a
        # hunt through nine stages.
        assert "script" in message and "timeline" in message

    def test_a_stage_cannot_require_itself(self) -> None:
        with pytest.raises(PipelineError, match="requires itself"):
            _pipeline(_stage("script", ["script"]))

    def test_a_stage_needs_a_queue(self) -> None:
        with pytest.raises(PipelineError, match="no queue"):
            _pipeline(_stage("script", queue=""))

    def test_phases_must_be_real(self) -> None:
        with pytest.raises(PipelineError, match="no valid generating phase"):
            _pipeline(_stage("script", phase_generating="THINKING_ABOUT_IT"))

    def test_an_empty_declaration_is_rejected(self) -> None:
        with pytest.raises(PipelineError, match="no 'stages' list"):
            Pipeline.from_mapping({"stages": []})

    def test_a_valid_graph_loads(self) -> None:
        """Positive control. Without it every test above passes against a
        ``from_mapping`` that raises unconditionally."""
        pipeline = _pipeline(_stage("research"), _stage("script", ["research"]))
        assert pipeline.has_stage(ArtifactKind.SCRIPT)


class TestQueries:
    @pytest.fixture
    def pipeline(self) -> Pipeline:
        return _pipeline(
            _stage("research"),
            _stage("script", ["research"]),
            _stage("scene_set", ["script"]),
            _stage("voice", ["scene_set"]),
            _stage("prompt", ["scene_set"]),
            _stage("image", ["prompt"]),
            _stage("timeline", ["image", "voice"]),
        )

    def test_roots_are_the_stages_that_need_nothing(self, pipeline: Pipeline) -> None:
        assert pipeline.roots() == frozenset({ArtifactKind.RESEARCH})

    def test_stages_come_back_in_dependency_order(self, pipeline: Pipeline) -> None:
        """Every stage appears after everything it requires."""
        seen: set[ArtifactKind] = set()
        for stage in pipeline.stages:
            assert stage.requires <= seen, f"{stage.produces.value} came too early"
            seen.add(stage.produces)

    def test_dependents_are_direct_only(self, pipeline: Pipeline) -> None:
        assert pipeline.dependents(ArtifactKind.SCENE_SET) == frozenset(
            {ArtifactKind.VOICE, ArtifactKind.PROMPT}
        )

    def test_descendants_are_the_staleness_blast_radius(
        self, pipeline: Pipeline
    ) -> None:
        """Finding S2. Approving a new script invalidates everything below it —
        and *not* the script itself, which is the whole point of approving it."""
        blast = pipeline.descendants(ArtifactKind.SCRIPT)
        assert blast == frozenset(
            {
                ArtifactKind.SCENE_SET,
                ArtifactKind.PROMPT,
                ArtifactKind.IMAGE,
                ArtifactKind.VOICE,
                ArtifactKind.TIMELINE,
            }
        )
        assert ArtifactKind.SCRIPT not in blast
        assert ArtifactKind.RESEARCH not in blast

    def test_images_and_voice_are_independent(self, pipeline: Pipeline) -> None:
        """ADR-009's reason for existing: neither is downstream of the other,
        so they run concurrently."""
        assert ArtifactKind.VOICE not in pipeline.descendants(ArtifactKind.IMAGE)
        assert ArtifactKind.IMAGE not in pipeline.descendants(ArtifactKind.VOICE)

    def test_unmet_names_what_is_missing(self, pipeline: Pipeline) -> None:
        """The UI renders this — "waiting on: script" beats a dead button."""
        assert pipeline.unmet(ArtifactKind.SCENE_SET, []) == frozenset(
            {ArtifactKind.SCRIPT}
        )
        assert (
            pipeline.unmet(ArtifactKind.SCENE_SET, [ArtifactKind.SCRIPT]) == frozenset()
        )

    def test_unknown_kinds_raise_rather_than_return_empty(
        self, pipeline: Pipeline
    ) -> None:
        """An empty answer would read as "nothing blocks this", which for a
        stage that does not exist is the most dangerous possible reply."""
        with pytest.raises(PipelineError, match="no stage produces"):
            pipeline.stage_for(ArtifactKind.MUSIC)


class TestShippedDeclaration:
    """``templates/pipeline.yaml`` itself — a separate claim from "the loader
    works". This is the file the containers actually boot with."""

    @pytest.fixture
    def pipeline(self) -> Pipeline:
        return Pipeline.from_mapping(load_pipeline_mapping())

    def test_it_loads(self, pipeline: Pipeline) -> None:
        assert pipeline.stages

    def test_it_matches_the_stage_table(self, pipeline: Pipeline) -> None:
        """SADD §13 lists the stages and their queues. Transcribed once here,
        so a queue renamed in the YAML and nowhere else fails a test rather
        than sending jobs to a queue no worker consumes."""
        assert {s.produces.value: s.queue for s in pipeline.stages} == {
            "research": "llm",
            "script": "llm",
            "scene_set": "llm",
            "prompt": "llm",
            "image": "image",
            "voice": "voice",
            "timeline": "timeline",
            "render": "render",
            # M5-01/02. `thumbnail` is on `image` despite calling no provider:
            # that worker carries the fonts and Pillow M4-02 put there.
            "caption": "llm",
            "thumbnail": "image",
            "package": "package",
        }

    def test_the_publish_stages_come_after_the_render(self, pipeline: Pipeline) -> None:
        """**Order here is not cosmetic.** ``Pipeline.stages`` is the sequence
        ``derive_phase`` walks, returning the earliest stage that is not
        approved — so a caption that depended only on ``script`` would sort
        third and report ``PACKAGING`` for a project that had not written its
        scenes yet. The edge to ``render`` is what holds it back, and this is
        the assertion that says so."""
        order = [stage.produces.value for stage in pipeline.stages]
        assert order.index("caption") > order.index("render")
        assert order.index("thumbnail") > order.index("caption")
        assert order[-1] == "package"

    def test_a_project_starts_at_research(self, pipeline: Pipeline) -> None:
        assert pipeline.roots() == frozenset({ArtifactKind.RESEARCH})

    def test_voice_is_not_per_scene(self, pipeline: Pipeline) -> None:
        """Finding B3 revised: one synthesis call for the whole script, with
        word timestamps. Twenty isolated reads concatenate into a list, not a
        narration — so this flag being wrong is an audible defect."""
        assert pipeline.stage_for(ArtifactKind.VOICE).parallelizable_per_scene is False
        assert pipeline.stage_for(ArtifactKind.IMAGE).parallelizable_per_scene is True

    def test_approving_a_script_invalidates_all_media(self, pipeline: Pipeline) -> None:
        """The cascade M2-04 will walk, asserted on the real graph."""
        blast = pipeline.descendants(ArtifactKind.SCRIPT)
        assert {ArtifactKind.IMAGE, ArtifactKind.VOICE, ArtifactKind.RENDER} <= blast
