"""M4-09 — ``render.generate``, encoding for real.

``test_rendering`` covers the graph without ffmpeg. This runs the actual
encode, because the properties that matter most are properties of the *file*:
its duration, its streams, and where its ``moov`` box sits.

The timeline is seeded directly rather than compiled through the whole
pipeline. What is under test here is the stage — fetch, burn, encode, verify,
upload — and driving research→script→scenes→prompts→images→voice first would
add a minute of setup to test none of it. The full chain is M4-12's job.

Small on purpose: 320×568 and about two seconds. A 1080×1920 encode of a real
narration takes long enough to make the suite unpleasant, and every property
asserted below is resolution-independent.
"""

from __future__ import annotations

import io
from typing import Any

import pytest
import sqlalchemy as sa
from PIL import Image
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from videoforge.services.dispatch import RecordingDispatcher
from videoforge.services.jobs import JobService
from videoforge.services.review import ReviewService
from videoforge_persistence.models import Workspace
from videoforge_persistence.uow import unit_of_work
from videoforge_shared.enums import ArtifactKind, ArtifactState, VersionOrigin
from videoforge_shared.hashing import sha256_bytes
from videoforge_shared.ids import new_ulid
from videoforge_shared.storage import StoredObject
from videoforge_shared.tasks import RENDER_GENERATE

pytestmark = pytest.mark.integration

_W, _H, _FPS = 320, 568, 30


def _png(colour: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (_W, _H), colour).save(buffer, format="PNG")
    return buffer.getvalue()


def _silence(milliseconds: int) -> bytes:
    """A real decodable MP3, from the mock voice provider.

    Real bytes rather than a stub: they cross storage, ffmpeg's demuxer and an
    AAC encoder, and a not-quite-an-MP3 fails in all three.
    """
    from videoforge_providers.mock import MockVoiceProvider
    from videoforge_providers.models import VoiceRequest

    text = "a" * max(1, milliseconds // 70)
    return MockVoiceProvider().synthesise(VoiceRequest(text=text, voice_id="v")).audio


@pytest.fixture()
def sessions(db_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def objects() -> dict[str, bytes]:
    return {}


@pytest.fixture(autouse=True)
def _fake_storage(
    monkeypatch: pytest.MonkeyPatch, objects: dict[str, bytes]
) -> dict[str, bytes]:
    import videoforge_workers.render_stage as render_stage

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

    monkeypatch.setattr(render_stage, "storage", _InMemory)
    return objects


def _store(objects: dict[str, bytes], data: bytes, name: str) -> str:
    digest = sha256_bytes(data)
    key = f"{digest[:2]}/{digest}/{name}"
    objects[key] = data
    return key


@pytest.fixture()
def project_with_timeline(
    sessions: sessionmaker[Session], objects: dict[str, bytes]
) -> Any:
    """An approved timeline whose frames and narration exist in storage."""
    frames = [
        _store(objects, _png((220, 40, 40)), "f1.png"),
        _store(objects, _png((40, 180, 60)), "f2.png"),
    ]
    narration = _store(objects, _silence(2000), "n.mp3")

    timeline = {
        "schema_version": 1,
        "project_id": "seeded-later",
        "total_ms": 2500,
        "tail_ms": 500,
        "video": {"width": _W, "height": _H, "fps": _FPS},
        "clips": [
            {
                "scene_id": "s1",
                "scene_index": 1,
                "kind": "illustration",
                "storage_key": frames[0],
                "start_ms": 0,
                "end_ms": 1200,
            },
            {
                "scene_id": "s2",
                "scene_index": 2,
                "kind": "illustration",
                "storage_key": frames[1],
                "start_ms": 1000,
                "end_ms": 2500,
            },
        ],
        "transitions": [
            {
                "kind": "crossfade",
                "from_clip": 0,
                "start_ms": 1000,
                "duration_ms": 200,
            }
        ],
        "captions": [{"text": "a burned caption", "start_ms": 200, "end_ms": 900}],
        "audio": [
            {
                "role": "narration",
                "storage_key": narration,
                "start_ms": 0,
                "duration_ms": 2000,
                "gain": [{"at_ms": 0, "gain_db": 0.0}],
            }
        ],
        "source": {
            "scene_set_version_id": "ss",
            "voice_version_id": "vv",
            "image_version_ids": {},
        },
    }

    workspace_id = new_ulid()
    with unit_of_work(sessions) as uow:
        uow.session.add(Workspace(id=workspace_id, name="render"))
        uow.flush()
        series = uow.series.create(workspace_id=workspace_id, title="Explainers")
        uow.flush()
        project = uow.projects.create(
            workspace_id=workspace_id, series_id=series.id, topic="tides"
        )
        uow.flush()
        timeline["project_id"] = project.id

        artifact = uow.artifacts.create(project.id, ArtifactKind.TIMELINE, None)
        uow.flush()
        version = uow.versions.add_version(
            artifact,
            origin=VersionOrigin.GENERATED,
            content_hash="tl",
            inline_content=timeline,
        )
        artifact.state = ArtifactState.AWAITING_APPROVAL
        uow.flush()
        ReviewService(uow).approve(version.id)
        project_id = project.id

    yield project_id

    with unit_of_work(sessions) as uow:
        uow.session.execute(
            sa.text("DELETE FROM workspace WHERE id = :id"), {"id": workspace_id}
        )
        uow.session.execute(
            sa.text("DELETE FROM outbox_event WHERE payload->>'project_id' = :id"),
            {"id": project_id},
        )


def _render(
    monkeypatch: pytest.MonkeyPatch,
    sessions: sessionmaker[Session],
    project_id: str,
) -> None:
    import videoforge_workers.db as worker_db
    from videoforge_workers.render_stage import render_body
    from videoforge_workers.skeleton import run_job

    with unit_of_work(sessions) as uow:
        job_id = (
            JobService(uow, RecordingDispatcher())
            .request(
                project_id=project_id,
                kind=ArtifactKind.RENDER,
                spec=RENDER_GENERATE,
            )
            .job.id
        )
    monkeypatch.setattr(worker_db, "get_session_factory", lambda: sessions)
    assert run_job(job_id, render_body, task_name=RENDER_GENERATE.name)


def _version_meta(sessions: sessionmaker[Session], project_id: str) -> dict[str, Any]:
    with unit_of_work(sessions) as uow:
        artifact = uow.artifacts.find(project_id, ArtifactKind.RENDER)
        assert artifact is not None
        version = uow.versions.latest(artifact.id)
        assert version is not None
        return {
            "storage_key": version.storage_key,
            "meta": dict(version.meta or {}),
            "state": artifact.state,
        }


class TestEncode:
    def test_it_produces_a_playable_mp4(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        project_with_timeline: str,
        objects: dict[str, bytes],
    ) -> None:
        _render(monkeypatch, sessions, project_with_timeline)
        row = _version_meta(sessions, project_with_timeline)

        assert row["state"] == ArtifactState.AWAITING_APPROVAL
        assert row["storage_key"]
        assert objects[row["storage_key"]][:12].find(b"ftyp") > 0
        assert row["meta"]["mime_type"] == "video/mp4"

    def test_the_duration_matches_the_timeline(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        project_with_timeline: str,
    ) -> None:
        """The single number that catches an offset arithmetic error — which
        otherwise produces a video that plays fine and drifts out of sync."""
        _render(monkeypatch, sessions, project_with_timeline)
        meta = _version_meta(sessions, project_with_timeline)["meta"]
        assert abs(int(meta["duration_ms"]) - 2500) <= 100

    def test_the_graph_is_kept_for_debugging(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        project_with_timeline: str,
    ) -> None:
        """A few hundred bytes, and the single best artifact for working out
        why a render looks wrong."""
        _render(monkeypatch, sessions, project_with_timeline)
        graph = _version_meta(sessions, project_with_timeline)["meta"]["filter_graph"]
        assert "xfade" in graph
        assert "subtitles=" in graph

    def test_a_render_costs_nothing_but_records_usage(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        project_with_timeline: str,
    ) -> None:
        _render(monkeypatch, sessions, project_with_timeline)
        with unit_of_work(sessions) as uow:
            rows = uow.session.execute(
                sa.text(
                    "SELECT u.provider, u.unit_cost_estimate FROM provider_usage u"
                    " JOIN generation_job j ON j.id = u.job_id"
                    " WHERE j.project_id = :p AND u.operation = 'render.encode'"
                ),
                {"p": project_with_timeline},
            ).all()
        assert len(rows) == 1
        assert rows[0][0] == "local"
        assert float(rows[0][1]) == 0.0


class TestSelfChecks:
    def test_a_duration_mismatch_fails_the_job(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        project_with_timeline: str,
    ) -> None:
        """The check that would have caught a bad offset.

        Simulated by telling the verifier the video should be four seconds
        long when the timeline it was built from says two and a half — the
        shape a real offset error takes, without having to write one.
        """
        import videoforge_workers.render_stage as render_stage
        from videoforge_workers.render import FfmpegError, _probe

        original = _probe

        def _lying_probe(path: str) -> dict[str, Any]:
            probe = original(path)
            probe["format"]["duration"] = "4.000"
            return probe

        monkeypatch.setattr(render_stage, "_probe", _lying_probe)
        with pytest.raises(FfmpegError, match="the offsets and the video disagree"):
            _render(monkeypatch, sessions, project_with_timeline)

    def test_a_libass_font_failure_fails_the_job(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        project_with_timeline: str,
    ) -> None:
        """A silent fallback to tofu boxes is a successful encode of an
        unusable video, and ffmpeg exits 0 for it."""
        import videoforge_workers.render_stage as render_stage
        from videoforge_workers.render import FfmpegError, _run

        original = _run

        def _noisy_run(cmd: list[str], **kwargs: Any) -> str:
            return original(cmd, **kwargs) + "\n[ass] fontselect: failed\n"

        monkeypatch.setattr(render_stage, "_run", _noisy_run)
        with pytest.raises(FfmpegError, match="caption font problem"):
            _render(monkeypatch, sessions, project_with_timeline)

    def test_the_moov_box_precedes_mdat(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        project_with_timeline: str,
        objects: dict[str, bytes],
    ) -> None:
        """Checked on the actual bytes rather than trusted from
        ``+faststart`` — it is what lets the review screen start playing before
        the file finishes arriving (M0-10's path)."""
        from videoforge_workers.render import moov_before_mdat

        _render(monkeypatch, sessions, project_with_timeline)
        key = _version_meta(sessions, project_with_timeline)["storage_key"]
        assert moov_before_mdat(objects[key])
