"""M1-03: repositories, against a real PostgreSQL.

Most of these could be faked. Three cannot, and they are the reason this file
is an integration test rather than a unit test:

- ``reserve`` relies on ``INSERT ... ON CONFLICT`` against a real unique index.
- ``claim`` relies on an UPDATE's ``rowcount`` under a real predicate.
- ``claim_orphans`` and ``claim_unpublished`` rely on ``FOR UPDATE SKIP LOCKED``.

A mock would return whatever it was told for all three, which is precisely the
class of bug — a guard that does not guard — that this milestone exists to
rule out.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine
from sqlalchemy.orm import Session
from videoforge_domain.job_lifecycle import may_retry

from videoforge_persistence.models import Workspace
from videoforge_persistence.repositories import (
    ArtifactRepository,
    ArtifactVersionRepository,
    AuditRepository,
    CommentRepository,
    JobRepository,
    OutboxRepository,
    ProjectRepository,
    ReviewRepository,
    SeriesRepository,
    WorkspaceRepository,
)
from videoforge_shared.enums import (
    ArtifactKind,
    ArtifactState,
    JobStatus,
    ReviewDecisionKind,
    SubjectType,
    TransitionCause,
    VersionOrigin,
    VersionStatus,
)
from videoforge_shared.ids import new_ulid

pytestmark = pytest.mark.integration


def _job_status(session: Session, job_id: str) -> JobStatus:
    """Read the status straight from the row.

    Used instead of ``refresh(job)`` plus an attribute check where a test
    asserts the same job's status more than once: the ORM attribute's type is
    narrowed by the first assertion, and the second then looks to a type
    checker like a comparison that can never hold. Re-reading is also the
    stronger assertion — it checks what is *in the database*, not what the
    identity map believes.
    """
    return JobStatus(
        session.execute(
            sa.text("SELECT status FROM generation_job WHERE id = :id"),
            {"id": job_id},
        ).scalar_one()
    )


@pytest.fixture()
def spine(db_session: Session) -> dict[str, str]:
    """Workspace → series → project → script artifact."""
    workspace = WorkspaceRepository(db_session)
    series_repo = SeriesRepository(db_session)
    projects = ProjectRepository(db_session)
    artifacts = ArtifactRepository(db_session)

    ws = Workspace(id=new_ulid(), name="repo-test")
    db_session.add(ws)
    db_session.flush()
    # `workspace` is exercised here rather than only constructed, so the
    # single-workspace lookup every call site depends on is covered.
    assert workspace.sole() is not None

    series = series_repo.create(workspace_id=ws.id, title="Explainers")
    db_session.flush()
    project = projects.create(
        workspace_id=ws.id, series_id=series.id, topic="photosynthesis"
    )
    db_session.flush()
    artifact = artifacts.create(project.id, ArtifactKind.SCRIPT)
    db_session.flush()
    return {
        "workspace_id": ws.id,
        "series_id": series.id,
        "project_id": project.id,
        "artifact_id": artifact.id,
    }


class TestArtifactVersions:
    def test_versions_are_numbered_and_chained(
        self, db_session: Session, spine: dict[str, str]
    ) -> None:
        """Lineage (§10.3 rule 2) forms without the caller doing anything."""
        artifacts = ArtifactRepository(db_session)
        versions = ArtifactVersionRepository(db_session)
        artifact = artifacts.get(spine["artifact_id"])
        assert artifact is not None

        first = versions.add_version(
            artifact,
            origin=VersionOrigin.GENERATED,
            content_hash="h1",
            inline_content={"text": "draft one"},
        )
        second = versions.add_version(
            artifact,
            origin=VersionOrigin.GENERATED,
            content_hash="h2",
            inline_content={"text": "draft two"},
        )

        assert (first.version_no, second.version_no) == (1, 2)
        assert first.parent_version_id is None
        assert second.parent_version_id == first.id

        # No refresh(): `add_version` expires the counter, so simply reading
        # the attribute must return the post-increment value. Without that
        # expiry this assertion sees a stale 0 while the database holds 2.
        assert artifact.current_version_no == 2

    def test_latest_for_finds_by_project_and_kind(
        self, db_session: Session, spine: dict[str, str]
    ) -> None:
        artifacts = ArtifactRepository(db_session)
        versions = ArtifactVersionRepository(db_session)
        artifact = artifacts.get(spine["artifact_id"])
        assert artifact is not None
        versions.add_version(
            artifact,
            origin=VersionOrigin.GENERATED,
            content_hash="h1",
            inline_content={"n": 1},
        )
        latest = versions.add_version(
            artifact,
            origin=VersionOrigin.GENERATED,
            content_hash="h2",
            inline_content={"n": 2},
        )

        found = versions.latest_for(spine["project_id"], ArtifactKind.SCRIPT)
        assert found is not None
        assert found.id == latest.id

    def test_latest_for_matches_null_scene_ref(
        self, db_session: Session, spine: dict[str, str]
    ) -> None:
        """The ``IS NULL`` branch, which an ``== None`` would silently break.

        Project-wide artifacts all have ``scene_ref IS NULL``, and SQL
        equality against NULL matches nothing — so this lookup would return
        None for every script, timeline and render in the system while looking
        entirely reasonable.
        """
        artifacts = ArtifactRepository(db_session)
        versions = ArtifactVersionRepository(db_session)
        artifact = artifacts.get(spine["artifact_id"])
        assert artifact is not None
        versions.add_version(
            artifact,
            origin=VersionOrigin.GENERATED,
            content_hash="h",
            inline_content={},
        )
        assert (
            versions.latest_for(
                spine["project_id"], ArtifactKind.SCRIPT, scene_ref=None
            )
            is not None
        )

    def test_status_view_is_readable_through_the_repository(
        self, db_session: Session, spine: dict[str, str]
    ) -> None:
        artifacts = ArtifactRepository(db_session)
        versions = ArtifactVersionRepository(db_session)
        reviews = ReviewRepository(db_session)
        artifact = artifacts.get(spine["artifact_id"])
        assert artifact is not None

        v1 = versions.add_version(
            artifact,
            origin=VersionOrigin.GENERATED,
            content_hash="h1",
            inline_content={"n": 1},
        )
        v2 = versions.add_version(
            artifact,
            origin=VersionOrigin.GENERATED,
            content_hash="h2",
            inline_content={"n": 2},
        )
        reviews.record(artifact_version_id=v2.id, decision=ReviewDecisionKind.APPROVE)
        db_session.flush()

        approved = versions.approved_version(artifact.id)
        assert approved is not None
        assert approved.artifact_version_id == v2.id
        assert approved.status is VersionStatus.APPROVED

        status_v1 = versions.status_of(v1.id)
        assert status_v1 is not None
        assert status_v1.status is VersionStatus.SUPERSEDED


class TestJobReservation:
    """SADD §14.3 — the guarantees at-least-once delivery depends on."""

    def test_reserve_is_idempotent(
        self, db_session: Session, spine: dict[str, str]
    ) -> None:
        """The same key twice yields one job, and says so.

        ``created`` is what the service branches on to decide whether to
        enqueue a Celery task. If this returned ``True`` both times the task
        would be dispatched twice, which is the whole failure being designed
        against.
        """
        jobs = JobRepository(db_session)
        first = jobs.reserve(
            project_id=spine["project_id"],
            task_name="script.generate",
            queue="llm",
            idempotency_key="script:v1",
        )
        second = jobs.reserve(
            project_id=spine["project_id"],
            task_name="script.generate",
            queue="llm",
            idempotency_key="script:v1",
        )

        assert first.created is True
        assert second.created is False
        assert first.job.id == second.job.id

        count = db_session.execute(
            sa.text(
                "SELECT count(*) FROM generation_job"
                " WHERE idempotency_key = 'script:v1'"
            )
        ).scalar_one()
        assert count == 1

    def test_claim_succeeds_once(
        self, db_session: Session, spine: dict[str, str]
    ) -> None:
        """The compare-and-set. Two claims, one winner.

        This is the double-delivery guard in miniature: the second caller must
        be told it lost so it drops the message instead of running the task.
        """
        jobs = JobRepository(db_session)
        reserved = jobs.reserve(
            project_id=spine["project_id"],
            task_name="script.generate",
            queue="llm",
            idempotency_key="script:claim",
        )
        assert jobs.claim(reserved.job.id, celery_task_id="celery-1") is True
        assert jobs.claim(reserved.job.id, celery_task_id="celery-2") is False

        db_session.refresh(reserved.job)
        assert reserved.job.status is JobStatus.RUNNING
        # Exactly one attempt was counted — a losing claim must not increment.
        assert reserved.job.attempt == 1
        assert reserved.job.celery_task_id == "celery-1"

    def test_mark_succeeded_only_from_running(
        self, db_session: Session, spine: dict[str, str]
    ) -> None:
        jobs = JobRepository(db_session)
        reserved = jobs.reserve(
            project_id=spine["project_id"],
            task_name="script.generate",
            queue="llm",
            idempotency_key="script:done",
        )
        # Not RUNNING yet.
        assert jobs.mark_succeeded(reserved.job.id) is False
        assert jobs.claim(reserved.job.id) is True
        assert jobs.mark_succeeded(reserved.job.id) is True
        # And not twice.
        assert jobs.mark_succeeded(reserved.job.id) is False

    def test_failure_requeues_within_budget(
        self, db_session: Session, spine: dict[str, str]
    ) -> None:
        """The repository writes; ``videoforge_domain`` decides.

        Using ``may_retry`` here rather than reimplementing the comparison is
        the point — the retry policy stays unit-testable without a database,
        and there is one definition of the budget.
        """
        jobs = JobRepository(db_session)
        reserved = jobs.reserve(
            project_id=spine["project_id"],
            task_name="script.generate",
            queue="llm",
            idempotency_key="script:retry",
            max_attempts=2,
        )
        job = reserved.job
        jobs.claim(job.id)
        db_session.refresh(job)

        requeue = may_retry(JobStatus.FAILED, job.attempt, job.max_attempts)
        assert requeue is True
        assert jobs.mark_failed(job.id, {"msg": "boom"}, requeue=True) is True
        assert _job_status(db_session, job.id) is JobStatus.QUEUED

        jobs.claim(job.id)
        db_session.refresh(job)
        assert job.attempt == 2
        assert may_retry(JobStatus.FAILED, job.attempt, job.max_attempts) is False
        assert jobs.mark_failed(job.id, {"msg": "boom"}, requeue=False) is True
        assert _job_status(db_session, job.id) is JobStatus.FAILED

    def test_claim_orphans_finds_only_stale_running_jobs(
        self, db_session: Session, spine: dict[str, str]
    ) -> None:
        """§14.4: the reconciler's query.

        A fresh RUNNING job must be left alone — reaping one that is merely
        slow would kill work in progress, which is worse than the orphan.
        """
        jobs = JobRepository(db_session)
        stale = jobs.reserve(
            project_id=spine["project_id"],
            task_name="t",
            queue="llm",
            idempotency_key="orphan:stale",
        ).job
        fresh = jobs.reserve(
            project_id=spine["project_id"],
            task_name="t",
            queue="llm",
            idempotency_key="orphan:fresh",
        ).job
        jobs.claim(stale.id)
        jobs.claim(fresh.id)
        # Backdate the stale one past any plausible timeout.
        db_session.execute(
            sa.text(
                "UPDATE generation_job SET started_at = now() - interval '2 hours'"
                " WHERE id = :id"
            ),
            {"id": stale.id},
        )

        claimed = jobs.claim_orphans(timedelta(hours=1))
        assert [job.id for job in claimed] == [stale.id]

        db_session.refresh(stale)
        db_session.refresh(fresh)
        assert stale.status is JobStatus.ORPHANED
        assert fresh.status is JobStatus.RUNNING

    def test_orphan_can_be_requeued(
        self, db_session: Session, spine: dict[str, str]
    ) -> None:
        jobs = JobRepository(db_session)
        job = jobs.reserve(
            project_id=spine["project_id"],
            task_name="t",
            queue="llm",
            idempotency_key="orphan:requeue",
        ).job
        jobs.claim(job.id)
        db_session.execute(
            sa.text(
                "UPDATE generation_job SET started_at = now() - interval '2 hours'"
                " WHERE id = :id"
            ),
            {"id": job.id},
        )
        jobs.claim_orphans(timedelta(hours=1))
        assert jobs.requeue(job.id) is True
        db_session.refresh(job)
        assert job.status is JobStatus.QUEUED
        assert job.error is None


class TestOutbox:
    def test_enqueue_claim_and_publish(self, db_session: Session) -> None:
        """Measured as a *delta*, not an absolute.

        ``backlog()`` is a global health metric — deliberately unscoped, since
        the drain's question is "is anything undelivered anywhere". Asserting
        it equals 2 quietly assumed an empty database, which held only while
        no other test committed. ``test_double_delivery`` then began
        committing real ``job.requested`` events (outbox rows have no FK to
        workspace by design, so they survive its teardown) and this failed
        with 6 — a test-ordering dependency, not a defect in the repository.
        """
        outbox = OutboxRepository(db_session)
        before = outbox.backlog()

        outbox.enqueue(event_type="artifact.created", payload={"n": 1})
        outbox.enqueue(event_type="artifact.created", payload={"n": 2})
        db_session.flush()
        assert outbox.backlog() == before + 2

        claimed = outbox.claim_unpublished()
        mine = [e for e in claimed if e.event_type == "artifact.created"]
        assert len(mine) == 2

        assert outbox.mark_published([e.id for e in mine]) == 2
        assert outbox.backlog() == before

    def test_mark_published_is_idempotent(self, db_session: Session) -> None:
        """A drain that crashes after publishing re-publishes on restart.

        That is at-least-once, by design. What must not happen is the
        timestamp being rewritten on the second pass — ``published_at`` is
        evidence of when delivery first occurred.
        """
        outbox = OutboxRepository(db_session)
        event = outbox.enqueue(event_type="x", payload={})
        db_session.flush()

        assert outbox.mark_published([event.id]) == 1
        first_stamp = db_session.execute(
            sa.text("SELECT published_at FROM outbox_event WHERE id = :id"),
            {"id": event.id},
        ).scalar_one()

        assert outbox.mark_published([event.id]) == 0
        second_stamp = db_session.execute(
            sa.text("SELECT published_at FROM outbox_event WHERE id = :id"),
            {"id": event.id},
        ).scalar_one()
        assert first_stamp == second_stamp

    def test_empty_list_is_a_noop(self, db_session: Session) -> None:
        assert OutboxRepository(db_session).mark_published([]) == 0


class TestAudit:
    def test_transition_and_event_share_a_correlation_id(
        self, db_session: Session, spine: dict[str, str]
    ) -> None:
        """The thread from nginx to the audit table (M0-06/08).

        An operator with one request id must be able to pull the whole story;
        that only works if the ambient id lands on the rows by default rather
        than by every call site remembering to pass it.
        """
        from videoforge_shared.correlation import correlation_context

        audit = AuditRepository(db_session)
        with correlation_context("req-abc123"):
            audit.record_transition(
                subject_type=SubjectType.ARTIFACT,
                subject_id=spine["artifact_id"],
                from_state=ArtifactState.PENDING.value,
                to_state=ArtifactState.GENERATING.value,
                cause=TransitionCause.SYSTEM,
            )
            audit.record_event(
                event_type="artifact.generation_started",
                subject_type=SubjectType.ARTIFACT,
                subject_id=spine["artifact_id"],
            )
        db_session.flush()

        history = audit.history_for(SubjectType.ARTIFACT, spine["artifact_id"])
        assert len(history) == 1
        assert history[0].correlation_id == "req-abc123"

        event_correlation = db_session.execute(
            sa.text("SELECT correlation_id FROM audit_event WHERE subject_id = :id"),
            {"id": spine["artifact_id"]},
        ).scalar_one()
        assert event_correlation == "req-abc123"


class TestStaleness:
    def test_mark_stale_is_idempotent(
        self, db_session: Session, spine: dict[str, str]
    ) -> None:
        """Finding S2. Re-running the cascade must not reset the clock.

        If it did, "stale since" would always read "just now" and the UI could
        never show how long something has been out of date — which is the only
        reason the column is a timestamp rather than a boolean.
        """
        artifacts = ArtifactRepository(db_session)
        artifact_id = spine["artifact_id"]

        assert artifacts.mark_stale([artifact_id]) == 1
        first = db_session.execute(
            sa.text("SELECT stale_since FROM artifact WHERE id = :id"),
            {"id": artifact_id},
        ).scalar_one()

        assert artifacts.mark_stale([artifact_id]) == 0
        second = db_session.execute(
            sa.text("SELECT stale_since FROM artifact WHERE id = :id"),
            {"id": artifact_id},
        ).scalar_one()
        assert first == second

        artifacts.clear_stale(artifact_id)
        assert (
            db_session.execute(
                sa.text("SELECT stale_since FROM artifact WHERE id = :id"),
                {"id": artifact_id},
            ).scalar_one()
            is None
        )

    def test_mark_stale_with_no_ids_is_a_noop(self, db_session: Session) -> None:
        assert ArtifactRepository(db_session).mark_stale([]) == 0


class TestProjectPointers:
    def test_active_pointers_merge_rather_than_overwrite(
        self, db_session: Session, spine: dict[str, str]
    ) -> None:
        """Concurrent approvals in different stages must both survive.

        Read-modify-write in Python would lose one of these: both callers read
        the same object and the later write erases the earlier key. The JSONB
        ``||`` merge happens server-side, so it cannot.
        """
        projects = ProjectRepository(db_session)
        projects.set_active_pointer(spine["project_id"], "script", "01AVSCRIPT")
        projects.set_active_pointer(spine["project_id"], "research", "01AVRESEARCH")

        pointers = db_session.execute(
            sa.text("SELECT active_pointers FROM video_project WHERE id = :id"),
            {"id": spine["project_id"]},
        ).scalar_one()
        assert pointers == {"script": "01AVSCRIPT", "research": "01AVRESEARCH"}

        projects.set_active_pointer(spine["project_id"], "script", None)
        pointers = db_session.execute(
            sa.text("SELECT active_pointers FROM video_project WHERE id = :id"),
            {"id": spine["project_id"]},
        ).scalar_one()
        assert pointers == {"research": "01AVRESEARCH"}

    def test_set_phase_skips_a_no_op(
        self, db_session: Session, spine: dict[str, str]
    ) -> None:
        """A recompute landing on the same phase must not bump the timestamp."""
        from videoforge_shared.enums import ProjectPhase

        projects = ProjectRepository(db_session)
        assert projects.set_phase(spine["project_id"], ProjectPhase.SCRIPTING) is True
        assert projects.set_phase(spine["project_id"], ProjectPhase.SCRIPTING) is False


class TestReviewsAndComments:
    def test_latest_decision_matches_the_view(
        self, db_session: Session, spine: dict[str, str]
    ) -> None:
        """The repository and the view must agree on "effective decision".

        They order identically (``decided_at DESC, id DESC``). If they
        diverged, the API would report a version as rejected while the view
        called it approved.
        """
        artifacts = ArtifactRepository(db_session)
        versions = ArtifactVersionRepository(db_session)
        reviews = ReviewRepository(db_session)
        artifact = artifacts.get(spine["artifact_id"])
        assert artifact is not None
        version = versions.add_version(
            artifact,
            origin=VersionOrigin.GENERATED,
            content_hash="h",
            inline_content={},
        )
        reviews.record(
            artifact_version_id=version.id, decision=ReviewDecisionKind.REJECT
        )
        db_session.flush()
        reviews.record(
            artifact_version_id=version.id, decision=ReviewDecisionKind.APPROVE
        )
        db_session.flush()

        latest = reviews.latest_for_version(version.id)
        assert latest is not None
        assert latest.decision is ReviewDecisionKind.APPROVE
        assert len(reviews.for_version(version.id)) == 2

        status = versions.status_of(version.id)
        assert status is not None
        assert status.status is VersionStatus.APPROVED

    def test_comments_are_editable(
        self, db_session: Session, spine: dict[str, str]
    ) -> None:
        """Unlike decisions — a typo in a note is not history."""
        artifacts = ArtifactRepository(db_session)
        versions = ArtifactVersionRepository(db_session)
        comments = CommentRepository(db_session)
        artifact = artifacts.get(spine["artifact_id"])
        assert artifact is not None
        version = versions.add_version(
            artifact,
            origin=VersionOrigin.GENERATED,
            content_hash="h",
            inline_content={},
        )
        comment = comments.add(artifact_version_id=version.id, body="tighten the intro")
        db_session.flush()

        assert comments.edit(comment.id, "tighten the opening") is True
        db_session.refresh(comment)
        assert comment.body == "tighten the opening"
        assert comments.delete(comment.id) is True
        assert comments.for_version(version.id) == []


def test_repositories_never_commit(db_engine: Engine, spine: dict[str, str]) -> None:
    """The unit-of-work rule, enforced rather than documented.

    A repository that committed on its own would break §10.3 rule 6: a worker
    must land its artifact version, state transition, audit event and outbox
    row atomically. This opens its own connection to check the database from
    outside the test's uncommitted transaction — if any repository had
    committed, ``spine``'s rows would be visible here.
    """
    with db_engine.connect() as external:
        visible = external.execute(
            sa.text("SELECT count(*) FROM video_project WHERE id = :id"),
            {"id": spine["project_id"]},
        ).scalar_one()
    assert visible == 0
