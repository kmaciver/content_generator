"""Durable job records and what they cost.

The API never generates (SADD's architectural rule): it writes a
``generation_job`` row and returns. This table is therefore the boundary
between "a user asked for something" and "a worker did something", and it is
the state of record for both — Redis is a transport, not a ledger (ADR-010).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from videoforge_persistence.base import Base
from videoforge_persistence.columns import (
    TimestampType,
    ULIDType,
    created_at_col,
    jsonb_col,
    nullable_jsonb_col,
    ulid_pk,
    updated_at_col,
)
from videoforge_persistence.enum_types import JOB_STATUS
from videoforge_shared.enums import JobStatus


class GenerationJob(Base):
    """One unit of work handed to a worker.

    ``idempotency_key`` is the load-bearing column. Celery with a Redis broker
    guarantees *at-least-once* delivery (ADR-010, §14.3), so the same task can
    and will arrive twice — on a visibility-timeout expiry, on a worker
    restart mid-task, on a broker failover. A unique index here is what turns
    "the same job twice" into an insert conflict the service can detect,
    rather than two artifact versions and a confused reviewer. M1-04's
    double-delivery test is the proof.
    """

    __tablename__ = "generation_job"

    id: Mapped[str] = ulid_pk()
    project_id: Mapped[str] = mapped_column(
        ULIDType, sa.ForeignKey("video_project.id", ondelete="CASCADE"), nullable=False
    )
    #: Nullable because some jobs (a whole-project package build) do not
    #: produce exactly one artifact.
    artifact_id: Mapped[str | None] = mapped_column(
        ULIDType, sa.ForeignKey("artifact.id", ondelete="CASCADE"), nullable=True
    )
    task_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: Stored, not inferred from ``task_name``: the operator's first question
    #: about a stuck job is "which queue is it on", and a join-free answer
    #: keeps that cheap. Also survives a task being re-routed later.
    queue: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        JOB_STATUS, nullable=False, default=JobStatus.QUEUED
    )
    attempt: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=0, server_default=sa.text("0")
    )
    max_attempts: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, default=3, server_default=sa.text("3")
    )
    #: Celery's own id, for correlating with Flower and worker logs. Not
    #: unique: a retry reuses the row and gets a new Celery id.
    celery_task_id: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    #: Exact inputs this job ran against — the version ids and parameters —
    #: so a failed job can be explained without guessing what the state was.
    input_snapshot: Mapped[dict[str, Any]] = jsonb_col()
    error: Mapped[dict[str, Any] | None] = nullable_jsonb_col()
    #: See the class docstring. UNIQUE is the entire mechanism.
    idempotency_key: Mapped[str] = mapped_column(sa.Text, nullable=False)
    queued_at: Mapped[datetime] = mapped_column(
        TimestampType, nullable=False, server_default=sa.func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(TimestampType, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(TimestampType, nullable=True)
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    __table_args__ = (
        sa.UniqueConstraint("idempotency_key"),
        # The reconciler's query (§14.4): "RUNNING and started before X".
        # Partial, because RUNNING rows are a tiny slice of the table and the
        # reconciler runs on a schedule forever.
        sa.Index(
            "ix_generation_job_running_started_at",
            "started_at",
            postgresql_where=sa.text("status = 'RUNNING'"),
        ),
        sa.Index("ix_generation_job_project_id_created_at", "project_id", "created_at"),
        sa.Index("ix_generation_job_artifact_id", "artifact_id"),
        sa.CheckConstraint("attempt >= 0", name="attempt_non_negative"),
        sa.CheckConstraint("max_attempts > 0", name="max_attempts_positive"),
    )


class ProviderUsage(Base):
    """What one provider call consumed — immutable (SADD §10.2).

    Recorded per call rather than per job because a single job may make
    several (a retry against a fallback model, an image plus its upscale).
    Aggregating up to the daily spend cap (finding S10, M3) needs the leaves.
    """

    __tablename__ = "provider_usage"

    id: Mapped[str] = ulid_pk()
    job_id: Mapped[str] = mapped_column(
        ULIDType, sa.ForeignKey("generation_job.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(sa.Text, nullable=False)
    model: Mapped[str] = mapped_column(sa.Text, nullable=False)
    operation: Mapped[str] = mapped_column(sa.Text, nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    output_tokens: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    images: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    audio_seconds: Mapped[float | None] = mapped_column(sa.Float, nullable=True)
    #: ``NUMERIC``, never float. Money summed in binary floating point drifts,
    #: and this column's whole purpose is a spend cap that must not drift.
    #: "estimate" because provider pricing is inferred locally, not billed.
    unit_cost_estimate: Mapped[Decimal] = mapped_column(
        sa.Numeric(12, 6), nullable=False, server_default=sa.text("0")
    )
    latency_ms: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    raw_meta: Mapped[dict[str, Any]] = jsonb_col()
    created_at: Mapped[datetime] = created_at_col()
    # Immutable: no ``updated_at``, trigger raises on UPDATE.

    __table_args__ = (
        sa.Index("ix_provider_usage_job_id", "job_id"),
        # The daily-spend rollup (S10) scans by day and provider.
        sa.Index("ix_provider_usage_created_at_provider", "created_at", "provider"),
    )
