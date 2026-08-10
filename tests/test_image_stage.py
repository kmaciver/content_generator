"""M3-07: ``image.generate`` — the first stage that produces pixels.

``test_pipeline_stages.py`` covers the shape every text stage shares. What is
new here is what only this stage does:

* versions carry a **storage_key** rather than inline content — a megabyte of
  JPEG has no business in a jsonb column, and the CHECK constraint permits
  exactly one of the two;
* generation runs against the project's **pinned** branding, not the series'
  current approvals, which is the whole point of ADR-016's pin;
* the approved **reference sheets** reach the provider, which is the only
  reason M3-04b exists.

The chain runs for real up to approved prompts, because the interesting
failures are at the joins.
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
from videoforge_providers.models import ImageRequest
from videoforge_shared.enums import ArtifactKind, ArtifactState
from videoforge_shared.ids import new_ulid
from videoforge_shared.tasks import (
    IMAGES_GENERATE,
    PROMPTS_GENERATE,
    REFERENCES_GENERATE,
    RESEARCH_GENERATE,
    SCENES_GENERATE,
    SCRIPT_GENERATE,
)

pytestmark = pytest.mark.integration

_BODIES = {
    ArtifactKind.RESEARCH: ("research", "research_body", RESEARCH_GENERATE),
    ArtifactKind.SCRIPT: ("script", "script_body", SCRIPT_GENERATE),
    ArtifactKind.SCENE_SET: ("scenes", "scenes_body", SCENES_GENERATE),
    ArtifactKind.PROMPT: ("prompts_stage", "prompts_body", PROMPTS_GENERATE),
    ArtifactKind.IMAGE: ("images", "images_body", IMAGES_GENERATE),
}


@pytest.fixture()
def sessions(db_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)


class _Recorder:
    """A mock image provider that remembers what it was asked for.

    Wrapping rather than replacing ``MockImageProvider``: the bytes still have
    to be a real decodable PNG, because they travel through content-addressed
    storage and a version row, and a fake that returned a fixed string would
    pass this test while failing everywhere those matter.
    """

    name = "mock"

    def __init__(self) -> None:
        from videoforge_providers.mock import MockImageProvider

        self._inner = MockImageProvider()
        self.requests: list[ImageRequest] = []

    def capabilities(self) -> Any:
        return self._inner.capabilities()

    def generate(self, req: ImageRequest) -> Any:
        self.requests.append(req)
        return self._inner.generate(req)


@pytest.fixture()
def recorder(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    import videoforge_workers.images as images

    provider = _Recorder()
    monkeypatch.setattr(images, "_provider", lambda: provider)
    return provider


@pytest.fixture(autouse=True)
def _fake_storage(monkeypatch: pytest.MonkeyPatch) -> None:
    """One in-memory object store, shared by both modules that write images.

    ``references`` and ``images`` each expose their own ``storage`` seam, and
    this stage *reads back* what the other wrote — so a per-module fake would
    make the reference sheets invisible to the very stage that consumes them.
    """
    import videoforge_workers.images as images
    import videoforge_workers.references as references
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

    monkeypatch.setattr(references, "storage", _InMemory)
    monkeypatch.setattr(images, "storage", _InMemory)


@pytest.fixture(autouse=True)
def _fake_normalise(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the ffmpeg encode, keep the geometry.

    The tooling image has no ffmpeg, and standing one up for this file would
    buy nothing: ``test_imaging`` covers the crop arithmetic exactly, and what
    is under test *here* is that the stage stores two objects and points the
    version at the right one. The real :func:`crop_plan` still runs, so the
    recorded dimensions and discard fraction are the true ones.
    """
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
        # Different bytes, so a test asserting the version points at the
        # derivative cannot pass by accident on a content-addressed collision.
        return NormalisedImage(
            data + b"normalised",
            "image/png",
            plan.target_width,
            plan.target_height,
            plan,
        )

    monkeypatch.setattr(images, "normalise", _fake)


@pytest.fixture()
def branded_project(sessions: sessionmaker[Session]) -> Any:
    """A project whose series has an approved character, style and sheets."""
    workspace_id = new_ulid()
    with unit_of_work(sessions) as uow:
        uow.session.add(Workspace(id=workspace_id, name="images"))
        uow.flush()
        series = uow.series.create(workspace_id=workspace_id, title="Explainers")
        uow.flush()

        character = uow.branding.add_character_version(
            series.id,
            name="Pip",
            immutable_traits={"head": "a smooth cream #F4EDE4 circle, no hair"},
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
    scene_ref: str | None = None,
    **extra: Any,
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
            .request(
                project_id=project_id,
                kind=kind,
                spec=spec,
                scene_ref=scene_ref,
                regenerate=regenerate,
                input_extra=extra or None,
            )
            .job.id
        )

    monkeypatch.setattr(worker_db, "get_session_factory", lambda: sessions)
    assert run_job(job_id, body, task_name=spec.name) is True


def _approve(
    sessions: sessionmaker[Session], project_id: str, kind: ArtifactKind
) -> None:
    """Approve **every** artifact of this kind, per-scene ones included.

    Not just the trigger. ``_check_pipeline`` collapses per-scene artifacts to
    the least advanced of their kind, so a project whose scene-set is approved
    but whose twenty prompts are not has *not* met the image stage's
    prerequisite — nineteen approved prompts and one still awaiting review is
    not an approved prompt stage, and a downstream stage that started on that
    basis would consume a hole.
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


def _advance_to_prompts(
    monkeypatch: pytest.MonkeyPatch,
    sessions: sessionmaker[Session],
    project_id: str,
) -> None:
    for kind in (
        ArtifactKind.RESEARCH,
        ArtifactKind.SCRIPT,
        ArtifactKind.SCENE_SET,
        ArtifactKind.PROMPT,
    ):
        _run(monkeypatch, sessions, project_id, kind)
        _approve(sessions, project_id, kind)


def _approve_character_with_sheets(
    monkeypatch: pytest.MonkeyPatch,
    sessions: sessionmaker[Session],
    ids: dict[str, str],
) -> str:
    """Generate a reference group and approve the character against it."""
    import videoforge_workers.db as worker_db
    from videoforge_workers.references import references_body
    from videoforge_workers.skeleton import run_job

    group_id = new_ulid()
    with unit_of_work(sessions) as uow:
        job_id = (
            JobService(uow, RecordingDispatcher())
            .request_series_job(
                series_id=ids["series"],
                spec=REFERENCES_GENERATE,
                idempotency_key_suffix=f"character:{ids['character']}",
                input_snapshot={
                    "character_id": ids["character"],
                    "group_id": group_id,
                },
            )
            .job.id
        )
    monkeypatch.setattr(worker_db, "get_session_factory", lambda: sessions)
    assert run_job(job_id, references_body, task_name="references.generate")

    with unit_of_work(sessions) as uow:
        uow.branding.approve_character(ids["character"], reference_group_id=group_id)
    return group_id


class TestFanOut:
    def test_one_image_artifact_per_scene(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded_project: dict[str, str],
        recorder: _Recorder,
    ) -> None:
        project = branded_project["project"]
        _approve_character_with_sheets(monkeypatch, sessions, branded_project)
        _advance_to_prompts(monkeypatch, sessions, project)
        _run(monkeypatch, sessions, project, ArtifactKind.IMAGE)

        with unit_of_work(sessions) as uow:
            scenes = uow.scenes.for_approved_set(project)
            per_scene = [
                a
                for a in uow.artifacts.for_project(project)
                if ArtifactKind(a.kind) is ArtifactKind.IMAGE and a.scene_ref
            ]
        assert len(per_scene) == len(scenes)
        assert len(recorder.requests) == len(scenes)

    def test_versions_carry_a_storage_key_not_inline_content(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded_project: dict[str, str],
        recorder: _Recorder,
    ) -> None:
        """The CHECK constraint permits exactly one, and an image is bytes."""
        project = branded_project["project"]
        _approve_character_with_sheets(monkeypatch, sessions, branded_project)
        _advance_to_prompts(monkeypatch, sessions, project)
        _run(monkeypatch, sessions, project, ArtifactKind.IMAGE)

        with unit_of_work(sessions) as uow:
            scenes = uow.scenes.for_approved_set(project)
            artifact = uow.artifacts.find(project, ArtifactKind.IMAGE, scenes[0].id)
            assert artifact is not None
            version = uow.versions.latest(artifact.id)
            assert version is not None
            assert version.storage_key
            assert version.inline_content is None
            assert version.meta["prompt"]
            assert version.meta["character_version_id"]
            assert version.meta["style_version_id"]

    def test_the_trigger_artifact_is_completed_with_a_manifest(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded_project: dict[str, str],
        recorder: _Recorder,
    ) -> None:
        """Otherwise it stays GENERATING forever and the project's phase, which
        takes the least advanced artifact of a kind, never moves."""
        project = branded_project["project"]
        _approve_character_with_sheets(monkeypatch, sessions, branded_project)
        _advance_to_prompts(monkeypatch, sessions, project)
        _run(monkeypatch, sessions, project, ArtifactKind.IMAGE)

        with unit_of_work(sessions) as uow:
            trigger = uow.artifacts.find(project, ArtifactKind.IMAGE)
            assert trigger is not None
            assert trigger.state is ArtifactState.AWAITING_APPROVAL
            version = uow.versions.latest(trigger.id)
            assert version is not None
            assert version.inline_content is not None
            assert version.inline_content["images"]

    def test_max_scenes_caps_the_run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded_project: dict[str, str],
        recorder: _Recorder,
    ) -> None:
        """A twenty-scene run costs real money; trying a convention on two
        scenes first is the ordinary way to work."""
        project = branded_project["project"]
        _approve_character_with_sheets(monkeypatch, sessions, branded_project)
        _advance_to_prompts(monkeypatch, sessions, project)
        _run(monkeypatch, sessions, project, ArtifactKind.IMAGE, max_scenes=2)

        with unit_of_work(sessions) as uow:
            scenes = uow.scenes.for_approved_set(project)
        assert len(scenes) > 2, "fixture must have more scenes than the cap"
        assert len(recorder.requests) == 2


class TestNormalisation:
    """M3-08 / B2: the version points at the frame the renderer will use."""

    def test_the_version_carries_the_normalised_frame(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded_project: dict[str, str],
        recorder: _Recorder,
    ) -> None:
        """**The reviewer must approve the frame that ships.**

        Normalising after approval would mean the crop happens to an image
        somebody already signed off, which is the review gate approving one
        thing and the renderer using another.
        """
        from videoforge_shared.settings import load_worker_settings

        render = load_worker_settings().render
        project = branded_project["project"]
        _approve_character_with_sheets(monkeypatch, sessions, branded_project)
        _advance_to_prompts(monkeypatch, sessions, project)
        _run(monkeypatch, sessions, project, ArtifactKind.IMAGE, max_scenes=1)

        with unit_of_work(sessions) as uow:
            scenes = uow.scenes.for_approved_set(project)
            artifact = uow.artifacts.find(project, ArtifactKind.IMAGE, scenes[0].id)
            assert artifact is not None
            version = uow.versions.latest(artifact.id)
            assert version is not None
            meta = version.meta

        assert meta["width"] == render.width
        assert meta["height"] == render.height

    def test_the_provider_original_is_kept_beside_it(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded_project: dict[str, str],
        recorder: _Recorder,
    ) -> None:
        """B2: keeping the original means a future re-crop — or a series that
        moves to 16:9 — never needs a paid regeneration."""
        project = branded_project["project"]
        _approve_character_with_sheets(monkeypatch, sessions, branded_project)
        _advance_to_prompts(monkeypatch, sessions, project)
        _run(monkeypatch, sessions, project, ArtifactKind.IMAGE, max_scenes=1)

        with unit_of_work(sessions) as uow:
            scenes = uow.scenes.for_approved_set(project)
            artifact = uow.artifacts.find(project, ArtifactKind.IMAGE, scenes[0].id)
            assert artifact is not None
            version = uow.versions.latest(artifact.id)
            assert version is not None

        assert version.meta["source_storage_key"]
        assert version.meta["source_storage_key"] != version.storage_key
        assert version.meta["source_width"]
        assert version.meta["source_height"]
        # What closing the gap cost, recorded per frame rather than inferred.
        assert "discarded" in version.meta


class TestFrameConstraints:
    """A scene image must be usable as a video frame.

    Measured on the first live run (2026-08-08): two of five scenes came back
    as split panels and one carried mirror-written text. Both are properties of
    *being a frame* rather than of this series' look, so neither may depend on
    an operator remembering to forbid them in a style.
    """

    def test_every_scene_refuses_panels_and_text(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded_project: dict[str, str],
        recorder: _Recorder,
    ) -> None:
        project = branded_project["project"]
        _approve_character_with_sheets(monkeypatch, sessions, branded_project)
        _advance_to_prompts(monkeypatch, sessions, project)
        _run(monkeypatch, sessions, project, ArtifactKind.IMAGE, max_scenes=2)

        assert len(recorder.requests) == 2
        for request in recorder.requests:
            negative = request.negative_prompt
            assert "split screen" in negative
            assert "multiple panels" in negative
            assert "writing" in negative
            # The positive prompt states what must be present and never names
            # what must not — an image model reads the noun, not the
            # instruction. ``test_prompts`` asserts that property of the
            # template itself; this only checks it reached the provider.
            # Whitespace-normalised: the frame is wrapped prose.
            positive = " ".join(request.prompt.split())
            assert "One continuous frame" in positive
            assert "runs to all four edges" in positive

    def test_the_style_does_not_have_to_ask_for_this(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded_project: dict[str, str],
        recorder: _Recorder,
    ) -> None:
        """The fixture's style says only ``medium: flat vector``.

        If these constraints ever migrate into an operator-editable style, this
        fails — which is the point. A series drawn any way at all still cannot
        use a frame split down the middle.
        """
        project = branded_project["project"]
        _approve_character_with_sheets(monkeypatch, sessions, branded_project)
        _advance_to_prompts(monkeypatch, sessions, project)
        _run(monkeypatch, sessions, project, ArtifactKind.IMAGE, max_scenes=1)

        with unit_of_work(sessions) as uow:
            style = uow.branding.style(branded_project["style"])
            assert style is not None
            assert "avoid" not in style.fields
        assert "split screen" in recorder.requests[0].negative_prompt


class TestReferences:
    def test_the_approved_sheets_reach_the_provider(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded_project: dict[str, str],
        recorder: _Recorder,
    ) -> None:
        """**The only reason M3-04b exists.** Sheets that were generated,
        reviewed and approved but never sent would be pure ceremony."""
        project = branded_project["project"]
        _approve_character_with_sheets(monkeypatch, sessions, branded_project)
        _advance_to_prompts(monkeypatch, sessions, project)
        _run(monkeypatch, sessions, project, ArtifactKind.IMAGE, max_scenes=1)

        assert recorder.requests
        references = recorder.requests[0].references
        assert len(references) == 4
        assert all(r.data for r in references)
        # Front first: a provider with a small reference budget must spend it
        # on the views that discriminate, not on whichever was written first.
        assert references[0].role.startswith("front")

    def test_a_character_without_sheets_still_generates(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded_project: dict[str, str],
        recorder: _Recorder,
    ) -> None:
        """Text alone already produces a recognisable character, so refusing
        here would make sheets mandatory for a measurable-not-assumed gain."""
        project = branded_project["project"]
        with unit_of_work(sessions) as uow:
            uow.branding.approve_character(branded_project["character"])
        _advance_to_prompts(monkeypatch, sessions, project)
        _run(monkeypatch, sessions, project, ArtifactKind.IMAGE, max_scenes=1)

        assert recorder.requests
        assert recorder.requests[0].references == ()


class TestPinning:
    def test_generation_uses_the_pin_not_the_series(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded_project: dict[str, str],
        recorder: _Recorder,
    ) -> None:
        """**ADR-016's whole point.** An episode half-generated against v1 must
        finish against v1, or its later scenes will not match its earlier ones.
        """
        project = branded_project["project"]
        _approve_character_with_sheets(monkeypatch, sessions, branded_project)
        _advance_to_prompts(monkeypatch, sessions, project)
        # The first image request pins. Run one scene to establish it.
        _run(monkeypatch, sessions, project, ArtifactKind.IMAGE, max_scenes=1)

        # The series moves on: a new character is approved, superseding the pin.
        with unit_of_work(sessions) as uow:
            successor = uow.branding.add_character_version(
                branded_project["series"],
                name="Pip",
                immutable_traits={"head": "a jagged obsidian shard"},
            )
            uow.flush()
            uow.branding.approve_character(successor.id)
            successor_id = successor.id

        _run(
            monkeypatch,
            sessions,
            project,
            ArtifactKind.IMAGE,
            regenerate=True,
            max_scenes=1,
        )

        with unit_of_work(sessions) as uow:
            row = uow.projects.get(project)
            assert row is not None
            assert row.character_version_id == branded_project["character"]
            assert row.character_version_id != successor_id
        # The prompt the provider actually received still describes the pinned
        # character, not the one the series now considers canonical.
        assert "obsidian" not in recorder.requests[-1].prompt
        assert "cream" in recorder.requests[-1].prompt

    def test_an_unpinned_project_fails_loudly(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded_project: dict[str, str],
        recorder: _Recorder,
    ) -> None:
        """Arriving here unpinned means the admission path was bypassed.

        Silently re-branding an episode mid-generation is a far worse outcome
        than a failed job, so the worker refuses to choose branding itself.
        """
        from videoforge_workers.images import images_body

        project = branded_project["project"]
        _approve_character_with_sheets(monkeypatch, sessions, branded_project)
        _advance_to_prompts(monkeypatch, sessions, project)
        _run(monkeypatch, sessions, project, ArtifactKind.IMAGE, max_scenes=1)

        # Strip the pin behind the service's back, which is the only way to
        # reach this state — `pin_branding` is write-once in SQL.
        with unit_of_work(sessions) as uow:
            uow.session.execute(
                sa.text(
                    "UPDATE video_project SET character_version_id = NULL, "
                    "style_version_id = NULL WHERE id = :id"
                ),
                {"id": project},
            )

        with unit_of_work(sessions) as uow:
            artifact = uow.artifacts.find(project, ArtifactKind.IMAGE)
            assert artifact is not None
            job = uow.jobs.reserve(
                project_id=project,
                task_name=IMAGES_GENERATE.name,
                queue=IMAGES_GENERATE.queue,
                idempotency_key=new_ulid(),
                artifact_id=artifact.id,
                input_snapshot={"artifact_id": artifact.id, "max_scenes": 1},
            ).job
            job_id = job.id

        import videoforge_workers.db as worker_db
        from videoforge_workers.skeleton import run_job

        monkeypatch.setattr(worker_db, "get_session_factory", lambda: sessions)
        with pytest.raises(RuntimeError, match="pinned branding"):
            run_job(job_id, images_body, task_name=IMAGES_GENERATE.name)


class TestMetering:
    def test_every_image_is_metered(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded_project: dict[str, str],
        recorder: _Recorder,
    ) -> None:
        """Images are the most expensive thing in the system; one usage row per
        image is what keeps the S10 daily cap honest."""
        project = branded_project["project"]
        _approve_character_with_sheets(monkeypatch, sessions, branded_project)
        _advance_to_prompts(monkeypatch, sessions, project)
        _run(monkeypatch, sessions, project, ArtifactKind.IMAGE, max_scenes=3)

        with unit_of_work(sessions) as uow:
            rows = uow.session.execute(
                sa.text(
                    "SELECT count(*), coalesce(sum(images), 0) FROM provider_usage u "
                    "JOIN generation_job j ON j.id = u.job_id "
                    "WHERE j.project_id = :p AND u.operation = 'image.generate'"
                ),
                {"p": project},
            ).one()
        assert rows[0] == 3
        assert rows[1] == 3


class TestContactSheetAndBatchApproval:
    """M3-09 / risk R9: the human gate must not be the bottleneck.

    Twenty scene images means twenty approvals, and the batch path is what
    stops that being the cost of every video. The properties that matter are
    that the server decides *which* versions may be approved, and that a raced
    tile is skipped and named rather than silently swept up.
    """

    def _sheet(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded_project: dict[str, str],
    ) -> str:
        project = branded_project["project"]
        _approve_character_with_sheets(monkeypatch, sessions, branded_project)
        _advance_to_prompts(monkeypatch, sessions, project)
        _run(monkeypatch, sessions, project, ArtifactKind.IMAGE, max_scenes=3)
        return project

    def test_pending_ids_come_from_the_fsm(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded_project: dict[str, str],
        recorder: _Recorder,
    ) -> None:
        """The client submits exactly what the server said was approvable.

        Filtering the list in TypeScript would be a second copy of the rule the
        machine enforces — the drift ``capabilities`` exists to prevent.
        """
        from videoforge_domain.artifact_lifecycle import capabilities
        from videoforge_shared.enums import ArtifactState

        project = self._sheet(monkeypatch, sessions, branded_project)
        with unit_of_work(sessions) as uow:
            per_scene = [
                a
                for a in uow.artifacts.for_project(project)
                if ArtifactKind(a.kind) is ArtifactKind.IMAGE and a.scene_ref
            ]
            approvable = [
                a
                for a in per_scene
                if capabilities(ArtifactState(a.state))["can_approve"]
            ]
        assert len(per_scene) == 3
        assert len(approvable) == 3

    def test_approving_a_batch_approves_every_one(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded_project: dict[str, str],
        recorder: _Recorder,
    ) -> None:
        from videoforge.services.review import ReviewService
        from videoforge_shared.enums import ArtifactState

        project = self._sheet(monkeypatch, sessions, branded_project)
        with unit_of_work(sessions) as uow:
            ids = [
                uow.versions.latest(a.id).id  # type: ignore[union-attr]
                for a in uow.artifacts.for_project(project)
                if ArtifactKind(a.kind) is ArtifactKind.IMAGE and a.scene_ref
            ]
            outcome = ReviewService(uow).approve_many(ids)
            assert len(outcome.approved) == 3
            assert outcome.skipped == ()

        with unit_of_work(sessions) as uow:
            states = {
                ArtifactState(a.state)
                for a in uow.artifacts.for_project(project)
                if ArtifactKind(a.kind) is ArtifactKind.IMAGE and a.scene_ref
            }
        assert states == {ArtifactState.APPROVED}

    def test_a_raced_version_is_skipped_and_the_rest_still_land(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded_project: dict[str, str],
        recorder: _Recorder,
    ) -> None:
        """**Partial success is the honest outcome.**

        One tile that moved while the reviewer was scrolling must not cost the
        other nineteen. The skip carries its reason so the UI can say which
        ones still need a look.
        """
        from videoforge.services.review import ReviewService
        from videoforge_shared.enums import ArtifactState

        project = self._sheet(monkeypatch, sessions, branded_project)
        with unit_of_work(sessions) as uow:
            artifacts = [
                a
                for a in uow.artifacts.for_project(project)
                if ArtifactKind(a.kind) is ArtifactKind.IMAGE and a.scene_ref
            ]
            ids = [uow.versions.latest(a.id).id for a in artifacts]  # type: ignore[union-attr]
            # One scene starts regenerating behind the reviewer's back.
            artifacts[0].state = ArtifactState.GENERATING

            outcome = ReviewService(uow).approve_many(ids)

        assert len(outcome.approved) == 2
        assert len(outcome.skipped) == 1
        assert outcome.skipped[0].reason

    def test_an_unknown_version_is_skipped_not_fatal(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded_project: dict[str, str],
        recorder: _Recorder,
    ) -> None:
        from videoforge.services.review import ReviewService

        project = self._sheet(monkeypatch, sessions, branded_project)
        with unit_of_work(sessions) as uow:
            good = [
                uow.versions.latest(a.id).id  # type: ignore[union-attr]
                for a in uow.artifacts.for_project(project)
                if ArtifactKind(a.kind) is ArtifactKind.IMAGE and a.scene_ref
            ][:1]
            outcome = ReviewService(uow).approve_many([*good, new_ulid()])

        assert len(outcome.approved) == 1
        assert len(outcome.skipped) == 1
        assert "not found" in outcome.skipped[0].reason


class TestPerSceneRegeneration:
    """A request naming one scene regenerates that scene and nothing else.

    **Found by driving the contact sheet, not by a test.** M3-07 always fanned
    out over every scene and always closed the project-wide artifact with a
    manifest. For a per-scene request the trigger *is* that scene's artifact,
    so the loop completed it and the manifest step then tried to complete it
    again — ``cannot apply 'generation_succeeded' to an artifact in state
    'AWAITING_APPROVAL'``. The FSM caught it, which is why it was a failed job
    rather than a corrupt one.
    """

    def test_only_the_named_scene_is_generated(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded_project: dict[str, str],
        recorder: _Recorder,
    ) -> None:
        project = branded_project["project"]
        _approve_character_with_sheets(monkeypatch, sessions, branded_project)
        _advance_to_prompts(monkeypatch, sessions, project)
        _run(monkeypatch, sessions, project, ArtifactKind.IMAGE, max_scenes=2)
        before = len(recorder.requests)

        with unit_of_work(sessions) as uow:
            target = uow.scenes.for_approved_set(project)[0]
            scene_id, scene_index = target.id, target.index

        _run(
            monkeypatch,
            sessions,
            project,
            ArtifactKind.IMAGE,
            regenerate=True,
            scene_ref=scene_id,
        )

        # Exactly one more provider call, for the scene that was named.
        assert len(recorder.requests) == before + 1
        with unit_of_work(sessions) as uow:
            artifact = uow.artifacts.find(project, ArtifactKind.IMAGE, scene_id)
            assert artifact is not None
            assert artifact.state is ArtifactState.AWAITING_APPROVAL
            version = uow.versions.latest(artifact.id)
            assert version is not None
            assert version.version_no == 2
            assert version.meta["scene_index"] == scene_index

    def test_a_scene_the_set_has_moved_past_fails_loudly(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded_project: dict[str, str],
        recorder: _Recorder,
    ) -> None:
        """The reachable case, which is not "an id that never existed".

        ``artifact.scene_ref`` has a foreign key, so a fabricated id is
        rejected when the *job* is created — the guard in the worker is for a
        scene that was real and approved when the job was queued, and whose
        scene set has since been regenerated. Drawing that frame would attach
        an image to a scene no longer in the video.
        """
        import videoforge_workers.db as worker_db
        from videoforge_workers.images import images_body
        from videoforge_workers.skeleton import run_job

        project = branded_project["project"]
        _approve_character_with_sheets(monkeypatch, sessions, branded_project)
        _advance_to_prompts(monkeypatch, sessions, project)

        with unit_of_work(sessions) as uow:
            scene_id = uow.scenes.for_approved_set(project)[0].id
            job_id = (
                JobService(uow, RecordingDispatcher())
                .request(
                    project_id=project,
                    kind=ArtifactKind.IMAGE,
                    spec=IMAGES_GENERATE,
                    scene_ref=scene_id,
                )
                .job.id
            )

        # The scene set moves on while the job sits in the queue.
        _run(monkeypatch, sessions, project, ArtifactKind.SCENE_SET, regenerate=True)
        _approve(sessions, project, ArtifactKind.SCENE_SET)

        monkeypatch.setattr(worker_db, "get_session_factory", lambda: sessions)
        with pytest.raises(RuntimeError, match="not in the approved scene set"):
            run_job(job_id, images_body, task_name=IMAGES_GENERATE.name)


class TestCorrectionFeedback:
    """M3-10: a rejection changes the next attempt.

    Regenerating already worked and so did rejecting; what was missing is the
    bit between them. Without this, a reviewer says what is wrong and the next
    generation runs against exactly the prompt that just failed.
    """

    def _generated(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded_project: dict[str, str],
    ) -> tuple[str, str]:
        project = branded_project["project"]
        _approve_character_with_sheets(monkeypatch, sessions, branded_project)
        _advance_to_prompts(monkeypatch, sessions, project)
        _run(monkeypatch, sessions, project, ArtifactKind.IMAGE, max_scenes=1)
        with unit_of_work(sessions) as uow:
            scene_id = uow.scenes.for_approved_set(project)[0].id
        return project, scene_id

    def test_a_rejection_reaches_the_next_prompt(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded_project: dict[str, str],
        recorder: _Recorder,
    ) -> None:
        from videoforge_domain.rejection import RejectionReason

        project, scene_id = self._generated(monkeypatch, sessions, branded_project)

        with unit_of_work(sessions) as uow:
            artifact = uow.artifacts.find(project, ArtifactKind.IMAGE, scene_id)
            assert artifact is not None
            version = uow.versions.latest(artifact.id)
            assert version is not None
            ReviewService(uow).reject(
                version.id,
                reasons=[
                    RejectionReason.TEXT_ARTIFACTS.value,
                    RejectionReason.ANATOMY.value,
                ],
                comment="the sign says something",
            )

        _run(
            monkeypatch,
            sessions,
            project,
            ArtifactKind.IMAGE,
            regenerate=True,
            scene_ref=scene_id,
        )

        latest = recorder.requests[-1]
        assert "A previous attempt at this exact frame was rejected" in latest.prompt
        assert "blank" in latest.prompt
        assert "two arms and two legs" in latest.prompt
        # The reviewer's own words travel too — the taxonomy cannot say this.
        assert "the sign says something" in latest.prompt
        # And the prohibitions land in the negative channel, never the positive.
        assert "handwriting" in latest.negative_prompt
        assert "extra limbs" in latest.negative_prompt

    def test_a_first_generation_carries_no_correction(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded_project: dict[str, str],
        recorder: _Recorder,
    ) -> None:
        """The ordinary case must cost no extra prompt text at all."""
        self._generated(monkeypatch, sessions, branded_project)
        assert "was rejected" not in recorder.requests[-1].prompt

    def test_the_reasons_are_stored_on_the_decision(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded_project: dict[str, str],
        recorder: _Recorder,
    ) -> None:
        """Countable, which free text is not. "Character drift on 6 of 20
        scenes" is a fact you can act on."""
        from videoforge_domain.rejection import RejectionReason

        project, scene_id = self._generated(monkeypatch, sessions, branded_project)
        with unit_of_work(sessions) as uow:
            artifact = uow.artifacts.find(project, ArtifactKind.IMAGE, scene_id)
            assert artifact is not None
            version = uow.versions.latest(artifact.id)
            assert version is not None
            ReviewService(uow).reject(
                version.id, reasons=[RejectionReason.CHARACTER_DRIFT.value]
            )

        with unit_of_work(sessions) as uow:
            artifact = uow.artifacts.find(project, ArtifactKind.IMAGE, scene_id)
            assert artifact is not None
            rejection = uow.reviews.last_rejection(artifact.id)
            assert rejection is not None
            assert rejection.reasons == ["character_drift"]

    def test_approving_ends_the_correction(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded_project: dict[str, str],
        recorder: _Recorder,
    ) -> None:
        """``last_rejection`` is the *most recent* REJECT across the artifact's
        versions, so a later rejection supersedes an earlier one — and a
        regeneration after an approval still carries the last complaint, which
        is correct: nothing has said the problem is fixed except the picture.
        """
        from videoforge_domain.rejection import RejectionReason

        project, scene_id = self._generated(monkeypatch, sessions, branded_project)
        with unit_of_work(sessions) as uow:
            artifact = uow.artifacts.find(project, ArtifactKind.IMAGE, scene_id)
            assert artifact is not None
            version = uow.versions.latest(artifact.id)
            assert version is not None
            ReviewService(uow).reject(
                version.id, reasons=[RejectionReason.QUALITY.value]
            )
            artifact_id = artifact.id

        _run(
            monkeypatch,
            sessions,
            project,
            ArtifactKind.IMAGE,
            regenerate=True,
            scene_ref=scene_id,
        )
        assert "crisp shapes" in recorder.requests[-1].prompt

        # A second, different rejection replaces the first rather than adding.
        with unit_of_work(sessions) as uow:
            version = uow.versions.latest(artifact_id)
            assert version is not None
            ReviewService(uow).reject(
                version.id, reasons=[RejectionReason.OFF_BRIEF.value]
            )

        _run(
            monkeypatch,
            sessions,
            project,
            ArtifactKind.IMAGE,
            regenerate=True,
            scene_ref=scene_id,
        )
        assert "Read the scene description again" in recorder.requests[-1].prompt
        assert "crisp shapes" not in recorder.requests[-1].prompt


class TestUsageOperation:
    def test_image_spend_is_filed_as_image(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded_project: dict[str, str],
        recorder: _Recorder,
    ) -> None:
        """``complete_stored_generation`` is shared with M3-12's narration, and
        its ``operation`` was a hardcoded constant until a live run filed an
        ElevenLabs call under ``image.generate``. The S10 cap reads this column,
        so a wrong label is spend attributed to the wrong modality.
        """
        project = branded_project["project"]
        _approve_character_with_sheets(monkeypatch, sessions, branded_project)
        _advance_to_prompts(monkeypatch, sessions, project)
        _run(monkeypatch, sessions, project, ArtifactKind.IMAGE, max_scenes=1)

        with unit_of_work(sessions) as uow:
            operations = uow.session.execute(
                sa.text(
                    "SELECT DISTINCT u.operation FROM provider_usage u "
                    "JOIN generation_job j ON j.id = u.job_id "
                    "WHERE j.project_id = :p AND u.operation <> 'llm.complete'"
                ),
                {"p": project},
            ).scalars()
        assert set(operations) == {"image.generate"}


class TestCardScenes:
    """M4-02. A card is rendered here, not bought.

    The three properties that make cards worth having are all asserted:
    the provider is never called, the frame still exists, and no human is
    asked to approve words they already approved.
    """

    def _make_card(self, sessions: sessionmaker[Session], project_id: str) -> str:
        """Append a card scene to the approved set.

        **Appended, not converted.** The first version of this UPDATEd an
        existing scene into a card and was refused by the append-only trigger
        — *"relation scene is append-only; UPDATE is forbidden (SADD 10.3)"* —
        which is the trigger doing its job on a test that had misread
        immutability as a convention. An INSERT is what the schema permits and
        what a real regenerated scene set does anyway.
        """
        with unit_of_work(sessions) as uow:
            scenes = uow.scenes.for_approved_set(project_id)
            scene_id = new_ulid()
            uow.session.execute(
                sa.text(
                    'INSERT INTO scene (id, scene_set_id, "index", narration_text,'
                    " visual_brief, target_duration_ms, kind, card_text)"
                    " VALUES (:id, :ss, :i, 'Step five.', 'a step marker',"
                    " 2000, 'card', 'Step 5')"
                ),
                {
                    "id": scene_id,
                    "ss": scenes[0].scene_set_id,
                    "i": len(scenes) + 1,
                },
            )
        return scene_id

    def test_a_card_scene_never_reaches_the_provider(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded_project: dict[str, str],
        recorder: _Recorder,
    ) -> None:
        """The point of §1.0.3: no call, no cost, no drift."""
        project = branded_project["project"]
        _approve_character_with_sheets(monkeypatch, sessions, branded_project)
        _advance_to_prompts(monkeypatch, sessions, project)
        self._make_card(sessions, project)

        _run(monkeypatch, sessions, project, ArtifactKind.IMAGE)

        with unit_of_work(sessions) as uow:
            scene_count = len(uow.scenes.for_approved_set(project))
        # One fewer provider call than there are scenes — exactly the card.
        assert len(recorder.requests) == scene_count - 1

    def test_a_card_still_produces_an_approved_frame(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded_project: dict[str, str],
    ) -> None:
        """Approved without a human, because the text was approved at the
        scene-set gate and there is no second judgement to make (§1.0.3).

        The frame is real: a ``storage_key``, like every other image version.
        """
        project = branded_project["project"]
        _approve_character_with_sheets(monkeypatch, sessions, branded_project)
        _advance_to_prompts(monkeypatch, sessions, project)
        scene_id = self._make_card(sessions, project)

        _run(monkeypatch, sessions, project, ArtifactKind.IMAGE)

        with unit_of_work(sessions) as uow:
            artifact = uow.artifacts.find(project, ArtifactKind.IMAGE, scene_id)
            assert artifact is not None
            assert artifact.state == ArtifactState.APPROVED
            version = uow.versions.latest(artifact.id)
            assert version is not None
            assert version.storage_key
            assert version.inline_content is None
            assert version.meta["card_text"] == "Step 5"
            assert version.prompt_template_ref == "card@1"

    def test_the_card_approval_names_no_reviewer(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded_project: dict[str, str],
    ) -> None:
        """The audit trail must not attribute an automatic approval to a
        person, and must say *why* it was automatic — the same mechanism
        serves series policy, and the two have to stay distinguishable."""
        project = branded_project["project"]
        _approve_character_with_sheets(monkeypatch, sessions, branded_project)
        _advance_to_prompts(monkeypatch, sessions, project)
        scene_id = self._make_card(sessions, project)

        _run(monkeypatch, sessions, project, ArtifactKind.IMAGE)

        with unit_of_work(sessions) as uow:
            artifact = uow.artifacts.find(project, ArtifactKind.IMAGE, scene_id)
            assert artifact is not None
            version = uow.versions.latest(artifact.id)
            assert version is not None
            row = uow.session.execute(
                sa.text(
                    "SELECT reviewer_id, comment FROM review_decision"
                    " WHERE artifact_version_id = :v"
                ),
                {"v": version.id},
            ).one()
        assert row[0] is None
        assert "card" in row[1]

    def test_a_card_costs_nothing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded_project: dict[str, str],
    ) -> None:
        """A usage row with a real zero rather than no row at all: a gap in
        ``provider_usage`` reads like a missing record, and the S10 cap should
        sum a genuine zero for a frame that cost nothing."""
        project = branded_project["project"]
        _approve_character_with_sheets(monkeypatch, sessions, branded_project)
        _advance_to_prompts(monkeypatch, sessions, project)
        self._make_card(sessions, project)

        _run(monkeypatch, sessions, project, ArtifactKind.IMAGE)

        with unit_of_work(sessions) as uow:
            rows = uow.session.execute(
                sa.text(
                    "SELECT u.provider, u.unit_cost_estimate FROM provider_usage u"
                    " JOIN generation_job j ON j.id = u.job_id"
                    " WHERE j.project_id = :p AND u.operation = 'card.render'"
                ),
                {"p": project},
            ).all()
        assert len(rows) == 1
        assert rows[0][0] == "local"
        assert float(rows[0][1]) == 0.0

    def test_the_manifest_accounts_for_the_card(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        branded_project: dict[str, str],
    ) -> None:
        """ "Twenty scenes, nineteen images" has two explanations and only one
        of them is fine. The manifest carries the third number, and stays in
        scene order even though cards are rendered first."""
        project = branded_project["project"]
        _approve_character_with_sheets(monkeypatch, sessions, branded_project)
        _advance_to_prompts(monkeypatch, sessions, project)
        self._make_card(sessions, project)

        _run(monkeypatch, sessions, project, ArtifactKind.IMAGE)

        with unit_of_work(sessions) as uow:
            trigger = uow.artifacts.find(project, ArtifactKind.IMAGE)
            assert trigger is not None
            version = uow.versions.latest(trigger.id)
            assert version is not None
            content = version.inline_content
        assert content is not None
        assert content["cards"] == 1
        assert content["generated"] == content["scene_count"]
        indexes = [entry["scene_index"] for entry in content["images"]]
        assert indexes == sorted(indexes)
