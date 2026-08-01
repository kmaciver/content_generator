"""The three write-also tables: transitions, audit, outbox.

SADD §10.3 rule 6: *every write path that changes state also inserts a
``state_transition`` and an ``audit_event`` in the same transaction.* Note
"same transaction" — that is what makes the audit trail complete rather than
best-effort. No triggers do this; triggers only *enforce* immutability.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from videoforge_persistence.base import Base
from videoforge_persistence.columns import (
    TimestampType,
    ULIDType,
    created_at_col,
    jsonb_col,
    ulid_pk,
)
from videoforge_persistence.enum_types import SUBJECT_TYPE, TRANSITION_CAUSE
from videoforge_shared.enums import SubjectType, TransitionCause


class StateTransition(Base):
    """Every state change, with its cause — immutable.

    The ``subject`` is a polymorphic ``(type, id)`` pair with **no foreign
    key**, deliberately. An audit record whose survival depends on its subject
    still existing is not an audit record; a deleted project must leave its
    history behind. The cost is that referential integrity here is the
    services' job.

    ``correlation_id`` is what stitches a transition to the nginx request and
    the worker log line that caused it (the id threaded through in M0-06/08).
    """

    __tablename__ = "state_transition"

    id: Mapped[str] = ulid_pk()
    subject_type: Mapped[SubjectType] = mapped_column(SUBJECT_TYPE, nullable=False)
    subject_id: Mapped[str] = mapped_column(ULIDType, nullable=False)
    #: Nullable ``from_state``: an artifact's first transition comes from
    #: nowhere. Plain text, not an enum — one column carries artifact states,
    #: job statuses, and project phases, and three enums cannot share a column.
    from_state: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    to_state: Mapped[str] = mapped_column(sa.Text, nullable=False)
    cause: Mapped[TransitionCause] = mapped_column(TRANSITION_CAUSE, nullable=False)
    actor_id: Mapped[str | None] = mapped_column(
        ULIDType, sa.ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
    job_id: Mapped[str | None] = mapped_column(
        ULIDType, sa.ForeignKey("generation_job.id", ondelete="SET NULL"), nullable=True
    )
    correlation_id: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (
        # "The history of this thing, oldest first" — the only query the UI
        # timeline makes.
        sa.Index(
            "ix_state_transition_subject", "subject_type", "subject_id", "created_at"
        ),
        sa.Index("ix_state_transition_correlation_id", "correlation_id"),
    )


class AuditEvent(Base):
    """The superset log — immutable.

    Broader than :class:`StateTransition` on purpose: it records things that
    changed *nothing* (a version was viewed, a download was issued, a comment
    was posted) alongside things that did. When the question is "what happened
    here?", a log that only contains state changes answers half of it.
    """

    __tablename__ = "audit_event"

    id: Mapped[str] = ulid_pk()
    event_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    subject_type: Mapped[SubjectType] = mapped_column(SUBJECT_TYPE, nullable=False)
    subject_id: Mapped[str] = mapped_column(ULIDType, nullable=False)
    actor_id: Mapped[str | None] = mapped_column(
        ULIDType, sa.ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True
    )
    payload: Mapped[dict[str, Any]] = jsonb_col()
    correlation_id: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (
        sa.Index("ix_audit_event_subject", "subject_type", "subject_id", "created_at"),
        sa.Index("ix_audit_event_correlation_id", "correlation_id"),
        sa.Index("ix_audit_event_event_type_created_at", "event_type", "created_at"),
    )


class OutboxEvent(Base):
    """Transactional outbox (SADD §14.5, ADR-003) — **mutable, unlike its neighbours.**

    The pattern exists to solve one problem: a service that commits a database
    change and *then* publishes to Redis can crash in between, losing the
    event; one that publishes first can publish an event for a transaction
    that later rolls back. Writing the event into the same transaction as the
    state change makes the two atomic, and a separate drain worker (M1-05)
    publishes and stamps ``published_at``.

    That stamp is why this table has no immutability trigger — it is the one
    audit-adjacent table with a legitimate UPDATE path. Rows are otherwise
    never modified.

    Per finding S7 the drain publishes to Redis pub/sub with **no consumer**
    in M1: the outbox is load-bearing for correctness and audit regardless,
    while SSE waits for M5 and real UI to justify its complexity.
    """

    __tablename__ = "outbox_event"

    id: Mapped[str] = ulid_pk()
    event_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    payload: Mapped[dict[str, Any]] = jsonb_col()
    correlation_id: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    created_at: Mapped[datetime] = created_at_col()
    published_at: Mapped[datetime | None] = mapped_column(TimestampType, nullable=True)

    __table_args__ = (
        # The drain's only query: unpublished, oldest first. Partial, so the
        # index stays the size of the backlog rather than the size of history
        # — it empties as fast as the drain runs.
        sa.Index(
            "ix_outbox_event_unpublished",
            "created_at",
            postgresql_where=sa.text("published_at IS NULL"),
        ),
    )
