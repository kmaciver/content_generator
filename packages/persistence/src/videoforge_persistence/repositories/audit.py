"""Transitions, audit events, and the outbox — the write-also tables.

SADD §10.3 rule 6 requires all of these to be written *in the same transaction*
as the state change that caused them. None of these methods commit, which is
what makes that possible.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa

from videoforge_persistence.models import AuditEvent, OutboxEvent, StateTransition
from videoforge_persistence.repositories.base import Repository, affected_rows
from videoforge_shared.correlation import get_correlation_id
from videoforge_shared.enums import SubjectType, TransitionCause
from videoforge_shared.ids import new_ulid

__all__ = ["AuditRepository", "OutboxRepository"]


class AuditRepository(Repository):
    """``state_transition`` + ``audit_event``.

    Correlation ids default to the ambient one rather than being passed at
    every call site: the whole value of the id threaded through nginx → Flask
    → Celery (M0-06/08) is that an operator can pull one request's entire
    story out of the logs *and* the audit tables. A call site that forgets to
    pass it silently breaks that, and nothing fails.
    """

    def record_transition(
        self,
        *,
        subject_type: SubjectType,
        subject_id: str,
        to_state: str,
        cause: TransitionCause,
        from_state: str | None = None,
        actor_id: str | None = None,
        job_id: str | None = None,
        correlation_id: str | None = None,
    ) -> StateTransition:
        transition = StateTransition(
            id=new_ulid(),
            subject_type=subject_type,
            subject_id=subject_id,
            from_state=from_state,
            to_state=to_state,
            cause=cause,
            actor_id=actor_id,
            job_id=job_id,
            correlation_id=correlation_id or get_correlation_id(),
        )
        self.session.add(transition)
        return transition

    def record_event(
        self,
        *,
        event_type: str,
        subject_type: SubjectType,
        subject_id: str,
        payload: dict[str, Any] | None = None,
        actor_id: str | None = None,
        correlation_id: str | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            id=new_ulid(),
            event_type=event_type,
            subject_type=subject_type,
            subject_id=subject_id,
            payload=payload or {},
            actor_id=actor_id,
            correlation_id=correlation_id or get_correlation_id(),
        )
        self.session.add(event)
        return event

    def history_for(
        self, subject_type: SubjectType, subject_id: str
    ) -> list[StateTransition]:
        """Oldest first — the order the UI timeline reads."""
        stmt = (
            sa.select(StateTransition)
            .where(
                StateTransition.subject_type == subject_type,
                StateTransition.subject_id == subject_id,
            )
            .order_by(StateTransition.created_at, StateTransition.id)
        )
        return list(self.session.scalars(stmt))


class OutboxRepository(Repository):
    """Transactional outbox (SADD §14.5, ADR-003)."""

    def enqueue(
        self,
        *,
        event_type: str,
        payload: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> OutboxEvent:
        """Write the event into the caller's transaction.

        Not published here, and deliberately not publishable here: publishing
        inside the transaction would emit an event for work that may still
        roll back. The drain (M1-05) publishes only what committed.
        """
        event = OutboxEvent(
            id=new_ulid(),
            event_type=event_type,
            payload=payload or {},
            correlation_id=correlation_id or get_correlation_id(),
        )
        self.session.add(event)
        return event

    def claim_unpublished(self, limit: int = 100) -> list[OutboxEvent]:
        """The drain's read.

        ``FOR UPDATE SKIP LOCKED`` is what lets more than one drain run
        without coordination — a second drainer skips rows the first holds
        instead of blocking on them or, worse, publishing them twice.

        Ordered by ``created_at, id`` so consumers see events in the order
        they happened; ULIDs make the id tiebreak agree with the timestamp
        when two events share one transaction's frozen ``now()``.
        """
        stmt = (
            sa.select(OutboxEvent)
            .where(OutboxEvent.published_at.is_(None))
            .order_by(OutboxEvent.created_at, OutboxEvent.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list(self.session.scalars(stmt))

    def mark_published(
        self, event_ids: list[str], *, at: datetime | None = None
    ) -> int:
        """Stamp events as delivered.

        The one legitimate UPDATE among the audit-adjacent tables, which is
        why ``outbox_event`` carries no immutability trigger.

        ``published_at IS NULL`` in the WHERE clause keeps it idempotent: a
        drain that crashes after publishing but before committing will
        re-publish on restart (at-least-once, by design) and must not rewrite
        the original timestamp when it does.
        """
        if not event_ids:
            return 0
        result = self.session.execute(
            sa.update(OutboxEvent)
            .where(
                OutboxEvent.id.in_(event_ids),
                OutboxEvent.published_at.is_(None),
            )
            .values(published_at=at or sa.func.now())
        )
        return affected_rows(result)

    def backlog(self) -> int:
        """Unpublished count — the drain's health signal.

        A number that only goes up means the drain is dead, and because S7
        gives the events no consumer in M1, nothing else would notice.
        """
        return int(
            self.session.execute(
                sa.select(sa.func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.published_at.is_(None))
            ).scalar_one()
        )
