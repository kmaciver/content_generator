"""Job queries — including the two that carry the correctness of the system.

``reserve`` (idempotent insert) and ``claim`` (compare-and-set) are the pair
that makes at-least-once delivery survivable (SADD §14.3). M1-04 builds the
job service on them; the mechanics live here because they are queries, and
queries live in repositories.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert

from videoforge_persistence.models import GenerationJob, ProviderUsage
from videoforge_persistence.repositories.base import Repository, affected_rows
from videoforge_shared.enums import JobStatus
from videoforge_shared.ids import new_ulid

__all__ = ["JobRepository", "ProviderUsageRepository", "ReservedJob"]


@dataclass(frozen=True, slots=True)
class ReservedJob:
    """Result of :meth:`JobRepository.reserve`.

    ``created`` is the whole point: it tells the caller whether *this* request
    produced the job or merely found the one an earlier duplicate created.
    A service that ignores it will enqueue the same Celery task twice.
    """

    job: GenerationJob
    created: bool


class JobRepository(Repository):
    def get(self, job_id: str) -> GenerationJob | None:
        return self.session.get(GenerationJob, job_id)

    def by_idempotency_key(self, key: str) -> GenerationJob | None:
        return self.session.scalars(
            sa.select(GenerationJob).where(GenerationJob.idempotency_key == key)
        ).one_or_none()

    def live_by_idempotency_key(self, key: str) -> GenerationJob | None:
        """The job currently *holding* ``key``, if any.

        A job that ended badly releases its key (see
        ``GenerationJob._DEAD_STATUSES``), so this is the lookup that pairs
        with the partial unique index. ``by_idempotency_key`` still returns
        history, which is what an operator asking "what happened to this
        request?" wants.
        """
        return self.session.scalars(
            sa.select(GenerationJob)
            .where(
                GenerationJob.idempotency_key == key,
                GenerationJob.status.not_in(
                    [JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.ORPHANED]
                ),
            )
            .order_by(GenerationJob.created_at.desc())
            .limit(1)
        ).one_or_none()

    def reserve(
        self,
        *,
        task_name: str,
        queue: str,
        idempotency_key: str,
        project_id: str | None = None,
        series_id: str | None = None,
        artifact_id: str | None = None,
        input_snapshot: dict[str, Any] | None = None,
        max_attempts: int = 3,
    ) -> ReservedJob:
        """Insert the job, or return the existing one with the same key.

        ``INSERT ... ON CONFLICT DO NOTHING`` rather than "SELECT then INSERT
        if missing": the latter has a window between the two statements in
        which a concurrent request inserts, and the loser raises
        ``IntegrityError`` from what looked like a safe check. Under
        at-least-once delivery that window is not theoretical — duplicate
        deliveries arrive *close together*, which is exactly when it is open.

        The unique index on ``idempotency_key`` is what makes this atomic;
        this method just declines to fight it. The index is **partial** —
        it covers live jobs only — so a request whose previous attempt died
        can be made again, while a redelivery of a SUCCEEDED job still
        collides (§14.3).
        """
        # Exactly one scope, matching `ck_generation_job_scope`. Checked here
        # too rather than left to the constraint: an IntegrityError at flush
        # names the constraint, not the call site that got it wrong, and this
        # is a programming error rather than a data one.
        if (project_id is None) == (series_id is None):
            raise ValueError(
                "a job needs exactly one of project_id or series_id; "
                f"got project_id={project_id!r}, series_id={series_id!r}"
            )

        values = {
            "id": new_ulid(),
            "project_id": project_id,
            "series_id": series_id,
            "artifact_id": artifact_id,
            "task_name": task_name,
            "queue": queue,
            "status": JobStatus.QUEUED,
            "input_snapshot": input_snapshot or {},
            "idempotency_key": idempotency_key,
            "max_attempts": max_attempts,
        }
        stmt = (
            pg_insert(GenerationJob)
            .values(**values)
            # `index_where` must match the partial index exactly, or Postgres
            # cannot infer which index the conflict clause means and raises
            # "there is no unique or exclusion constraint matching".
            .on_conflict_do_nothing(
                index_elements=[GenerationJob.idempotency_key],
                index_where=sa.text(
                    "status NOT IN ('FAILED', 'CANCELLED', 'ORPHANED')"
                ),
            )
            .returning(GenerationJob.id)
        )
        inserted_id = self.session.execute(stmt).scalar_one_or_none()

        if inserted_id is None:
            # Someone else owns this key. Their row is the job.
            # Only a *live* job can hold the key now, so this cannot return a
            # dead one — but the lookup filters anyway, because
            # `by_idempotency_key` is used elsewhere and a caller that got a
            # FAILED job back here would report it as the reservation.
            existing = self.live_by_idempotency_key(idempotency_key)
            if existing is None:  # pragma: no cover - needs real concurrency
                # The conflicting row exists but is not visible to this
                # transaction — a concurrent insert that has not committed.
                # ``ON CONFLICT DO NOTHING`` does not block on it (unlike
                # ``DO UPDATE``, which would wait), so we land here with a
                # conflict and nothing to return.
                #
                # Raising is deliberate: the caller must retry, and the one
                # thing it must NOT do is proceed as though it owns the job,
                # which would enqueue the task a second time — the exact
                # duplicate this method exists to prevent.
                raise RuntimeError(
                    f"idempotency key {idempotency_key!r} is held by an "
                    "uncommitted transaction; retry"
                )
            return ReservedJob(job=existing, created=False)

        self.session.flush()
        job = self.session.get(GenerationJob, inserted_id)
        assert job is not None
        return ReservedJob(job=job, created=True)

    def claim(self, job_id: str, celery_task_id: str | None = None) -> bool:
        """QUEUED → RUNNING, atomically. Returns whether *this* caller won.

        The guard is the ``status = 'QUEUED'`` predicate inside the UPDATE.
        Two workers handed the same message both issue this statement; the
        database serialises them, the first changes one row, the second
        changes zero. The loser sees ``False`` and must drop the message
        rather than run the task — which is what stops a redelivery producing
        a second artifact version (§14.3).

        Doing the same thing by reading the status and then updating would
        reintroduce the race the guard exists to close.
        """
        result = self.session.execute(
            sa.update(GenerationJob)
            .where(
                GenerationJob.id == job_id,
                GenerationJob.status == JobStatus.QUEUED,
            )
            .values(
                status=JobStatus.RUNNING,
                started_at=sa.func.now(),
                attempt=GenerationJob.attempt + 1,
                celery_task_id=celery_task_id,
            )
        )
        return affected_rows(result) == 1

    def mark_succeeded(self, job_id: str) -> bool:
        """RUNNING → SUCCEEDED. Guarded the same way, for the same reason."""
        result = self.session.execute(
            sa.update(GenerationJob)
            .where(
                GenerationJob.id == job_id,
                GenerationJob.status == JobStatus.RUNNING,
            )
            .values(status=JobStatus.SUCCEEDED, finished_at=sa.func.now())
        )
        return affected_rows(result) == 1

    def mark_failed(self, job_id: str, error: dict[str, Any], *, requeue: bool) -> bool:
        """RUNNING → FAILED, or back to QUEUED when attempts remain.

        The retry decision belongs to ``videoforge_domain.job_lifecycle``
        (``may_retry``); this method only writes what it was told, so the
        policy stays testable without a database.
        """
        target = JobStatus.QUEUED if requeue else JobStatus.FAILED
        result = self.session.execute(
            sa.update(GenerationJob)
            .where(
                GenerationJob.id == job_id,
                GenerationJob.status == JobStatus.RUNNING,
            )
            .values(
                status=target,
                error=error,
                finished_at=None if requeue else sa.func.now(),
            )
        )
        return affected_rows(result) == 1

    def claim_orphans(self, older_than: timedelta) -> list[GenerationJob]:
        """SADD §10.1's other named example, and §14.4's reconciler.

        A RUNNING job whose worker died leaves no trace in Redis — the message
        is gone and nothing will ever complete it. The only evidence is a row
        that has been RUNNING longer than any task should take, which is why
        Postgres is the state of record and the broker is not (ADR-010).

        ``FOR UPDATE SKIP LOCKED`` so two reconcilers (a restart overlapping
        its predecessor) never claim the same job. Without it they would both
        mark it orphaned and both requeue it — turning a recovery mechanism
        into a duplicator.

        The cutoff is computed **server-side**. ``started_at`` is written with
        ``now()``, i.e. the database's clock; subtracting the timeout from the
        *reconciler container's* clock would compare two different clocks, and
        the error term is skew rather than elapsed time. Skew in one direction
        reaps jobs that are still running, in the other it never reaps at all.
        """
        cutoff = sa.func.now() - sa.cast(sa.literal(older_than), sa.Interval)
        stmt = (
            sa.select(GenerationJob)
            .where(
                GenerationJob.status == JobStatus.RUNNING,
                GenerationJob.started_at < cutoff,
            )
            .with_for_update(skip_locked=True)
        )
        jobs = list(self.session.scalars(stmt))
        if jobs:
            self.session.execute(
                sa.update(GenerationJob)
                .where(GenerationJob.id.in_([job.id for job in jobs]))
                .values(status=JobStatus.ORPHANED, finished_at=sa.func.now())
            )
        return jobs

    def requeue(self, job_id: str) -> bool:
        """FAILED or ORPHANED → QUEUED."""
        result = self.session.execute(
            sa.update(GenerationJob)
            .where(
                GenerationJob.id == job_id,
                GenerationJob.status.in_([JobStatus.FAILED, JobStatus.ORPHANED]),
            )
            .values(status=JobStatus.QUEUED, finished_at=None, error=None)
        )
        return affected_rows(result) == 1


class ProviderUsageRepository(Repository):
    def record(
        self,
        *,
        job_id: str,
        provider: str,
        model: str,
        operation: str,
        latency_ms: int,
        unit_cost_estimate: float = 0.0,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
        images: int | None = None,
        audio_seconds: float | None = None,
        raw_meta: dict[str, Any] | None = None,
    ) -> ProviderUsage:
        usage = ProviderUsage(
            id=new_ulid(),
            job_id=job_id,
            provider=provider,
            model=model,
            operation=operation,
            latency_ms=latency_ms,
            unit_cost_estimate=unit_cost_estimate,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            images=images,
            audio_seconds=audio_seconds,
            raw_meta=raw_meta or {},
        )
        self.session.add(usage)
        return usage

    def _spend_where(self, condition: sa.ColumnElement[bool]) -> Decimal:
        """Sum ``unit_cost_estimate`` over the matching rows.

        ``coalesce`` because ``sum()`` over no rows is NULL, and a cap check
        that compares NULL against a threshold silently passes.

        Returns ``Decimal``, converted **via ``str``**. The column is a float,
        and ``Decimal(0.1)`` built from a float carries the binary
        representation error straight into a money comparison;
        ``Decimal(str(0.1))`` does not. Far below anything that matters for a
        spend estimate — the point is not to write the pattern that *does*
        matter elsewhere.
        """
        total = self.session.execute(
            sa.select(
                sa.func.coalesce(sa.func.sum(ProviderUsage.unit_cost_estimate), 0)
            ).where(condition)
        ).scalar_one()
        return Decimal(str(total))

    def spend_since(self, since: datetime) -> Decimal:
        """Total estimated spend since a caller-supplied instant."""
        return self._spend_where(ProviderUsage.created_at >= since)

    def spend_today(self) -> Decimal:
        """Estimated spend since the start of the current **UTC** day (S10).

        The boundary is computed **server-side** — ``date_trunc('day', now())``
        — for the reason ``claim_orphans`` spells out one class up: rows are
        stamped with the database's clock, and deriving midnight from a worker
        container's clock would compare two clocks and call the difference
        elapsed time. Five containers would each cap at a slightly different
        moment, and the drift would be invisible.

        UTC rather than a local zone: every timestamp in the system is
        ``timestamptz`` and the deployment has no configured locale. An
        operator in UTC-8 sees the cap reset mid-afternoon. Documenting that
        beats inventing a timezone setting nobody has asked for.
        """
        midnight = sa.func.date_trunc("day", sa.func.now())
        return self._spend_where(ProviderUsage.created_at >= midnight)
