"""ADR-019 — ``voice_direction.generate`` and its effect on ``voice.generate``.

``packages/domain/tests/test_voice_direction`` covers the rules. What only the
stages can get wrong is the joins: reading the approved scenes, refusing a
direction for the wrong scene set, sending the tags to the provider, and
keeping them out of the words captions are built from.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker
from tests.test_timeline_stage import _approve, _run

from videoforge.services.review import ReviewService
from videoforge_domain.voice_direction import InvalidDirectionError
from videoforge_persistence.uow import unit_of_work
from videoforge_shared.enums import ArtifactKind

pytestmark = pytest.mark.integration


@pytest.fixture()
def scened(
    monkeypatch: pytest.MonkeyPatch,
    sessions: sessionmaker[Session],
    branded_project: dict[str, str],
) -> str:
    """A project with approved scenes and prompts — where direction starts."""
    from tests.test_image_stage import _approve_character_with_sheets

    project = branded_project["project"]
    _approve_character_with_sheets(monkeypatch, sessions, branded_project)
    for kind in (
        ArtifactKind.RESEARCH,
        ArtifactKind.SCRIPT,
        ArtifactKind.SCENE_SET,
        ArtifactKind.PROMPT,
    ):
        _run(monkeypatch, sessions, project, kind)
        _approve(sessions, project, kind)
    return project


@pytest.fixture()
def tagging_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real mock completion, with ``parsed`` replaced by a model that tags
    scene 1, rewords scene 2, and leaves the rest alone."""
    import videoforge_workers.stages as stages
    import videoforge_workers.voice_direction as stage

    real = stages.llm_complete

    def _fake(ctx: Any, prompt: Any, schema: Any) -> Any:
        result = real(ctx, prompt, schema)
        project_id = stages.load_artifact(ctx).project_id
        scenes = ctx.uow.scenes.for_approved_set(project_id)
        tagged = []
        for scene in scenes:
            text = scene.narration_text.strip()
            if scene.index == 1:
                text = f"[whispers] {text} [laughs]"
            elif scene.index == 2:
                text = "[excited] Completely different words."
            tagged.append({"scene_index": scene.index, "narration": text})
        return result.model_copy(update={"parsed": {"scenes": tagged}})

    monkeypatch.setattr(stage, "llm_complete", _fake)


def _direction(sessions: sessionmaker[Session], project_id: str) -> dict[str, Any]:
    with unit_of_work(sessions) as uow:
        artifact = uow.artifacts.find(project_id, ArtifactKind.VOICE_DIRECTION)
        assert artifact is not None
        version = uow.versions.latest(artifact.id)
        assert version is not None and version.inline_content is not None
        return dict(version.inline_content)


def _voice_meta(sessions: sessionmaker[Session], project_id: str) -> dict[str, Any]:
    with unit_of_work(sessions) as uow:
        artifact = uow.artifacts.find(project_id, ArtifactKind.VOICE)
        assert artifact is not None
        version = uow.versions.latest(artifact.id)
        assert version is not None
        return dict(version.meta)


@pytest.mark.usefixtures("tagging_llm")
class TestGeneration:
    def test_suggested_tags_become_positions(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        scened: str,
    ) -> None:
        _run(monkeypatch, sessions, scened, ArtifactKind.VOICE_DIRECTION)
        content = _direction(sessions, scened)

        first = content["scenes"][0]
        word_count = len(first["narration"].split())
        assert first["tags"] == [
            {"word": 0, "tag": "whispers"},
            {"word": word_count, "tag": "laughs"},
        ]

    def test_a_scene_whose_words_changed_is_left_untagged(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        scened: str,
    ) -> None:
        _run(monkeypatch, sessions, scened, ArtifactKind.VOICE_DIRECTION)
        content = _direction(sessions, scened)

        assert content["scenes"][1]["tags"] == []
        assert content["untagged_by_mismatch"] == [2]
        # The narration is the scene's, never the model's rewrite.
        assert "Completely different" not in content["scenes"][1]["narration"]


@pytest.mark.usefixtures("tagging_llm")
class TestHumanEdit:
    def test_a_reviewer_can_move_tags(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        scened: str,
    ) -> None:
        _run(monkeypatch, sessions, scened, ArtifactKind.VOICE_DIRECTION)
        content = _direction(sessions, scened)
        content["scenes"][1]["tags"] = [{"word": 1, "tag": "serious"}]

        with unit_of_work(sessions) as uow:
            artifact = uow.artifacts.find(scened, ArtifactKind.VOICE_DIRECTION)
            assert artifact is not None
            ReviewService(uow).edit(artifact.id, content)

        assert _direction(sessions, scened)["scenes"][1]["tags"] == [
            {"word": 1, "tag": "serious"}
        ]

    def test_an_unknown_tag_is_refused(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        scened: str,
    ) -> None:
        _run(monkeypatch, sessions, scened, ArtifactKind.VOICE_DIRECTION)
        content = _direction(sessions, scened)
        content["scenes"][0]["tags"] = [{"word": 0, "tag": "mysterious"}]

        with pytest.raises(InvalidDirectionError), unit_of_work(sessions) as uow:
            artifact = uow.artifacts.find(scened, ArtifactKind.VOICE_DIRECTION)
            assert artifact is not None
            ReviewService(uow).edit(artifact.id, content)


@pytest.mark.usefixtures("tagging_llm")
class TestVoiceWithTags:
    def test_tags_are_performed_but_never_captioned(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        scened: str,
    ) -> None:
        import videoforge_workers.voice as voice

        sent: list[str] = []
        real_provider = voice._provider

        def _recording_provider() -> Any:
            provider = real_provider()
            original = provider.synthesise

            def _synthesise(req: Any) -> Any:
                sent.append(req.text)
                return original(req)

            monkeypatch.setattr(provider, "synthesise", _synthesise)
            return provider

        monkeypatch.setattr(voice, "_provider", _recording_provider)

        _run(monkeypatch, sessions, scened, ArtifactKind.VOICE_DIRECTION)
        _approve(sessions, scened, ArtifactKind.VOICE_DIRECTION)
        _run(monkeypatch, sessions, scened, ArtifactKind.VOICE)

        assert sent and sent[0].startswith("[whispers] ")
        assert "[laughs]" in sent[0]

        meta = _voice_meta(sessions, scened)
        assert meta["tag_count"] == 2
        assert meta["tag_strip_method"] == "offset"
        spoken = [w["text"] for span in meta["spans"] for w in span["words"]]
        assert not any("[" in word or "]" in word for word in spoken)

        direction = _direction(sessions, scened)
        expected = " ".join(s["narration"] for s in direction["scenes"]).split()
        assert spoken == expected
