"""M1-05 + M1-07: the outbox drain, and the first real stage end to end.

Topic in → job → worker → artifact version awaiting approval, on the mock
provider, with the audit trail explaining every step. This is the vertical
slice M1 exists to prove; everything after it is the same shape with a
different provider call in the middle.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from videoforge.services.dispatch import RecordingDispatcher
from videoforge.services.jobs import JobService
from videoforge.services.review import ReviewService
from videoforge_domain.approval_policy import ApprovalPolicy
from videoforge_persistence.models import Workspace
from videoforge_persistence.uow import unit_of_work
from videoforge_prompts import template_ref
from videoforge_shared.enums import (
    ArtifactKind,
    ArtifactState,
    JobStatus,
    VersionStatus,
)
from videoforge_shared.ids import new_ulid
from videoforge_shared.tasks import RESEARCH_GENERATE, SCRIPT_GENERATE

pytestmark = pytest.mark.integration


@pytest.fixture()
def sessions(db_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def world(sessions: sessionmaker[Session]) -> Any:
    """Workspace → series → project, committed, with cleanup."""
    workspace_id = new_ulid()
    with unit_of_work(sessions) as uow:
        uow.session.add(Workspace(id=workspace_id, name="script-stage"))
        uow.flush()
        series = uow.series.create(workspace_id=workspace_id, title="Explainers")
        uow.flush()
        project = uow.projects.create(
            workspace_id=workspace_id,
            series_id=series.id,
            topic="how photosynthesis works",
        )
        uow.flush()
        ids = {
            "workspace_id": workspace_id,
            "series_id": series.id,
            "project_id": project.id,
        }

    yield ids

    with unit_of_work(sessions) as uow:
        uow.session.execute(
            sa.text("DELETE FROM workspace WHERE id = :id"), {"id": workspace_id}
        )
        uow.session.execute(
            sa.text("DELETE FROM outbox_event WHERE payload->>'project_id' = :id"),
            {"id": ids["project_id"]},
        )


def _run_script_job(
    monkeypatch: pytest.MonkeyPatch,
    sessions: sessionmaker[Session],
    job_id: str,
) -> bool:
    import videoforge_workers.db as worker_db
    from videoforge_workers.script import script_body
    from videoforge_workers.skeleton import run_job

    monkeypatch.setattr(worker_db, "get_session_factory", lambda: sessions)
    return run_job(job_id, script_body, task_name=SCRIPT_GENERATE.name)


def _request_script(sessions: sessionmaker[Session], project_id: str) -> str:
    with unit_of_work(sessions) as uow:
        outcome = JobService(uow, RecordingDispatcher()).request(
            project_id=project_id, kind=ArtifactKind.SCRIPT, spec=SCRIPT_GENERATE
        )
        return outcome.job.id


def _run_research_job(
    monkeypatch: pytest.MonkeyPatch,
    sessions: sessionmaker[Session],
    job_id: str,
) -> bool:
    import videoforge_workers.db as worker_db
    from videoforge_workers.research import research_body
    from videoforge_workers.skeleton import run_job

    monkeypatch.setattr(worker_db, "get_session_factory", lambda: sessions)
    return run_job(job_id, research_body, task_name=RESEARCH_GENERATE.name)


def _approved_research(
    monkeypatch: pytest.MonkeyPatch,
    sessions: sessionmaker[Session],
    project_id: str,
) -> None:
    """**[M2-09]** Script now has an upstream.

    Run through the real research stage and a real approval rather than
    hand-inserting a version: seeding the row directly would test the script
    stage against a shape nothing else produces, and the first time the
    research schema changed these tests would keep passing against a fiction.
    """
    with unit_of_work(sessions) as uow:
        outcome = JobService(uow, RecordingDispatcher()).request(
            project_id=project_id, kind=ArtifactKind.RESEARCH, spec=RESEARCH_GENERATE
        )
        research_job_id = outcome.job.id
    assert _run_research_job(monkeypatch, sessions, research_job_id) is True

    with unit_of_work(sessions) as uow:
        artifact = uow.artifacts.find(project_id, ArtifactKind.RESEARCH)
        assert artifact is not None
        version = uow.versions.latest(artifact.id)
        assert version is not None
        ReviewService(uow).approve(version.id)


class TestScriptStage:
    def test_topic_to_awaiting_approval(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        world: dict[str, str],
    ) -> None:
        """The slice: a topic becomes a reviewable script version."""
        _approved_research(monkeypatch, sessions, world["project_id"])
        job_id = _request_script(sessions, world["project_id"])
        assert _run_script_job(monkeypatch, sessions, job_id) is True

        with unit_of_work(sessions) as uow:
            artifact = uow.artifacts.find(world["project_id"], ArtifactKind.SCRIPT)
            assert artifact is not None
            assert artifact.state is ArtifactState.AWAITING_APPROVAL

            versions = uow.versions.history(artifact.id)
            assert len(versions) == 1
            version = versions[0]
            assert version.version_no == 1
            assert version.inline_content is not None
            assert version.inline_content["script"]
            # Storage is mutually exclusive with inline content, and a script
            # is small enough to belong in the row.
            assert version.storage_key is None

            status = uow.versions.status_of(version.id)
            assert status is not None
            assert status.status is VersionStatus.AWAITING_APPROVAL

            job = uow.jobs.get(job_id)
            assert job is not None
            assert job.status is JobStatus.SUCCEEDED

    def test_version_records_its_provenance(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        world: dict[str, str],
    ) -> None:
        """SADD §10.3 rule 4 — reproducibility by construction.

        A version that cannot say which prompt and which model produced it
        makes "why does this video look like this?" unanswerable, and no later
        milestone can retrofit the answer.
        """
        _approved_research(monkeypatch, sessions, world["project_id"])
        job_id = _request_script(sessions, world["project_id"])
        _run_script_job(monkeypatch, sessions, job_id)

        with unit_of_work(sessions) as uow:
            artifact = uow.artifacts.find(world["project_id"], ArtifactKind.SCRIPT)
            assert artifact is not None
            version = uow.versions.latest(artifact.id)
            assert version is not None
            # **[M2-05]** Compared against the template's own ref rather than a
            # literal. The old value was the bare string "script/v1", which
            # stayed identical however the prompt was edited — so this
            # assertion passed while the column recorded a name rather than a
            # prompt. The shape check below is what makes the difference
            # visible: a ref now carries a content digest.
            assert version.prompt_template_ref == template_ref("script")
            assert version.prompt_template_ref is not None
            assert version.prompt_template_ref.startswith("script@")
            assert "+" in version.prompt_template_ref
            assert version.provider_ref == "mock"
            assert version.meta["model"]
            assert version.generation_job_id == job_id

    def test_content_hash_is_canonical(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        world: dict[str, str],
    ) -> None:
        """The hash must depend on content, not on dict ordering.

        It is what the timeline's ``input_snapshot`` pins, so a hash that
        changed with key order would break reproducibility silently.
        """
        from videoforge_shared.hashing import sha256_bytes

        _approved_research(monkeypatch, sessions, world["project_id"])
        job_id = _request_script(sessions, world["project_id"])
        _run_script_job(monkeypatch, sessions, job_id)

        with unit_of_work(sessions) as uow:
            artifact = uow.artifacts.find(world["project_id"], ArtifactKind.SCRIPT)
            assert artifact is not None
            version = uow.versions.latest(artifact.id)
            assert version is not None
            expected = sha256_bytes(
                json.dumps(
                    version.inline_content, sort_keys=True, separators=(",", ":")
                ).encode()
            )
            assert version.content_hash == expected

    def test_usage_is_recorded_against_the_job(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        world: dict[str, str],
    ) -> None:
        """The input to the S10 spend cap. Zero rows means no cap can work."""
        _approved_research(monkeypatch, sessions, world["project_id"])
        job_id = _request_script(sessions, world["project_id"])
        _run_script_job(monkeypatch, sessions, job_id)

        with unit_of_work(sessions) as uow:
            rows = uow.session.execute(
                sa.text(
                    "SELECT provider, operation, input_tokens FROM provider_usage"
                    " WHERE job_id = :id"
                ),
                {"id": job_id},
            ).all()
        assert len(rows) == 1
        assert rows[0].provider == "mock"
        assert rows[0].operation == "llm.complete"
        assert rows[0].input_tokens > 0

    def test_audit_trail_explains_every_step(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        world: dict[str, str],
    ) -> None:
        """M1's exit criterion in miniature.

        Two transitions: PENDING→GENERATING when the job was requested, and
        GENERATING→AWAITING_APPROVAL when it finished. If either is missing,
        the trail has a gap exactly where someone would look.
        """
        _approved_research(monkeypatch, sessions, world["project_id"])
        job_id = _request_script(sessions, world["project_id"])
        _run_script_job(monkeypatch, sessions, job_id)

        with unit_of_work(sessions) as uow:
            artifact = uow.artifacts.find(world["project_id"], ArtifactKind.SCRIPT)
            assert artifact is not None
            from videoforge_shared.enums import SubjectType

            history = uow.audit.history_for(SubjectType.ARTIFACT, artifact.id)
            assert [(t.from_state, t.to_state) for t in history] == [
                ("PENDING", "GENERATING"),
                ("GENERATING", "AWAITING_APPROVAL"),
            ]

    def test_regeneration_produces_a_second_version(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        world: dict[str, str],
    ) -> None:
        """Reject → regenerate, the loop the review UI is built around."""
        from videoforge_shared.enums import ReviewDecisionKind

        _approved_research(monkeypatch, sessions, world["project_id"])
        first_job = _request_script(sessions, world["project_id"])
        _run_script_job(monkeypatch, sessions, first_job)

        with unit_of_work(sessions) as uow:
            artifact = uow.artifacts.find(world["project_id"], ArtifactKind.SCRIPT)
            assert artifact is not None
            v1 = uow.versions.latest(artifact.id)
            assert v1 is not None
            uow.reviews.record(
                artifact_version_id=v1.id, decision=ReviewDecisionKind.REJECT
            )
            artifact.state = ArtifactState.REJECTED

        with unit_of_work(sessions) as uow:
            outcome = JobService(uow, RecordingDispatcher()).request(
                project_id=world["project_id"],
                kind=ArtifactKind.SCRIPT,
                spec=SCRIPT_GENERATE,
                regenerate=True,
            )
            assert outcome.created is True
            second_job = outcome.job.id

        _run_script_job(monkeypatch, sessions, second_job)

        with unit_of_work(sessions) as uow:
            artifact = uow.artifacts.find(world["project_id"], ArtifactKind.SCRIPT)
            assert artifact is not None
            versions = uow.versions.history(artifact.id)
            assert [v.version_no for v in versions] == [2, 1]
            # Lineage: v2 points at the version it replaced.
            assert versions[0].parent_version_id == versions[1].id

            statuses = {
                s.version_no: s.status
                for s in uow.versions.statuses_for_artifact(artifact.id)
            }
            assert statuses[1] is VersionStatus.REJECTED
            assert statuses[2] is VersionStatus.AWAITING_APPROVAL


class TestAutoApproval:
    def test_policy_can_skip_the_human_gate(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        world: dict[str, str],
    ) -> None:
        """SADD §11, opt-in per series.

        The automatic approval writes a real ``review_decision`` with a NULL
        reviewer rather than special-casing the status view — one definition of
        APPROVED, and an audit trail that shows no human was involved.
        """
        policy = ApprovalPolicy.all_manual().with_automatic(ArtifactKind.SCRIPT)
        with unit_of_work(sessions) as uow:
            series = uow.series.get(world["series_id"])
            assert series is not None
            series.auto_approve_policy = policy.to_jsonb()

        _approved_research(monkeypatch, sessions, world["project_id"])
        job_id = _request_script(sessions, world["project_id"])
        _run_script_job(monkeypatch, sessions, job_id)

        with unit_of_work(sessions) as uow:
            artifact = uow.artifacts.find(world["project_id"], ArtifactKind.SCRIPT)
            assert artifact is not None
            assert artifact.state is ArtifactState.APPROVED

            version = uow.versions.latest(artifact.id)
            assert version is not None
            approved = uow.versions.approved_version(artifact.id)
            assert approved is not None
            assert approved.artifact_version_id == version.id

            decision = uow.reviews.latest_for_version(version.id)
            assert decision is not None
            assert (
                decision.reviewer_id is None
            ), "an automatic approval must not be attributed to a person"

            project = uow.projects.get(world["project_id"])
            assert project is not None
            assert project.active_pointers["script"] == version.id

    def test_default_policy_still_requires_a_human(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        world: dict[str, str],
    ) -> None:
        """The default must be all-manual — gates are opt-out, never opt-in."""
        _approved_research(monkeypatch, sessions, world["project_id"])
        job_id = _request_script(sessions, world["project_id"])
        _run_script_job(monkeypatch, sessions, job_id)

        with unit_of_work(sessions) as uow:
            artifact = uow.artifacts.find(world["project_id"], ArtifactKind.SCRIPT)
            assert artifact is not None
            assert artifact.state is ArtifactState.AWAITING_APPROVAL
            assert uow.versions.approved_version(artifact.id) is None


class TestOutboxDrain:
    """M1-05. Finding S7: it publishes, and nothing subscribes — by design."""

    def test_drain_publishes_and_stamps(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        world: dict[str, str],
    ) -> None:
        import videoforge_workers.db as worker_db
        from videoforge_workers.outbox import EVENTS_CHANNEL, drain_once

        monkeypatch.setattr(worker_db, "get_session_factory", lambda: sessions)

        _approved_research(monkeypatch, sessions, world["project_id"])
        job_id = _request_script(sessions, world["project_id"])
        _run_script_job(monkeypatch, sessions, job_id)

        published: list[tuple[str, str]] = []

        class FakePipeline:
            def publish(self, channel: str, message: str) -> None:
                published.append((channel, message))

            def execute(self) -> None:
                pass

        class FakeRedis:
            def pipeline(self, transaction: bool = True) -> FakePipeline:
                return FakePipeline()

        count = drain_once(client=FakeRedis())  # type: ignore[arg-type]
        assert count >= 2  # job.requested + artifact.version_created
        assert all(channel == EVENTS_CHANNEL for channel, _ in published)

        types = {json.loads(m)["event_type"] for _, m in published}
        assert {"job.requested", "artifact.version_created"} <= types

        with unit_of_work(sessions) as uow:
            assert uow.outbox.backlog() == 0

    def test_drain_is_a_noop_when_empty(
        self, monkeypatch: pytest.MonkeyPatch, sessions: sessionmaker[Session]
    ) -> None:
        """Called every second by beat, so the empty case is the common case
        and must not publish, stamp, or log anything interesting."""
        import videoforge_workers.db as worker_db
        from videoforge_workers.outbox import drain_once

        monkeypatch.setattr(worker_db, "get_session_factory", lambda: sessions)
        with unit_of_work(sessions) as uow:
            uow.session.execute(sa.text("DELETE FROM outbox_event"))

        assert drain_once(client=None) == 0
