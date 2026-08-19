"""M5-01 and M5-02 behind the task skeleton — ``caption`` and ``thumbnail``.

``test_caption`` and ``test_cover`` already cover what these stages *decide*:
the Instagram limits, and what the cover looks like. What only a stage can get
wrong is the joins — reading the right approved artifact, refusing when it is
missing, storing bytes where the API will look for them, and not charging for
work that costs nothing.

Built on ``test_timeline_stage``'s harness rather than a second copy of it, for
the reason that suite gives for reusing ``test_image_stage``'s: the chain up to
approved images and voice is identical, and two versions of it would drift.

**The render is stubbed, not run.** ``caption`` requires ``render`` for
*ordering* reasons — the pipeline file explains why at length — and its body
reads the approved **script**, never a pixel. Encoding a real MP4 to satisfy an
admission check would add FFmpeg to every test here and prove nothing about the
caption. ``test_render_stage`` owns that.
"""

from __future__ import annotations

import io
import json
import zipfile
from typing import Any

import pytest
import sqlalchemy as sa
from PIL import Image
from sqlalchemy.orm import Session, sessionmaker
from tests.test_timeline_stage import _approve

from videoforge.services.review import ReviewService
from videoforge_persistence.uow import unit_of_work
from videoforge_shared.enums import ArtifactKind, ArtifactState, VersionOrigin
from videoforge_shared.tasks import (
    CAPTION_GENERATE,
    PACKAGE_ASSEMBLE,
    THUMBNAIL_GENERATE,
)

pytestmark = pytest.mark.integration

_BODIES = {
    ArtifactKind.CAPTION: ("caption", "caption_body", CAPTION_GENERATE),
    ArtifactKind.THUMBNAIL: ("thumbnail", "thumbnail_body", THUMBNAIL_GENERATE),
    ArtifactKind.PACKAGE: ("package_stage", "assemble_body", PACKAGE_ASSEMBLE),
}


@pytest.fixture(autouse=True)
def _publish_storage(
    monkeypatch: pytest.MonkeyPatch,
    _fake_storage: None,  # noqa: PT019 - ordering, not a value
) -> None:
    """Point the M5 workers at the same in-memory store as the rest.

    ``test_timeline_stage``'s fixture patches the modules that existed when it
    was written; this one extends it rather than forking it, so a cover written
    by one stage is readable by the packager in the same run.

    Both modules, not one: the packager reaching the real MinIO was the first
    failure this suite produced, and it looked like a connection problem rather
    than a missing patch.
    """
    import videoforge_workers.images as images
    import videoforge_workers.package_stage as package_stage
    import videoforge_workers.thumbnail as thumbnail

    for module in (thumbnail, package_stage):
        monkeypatch.setattr(module, "storage", images.storage)


def _run(
    monkeypatch: pytest.MonkeyPatch,
    sessions: sessionmaker[Session],
    project_id: str,
    kind: ArtifactKind,
) -> None:
    """Dispatch and execute one stage, through the real job service."""
    import importlib

    import videoforge_workers.db as worker_db
    from videoforge.services.dispatch import RecordingDispatcher
    from videoforge.services.jobs import JobService
    from videoforge_workers.skeleton import run_job

    module_name, body_name, spec = _BODIES[kind]
    body = getattr(
        importlib.import_module(f"videoforge_workers.{module_name}"), body_name
    )
    with unit_of_work(sessions) as uow:
        job_id = (
            JobService(uow, RecordingDispatcher())
            .request(project_id=project_id, kind=kind, spec=spec)
            .job.id
        )
    monkeypatch.setattr(worker_db, "get_session_factory", lambda: sessions)
    assert run_job(job_id, body, task_name=spec.name)


def _stub_render(sessions: sessionmaker[Session], project_id: str) -> None:
    """An approved render with real bytes behind it, without encoding one.

    ``caption`` requires ``render`` for *ordering* reasons the pipeline file
    explains, and reads the approved script rather than a pixel; ``package``
    reads the bytes but does not care what they decode to. Running FFmpeg to
    satisfy either would add minutes to this suite and prove something
    ``test_render_stage`` already owns.

    Written at INSERT, never by UPDATE: ``artifact_version`` is append-only and
    the trigger refuses (§10.3). The first version of this fixture patched the
    row afterwards and the schema caught it.
    """
    import videoforge_workers.images as images

    stored = images.storage().put_bytes(
        "artifacts", b"\x00\x00\x00 ftypmp42", "render.mp4"
    )
    with unit_of_work(sessions) as uow:
        artifact = uow.artifacts.create(project_id, ArtifactKind.RENDER, None)
        uow.flush()
        version = uow.versions.add_version(
            artifact,
            origin=VersionOrigin.GENERATED,
            content_hash=stored.sha256,
            storage_key=stored.key,
            meta={
                "duration_ms": 22600,
                "width": 1080,
                "height": 1920,
                "scene_marks": [{"scene_index": 1}],
            },
        )
        artifact.state = ArtifactState.AWAITING_APPROVAL
        uow.flush()
        ReviewService(uow).approve(version.id)


def _content(
    sessions: sessionmaker[Session], project_id: str, kind: ArtifactKind
) -> dict[str, Any]:
    with unit_of_work(sessions) as uow:
        artifact = uow.artifacts.find(project_id, kind)
        assert artifact is not None
        version = uow.versions.latest(artifact.id)
        assert version is not None
        assert version.inline_content is not None
        return dict(version.inline_content)


def _meta(
    sessions: sessionmaker[Session], project_id: str, kind: ArtifactKind
) -> dict[str, Any]:
    with unit_of_work(sessions) as uow:
        artifact = uow.artifacts.find(project_id, kind)
        assert artifact is not None
        version = uow.versions.latest(artifact.id)
        assert version is not None
        return dict(version.meta or {})


@pytest.fixture()
def rendered(
    sessions: sessionmaker[Session],
    ready: str,  # noqa: F811 - the imported fixture
) -> str:
    """``ready`` plus an approved render — where ``caption`` is admitted from."""
    _stub_render(sessions, ready)
    return ready


class TestCaption:
    def test_it_produces_a_reviewable_caption(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        rendered: str,
    ) -> None:
        _run(monkeypatch, sessions, rendered, ArtifactKind.CAPTION)

        with unit_of_work(sessions) as uow:
            artifact = uow.artifacts.find(rendered, ArtifactKind.CAPTION)
            assert artifact is not None
            assert artifact.state == ArtifactState.AWAITING_APPROVAL

    def test_the_caption_is_inline_not_stored(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        rendered: str,
    ) -> None:
        """It is text a human edits. A storage key would put the one artifact
        most likely to be reworded behind a download."""
        _run(monkeypatch, sessions, rendered, ArtifactKind.CAPTION)

        with unit_of_work(sessions) as uow:
            artifact = uow.artifacts.find(rendered, ArtifactKind.CAPTION)
            assert artifact is not None
            version = uow.versions.latest(artifact.id)
            assert version is not None
            assert version.storage_key is None
            assert version.inline_content is not None

    def test_it_carries_every_field_the_packager_needs(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        rendered: str,
    ) -> None:
        """F10's contents, and ``hook`` for the cover. A stage that dropped one
        would fail in the packager, two tickets away from the cause."""
        _run(monkeypatch, sessions, rendered, ArtifactKind.CAPTION)
        content = _content(sessions, rendered, ArtifactKind.CAPTION)

        assert content["caption"].strip()
        assert content["hook"].strip()
        assert isinstance(content["hashtags"], list)
        assert content["preview"] == content["caption"][: len(content["preview"])]

    def test_hashtags_arrive_normalised(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        rendered: str,
    ) -> None:
        """Bare, lowercase, and free of the characters Instagram silently drops
        — asserted on what the *stage* stored, not on ``normalise`` in
        isolation, because the packager reads this row."""
        _run(monkeypatch, sessions, rendered, ArtifactKind.CAPTION)
        tags = _content(sessions, rendered, ArtifactKind.CAPTION)["hashtags"]

        assert tags, "the mock schema synthesises an array; none survived"
        for tag in tags:
            assert tag == tag.lower()
            assert not tag.startswith("#")
            assert tag.isalnum() or "_" in tag

    def test_it_refuses_before_the_render_is_approved(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        ready: str,  # noqa: F811 - approved through voice, no render
    ) -> None:
        """The admission check, on the edge M5-01 reversed. Without it the
        stage would run early and the project would report ``PACKAGING`` while
        its scenes were still being drawn."""
        from videoforge.services.admission import AdmissionError

        with pytest.raises(AdmissionError, match="render"):
            _run(monkeypatch, sessions, ready, ArtifactKind.CAPTION)


class TestThumbnail:
    @pytest.fixture()
    def captioned(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        rendered: str,
    ) -> str:
        _run(monkeypatch, sessions, rendered, ArtifactKind.CAPTION)
        _approve(sessions, rendered, ArtifactKind.CAPTION)
        return rendered

    def test_it_stores_a_decodable_cover(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        captioned: str,
    ) -> None:
        """The bytes travel through content-addressed storage and an ``<img>``
        tag, so "it wrote something" is not the claim — "it wrote a picture" is.
        """
        import videoforge_workers.images as images

        _run(monkeypatch, sessions, captioned, ArtifactKind.THUMBNAIL)

        with unit_of_work(sessions) as uow:
            artifact = uow.artifacts.find(captioned, ArtifactKind.THUMBNAIL)
            assert artifact is not None
            version = uow.versions.latest(artifact.id)
            assert version is not None
            assert version.storage_key is not None
            key = version.storage_key

        data = images.storage().get_bytes_verified("assets", key)
        cover = Image.open(io.BytesIO(data))
        assert cover.size == (1080, 1920)

    def test_it_records_the_scene_it_used(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        captioned: str,
    ) -> None:
        """A reviewer asking for a different scene needs to know which one they
        have — and the cover is regenerable precisely because it is cheap."""
        _run(monkeypatch, sessions, captioned, ArtifactKind.THUMBNAIL)
        meta = _meta(sessions, captioned, ArtifactKind.THUMBNAIL)

        assert meta["scene_id"]
        assert meta["mime_type"] == "image/png"
        with unit_of_work(sessions) as uow:
            scenes = uow.scenes.for_approved_set(captioned)
            assert meta["scene_id"] in {scene.id for scene in scenes}

    def test_it_typesets_the_approved_hook(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        captioned: str,
    ) -> None:
        """The join this stage exists to make: the words come from the caption
        a human approved, not from the script or the topic."""
        hook = _content(sessions, captioned, ArtifactKind.CAPTION)["hook"]

        _run(monkeypatch, sessions, captioned, ArtifactKind.THUMBNAIL)
        assert _meta(sessions, captioned, ArtifactKind.THUMBNAIL)["hook"] == hook

    def test_it_costs_nothing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        captioned: str,
    ) -> None:
        """A real zero rather than no row: a gap in ``provider_usage`` reads
        like a missing record, and the S10 cap sums this column."""
        _run(monkeypatch, sessions, captioned, ArtifactKind.THUMBNAIL)

        with unit_of_work(sessions) as uow:
            costs = list(
                uow.session.execute(
                    sa.text(
                        "SELECT unit_cost_estimate FROM provider_usage"
                        " WHERE operation = 'thumbnail.generate'"
                    )
                ).scalars()
            )
        assert costs == [0], "a stage that spends nothing still records a row"

    def test_it_refuses_before_the_caption_is_approved(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        rendered: str,
    ) -> None:
        """Without the hook there is nothing to typeset, and a cover built from
        a caption nobody signed off is a picture that contradicts the post."""
        from videoforge.services.admission import AdmissionError

        with pytest.raises(AdmissionError, match="caption"):
            _run(monkeypatch, sessions, rendered, ArtifactKind.THUMBNAIL)


class TestPackage:
    """M5-03 — the archive, assembled from what the project actually holds.

    ``test_packaging`` covers the zip's own properties. What only the stage can
    get wrong is *what goes in it*: reading approved versions rather than the
    latest, pulling the render from the artifacts bucket and everything else
    from assets, and writing a row a support question can query.
    """

    @pytest.fixture()
    def packaged(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        rendered: str,
    ) -> str:
        """Caption and cover approved. The render already has bytes — see
        ``_stub_render``."""
        _run(monkeypatch, sessions, rendered, ArtifactKind.CAPTION)
        _approve(sessions, rendered, ArtifactKind.CAPTION)
        _run(monkeypatch, sessions, rendered, ArtifactKind.THUMBNAIL)
        _approve(sessions, rendered, ArtifactKind.THUMBNAIL)
        return rendered

    def _archive(
        self, sessions: sessionmaker[Session], project_id: str
    ) -> zipfile.ZipFile:
        import videoforge_workers.images as images

        with unit_of_work(sessions) as uow:
            artifact = uow.artifacts.find(project_id, ArtifactKind.PACKAGE)
            assert artifact is not None
            version = uow.versions.latest(artifact.id)
            assert version is not None
            key = version.storage_key
        assert key is not None
        return zipfile.ZipFile(
            io.BytesIO(images.storage().get_bytes_verified("artifacts", key))
        )

    def test_it_contains_everything_f10_promises(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        packaged: str,
    ) -> None:
        """Video, thumbnail, caption text, hashtags, metadata and the scene
        assets — the requirement, checked against the actual file."""
        _run(monkeypatch, sessions, packaged, ArtifactKind.PACKAGE)

        with self._archive(sessions, packaged) as zf:
            names = set(zf.namelist())
            assert {"video.mp4", "cover.png", "caption.txt", "hashtags.txt"} <= names
            assert "manifest.json" in names
            assert any(n.startswith("scenes/scene-") for n in names)

    def test_the_caption_is_plain_text_ready_to_paste(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        packaged: str,
    ) -> None:
        """Not JSON. This is the half of the package a person copies into
        Instagram, and quotes with escaped newlines are theirs to clean up."""
        caption = _content(sessions, packaged, ArtifactKind.CAPTION)
        _run(monkeypatch, sessions, packaged, ArtifactKind.PACKAGE)

        with self._archive(sessions, packaged) as zf:
            assert zf.read("caption.txt").decode() == caption["caption"]
            tags = zf.read("hashtags.txt").decode().split()
        assert tags == [f"#{tag}" for tag in caption["hashtags"]]

    def test_the_manifest_verifies_the_archive(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        packaged: str,
    ) -> None:
        """The point of the package: every hash in the manifest matches the
        bytes actually stored beside it, through the real stage."""
        from videoforge_shared.hashing import sha256_bytes

        _run(monkeypatch, sessions, packaged, ArtifactKind.PACKAGE)

        with self._archive(sessions, packaged) as zf:
            manifest = json.loads(zf.read("manifest.json"))
            for record in manifest["files"]:
                assert sha256_bytes(zf.read(record["path"])) == record["sha256"]

    def test_it_writes_a_queryable_row(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        packaged: str,
    ) -> None:
        """``publishing_package`` exists so "which packages contain scene 7?"
        is a query rather than a scan of ``meta``."""
        _run(monkeypatch, sessions, packaged, ArtifactKind.PACKAGE)

        with unit_of_work(sessions) as uow:
            rows = list(
                uow.session.execute(
                    sa.text(
                        "SELECT zip_key, manifest FROM publishing_package p"
                        " JOIN artifact_version v ON v.id = p.artifact_version_id"
                        " JOIN artifact a ON a.id = v.artifact_id"
                        " WHERE a.project_id = :p"
                    ),
                    {"p": packaged},
                )
            )
        assert len(rows) == 1
        assert rows[0][0]
        assert rows[0][1]["files"]

    def test_the_version_carries_the_manifest_for_review(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        packaged: str,
    ) -> None:
        """M5-04 lists the contents from the ordinary version endpoint, so the
        manifest has to reach ``meta`` as well as the row."""
        _run(monkeypatch, sessions, packaged, ArtifactKind.PACKAGE)
        meta = _meta(sessions, packaged, ArtifactKind.PACKAGE)

        assert meta["mime_type"] == "application/zip"
        assert meta["bytes"] > 0
        assert meta["manifest"]["video"]["duration_ms"] == 22600

    def test_it_refuses_before_the_cover_is_approved(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        rendered: str,
    ) -> None:
        """A package missing a file it claims to contain is worse than no
        package, and the admission check is what makes that unreachable."""
        from videoforge.services.admission import AdmissionError

        with pytest.raises(AdmissionError):
            _run(monkeypatch, sessions, rendered, ArtifactKind.PACKAGE)
