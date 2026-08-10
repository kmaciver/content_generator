"""M4-08 — ``timeline.compile`` behind the task skeleton.

``packages/timeline`` already tests what the compiler *decides*. What is new
here is what only the stage can get wrong: reading the right versions, pinning
them, and failing in a way a reviewer can act on.

The chain runs for real up to approved images and voice, because — as in
``test_image_stage`` — the interesting failures are at the joins.
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from videoforge.services.dispatch import RecordingDispatcher
from videoforge.services.jobs import JobService
from videoforge.services.review import ReviewService
from videoforge_domain.duration import SETTINGS_KEY
from videoforge_persistence.models import Workspace
from videoforge_persistence.uow import unit_of_work
from videoforge_shared.enums import ArtifactKind, ArtifactState
from videoforge_shared.ids import new_ulid
from videoforge_shared.tasks import (
    IMAGES_GENERATE,
    PROMPTS_GENERATE,
    RESEARCH_GENERATE,
    SCENES_GENERATE,
    SCRIPT_GENERATE,
    TIMELINE_COMPILE,
    VOICE_GENERATE,
)

pytestmark = pytest.mark.integration

_BODIES = {
    ArtifactKind.RESEARCH: ("research", "research_body", RESEARCH_GENERATE),
    ArtifactKind.SCRIPT: ("script", "script_body", SCRIPT_GENERATE),
    ArtifactKind.SCENE_SET: ("scenes", "scenes_body", SCENES_GENERATE),
    ArtifactKind.PROMPT: ("prompts_stage", "prompts_body", PROMPTS_GENERATE),
    ArtifactKind.IMAGE: ("images", "images_body", IMAGES_GENERATE),
    ArtifactKind.VOICE: ("voice", "voice_body", VOICE_GENERATE),
    ArtifactKind.TIMELINE: ("timeline_stage", "compile_body", TIMELINE_COMPILE),
}


@pytest.fixture()
def sessions(db_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)


@pytest.fixture(autouse=True)
def _fake_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    """One in-memory object store shared by every module that writes bytes."""
    import videoforge_workers.images as images
    import videoforge_workers.references as references
    import videoforge_workers.voice as voice
    from videoforge_shared.hashing import sha256_bytes
    from videoforge_shared.storage import StoredObject

    objects: dict[str, bytes] = {}

    class _InMemory:
        def put_bytes(self, bucket: str, data: bytes, filename: str) -> StoredObject:
            digest = sha256_bytes(data)
            key = f"{digest[:2]}/{digest}/{filename}"
            deduplicated = key in objects
            objects[key] = data
            return StoredObject(
                bucket=bucket,
                key=key,
                sha256=digest,
                size=len(data),
                deduplicated=deduplicated,
            )

        def get_bytes_verified(self, bucket: str, key: str) -> bytes:
            return objects[key]

    for module in (references, images, voice):
        monkeypatch.setattr(module, "storage", _InMemory)


@pytest.fixture(autouse=True)
def _fake_normalise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the ffmpeg encode, keep the geometry — as in ``test_image_stage``."""
    import videoforge_workers.images as images
    from videoforge_workers.imaging import NormalisedImage, crop_plan

    def _fake(
        data: bytes,
        *,
        mime_type: str,
        width: int,
        height: int,
        target: tuple[int, int],
    ) -> NormalisedImage:
        plan = crop_plan(width, height, *target)
        if plan.is_identity:
            return NormalisedImage(data, mime_type, width, height, plan)
        return NormalisedImage(
            data + b"n", "image/png", plan.target_width, plan.target_height, plan
        )

    monkeypatch.setattr(images, "normalise", _fake)


@pytest.fixture()
def branded_project(sessions: sessionmaker[Session]) -> Any:
    workspace_id = new_ulid()
    with unit_of_work(sessions) as uow:
        uow.session.add(Workspace(id=workspace_id, name="timeline"))
        uow.flush()
        series = uow.series.create(workspace_id=workspace_id, title="Explainers")
        uow.flush()
        character = uow.branding.add_character_version(
            series.id, name="Pip", immutable_traits={"head": "a cream circle"}
        )
        style = uow.branding.add_style_version(
            series.id, name="Flat", fields={"medium": "flat vector"}
        )
        uow.flush()
        uow.branding.approve_style(style.id)
        row = uow.projects.create(
            workspace_id=workspace_id,
            series_id=series.id,
            topic="why the tides move",
        )
        row.settings = {SETTINGS_KEY: 40_000}
        uow.flush()
        ids = {
            "project": row.id,
            "series": series.id,
            "character": character.id,
            "style": style.id,
        }

    yield ids

    with unit_of_work(sessions) as uow:
        uow.session.execute(
            sa.text("DELETE FROM workspace WHERE id = :id"), {"id": workspace_id}
        )
        uow.session.execute(
            sa.text("DELETE FROM outbox_event WHERE payload->>'project_id' = :id"),
            {"id": ids["project"]},
        )


def _run(
    monkeypatch: pytest.MonkeyPatch,
    sessions: sessionmaker[Session],
    project_id: str,
    kind: ArtifactKind,
    regenerate: bool = False,
) -> None:
    import importlib

    import videoforge_workers.db as worker_db
    from videoforge_workers.skeleton import run_job

    module_name, body_name, spec = _BODIES[kind]
    body = getattr(
        importlib.import_module(f"videoforge_workers.{module_name}"), body_name
    )
    with unit_of_work(sessions) as uow:
        job_id = (
            JobService(uow, RecordingDispatcher())
            .request(project_id=project_id, kind=kind, spec=spec, regenerate=regenerate)
            .job.id
        )
    monkeypatch.setattr(worker_db, "get_session_factory", lambda: sessions)
    assert run_job(job_id, body, task_name=spec.name)


def _approve(
    sessions: sessionmaker[Session], project_id: str, kind: ArtifactKind
) -> None:
    """Approve **every** artifact of this kind, per-scene ones included.

    Same rule as ``test_image_stage``: ``_check_pipeline`` collapses per-scene
    artifacts to the least advanced of their kind, so nineteen approved
    prompts and one still in review is not an approved prompt stage. The first
    version of this approved only the project-wide row and the image stage
    refused to run — the admission check doing its job on a test that had
    forgotten the rule.
    """
    with unit_of_work(sessions) as uow:
        artifacts = [
            a
            for a in uow.artifacts.for_project(project_id)
            if ArtifactKind(a.kind) is kind
        ]
        assert artifacts, kind
        for artifact in artifacts:
            version = uow.versions.latest(artifact.id)
            assert version is not None
            ReviewService(uow).approve(version.id)


@pytest.fixture()
def ready(
    monkeypatch: pytest.MonkeyPatch,
    sessions: sessionmaker[Session],
    branded_project: dict[str, str],
) -> str:
    """A project with approved scenes, frames and narration — the state
    ``timeline.compile`` is admitted from."""
    from tests.test_image_stage import _approve_character_with_sheets

    project = branded_project["project"]
    # Reused rather than re-written: the reference-sheet setup is identical and
    # a second copy would drift from the one the image suite keeps honest.
    _approve_character_with_sheets(monkeypatch, sessions, branded_project)

    for kind in (
        ArtifactKind.RESEARCH,
        ArtifactKind.SCRIPT,
        ArtifactKind.SCENE_SET,
        ArtifactKind.PROMPT,
    ):
        _run(monkeypatch, sessions, project, kind)
        _approve(sessions, project, kind)

    for kind in (ArtifactKind.IMAGE, ArtifactKind.VOICE):
        _run(monkeypatch, sessions, project, kind)
        _approve(sessions, project, kind)

    return project


def _timeline(sessions: sessionmaker[Session], project_id: str) -> dict[str, Any]:
    with unit_of_work(sessions) as uow:
        artifact = uow.artifacts.find(project_id, ArtifactKind.TIMELINE)
        assert artifact is not None
        version = uow.versions.latest(artifact.id)
        assert version is not None
        assert version.inline_content is not None
        return dict(version.inline_content)


class TestCompile:
    def test_it_produces_a_reviewable_timeline(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        ready: str,
    ) -> None:
        """An ordinary artifact version, awaiting approval like every other
        stage — the compiler is not special because it is pure."""
        _run(monkeypatch, sessions, ready, ArtifactKind.TIMELINE)

        with unit_of_work(sessions) as uow:
            artifact = uow.artifacts.find(ready, ArtifactKind.TIMELINE)
            assert artifact is not None
            assert artifact.state == ArtifactState.AWAITING_APPROVAL

    def test_the_timeline_is_inline_not_stored(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        ready: str,
    ) -> None:
        """Tens of kilobytes of JSON, and the artifact a human is most likely
        to read directly when a video comes out wrong."""
        _run(monkeypatch, sessions, ready, ArtifactKind.TIMELINE)
        content = _timeline(sessions, ready)
        assert content["schema_version"] == 1
        assert content["clips"]
        assert content["audio"]

    def test_one_clip_per_scene_in_order(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        ready: str,
    ) -> None:
        _run(monkeypatch, sessions, ready, ArtifactKind.TIMELINE)
        content = _timeline(sessions, ready)
        with unit_of_work(sessions) as uow:
            scenes = uow.scenes.for_approved_set(ready)
        assert [clip["scene_index"] for clip in content["clips"]] == [
            scene.index for scene in scenes
        ]

    def test_it_pins_the_versions_it_compiled(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        ready: str,
    ) -> None:
        """§10.3 rule 4. Without the pin, "why is scene 4 three seconds long?"
        has no answer once the voice artifact moves on."""
        _run(monkeypatch, sessions, ready, ArtifactKind.TIMELINE)
        source = _timeline(sessions, ready)["source"]

        with unit_of_work(sessions) as uow:
            voice = uow.artifacts.find(ready, ArtifactKind.VOICE)
            assert voice is not None
            approved = uow.versions.approved_version(voice.id)
            assert approved is not None
        assert source["voice_version_id"] == approved.artifact_version_id
        assert source["image_version_ids"]

    def test_the_video_outlives_the_narration(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        ready: str,
    ) -> None:
        _run(monkeypatch, sessions, ready, ArtifactKind.TIMELINE)
        content = _timeline(sessions, ready)
        narration = content["audio"][0]
        assert content["total_ms"] > narration["duration_ms"]

    def test_it_costs_nothing_but_still_records_usage(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        ready: str,
    ) -> None:
        """A gap in ``provider_usage`` reads like a missing record."""
        _run(monkeypatch, sessions, ready, ArtifactKind.TIMELINE)
        with unit_of_work(sessions) as uow:
            rows = uow.session.execute(
                sa.text(
                    "SELECT u.provider, u.unit_cost_estimate FROM provider_usage u"
                    " JOIN generation_job j ON j.id = u.job_id"
                    " WHERE j.project_id = :p AND j.task_name = 'timeline.compile'"
                ),
                {"p": ready},
            ).all()
        assert len(rows) == 1
        assert float(rows[0][1]) == 0.0


class TestRefusals:
    def test_a_scene_without_an_approved_frame_stops_the_compile(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        ready: str,
    ) -> None:
        """And names **every** missing scene, not the first.

        A reviewer with two stragglers wants one message, not two runs — so
        two scenes are added without frames and both must appear.

        Scenes are *appended* rather than un-approved: ``scene`` is
        append-only by trigger, and the artifact FSM refuses ``rejected`` on an
        APPROVED artifact — both of which the first version of this test
        discovered by being wrong. An added scene with no image is also the
        real-world shape, since that is what a regenerated scene set produces.
        """
        with unit_of_work(sessions) as uow:
            scenes = uow.scenes.for_approved_set(ready)
            for offset in (1, 2):
                uow.session.execute(
                    sa.text(
                        'INSERT INTO scene (id, scene_set_id, "index",'
                        " narration_text, visual_brief, target_duration_ms)"
                        " VALUES (:id, :ss, :i, 'unshot.', 'a scene', 2000)"
                    ),
                    {
                        "id": new_ulid(),
                        "ss": scenes[0].scene_set_id,
                        "i": len(scenes) + offset,
                    },
                )

        with pytest.raises(RuntimeError) as excinfo:
            _run(monkeypatch, sessions, ready, ArtifactKind.TIMELINE)

        message = str(excinfo.value)
        assert "no approved image for scenes" in message
        assert str(len(scenes) + 1) in message
        assert str(len(scenes) + 2) in message
