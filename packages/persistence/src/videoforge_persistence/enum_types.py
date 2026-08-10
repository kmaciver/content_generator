"""The Postgres ENUM types, as MetaData-level singletons.

Every enum type is declared exactly once, here, and models reference these
objects rather than building their own. Two reasons:

1. A type shared by two tables (``subject_type``) must not be created twice.
2. Alembic's autogenerate compares MetaData against the database. Types owned
   by MetaData show up as schema objects it can reason about; types buried
   inside a column declaration are much easier for it to miss, and a missed
   enum surfaces as a migration that fails halfway through on a fresh database.

Names match the SADD's vocabulary, and per §10.4 changing one later is an
explicit ``ALTER TYPE`` migration — never an edit to this file alone.
"""

from __future__ import annotations

import sqlalchemy as sa

from videoforge_persistence.base import Base
from videoforge_persistence.columns import pg_enum
from videoforge_shared.enums import (
    ArtifactKind,
    ArtifactState,
    BrandingStatus,
    JobStatus,
    ProjectPhase,
    ReviewDecisionKind,
    SceneKind,
    SubjectType,
    TransitionCause,
    UserRole,
    VersionOrigin,
)

_META = Base.metadata

USER_ROLE: sa.Enum = pg_enum(UserRole, "user_role", _META)
PROJECT_PHASE: sa.Enum = pg_enum(ProjectPhase, "project_phase", _META)
ARTIFACT_KIND: sa.Enum = pg_enum(ArtifactKind, "artifact_kind", _META)
ARTIFACT_STATE: sa.Enum = pg_enum(ArtifactState, "artifact_state", _META)
VERSION_ORIGIN: sa.Enum = pg_enum(VersionOrigin, "version_origin", _META)
JOB_STATUS: sa.Enum = pg_enum(JobStatus, "job_status", _META)
REVIEW_DECISION_KIND: sa.Enum = pg_enum(
    ReviewDecisionKind, "review_decision_kind", _META
)
#: Shared by ``state_transition`` and ``audit_event`` — the reason this
#: module exists.
SUBJECT_TYPE: sa.Enum = pg_enum(SubjectType, "subject_type", _META)
TRANSITION_CAUSE: sa.Enum = pg_enum(TransitionCause, "transition_cause", _META)
#: M3-02. Series branding lifecycle — see ``BrandingStatus`` for why it is its
#: own type rather than a reuse of ``artifact_state``.
BRANDING_STATUS: sa.Enum = pg_enum(BrandingStatus, "branding_status", _META)
#: M4-01 (§1.0.3). Whether a scene is drawn by a provider or rendered locally
#: from a template.
SCENE_KIND: sa.Enum = pg_enum(SceneKind, "scene_kind", _META)

#: Every type, for the migration to create and drop in one pass.
ALL_ENUM_TYPES: tuple[sa.Enum, ...] = (
    USER_ROLE,
    PROJECT_PHASE,
    ARTIFACT_KIND,
    ARTIFACT_STATE,
    VERSION_ORIGIN,
    JOB_STATUS,
    REVIEW_DECISION_KIND,
    SUBJECT_TYPE,
    TRANSITION_CAUSE,
    BRANDING_STATUS,
    SCENE_KIND,
)
