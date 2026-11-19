"""Per-scene artifacts of a replaced scene set must not hold a stage.

Regenerating scenes leaves the old scenes' images and prompts behind: they
point at scenes of a scene-set version that is no longer approved. The contact
sheet only shows the approved set's scenes, so an old image left
AWAITING_APPROVAL was invisible — yet the least-advanced-wins rollups counted
it, and the image stage sat at AWAITING_APPROVAL with every visible tile
approved and nothing left to click.

Found on a real project: 13 approved images for 13 current scenes, 8 orphans
from a 10-scene set that had been regenerated.
"""

from __future__ import annotations

import pytest
from flask.testing import FlaskClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from videoforge.app import create_app
from videoforge.services.admission import check_admission
from videoforge.services.dispatch import RecordingDispatcher
from videoforge.services.review import ReviewService
from videoforge_persistence.models import Scene, SceneSet, Workspace
from videoforge_persistence.projection import (
    get_pipeline,
    live_artifacts,
    recompute_phase,
)
from videoforge_persistence.uow import UnitOfWork, unit_of_work
from videoforge_shared.enums import ArtifactKind, ArtifactState, VersionOrigin
from videoforge_shared.ids import new_ulid
from videoforge_shared.settings import load_app_settings

pytestmark = pytest.mark.integration


@pytest.fixture()
def sessions(db_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)


def _scene_set_version(
    uow: UnitOfWork, scene_set_artifact_id: str, script_version_id: str
) -> tuple[str, str]:
    """Add a scene-set version with one scene. Returns (version id, scene id)."""
    artifact = uow.artifacts.get(scene_set_artifact_id)
    assert artifact is not None
    version = uow.versions.add_version(
        artifact,
        origin=VersionOrigin.GENERATED,
        content_hash=new_ulid(),
        inline_content={"scenes": []},
    )
    artifact.state = ArtifactState.AWAITING_APPROVAL
    uow.flush()
    set_id = new_ulid()
    uow.session.add(
        SceneSet(
            id=set_id,
            artifact_version_id=version.id,
            script_version_id=script_version_id,
        )
    )
    uow.flush()
    scene_id = new_ulid()
    uow.session.add(
        Scene(
            id=scene_id,
            scene_set_id=set_id,
            index=1,
            narration_text="n",
            visual_brief="v",
            target_duration_ms=3000,
        )
    )
    uow.flush()
    return version.id, scene_id


@pytest.fixture()
def regenerated(sessions: sessionmaker[Session]) -> dict[str, str]:
    """Everything up to voice approved; scenes were regenerated once, and the
    old scene's image was never decided."""
    with unit_of_work(sessions) as uow:
        workspace = Workspace(id=new_ulid(), name="orphans")
        uow.session.add(workspace)
        uow.flush()
        series = uow.series.create(workspace_id=workspace.id, title="s")
        uow.flush()
        project = uow.projects.create(
            workspace_id=workspace.id, series_id=series.id, topic="tides"
        )
        uow.flush()

        for kind in (
            ArtifactKind.RESEARCH,
            ArtifactKind.VOICE_DIRECTION,
            ArtifactKind.VOICE,
        ):
            uow.artifacts.create(project.id, kind, state=ArtifactState.APPROVED)
        script = uow.artifacts.create(
            project.id, ArtifactKind.SCRIPT, state=ArtifactState.APPROVED
        )
        uow.flush()
        script_version = uow.versions.add_version(
            script,
            origin=VersionOrigin.GENERATED,
            content_hash="script",
            inline_content={"script": "words"},
        )
        scene_set = uow.artifacts.create(project.id, ArtifactKind.SCENE_SET, None)
        uow.flush()

        old_version, old_scene = _scene_set_version(
            uow, scene_set.id, script_version.id
        )
        ReviewService(uow).approve(old_version)
        uow.flush()
        new_version, new_scene = _scene_set_version(
            uow, scene_set.id, script_version.id
        )
        ReviewService(uow).approve(new_version)
        uow.flush()

        # The orphans: the old scene's prompt (approved) and image (never
        # decided, because its scene was replaced before anyone looked).
        uow.artifacts.create(
            project.id, ArtifactKind.PROMPT, old_scene, state=ArtifactState.APPROVED
        )
        orphan = uow.artifacts.create(
            project.id,
            ArtifactKind.IMAGE,
            old_scene,
            state=ArtifactState.AWAITING_APPROVAL,
        )
        # The current scene, fully approved, plus the stage's manifest row.
        uow.artifacts.create(
            project.id, ArtifactKind.PROMPT, new_scene, state=ArtifactState.APPROVED
        )
        current = uow.artifacts.create(
            project.id, ArtifactKind.IMAGE, new_scene, state=ArtifactState.APPROVED
        )
        uow.artifacts.create(
            project.id, ArtifactKind.IMAGE, None, state=ArtifactState.APPROVED
        )
        uow.flush()
        return {"project": project.id, "orphan": orphan.id, "current": current.id}


class TestOrphanedSceneArtifacts:
    def test_live_artifacts_drop_the_replaced_scenes(
        self, sessions: sessionmaker[Session], regenerated: dict[str, str]
    ) -> None:
        with unit_of_work(sessions) as uow:
            ids = {a.id for a in live_artifacts(uow, regenerated["project"])}
        assert regenerated["current"] in ids
        assert regenerated["orphan"] not in ids

    def test_the_orphan_does_not_hold_the_phase(
        self, sessions: sessionmaker[Session], regenerated: dict[str, str]
    ) -> None:
        """Images and voice approved, timeline not started: TIMELINE_READY,
        not MEDIA_REVIEW."""
        with unit_of_work(sessions) as uow:
            phase = recompute_phase(uow, regenerated["project"])
        timeline = get_pipeline().stage_for(ArtifactKind.TIMELINE)
        assert phase is timeline.phase_generating

    def test_the_orphan_does_not_block_admission(
        self, sessions: sessionmaker[Session], regenerated: dict[str, str]
    ) -> None:
        with unit_of_work(sessions) as uow:
            check_admission(uow, regenerated["project"], ArtifactKind.TIMELINE)

    def test_the_project_page_reads_the_stage_approved(
        self, db_engine: Engine, regenerated: dict[str, str]
    ) -> None:
        """Through the endpoint — the screen the bug was reported on."""
        app = create_app(
            load_app_settings(), dispatcher=RecordingDispatcher(), engine=db_engine
        )
        app.config.update(TESTING=True)
        client: FlaskClient = app.test_client()
        response = client.get(f"/api/v1/projects/{regenerated['project']}")
        assert response.status_code == 200
        stages = {s["kind"]: s for s in response.get_json()["stages"]}
        assert stages["image"]["state"] == ArtifactState.APPROVED.value
