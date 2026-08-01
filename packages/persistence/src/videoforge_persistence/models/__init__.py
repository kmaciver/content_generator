"""The thirteen core tables (SADD §10.2).

Importing this package is what populates ``Base.metadata``. Alembic's env.py
imports it for exactly that reason — a model module that nothing imports is a
table Alembic will cheerfully generate a ``DROP TABLE`` for.

Grouped by lifetime rather than alphabetically:

- ``org``      — workspace, app_user, series: configuration, changes rarely.
- ``project``  — video_project: one row per video.
- ``artifact`` — artifact, artifact_version: identity vs content (§10.3).
- ``job``      — generation_job, provider_usage: execution and its cost.
- ``review``   — review_decision, comment: human verdicts and human notes.
- ``audit``    — state_transition, audit_event, outbox_event: the write-also
  tables every state change touches (§10.3 rule 6).
"""

from videoforge_persistence.models.artifact import Artifact, ArtifactVersion
from videoforge_persistence.models.audit import AuditEvent, OutboxEvent, StateTransition
from videoforge_persistence.models.job import GenerationJob, ProviderUsage
from videoforge_persistence.models.org import AppUser, Series, Workspace
from videoforge_persistence.models.project import VideoProject
from videoforge_persistence.models.review import Comment, ReviewDecision

__all__ = [
    "AppUser",
    "Artifact",
    "ArtifactVersion",
    "AuditEvent",
    "Comment",
    "GenerationJob",
    "OutboxEvent",
    "ProviderUsage",
    "ReviewDecision",
    "Series",
    "StateTransition",
    "VideoProject",
    "Workspace",
]
