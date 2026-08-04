"""M2-08 / M2-11 / M2-12: the stages added in batch 3, against a real database.

``test_script_stage.py`` already covers the shape a stage shares. What is new
here is what only these stages do: research as the DAG's root, scenes writing
*rows* alongside a version, and one job producing N artifacts.

The whole chain runs for real — research → approve → script → approve → scenes
→ approve → prompts — because the interesting failures are at the joins, and a
test that hand-seeded each upstream would keep passing after a schema change
that broke the real handoff.
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
from videoforge_shared.enums import ArtifactKind, ArtifactState, ProjectPhase
from videoforge_shared.ids import new_ulid
from videoforge_shared.tasks import (
    PROMPTS_GENERATE,
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
}


@pytest.fixture()
def sessions(db_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def project(sessions: sessionmaker[Session]) -> Any:
    workspace_id = new_ulid()
    with unit_of_work(sessions) as uow:
        uow.session.add(Workspace(id=workspace_id, name="stages"))
        uow.flush()
        series = uow.series.create(workspace_id=workspace_id, title="Explainers")
        uow.flush()
        row = uow.projects.create(
            workspace_id=workspace_id,
            series_id=series.id,
            topic="why the tides move",
        )
        row.settings = {SETTINGS_KEY: 40_000}
        uow.flush()
        project_id = row.id

    yield project_id

    with unit_of_work(sessions) as uow:
        uow.session.execute(
            sa.text("DELETE FROM workspace WHERE id = :id"), {"id": workspace_id}
        )
        uow.session.execute(
            sa.text("DELETE FROM outbox_event WHERE payload->>'project_id' = :id"),
            {"id": project_id},
        )


def _run(
    monkeypatch: pytest.MonkeyPatch,
    sessions: sessionmaker[Session],
    project_id: str,
    kind: ArtifactKind,
) -> None:
    """Request and execute one stage, calling the body directly (no broker)."""
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
            .request(project_id=project_id, kind=kind, spec=spec)
            .job.id
        )

    monkeypatch.setattr(worker_db, "get_session_factory", lambda: sessions)
    assert run_job(job_id, body, task_name=spec.name) is True


def _approve(
    sessions: sessionmaker[Session], project_id: str, kind: ArtifactKind
) -> None:
    with unit_of_work(sessions) as uow:
        artifact = uow.artifacts.find(project_id, kind)
        assert artifact is not None, kind
        version = uow.versions.latest(artifact.id)
        assert version is not None
        ReviewService(uow).approve(version.id)


def _advance(
    monkeypatch: pytest.MonkeyPatch,
    sessions: sessionmaker[Session],
    project_id: str,
    through: ArtifactKind,
) -> None:
    """Run and approve every stage up to and including ``through``."""
    order = [
        ArtifactKind.RESEARCH,
        ArtifactKind.SCRIPT,
        ArtifactKind.SCENE_SET,
        ArtifactKind.PROMPT,
    ]
    for kind in order[: order.index(through) + 1]:
        _run(monkeypatch, sessions, project_id, kind)
        _approve(sessions, project_id, kind)


class TestResearchStage:
    def test_research_needs_no_upstream(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        project: str,
    ) -> None:
        """The DAG's only root. If this ever needs something approved first,
        `templates/pipeline.yaml` and this test disagree and one is wrong."""
        _run(monkeypatch, sessions, project, ArtifactKind.RESEARCH)

        with unit_of_work(sessions) as uow:
            artifact = uow.artifacts.find(project, ArtifactKind.RESEARCH)
            assert artifact is not None
            assert artifact.state is ArtifactState.AWAITING_APPROVAL
            version = uow.versions.latest(artifact.id)
            assert version is not None
            assert version.inline_content is not None
            assert version.prompt_template_ref is not None
            assert version.prompt_template_ref.startswith("research@")


class TestScriptNeedsResearch:
    def test_script_fails_without_approved_research(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        project: str,
    ) -> None:
        """**M2-09.** Loudly, not by generating a script about nothing.

        The DAG is supposed to make this unreachable, so arriving here means a
        guard failed — and a script generated from no research would look
        entirely plausible while being unfounded.
        """
        with pytest.raises(RuntimeError, match="research"):
            _run(monkeypatch, sessions, project, ArtifactKind.SCRIPT)

    def test_script_uses_the_approved_research(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        project: str,
    ) -> None:
        _advance(monkeypatch, sessions, project, ArtifactKind.SCRIPT)

        with unit_of_work(sessions) as uow:
            artifact = uow.artifacts.find(project, ArtifactKind.SCRIPT)
            assert artifact is not None
            version = uow.versions.latest(artifact.id)
            assert version is not None
            assert version.inline_content is not None
            assert version.inline_content["script"]


class TestScenesStage:
    def test_scenes_writes_rows_alongside_the_version(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        project: str,
    ) -> None:
        """The point of M2-11: a version *and* the rows the image stage joins.

        Both in one transaction — rows that reference a version must not be
        able to exist without it.
        """
        _advance(monkeypatch, sessions, project, ArtifactKind.SCENE_SET)

        with unit_of_work(sessions) as uow:
            artifact = uow.artifacts.find(project, ArtifactKind.SCENE_SET)
            assert artifact is not None
            version = uow.versions.latest(artifact.id)
            assert version is not None

            rows = uow.session.execute(
                sa.text(
                    'SELECT s."index", s.narration_text, s.target_duration_ms'
                    " FROM scene s JOIN scene_set ss ON ss.id = s.scene_set_id"
                    ' WHERE ss.artifact_version_id = :v ORDER BY s."index"'
                ),
                {"v": version.id},
            ).all()
            assert rows, "scene rows were not written"
            # 1-based and gapless: "regenerate scene 4" has to mean something.
            assert [r[0] for r in rows] == list(range(1, len(rows) + 1))
            assert all(r[1].strip() for r in rows)
            assert all(r[2] > 0 for r in rows)

    def test_the_scene_set_pins_the_script_version_it_came_from(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        project: str,
    ) -> None:
        """§10.3 rule 4. Without the pin, "why does scene 4 say this?" has no
        answer once the script moves on."""
        _advance(monkeypatch, sessions, project, ArtifactKind.SCENE_SET)

        with unit_of_work(sessions) as uow:
            script = uow.artifacts.find(project, ArtifactKind.SCRIPT)
            assert script is not None
            script_version = uow.versions.latest(script.id)
            assert script_version is not None

            pinned = uow.session.execute(
                sa.text("SELECT script_version_id FROM scene_set")
            ).scalar_one()
            assert pinned == script_version.id


class TestPromptFanOut:
    def test_one_job_produces_one_artifact_per_scene(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        project: str,
    ) -> None:
        """**M2-12**, and the ticket that proves finding S1 was worth having.

        Twenty artifacts of kind `prompt` are only unambiguous because
        ``UNIQUE (project_id, kind, scene_ref) NULLS NOT DISTINCT`` separates
        them by scene.
        """
        _advance(monkeypatch, sessions, project, ArtifactKind.PROMPT)

        with unit_of_work(sessions) as uow:
            scene_ids = [
                row[0]
                for row in uow.session.execute(
                    sa.text('SELECT id FROM scene ORDER BY "index"')
                ).all()
            ]
            per_scene = uow.session.execute(
                sa.text(
                    "SELECT scene_ref FROM artifact"
                    " WHERE project_id = :p AND kind = 'prompt'"
                    " AND scene_ref IS NOT NULL"
                ),
                {"p": project},
            ).scalars()
            assert sorted(per_scene) == sorted(scene_ids)

            # Plus exactly one project-wide row: the manifest the job's own
            # artifact carries. Without it that artifact would never leave
            # GENERATING, and phase derivation — which takes the least
            # advanced artifact of a kind — would strand the project.
            manifests = uow.session.execute(
                sa.text(
                    "SELECT count(*) FROM artifact"
                    " WHERE project_id = :p AND kind = 'prompt'"
                    " AND scene_ref IS NULL"
                ),
                {"p": project},
            ).scalar_one()
            assert manifests == 1

    def test_every_prompt_artifact_has_a_version(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        project: str,
    ) -> None:
        """A partially-prompted scene set is the failure batching exists to
        avoid — the unit a human reviews is the whole set."""
        _advance(monkeypatch, sessions, project, ArtifactKind.PROMPT)

        with unit_of_work(sessions) as uow:
            missing = uow.session.execute(
                sa.text(
                    "SELECT a.id FROM artifact a"
                    " LEFT JOIN artifact_version v ON v.artifact_id = a.id"
                    " WHERE a.project_id = :p AND a.kind = 'prompt'"
                    " AND v.id IS NULL"
                ),
                {"p": project},
            ).all()
            assert missing == []


class TestPhaseAdvancesThroughTheChain:
    def test_the_project_phase_follows_the_stages(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        project: str,
    ) -> None:
        """M2-03 against the real pipeline rather than a dict literal."""
        with unit_of_work(sessions) as uow:
            assert ProjectPhase(uow.projects.get(project).phase) is ProjectPhase.DRAFT  # type: ignore[union-attr]

        _run(monkeypatch, sessions, project, ArtifactKind.RESEARCH)
        with unit_of_work(sessions) as uow:
            phase = ProjectPhase(uow.projects.get(project).phase)  # type: ignore[union-attr]
            assert phase is ProjectPhase.RESEARCH_REVIEW

        _approve(sessions, project, ArtifactKind.RESEARCH)
        with unit_of_work(sessions) as uow:
            phase = ProjectPhase(uow.projects.get(project).phase)  # type: ignore[union-attr]
            assert phase is ProjectPhase.SCRIPTING


class TestGenerationFailure:
    """The path a real provider found and the mock never could.

    Until a live 400 came back, a failing job left its artifact in GENERATING
    with no version, no error on screen, and a "this page updates itself"
    message that was never going to come true. The mock cannot fail, so nothing
    had exercised this since M1.
    """

    def test_a_failed_job_moves_its_artifact_to_failed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        project: str,
    ) -> None:
        import videoforge_workers.db as worker_db
        from videoforge_workers.skeleton import run_job

        with unit_of_work(sessions) as uow:
            job_id = (
                JobService(uow, RecordingDispatcher())
                .request(
                    project_id=project,
                    kind=ArtifactKind.RESEARCH,
                    spec=RESEARCH_GENERATE,
                )
                .job.id
            )

        monkeypatch.setattr(worker_db, "get_session_factory", lambda: sessions)

        def explode(_ctx: object) -> None:
            raise RuntimeError("provider said no")

        # `run_job` records the failure and then re-raises, so Celery sees it
        # too. The recording is what this test is about; the propagation is the
        # skeleton's contract with the broker.
        with pytest.raises(RuntimeError, match="provider said no"):
            run_job(job_id, explode, task_name=RESEARCH_GENERATE.name)

        with unit_of_work(sessions) as uow:
            artifact = uow.artifacts.find(project, ArtifactKind.RESEARCH)
            assert artifact is not None
            # FAILED, not GENERATING. The reviewer can see it and can act on it
            # — the FSM accepts REGENERATE_REQUESTED from here, which is the
            # door the UI's Regenerate button uses.
            assert artifact.state is ArtifactState.FAILED

            transitions = uow.session.execute(
                sa.text(
                    "SELECT to_state FROM state_transition"
                    " WHERE subject_type = 'artifact' AND subject_id = :a"
                    " ORDER BY created_at"
                ),
                {"a": artifact.id},
            ).scalars()
            assert "FAILED" in list(transitions)

    def test_the_project_phase_does_not_claim_progress(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        project: str,
    ) -> None:
        """A failed root stage must not leave the project looking busy.

        `RESEARCHING` with nothing running is the state the operator stared at
        for minutes; the phase has to fall back to the review of the stage that
        needs their attention.
        """
        import videoforge_workers.db as worker_db
        from videoforge_workers.skeleton import run_job

        with unit_of_work(sessions) as uow:
            job_id = (
                JobService(uow, RecordingDispatcher())
                .request(
                    project_id=project,
                    kind=ArtifactKind.RESEARCH,
                    spec=RESEARCH_GENERATE,
                )
                .job.id
            )
        monkeypatch.setattr(worker_db, "get_session_factory", lambda: sessions)

        def explode(_ctx: object) -> None:
            raise RuntimeError("no")

        with pytest.raises(RuntimeError):
            run_job(job_id, explode, task_name=RESEARCH_GENERATE.name)

        with unit_of_work(sessions) as uow:
            row = uow.projects.get(project)
            assert row is not None
            assert ProjectPhase(row.phase) is ProjectPhase.RESEARCH_REVIEW
