"""Persistence layer shared by the backend and the workers.

This package exists because BOTH apps write to the database — the API creates
jobs, the worker task skeleton inserts artifact versions in the same
transaction as its outputs (SADD §13) — and the apps must never import each
other. The SADD's §8 tree drew ``orm/`` under the backend; that placement
could not survive contact with the worker skeleton, so the data layer lives
here. Recorded as a SADD amendment in M0-13.

M0-07 shipped the foundations: declarative base with a naming convention, and
engine/session factories. M1-01 adds the thirteen core tables, the
immutability triggers, and the ``artifact_version_status`` view.

Importing this namespace registers every model on ``Base.metadata`` — which
is what Alembic autogenerate compares against the database (finding S9).
"""

from videoforge_persistence.base import Base
from videoforge_persistence.engine import create_engine_from_settings, session_factory
from videoforge_persistence.models import (
    AppUser,
    Artifact,
    ArtifactVersion,
    AuditEvent,
    Comment,
    GenerationJob,
    OutboxEvent,
    ProviderUsage,
    ReviewDecision,
    Series,
    StateTransition,
    VideoProject,
    Workspace,
)

__all__ = [
    "AppUser",
    "Artifact",
    "ArtifactVersion",
    "AuditEvent",
    "Base",
    "Comment",
    "GenerationJob",
    "OutboxEvent",
    "ProviderUsage",
    "ReviewDecision",
    "Series",
    "StateTransition",
    "VideoProject",
    "Workspace",
    "create_engine_from_settings",
    "session_factory",
]
