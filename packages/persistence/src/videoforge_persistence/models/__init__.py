"""The core tables (SADD §10.2) — M1's thirteen, M2's scene pair, M3's branding.

Importing this package is what populates ``Base.metadata``. Alembic's env.py
imports it for exactly that reason — a model module that nothing imports is a
table Alembic will cheerfully generate a ``DROP TABLE`` for.

Grouped by lifetime rather than alphabetically:

- ``org``      — workspace, app_user, series: configuration, changes rarely.
- ``project``  — video_project: one row per video.
- ``artifact`` — artifact, artifact_version: identity vs content (§10.3).
- ``scene``    — scene_set, scene: the first *structured* artifact content,
  hung off a version rather than an artifact (see the module docstring).
- ``job``      — generation_job, provider_usage: execution and its cost.
- ``review``   — review_decision, comment: human verdicts and human notes.
- ``audit``    — state_transition, audit_event, outbox_event: the write-also
  tables every state change touches (§10.3 rule 6).
- ``branding`` — series_character, character_reference, series_style: scoped to
  the *series*, not the project, and mutable where the artifact tables are not
  (ADR-016).
"""

from videoforge_persistence.models.artifact import Artifact, ArtifactVersion
from videoforge_persistence.models.audit import AuditEvent, OutboxEvent, StateTransition
from videoforge_persistence.models.branding import (
    CharacterReference,
    SeriesCharacter,
    SeriesStyle,
)
from videoforge_persistence.models.job import GenerationJob, ProviderUsage
from videoforge_persistence.models.org import AppUser, Series, Workspace
from videoforge_persistence.models.project import VideoProject
from videoforge_persistence.models.review import Comment, ReviewDecision
from videoforge_persistence.models.scene import Scene, SceneSet

__all__ = [
    "AppUser",
    "Artifact",
    "ArtifactVersion",
    "AuditEvent",
    "CharacterReference",
    "Comment",
    "GenerationJob",
    "OutboxEvent",
    "ProviderUsage",
    "ReviewDecision",
    "Scene",
    "SceneSet",
    "Series",
    "SeriesCharacter",
    "SeriesStyle",
    "StateTransition",
    "VideoProject",
    "Workspace",
]
