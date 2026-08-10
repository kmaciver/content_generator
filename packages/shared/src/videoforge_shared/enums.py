"""The state vocabulary — one definition, three consumers.

These live in ``shared`` rather than in ``persistence`` on purpose. The ORM
needs them to declare Postgres enum types; the domain layer (SADD §11) needs
them to express the FSMs; the workers need artifact kinds to route. If they
lived in ``persistence``, importing a state name would drag SQLAlchemy into
the pure domain layer and quietly destroy its "testable without a database"
property. ``shared`` depends on nothing, so nobody pays for the import.

**Values are the wire format.** Every member's *value* — not its name — is
what lands in Postgres, in JSON payloads, and in the API. They are transcribed
verbatim from SADD §10.2/§12, casing included: artifact kinds and causes are
lowercase there, states and statuses uppercase. That inconsistency is
inherited deliberately rather than tidied, because renaming a Postgres enum
label costs an ``ALTER TYPE`` migration (§10.4) and buys nothing.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "ArtifactKind",
    "ArtifactState",
    "BrandingStatus",
    "JobStatus",
    "ProjectPhase",
    "ReviewDecisionKind",
    "SceneKind",
    "SubjectType",
    "TransitionCause",
    "UserRole",
    "VersionOrigin",
    "VersionStatus",
]


class ArtifactKind(StrEnum):
    """What a given artifact *is* (SADD §10.2).

    The pipeline DAG in ``templates/pipeline.yaml`` (M2) keys off these, which
    is why adding a stage later is a config change rather than a schema one.
    """

    RESEARCH = "research"
    SCRIPT = "script"
    SCENE_SET = "scene_set"
    SCENE = "scene"
    PROMPT = "prompt"
    IMAGE = "image"
    VOICE = "voice"
    TIMELINE = "timeline"
    RENDER = "render"
    PACKAGE = "package"
    MUSIC = "music"


class SceneKind(StrEnum):
    """What a scene *is made of* — M4-01, from §1.0.3.

    Not every scene is a generated illustration. The reference videos intercut
    artwork with typographic cards ("Step 5" on cream paper), and the SADD had
    no concept of that: every scene assumed an ``image`` artifact from an
    ``ImageProvider``.

    A ``CARD`` renders locally from a template — no provider call, no cost, no
    style drift, and byte-identical between runs. Three cards in a twenty-scene
    video is a 15% cut in image spend and three fewer chances for R7 to bite.

    Lowercase values, matching ``ArtifactKind``'s inherited casing (§10.2).
    """

    ILLUSTRATION = "illustration"
    CARD = "card"


class ArtifactState(StrEnum):
    """Per-artifact lifecycle (SADD §12.2) — the workhorse FSM.

    Note what is absent: there is no ``SUPERSEDED`` here. Superseding is a
    property of a *version*, not of the artifact identity, and it is derived
    rather than stored (finding B1) — see :class:`VersionStatus`.
    """

    PENDING = "PENDING"
    GENERATING = "GENERATING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


class VersionOrigin(StrEnum):
    """How a version came to exist (SADD §10.3 rule 3).

    ``HUMAN_EDIT`` is mechanically identical to ``GENERATED`` — same table,
    same transitions — and differs only in the audit trail. That is the whole
    point: the pipeline must not care who typed the words.
    """

    GENERATED = "generated"
    HUMAN_EDIT = "human_edit"
    IMPORT = "import"


class VersionStatus(StrEnum):
    """**Derived, never stored** (finding B1).

    ``artifact_version`` is append-only and carries no status column, so the
    SADD's original "mark siblings SUPERSEDED" was literally unimplementable —
    an UPDATE against a table whose trigger raises on UPDATE. These values are
    computed by the ``artifact_version_status`` view from ``review_decision``
    rows, and this enum exists only to give Python a typed way to read it.

    Nothing should ever write one of these to a column. If you find yourself
    wanting to, re-read the view.
    """

    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


class BrandingStatus(StrEnum):
    """Lifecycle of a series-scoped branding asset (ADR-016, M3-02).

    **A separate enum that deliberately reuses existing words.** ADR-016 says
    branding statuses reuse `ArtifactState`'s vocabulary — "same words,
    separate column" — but the four labels it names do not match either
    existing enum: `ArtifactState` has no `SUPERSEDED`, and `VersionStatus` has
    no `PENDING`. Pointing the column at the nearest one would have meant
    either a status these rows cannot express or three labels they can never
    hold, and both make an operator reading the column guess.

    **`SUPERSEDED` is stored here, not derived.** That is the opposite of
    finding B1, and the difference is real: `artifact_version` is append-only,
    so marking siblings superseded was literally impossible and had to become a
    view. These tables are mutable by design — ADR-016 accepts reimplementing
    versioning as the price of series scoping — so an UPDATE is available, and
    a partial unique index enforces at most one `APPROVED` per series. Nothing
    here is append-only, so nothing here needs a view.

    There is no `REJECTED`. A rejected candidate group is simply never
    approved, and the next generation supersedes it; a terminal "no" would add
    a state with no transition out of it and no screen that shows it.
    """

    PENDING = "PENDING"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    APPROVED = "APPROVED"
    SUPERSEDED = "SUPERSEDED"


class JobStatus(StrEnum):
    """Execution mechanics only (SADD §12.3).

    Deliberately disjoint from approval logic: a job succeeding is a *cause*
    of an artifact transition, never the transition itself. ``ORPHANED`` is
    what the reconciler (§14.4) writes when a RUNNING job's worker vanished.
    """

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    ORPHANED = "ORPHANED"


class ProjectPhase(StrEnum):
    """Coarse, **derived** project phase (SADD §12.4).

    Cached on ``video_project`` for cheap listing, recomputed from artifact
    truth. Because it is derived it can never disagree with the artifacts;
    "rollback" is just rejecting a version and letting the phase fall back.
    """

    DRAFT = "DRAFT"
    RESEARCHING = "RESEARCHING"
    RESEARCH_REVIEW = "RESEARCH_REVIEW"
    SCRIPTING = "SCRIPTING"
    SCRIPT_REVIEW = "SCRIPT_REVIEW"
    SCENING = "SCENING"
    SCENES_REVIEW = "SCENES_REVIEW"
    MEDIA_GENERATION = "MEDIA_GENERATION"
    MEDIA_REVIEW = "MEDIA_REVIEW"
    TIMELINE_READY = "TIMELINE_READY"
    RENDERING = "RENDERING"
    RENDER_REVIEW = "RENDER_REVIEW"
    PACKAGING = "PACKAGING"
    READY_TO_PUBLISH = "READY_TO_PUBLISH"
    PUBLISHED = "PUBLISHED"


class ReviewDecisionKind(StrEnum):
    """A human's verdict on one specific version (SADD §17)."""

    APPROVE = "APPROVE"
    REJECT = "REJECT"


class SubjectType(StrEnum):
    """What a ``state_transition`` or ``audit_event`` is *about*.

    The subject is a polymorphic (type, id) pair with no foreign key, because
    the audit log must outlive its subjects — a hard-deleted project should
    not take its history with it.
    """

    PROJECT_PHASE = "project_phase"
    ARTIFACT = "artifact"
    JOB = "job"
    #: M3-02, added by ``ALTER TYPE`` per §10.4. Branding assets live outside
    #: the artifact tables (ADR-016) but their approvals are still history, and
    #: the polymorphic subject is what lets one audit log cover both without a
    #: foreign key to either.
    SERIES_CHARACTER = "series_character"
    SERIES_STYLE = "series_style"


class TransitionCause(StrEnum):
    """Why a transition happened (SADD §12.2).

    The closed set is the point: if a state changed and the cause is not one
    of these five, something wrote to the database outside a service.
    """

    JOB_SUCCEEDED = "job_succeeded"
    JOB_FAILED = "job_failed"
    REVIEW = "review"
    EDIT = "edit"
    SYSTEM = "system"
    RECONCILER = "reconciler"


class UserRole(StrEnum):
    """Future-proofing (SADD §10.2). v1 seeds exactly one ``OWNER``."""

    OWNER = "OWNER"
    EDITOR = "EDITOR"
    VIEWER = "VIEWER"
