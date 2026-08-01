"""Pure workflow rules — the layer that expresses what the database merely stores.

**This package deviates from SADD §8**, which draws ``domain/`` under the
backend. It cannot live there: §12.2 says artifact transitions are caused by
job success and failure, which happen in *workers*, and workers must never
import the backend (a rule enforced by structural tests, not convention). A
backend-owned FSM would leave workers either duplicating the rules or writing
states by hand — and duplicated rules are how a project ends up stuck in
GENERATING with no one able to say why.

This is the same contradiction that moved the ORM into ``packages/persistence``
during M0-07, resolved the same way. Both apps import it; neither owns it.

**Nothing here touches infrastructure.** No SQLAlchemy, no Flask, no Celery,
no clock, no I/O — every function is a pure function of its arguments, which
is what makes SADD §10.1's "DB-free domain tests" true rather than aspirational.
"""

from videoforge_domain.approval_policy import AUTO_APPROVE_KEY, ApprovalPolicy
from videoforge_domain.artifact_lifecycle import (
    ArtifactEvent,
    IllegalTransitionError,
    Transition,
    apply_event,
    can_approve,
    can_edit,
    can_regenerate,
    can_reject,
    capabilities,
    is_terminal,
    legal_events,
)
from videoforge_domain.job_lifecycle import (
    IllegalJobTransitionError,
    JobEvent,
    JobTransition,
    apply_job_event,
    is_finished,
    may_retry,
    next_status_after_failure,
)

__all__ = [
    "AUTO_APPROVE_KEY",
    "ApprovalPolicy",
    "ArtifactEvent",
    "IllegalJobTransitionError",
    "IllegalTransitionError",
    "JobEvent",
    "JobTransition",
    "Transition",
    "apply_event",
    "apply_job_event",
    "can_approve",
    "can_edit",
    "can_regenerate",
    "can_reject",
    "capabilities",
    "is_finished",
    "is_terminal",
    "legal_events",
    "may_retry",
    "next_status_after_failure",
]
