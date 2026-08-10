"""M1-04: the double-delivery test.

**This is the most important test in the milestone.** Everything else in M1 is
recoverable by re-running something; this is the property that, if wrong,
silently produces two artifact versions for one user intent and there is no
way to tell after the fact which one the reviewer meant to approve.

The architecture makes duplicate delivery *certain*, not merely possible:

- ``task_acks_late=True`` — a worker killed mid-task has its message
  redelivered. This is the correct setting; losing work silently is worse.
- ``visibility_timeout=3600`` — Redis re-presents anything unacked past the
  window, including a task that is simply slow.
- Users double-click, and browsers retry.

So the question is never "can this happen" but "what happens when it does".
The answer must be: exactly one job row, exactly one artifact version, and the
second delivery observably declining to run.

The test drives ``run_job`` directly rather than going through Celery. The
property under test lives in the database — the claim's compare-and-set — and
standing up a broker to observe it would only add ways for the test to be
wrong about something else.

**Uses ``research`` rather than ``script`` since M3-06.** The property is
stage-agnostic — it is about the job row, not about what the job produces — and
research is a *root* stage requiring nothing approved upstream. Script gained an
upstream in M2-09, and once the admission gate (M3-06) started enforcing it,
requesting a script on a bare project became a 409. Fabricating an approved
research artifact here to keep the old stage name would add setup that has
nothing to do with double delivery.
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from videoforge.services.dispatch import RecordingDispatcher
from videoforge.services.jobs import JobService, idempotency_key
from videoforge_persistence.models import Workspace
from videoforge_persistence.repositories.jobs import JobRepository
from videoforge_persistence.uow import unit_of_work
from videoforge_shared.enums import (
    ArtifactKind,
    ArtifactState,
    JobStatus,
    VersionOrigin,
)
from videoforge_shared.ids import new_ulid
from videoforge_shared.tasks import RESEARCH_GENERATE

pytestmark = pytest.mark.integration


@pytest.fixture()
def sessions(db_engine: Engine) -> sessionmaker[Session]:
    """A real sessionmaker — these tests commit on purpose.

    Unlike the repository tests, this one cannot use the rollback-isolated
    session: the claim's visibility to a *second* transaction is the whole
    point, and inside a single uncommitted transaction there is no second
    transaction to be visible to.
    """
    return sessionmaker(bind=db_engine, autoflush=False, expire_on_commit=False)


@pytest.fixture()
def project(sessions: sessionmaker[Session]) -> Any:
    """A committed workspace + project, torn down afterwards."""
    workspace_id = new_ulid()
    with unit_of_work(sessions) as uow:
        uow.session.add(Workspace(id=workspace_id, name="double-delivery"))
        uow.flush()
        proj = uow.projects.create(workspace_id=workspace_id, topic="photosynthesis")
        uow.flush()
        project_id = proj.id

    yield project_id

    with unit_of_work(sessions) as uow:
        # Cascades take the project's artifacts, versions and jobs with it.
        uow.session.execute(
            sa.text("DELETE FROM workspace WHERE id = :id"), {"id": workspace_id}
        )
        # Outbox rows are NOT cascaded — the table deliberately has no FK to
        # anything, because an event must survive its subject. That is correct
        # for production and means this fixture has to clean up after itself,
        # or it leaves rows in the session-scoped container for every later
        # test to trip over.
        uow.session.execute(
            sa.text("DELETE FROM outbox_event WHERE payload->>'project_id' = :id"),
            {"id": project_id},
        )


def _run(
    monkeypatch: pytest.MonkeyPatch,
    sessions: sessionmaker[Session],
    job_id: str,
    body: Any,
) -> bool:
    """Invoke ``run_job`` against the test database.

    The worker's session factory is process-global and built from environment
    settings; pointing it at the testcontainer is the one piece of wiring the
    test needs.
    """
    import videoforge_workers.db as worker_db
    from videoforge_workers.skeleton import run_job

    monkeypatch.setattr(worker_db, "get_session_factory", lambda: sessions)
    return run_job(job_id, body, task_name=RESEARCH_GENERATE.name)


class TestRequestIsIdempotent:
    """The producing side: asking twice must not create two jobs."""

    def test_two_requests_create_one_job(
        self, sessions: sessionmaker[Session], project: str
    ) -> None:
        dispatcher = RecordingDispatcher()

        job_ids = []
        created_flags = []
        for _ in range(2):
            with unit_of_work(sessions) as uow:
                service = JobService(uow, dispatcher)
                outcome = service.request(
                    project_id=project,
                    kind=ArtifactKind.RESEARCH,
                    spec=RESEARCH_GENERATE,
                )
                job_ids.append(outcome.job.id)
                created_flags.append(outcome.created)
            service.dispatch_pending()

        assert created_flags == [True, False]
        assert job_ids[0] == job_ids[1]

        # And critically: only the first dispatched. A second broker message
        # for one intent is how one job becomes two running tasks.
        assert len(dispatcher.sent) == 1

    def test_one_artifact_and_one_transition(
        self, sessions: sessionmaker[Session], project: str
    ) -> None:
        """The duplicate must not write a second audit trail either.

        Two transitions for one state change would make the artifact's history
        read as though it were generated twice — the audit log's job is to be
        the account of record, and an account with phantom entries is worse
        than none.
        """
        dispatcher = RecordingDispatcher()
        for _ in range(2):
            with unit_of_work(sessions) as uow:
                JobService(uow, dispatcher).request(
                    project_id=project,
                    kind=ArtifactKind.RESEARCH,
                    spec=RESEARCH_GENERATE,
                )

        with unit_of_work(sessions) as uow:
            artifacts = uow.artifacts.for_project(project)
            assert len(artifacts) == 1
            assert artifacts[0].state is ArtifactState.GENERATING

            transitions = uow.session.execute(
                sa.text("SELECT count(*) FROM state_transition WHERE subject_id = :id"),
                {"id": artifacts[0].id},
            ).scalar_one()
            assert transitions == 1

    def test_idempotency_key_tracks_the_next_version(
        self, sessions: sessionmaker[Session], project: str
    ) -> None:
        """A legitimate regeneration must get a *different* key.

        The guard would be useless in the other direction: if every request
        for an artifact shared one key, the second generation a user asked for
        would be silently swallowed as a duplicate.
        """
        first = idempotency_key(RESEARCH_GENERATE.name, "01ARTIFACT", 1)
        second = idempotency_key(RESEARCH_GENERATE.name, "01ARTIFACT", 2)
        assert first != second


class TestDoubleDelivery:
    """The consuming side: the same message twice must run once."""

    def test_second_delivery_does_not_run(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        project: str,
    ) -> None:
        """**The test this milestone exists for.**"""
        with unit_of_work(sessions) as uow:
            outcome = JobService(uow, RecordingDispatcher()).request(
                project_id=project, kind=ArtifactKind.RESEARCH, spec=RESEARCH_GENERATE
            )
            job_id = outcome.job.id
            artifact_id = outcome.artifact.id

        runs = 0

        def body(ctx: Any) -> None:
            nonlocal runs
            runs += 1
            artifact = ctx.uow.artifacts.get(ctx.input["artifact_id"])
            ctx.uow.versions.add_version(
                artifact,
                origin=VersionOrigin.GENERATED,
                content_hash=f"hash-{runs}",
                inline_content={"text": "photosynthesis, explained"},
                generation_job_id=ctx.job.id,
            )

        first = _run(monkeypatch, sessions, job_id, body)
        second = _run(monkeypatch, sessions, job_id, body)

        assert first is True
        assert second is False, "the redelivered twin must decline to run"
        assert runs == 1, "the task body must have executed exactly once"

        with unit_of_work(sessions) as uow:
            versions = uow.versions.history(artifact_id)
            assert len(versions) == 1, "one intent must produce one version"
            assert versions[0].version_no == 1

            job = uow.jobs.get(job_id)
            assert job is not None
            assert job.status is JobStatus.SUCCEEDED
            # The losing claim must not have counted as an attempt — otherwise
            # a few redeliveries would silently exhaust the retry budget of a
            # job that only ever ran once.
            assert job.attempt == 1

    def test_failure_records_and_reraises(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        project: str,
    ) -> None:
        """A failing body must roll back its work but still record the failure.

        Both halves matter: a half-written artifact version would be worse
        than none, and a failure with no row leaves a job stuck in RUNNING
        forever with nothing to explain it.

        **Amended 2026-08-03.** This asserted the job landed back in QUEUED,
        because `may_retry` said another attempt was allowed. Nothing ever made
        that attempt — the reconciler (§14.4) is deferred and Celery autoretry
        is not wired — so the job sat in a live status forever, holding its
        idempotency key, which made the artifact's Regenerate button a silent
        no-op. A single 529 from a real provider left a project unrecoverable.

        The job now ends FAILED, which is both true and actionable. See
        `skeleton._REQUEUE_IS_HONOURED`.
        """
        with unit_of_work(sessions) as uow:
            outcome = JobService(uow, RecordingDispatcher()).request(
                project_id=project, kind=ArtifactKind.RESEARCH, spec=RESEARCH_GENERATE
            )
            job_id = outcome.job.id
            artifact_id = outcome.artifact.id

        def failing_body(ctx: Any) -> None:
            artifact = ctx.uow.artifacts.get(ctx.input["artifact_id"])
            ctx.uow.versions.add_version(
                artifact,
                origin=VersionOrigin.GENERATED,
                content_hash="doomed",
                inline_content={"text": "half a script"},
            )
            raise RuntimeError("provider exploded")

        with pytest.raises(RuntimeError, match="provider exploded"):
            _run(monkeypatch, sessions, job_id, failing_body)

        with unit_of_work(sessions) as uow:
            assert (
                uow.versions.history(artifact_id) == []
            ), "the body's writes must have rolled back"
            job = uow.jobs.get(job_id)
            assert job is not None
            # max_attempts defaults to 3 and this was attempt 1, so it requeues.
            assert job.status is JobStatus.FAILED
            assert job.error is not None
            assert job.error["type"] == "RuntimeError"
            assert "provider exploded" in job.error["message"]

    def test_missing_job_is_dropped_not_retried(
        self, monkeypatch: pytest.MonkeyPatch, sessions: sessionmaker[Session]
    ) -> None:
        """A message for a job that no longer exists must not raise.

        Raising would cycle it through the retry budget and log three
        tracebacks for something that is simply gone — a project deleted while
        its work was in flight.
        """
        assert _run(monkeypatch, sessions, new_ulid(), lambda ctx: None) is False


def _spine(session: Session) -> tuple[str, str]:
    """Workspace → project → artifact, the minimum a job can reference."""
    workspace_id, project_id, artifact_id = new_ulid(), new_ulid(), new_ulid()
    session.execute(
        sa.text("INSERT INTO workspace (id, name) VALUES (:id, 'keys')"),
        {"id": workspace_id},
    )
    session.execute(
        sa.text(
            "INSERT INTO video_project (id, workspace_id, topic, phase)"
            " VALUES (:id, :ws, 'keys', 'DRAFT')"
        ),
        {"id": project_id, "ws": workspace_id},
    )
    session.execute(
        sa.text(
            "INSERT INTO artifact (id, project_id, kind, state)"
            " VALUES (:id, :p, 'script', 'GENERATING')"
        ),
        {"id": artifact_id, "p": project_id},
    )
    session.flush()
    return project_id, artifact_id


class TestDeadJobsReleaseTheirKey:
    """The 529 chain, as a test.

    A job that ended badly held its idempotency key forever. Because the key is
    derived from the version it would have produced — and a failure produces no
    version — the same request could never be made again: `reserve` kept
    returning the dead job, and the artifact's Regenerate button was a silent
    no-op. One overloaded-provider response made a project unrecoverable.
    """

    def test_a_failed_job_frees_the_key(self, db_session: Session) -> None:
        repo = JobRepository(db_session)
        project_id, artifact_id = _spine(db_session)
        key = "script.generate:one:v1"

        first = repo.reserve(
            project_id=project_id,
            task_name="script.generate",
            queue="llm",
            idempotency_key=key,
            artifact_id=artifact_id,
        )
        assert first.created is True

        db_session.execute(
            sa.text("UPDATE generation_job SET status = 'FAILED' WHERE id = :id"),
            {"id": first.job.id},
        )

        second = repo.reserve(
            project_id=project_id,
            task_name="script.generate",
            queue="llm",
            idempotency_key=key,
            artifact_id=artifact_id,
        )
        assert second.created is True, "a dead job must not block a retry"
        assert second.job.id != first.job.id

    def test_a_succeeded_job_still_holds_it(self, db_session: Session) -> None:
        """The half that must NOT change.

        This is the double-delivery guarantee (§14.3): a redelivered task whose
        twin already produced a version has to find the key taken, or the same
        job runs twice and the artifact gains a duplicate version.
        """
        repo = JobRepository(db_session)
        project_id, artifact_id = _spine(db_session)
        key = "script.generate:two:v1"

        first = repo.reserve(
            project_id=project_id,
            task_name="script.generate",
            queue="llm",
            idempotency_key=key,
            artifact_id=artifact_id,
        )
        db_session.execute(
            sa.text("UPDATE generation_job SET status = 'SUCCEEDED' WHERE id = :id"),
            {"id": first.job.id},
        )

        second = repo.reserve(
            project_id=project_id,
            task_name="script.generate",
            queue="llm",
            idempotency_key=key,
            artifact_id=artifact_id,
        )
        assert second.created is False
        assert second.job.id == first.job.id

    def test_a_queued_job_still_holds_it(self, db_session: Session) -> None:
        """In flight, not dead. A double-click must still collapse to one job."""
        repo = JobRepository(db_session)
        project_id, artifact_id = _spine(db_session)
        key = "script.generate:three:v1"

        first = repo.reserve(
            project_id=project_id,
            task_name="script.generate",
            queue="llm",
            idempotency_key=key,
            artifact_id=artifact_id,
        )
        second = repo.reserve(
            project_id=project_id,
            task_name="script.generate",
            queue="llm",
            idempotency_key=key,
            artifact_id=artifact_id,
        )
        assert second.created is False
        assert second.job.id == first.job.id
