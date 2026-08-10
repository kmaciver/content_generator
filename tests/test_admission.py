"""M3-06: the admission gate, against a real PostgreSQL.

Two rules that look alike and are not (ADR-016):

* **pipeline prerequisites** — the previous stage is in progress, so *wait*;
* **branding** — this project has no character, nothing in this pipeline will
  ever produce one, so go to another screen.

Both 409, and the messages have to say which is which.

The first of these did not exist before M3-06. M2-13 computed ``unmet`` in the
DTO to disable a button and asserted the service "will then independently
enforce" it; nothing did. These tests are what stop that regressing.
"""

from __future__ import annotations

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from videoforge.services.admission import (
    AdmissionError,
    check_admission,
    resolve_branding,
)
from videoforge.services.dispatch import RecordingDispatcher
from videoforge.services.jobs import JobService
from videoforge.services.review import ReviewService
from videoforge_persistence.models import Workspace
from videoforge_persistence.uow import UnitOfWork, unit_of_work
from videoforge_shared.enums import ArtifactKind, ArtifactState, VersionOrigin
from videoforge_shared.ids import new_ulid
from videoforge_shared.tasks import RESEARCH_GENERATE, SCRIPT_GENERATE

pytestmark = pytest.mark.integration


@pytest.fixture()
def sessions(db_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def world(sessions: sessionmaker[Session]) -> dict[str, str]:
    """Workspace → series → project, with no branding and no artifacts."""
    with unit_of_work(sessions) as uow:
        workspace = Workspace(id=new_ulid(), name="admission-test")
        uow.session.add(workspace)
        uow.flush()
        series = uow.series.create(workspace_id=workspace.id, title="Explainers")
        uow.flush()
        project = uow.projects.create(
            workspace_id=workspace.id, series_id=series.id, topic="tides"
        )
        uow.flush()
        return {
            "workspace": workspace.id,
            "series": series.id,
            "project": project.id,
        }


def _approve_stage(uow: UnitOfWork, project_id: str, kind: ArtifactKind) -> None:
    """Fabricate an approved artifact of ``kind`` without running a worker."""
    artifact = uow.artifacts.find(project_id, kind) or uow.artifacts.create(
        project_id, kind, state=ArtifactState.GENERATING
    )
    uow.flush()
    version = uow.versions.add_version(
        artifact,
        origin=VersionOrigin.GENERATED,
        content_hash="0" * 64,  # the column is varchar(64): the bare digest
        inline_content={"stub": True},
    )
    uow.flush()
    artifact.state = ArtifactState.AWAITING_APPROVAL
    ReviewService(uow).approve(version.id)


def _branding(uow: UnitOfWork, series_id: str) -> tuple[str, str]:
    character = uow.branding.add_character_version(series_id, name="Pip")
    style = uow.branding.add_style_version(series_id, name="Flat")
    uow.flush()
    uow.branding.approve_character(character.id)
    uow.branding.approve_style(style.id)
    uow.flush()
    return str(character.id), str(style.id)


class TestPipelinePrerequisites:
    def test_a_root_stage_is_always_admissible(
        self, sessions: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """`research` requires nothing, so a fresh project can start."""
        with unit_of_work(sessions) as uow:
            check_admission(uow, world["project"], ArtifactKind.RESEARCH)

    def test_a_stage_with_unapproved_inputs_is_refused(
        self, sessions: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """**The gap M3-06 closes.**

        Before this, the request got a 202, created a job, and failed in the
        worker at ``require_approved_content`` — whose own comment said
        arriving there meant the guard had failed. There was no guard.
        """
        with unit_of_work(sessions) as uow, pytest.raises(AdmissionError) as caught:
            check_admission(uow, world["project"], ArtifactKind.SCENE_SET)
        assert "script" in str(caught.value)

    def test_the_message_names_what_it_is_waiting_on(
        self, sessions: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """ "Waiting on: research" sends an operator somewhere. "Conflict" does
        not — the same argument `StageSummary.unmet` makes for the UI."""
        with unit_of_work(sessions) as uow, pytest.raises(AdmissionError) as caught:
            check_admission(uow, world["project"], ArtifactKind.SCRIPT)
        assert "waiting on: research" in str(caught.value)

    def test_admissible_once_the_input_is_approved(
        self, sessions: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        with unit_of_work(sessions) as uow:
            _approve_stage(uow, world["project"], ArtifactKind.RESEARCH)
        with unit_of_work(sessions) as uow:
            check_admission(uow, world["project"], ArtifactKind.SCRIPT)

    def test_a_generating_sibling_still_blocks(
        self, sessions: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Least-advanced-wins, the same rule phase derivation uses. An input
        that is merely *in progress* is not an approved input."""
        with unit_of_work(sessions) as uow:
            uow.artifacts.create(
                world["project"],
                ArtifactKind.RESEARCH,
                state=ArtifactState.GENERATING,
            )
        with unit_of_work(sessions) as uow, pytest.raises(AdmissionError):
            check_admission(uow, world["project"], ArtifactKind.SCRIPT)


class TestBrandingAdmission:
    def test_images_need_an_approved_character_and_style(
        self, sessions: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        with unit_of_work(sessions) as uow, pytest.raises(AdmissionError) as caught:
            resolve_branding(uow, world["project"])
        message = str(caught.value)
        assert "character" in message and "style" in message

    def test_a_project_with_no_series_is_refused(
        self, sessions: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """`series_id` is nullable for one-off videos, and a one-off video has
        no branding. ADR-016 rejected workspace-level defaults as machinery for
        a case with no user yet."""
        with unit_of_work(sessions) as uow:
            project = uow.projects.get(world["project"])
            assert project is not None
            project.series_id = None
        with unit_of_work(sessions) as uow, pytest.raises(AdmissionError) as caught:
            resolve_branding(uow, world["project"])
        assert "belongs to no series" in str(caught.value)

    def test_resolves_once_both_are_approved(
        self, sessions: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        with unit_of_work(sessions) as uow:
            character_id, style_id = _branding(uow, world["series"])
        with unit_of_work(sessions) as uow:
            branding = resolve_branding(uow, world["project"])
        assert branding.character.id == character_id
        assert branding.style.id == style_id
        assert branding.pinned is False

    def test_only_images_require_branding(
        self, sessions: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """The prompt stage names no style on purpose (`prompt.v1.jinja`), so
        requiring branding there would block a stage that cannot use it."""
        with unit_of_work(sessions) as uow:
            _approve_stage(uow, world["project"], ArtifactKind.RESEARCH)
            _approve_stage(uow, world["project"], ArtifactKind.SCRIPT)
            _approve_stage(uow, world["project"], ArtifactKind.SCENE_SET)
        with unit_of_work(sessions) as uow:
            check_admission(uow, world["project"], ArtifactKind.PROMPT)


class TestPinning:
    def test_a_pin_survives_the_series_moving_on(
        self, sessions: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """**The reason pinning exists.**

        Approving character v2 must not retroactively re-brand an episode
        built from v1 — that would be a staleness cascade across the whole back
        catalogue, triggered by an ordinary tweak.
        """
        with unit_of_work(sessions) as uow:
            v1_id, style_id = _branding(uow, world["series"])
            uow.projects.pin_branding(
                world["project"],
                character_version_id=v1_id,
                style_version_id=style_id,
            )

        with unit_of_work(sessions) as uow:
            v2 = uow.branding.add_character_version(world["series"], name="Pip v2")
            uow.flush()
            uow.branding.approve_character(v2.id)

        with unit_of_work(sessions) as uow:
            branding = resolve_branding(uow, world["project"])
            assert branding.character.id == v1_id
            assert branding.pinned is True
            # ...while the *series* has genuinely moved on.
            current = uow.branding.approved_character(world["series"])
            assert current is not None and current.id != v1_id

    def test_pinning_is_write_once(
        self, sessions: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Guarded in SQL, so two concurrent image jobs on a fresh project
        cannot both win."""
        with unit_of_work(sessions) as uow:
            character_id, style_id = _branding(uow, world["series"])
            first = uow.projects.pin_branding(
                world["project"],
                character_version_id=character_id,
                style_version_id=style_id,
            )
            second = uow.projects.pin_branding(
                world["project"],
                character_version_id=new_ulid(),
                style_version_id=new_ulid(),
            )
        assert (first, second) == (True, False)

        with unit_of_work(sessions) as uow:
            project = uow.projects.get(world["project"])
            assert project is not None
            assert project.character_version_id == character_id

    def test_a_dangling_pin_is_reported_not_silently_rebranded(
        self, sessions: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Only reachable by deleting the series. Falling back to the series'
        current branding would silently re-brand an existing video, which is
        the failure pinning exists to prevent."""
        with unit_of_work(sessions) as uow:
            uow.projects.pin_branding(
                world["project"],
                character_version_id=new_ulid(),
                style_version_id=new_ulid(),
            )
        with unit_of_work(sessions) as uow, pytest.raises(AdmissionError) as caught:
            resolve_branding(uow, world["project"])
        assert "no longer exists" in str(caught.value)


class TestThroughTheJobService:
    def test_a_refused_request_creates_no_job(
        self, sessions: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Admission runs **before** the idempotency reservation.

        A rejected attempt must not consume the key: it would park it on a live
        job row and make the legitimate retry — once the script is approved —
        look like a duplicate of the failure.
        """
        with unit_of_work(sessions) as uow:
            service = JobService(uow, RecordingDispatcher())
            with pytest.raises(AdmissionError):
                service.request(
                    project_id=world["project"],
                    kind=ArtifactKind.SCRIPT,
                    spec=SCRIPT_GENERATE,
                )

        with unit_of_work(sessions) as uow:
            assert uow.artifacts.find(world["project"], ArtifactKind.SCRIPT) is None

    def test_an_admissible_request_still_works(
        self, sessions: sessionmaker[Session], world: dict[str, str]
    ) -> None:
        """Positive control. A gate that refused everything would pass every
        test above and break the product."""
        with unit_of_work(sessions) as uow:
            outcome = JobService(uow, RecordingDispatcher()).request(
                project_id=world["project"],
                kind=ArtifactKind.RESEARCH,
                spec=RESEARCH_GENERATE,
            )
            assert outcome.created is True
