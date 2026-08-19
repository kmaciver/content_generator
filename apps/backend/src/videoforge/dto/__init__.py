"""Pydantic v2 request/response models (SADD §10.1, §19).

**ORM objects never leak through these.** That rule is what makes the
repository layer's decision to return ORM models safe: the boundary is here,
enforced by construction, because a DTO is built from explicit fields rather
than by serialising whatever the ORM happens to be holding. Serialising a
model directly would emit lazy-load queries from inside the response and leak
schema shape into the API contract.

The other half of the contract is ``capabilities``: every artifact response
carries what the *domain FSM* says is currently allowed, so the UI renders
buttons from the same table the services enforce. No TypeScript reimplements
the rules (§11).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from videoforge_domain.artifact_lifecycle import can_regenerate, capabilities
from videoforge_domain.captions import group_into_cues
from videoforge_domain.rejection import RejectionReason, reasons_for
from videoforge_domain.timing import WordTiming
from videoforge_persistence.models import (
    Artifact,
    ArtifactVersion,
    CharacterReference,
    GenerationJob,
    Scene,
    Series,
    SeriesCharacter,
    SeriesStyle,
    VideoProject,
)
from videoforge_persistence.projection import get_pipeline
from videoforge_persistence.repositories import VersionStatusRow
from videoforge_shared.enums import (
    ArtifactKind,
    ArtifactState,
    BrandingStatus,
    SceneKind,
)
from videoforge_shared.tasks import STAGE_TASKS

__all__ = [
    "ApproveCharacterRequest",
    "ApproveManyRequest",
    "ArtifactDetail",
    "ArtifactSummary",
    "BatchReviewResult",
    "BrandingDetail",
    "CaptionCuePreview",
    "CharacterSummary",
    "CommentRequest",
    "ContactSheet",
    "ContactTile",
    "CreateCharacterRequest",
    "CreateProjectRequest",
    "CreateStyleRequest",
    "EditContentRequest",
    "GenerateRequest",
    "JobResponse",
    "ProjectDetail",
    "ProjectSummary",
    "ReferenceSummary",
    "ReviewRequest",
    "SeriesSummary",
    "SkippedApprovalDetail",
    "StyleSummary",
    "VersionDetail",
    "VersionSummary",
]


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #


class CreateProjectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    topic: str = Field(min_length=1, max_length=500)
    series_id: str | None = None
    title: str | None = Field(default=None, max_length=200)


class GenerateRequest(BaseModel):
    """``POST /projects/{id}/generations`` (SADD §19.1)."""

    model_config = ConfigDict(extra="forbid")

    stage: ArtifactKind
    scene_id: str | None = None
    #: True when the caller knows a version exists and wants another. Drives
    #: which FSM event is applied, so the machine can refuse a regeneration
    #: the artifact's state does not allow.
    regenerate: bool = False
    #: Cap a per-scene fan-out at the first N scenes. Zero means every scene.
    #:
    #: A testing affordance for the expensive stages (M3-07): a twenty-scene
    #: image run costs real money, and trying a convention on three scenes
    #: first is the ordinary way to work. Carried into the job's
    #: ``input_snapshot`` rather than read from configuration, so the record of
    #: a run says it was limited — an environment variable would make a
    #: five-image project and a truncated twenty-image one indistinguishable
    #: afterwards.
    max_scenes: int = Field(default=0, ge=0, le=1000)


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    comment: str | None = Field(default=None, max_length=4000)
    #: Why, from a fixed vocabulary (M3-10). Validated **here**, at the
    #: boundary, so a bad value is a 400 rather than a row the worker later
    #: cannot interpret — while the worker still tolerates unknown strings,
    #: because rows outlive the vocabulary that wrote them.
    #:
    #: Meaningful on a rejection; harmless on an approval, where "approved,
    #: but the hands are odd" is a real thing to record.
    reasons: list[RejectionReason] = Field(default_factory=list, max_length=9)
    #: Optimistic concurrency (§19.1). Two tabs, or a regeneration that landed
    #: while the reviewer was reading, must not let an approval apply to
    #: content nobody looked at. Optional so a script can omit it; the UI
    #: always sends it.
    expected_version_no: int | None = None


class EditContentRequest(BaseModel):
    """``PUT /artifacts/{id}/content`` — a human edit becomes a new version."""

    model_config = ConfigDict(extra="forbid")

    content: dict[str, Any]


class CommentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=4000)
    anchor: dict[str, Any] | None = None


# --------------------------------------------------------------------------- #
# Responses
# --------------------------------------------------------------------------- #


class VersionSummary(BaseModel):
    id: str
    version_no: int
    origin: str
    #: **Derived**, from ``artifact_version_status`` (finding B1) — never a
    #: stored column. Present on every version so the review UI can render a
    #: version switcher without a request per version.
    status: str
    created_at: datetime
    created_by: str | None = None
    prompt_template_ref: str | None = None
    provider_ref: str | None = None

    @classmethod
    def of(
        cls, version: ArtifactVersion, status: VersionStatusRow | None
    ) -> VersionSummary:
        return cls(
            id=version.id,
            version_no=version.version_no,
            origin=version.origin.value,
            status=status.status.value if status else "AWAITING_APPROVAL",
            created_at=version.created_at,
            created_by=version.created_by,
            prompt_template_ref=version.prompt_template_ref,
            provider_ref=version.provider_ref,
        )


class CaptionCuePreview(BaseModel):
    """One caption, as the review player should show it.

    Structurally the timeline's ``CaptionCue``, and deliberately not imported
    from it: the backend does not depend on ``videoforge_timeline``, and this
    is not a timeline — it is a preview of what the burn will say, available
    before any timeline exists.
    """

    text: str
    start_ms: int
    end_ms: int


class VersionDetail(VersionSummary):
    content: dict[str, Any] | None = None
    storage_key: str | None = None
    content_hash: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)
    parent_version_id: str | None = None
    #: Where the bytes are, for versions that have any (M4-11).
    #:
    #: **Built here, like ``ContactTile``'s**, and for the reason that DTO
    #: already states: which bucket media lives in is a server fact (ADR-011).
    #: The review screen used to compose `/assets/assets/{key}` itself, which
    #: was right for images and voice and wrong for a **render** — those go to
    #: the artifacts bucket, so the client's guess would have 403'd on the
    #: first video the pipeline ever produced.
    asset_url: str | None = None
    #: Captions as the render will burn them, for versions that carry word
    #: timings — today, ``voice``.
    #:
    #: **Derived here rather than stored**, which is the opposite of what
    #: ``meta.spans`` does, and for a reason. Spans are a *measurement* of an
    #: approved narration: re-deriving them later against a different build
    #: would silently re-time audio a human already signed off. Grouping is a
    #: *presentation* choice made fresh at every timeline compile, so a cue
    #: stored beside the voice would go stale the moment the rules changed and
    #: would then disagree with the very thing it claims to preview.
    #:
    #: Sent from the server for the same reason ``capabilities`` and
    #: ``rejection_reasons`` are: one implementation of the rule
    #: (:func:`videoforge_domain.captions.group_into_cues`), read by the
    #: compiler and by this response. Grouping in TypeScript would be the
    #: second implementation S8 was withdrawn to avoid — and the reason the
    #: player showed one word at a time while the burn showed phrases.
    caption_cues: list[CaptionCuePreview] = Field(default_factory=list)

    @classmethod
    def of_detail(
        cls,
        version: ArtifactVersion,
        status: VersionStatusRow | None,
        *,
        bucket: str | None = None,
    ) -> VersionDetail:
        summary = VersionSummary.of(version, status)
        meta = dict(version.meta or {})
        return cls(
            **summary.model_dump(),
            content=version.inline_content,
            storage_key=version.storage_key,
            content_hash=version.content_hash,
            meta=meta,
            parent_version_id=version.parent_version_id,
            asset_url=(
                f"/assets/{bucket}/{version.storage_key}"
                if bucket and version.storage_key
                else None
            ),
            caption_cues=_caption_cues(meta),
        )


def _caption_cues(meta: dict[str, Any]) -> list[CaptionCuePreview]:
    """Group a voice version's stored spans into the cues the burn will show.

    Per span, never across one: a cue spanning a scene change would stay on
    screen while the image cut underneath it. The compiler enforces the same
    boundary by construction, by grouping one scene's words at a time.

    **Card scenes are skipped**, matching ``CompileOptions.caption_cards``:
    a card is already text on screen and a caption over it competes with the
    words the scene exists to show. Spans written before M4-01 carry no
    ``kind``, so an absent one reads as ``illustration`` — the same direction
    ``_kind_of`` fails in, and the shape every pre-M4 scene had.

    Grouping knobs are left at their defaults, which is also what
    ``timeline.compile`` passes today — ``CompileOptions`` exposes
    ``caption_max_characters`` and ``caption_target_dwell_ms`` and the stage
    sets neither. The day one side starts tuning them, this side has to read
    the same numbers or the preview stops being one.

    Anything malformed yields no cues rather than a 500: this is a preview
    beside a working audio player, and a reviewer who can still listen has
    lost less than one who gets an error page.
    """
    spans = meta.get("spans")
    if not isinstance(spans, list):
        return []

    cues: list[CaptionCuePreview] = []
    for span in spans:
        if not isinstance(span, dict):
            continue
        if str(span.get("kind") or SceneKind.ILLUSTRATION) == SceneKind.CARD:
            continue
        words = [
            WordTiming(
                text=str(word["text"]),
                start_ms=int(word["start_ms"]),
                end_ms=int(word["end_ms"]),
                # Unused by the grouping, which reads text and times only. The
                # offset is into the original script and no longer recoverable
                # from what was stored.
                offset=0,
            )
            for word in span.get("words") or []
            if isinstance(word, dict) and {"text", "start_ms", "end_ms"} <= word.keys()
        ]
        cues.extend(
            CaptionCuePreview(text=cue.text, start_ms=cue.start_ms, end_ms=cue.end_ms)
            for cue in group_into_cues(words)
        )
    return cues


class ArtifactSummary(BaseModel):
    id: str
    kind: str
    scene_ref: str | None = None
    state: str
    current_version_no: int
    stale_since: datetime | None = None
    #: What the FSM permits right now. The UI renders buttons from this and
    #: never decides for itself (§11).
    capabilities: dict[str, bool]
    #: Which structured rejection reasons apply to **this kind** of artifact.
    #:
    #: Sent for the same reason ``capabilities`` is: the server owns the rule
    #: and the client renders what it is given. The review screen used to
    #: render one hardcoded list on every rejectable artifact, so a reviewer
    #: rejecting a narration was offered "Anatomy" and "Text in image" — nine
    #: image failure modes, none of which a voice take can have. Empty is a
    #: real answer and means "comment only".
    rejection_reasons: list[str] = Field(default_factory=list)

    @classmethod
    def of(cls, artifact: Artifact) -> ArtifactSummary:
        kind = ArtifactKind(artifact.kind)
        return cls(
            id=artifact.id,
            kind=kind.value,
            scene_ref=artifact.scene_ref,
            state=artifact.state.value,
            current_version_no=artifact.current_version_no,
            stale_since=artifact.stale_since,
            capabilities=capabilities(ArtifactState(artifact.state)),
            rejection_reasons=[reason.value for reason in reasons_for(kind)],
        )


class ArtifactDetail(ArtifactSummary):
    versions: list[VersionSummary] = Field(default_factory=list)

    @classmethod
    def of_detail(
        cls, artifact: Artifact, versions: list[VersionSummary]
    ) -> ArtifactDetail:
        return cls(**ArtifactSummary.of(artifact).model_dump(), versions=versions)


class ProjectSummary(BaseModel):
    id: str
    topic: str
    title: str | None = None
    phase: str
    created_at: datetime

    @classmethod
    def of(cls, project: VideoProject) -> ProjectSummary:
        return cls(
            id=project.id,
            topic=project.topic,
            title=project.title,
            phase=project.phase.value,
            created_at=project.created_at,
        )


class StageSummary(BaseModel):
    """One pipeline stage, as the UI needs to see it (M2-13).

    The DAG is server-side (ADR-009) and stays there. Without this, a client
    wanting to know whether "Generate scenes" should be enabled would have to
    reimplement the dependency graph in TypeScript — the same drift the
    ``capabilities`` payload exists to prevent, one level up.

    ``unmet`` is the reason, not just the fact. "Waiting on: script" is an
    answer; a disabled button is a puzzle.
    """

    kind: str
    queue: str
    state: str | None = None
    artifact_id: str | None = None
    stale_since: datetime | None = None
    #: Kinds that must be APPROVED before this stage may run.
    requires: list[str] = Field(default_factory=list)
    #: Which of those are not approved yet. Empty means runnable.
    unmet: list[str] = Field(default_factory=list)
    can_generate: bool = False


class SceneSummary(BaseModel):
    """A scene of the approved scene set, for per-scene review (M2-13).

    Sent with the project so the UI can label a scene selector without
    fetching twenty artifacts to find out what they are about. ``narration``
    is the label a reviewer actually recognises — "Scene 4" alone is a number.
    """

    id: str
    index: int
    narration: str
    #: M4-01. ``card`` scenes have no generated image, so a UI that offered
    #: Regenerate on one would be offering an action the worker refuses.
    kind: str = SceneKind.ILLUSTRATION.value
    #: The words on the card, in full — never truncated. It is at most 60
    #: characters by constraint, and it is the entire content of the frame.
    card_text: str | None = None

    @classmethod
    def of(cls, scene: Scene) -> SceneSummary:
        text = scene.narration_text.strip()
        return cls(
            id=scene.id,
            index=scene.index,
            # Truncated here rather than in CSS: the wire payload for twenty
            # scenes is otherwise the whole script again, sent on every poll.
            narration=text if len(text) <= 90 else text[:87] + "…",
            kind=scene.kind.value,
            card_text=scene.card_text,
        )


class ProjectDetail(ProjectSummary):
    series_id: str | None = None
    #: A cache of the status view (B1), exposed for convenience. Clients
    #: needing certainty read each version's ``status`` instead.
    active_pointers: dict[str, Any] = Field(default_factory=dict)
    artifacts: list[ArtifactSummary] = Field(default_factory=list)
    #: The pipeline, in dependency order, with this project's progress on it.
    stages: list[StageSummary] = Field(default_factory=list)
    #: Scenes of the approved scene set, empty until one is approved.
    scenes: list[SceneSummary] = Field(default_factory=list)

    @classmethod
    def of_detail(
        cls,
        project: VideoProject,
        artifacts: list[Artifact],
        scenes: list[Scene] | None = None,
    ) -> ProjectDetail:
        summary = ProjectSummary.of(project)
        return cls(
            **summary.model_dump(),
            series_id=project.series_id,
            active_pointers=dict(project.active_pointers or {}),
            artifacts=[ArtifactSummary.of(a) for a in artifacts],
            stages=_stages(artifacts),
            scenes=[SceneSummary.of(s) for s in scenes or []],
        )


def _stages(artifacts: list[Artifact]) -> list[StageSummary]:
    """Project the pipeline graph onto one project's artifacts.

    Approval is read from ``artifact.state``, not from ``active_pointers``: the
    pointer column is a cache (B1) and this decides whether a button is
    enabled, which the service will then independently enforce.

    Per-scene artifacts collapse to the *least advanced* of their kind, the
    same rule phase derivation uses — nineteen approved images and one still
    generating is not an approved image stage.
    """
    pipeline = get_pipeline()
    by_kind: dict[ArtifactKind, Artifact] = {}
    for artifact in artifacts:
        kind = ArtifactKind(artifact.kind)
        current = by_kind.get(kind)
        if current is None or _rank(artifact) < _rank(current):
            by_kind[kind] = artifact

    approved = {
        kind
        for kind, artifact in by_kind.items()
        if ArtifactState(artifact.state) is ArtifactState.APPROVED
    }

    summaries: list[StageSummary] = []
    for stage in pipeline.stages:
        found = by_kind.get(stage.produces)
        unmet = sorted(k.value for k in pipeline.unmet(stage.produces, approved))
        state = ArtifactState(found.state) if found else None
        summaries.append(
            StageSummary(
                kind=stage.produces.value,
                queue=stage.queue,
                state=state.value if state else None,
                artifact_id=found.id if found else None,
                stale_since=found.stale_since if found else None,
                requires=sorted(k.value for k in stage.requires),
                unmet=unmet,
                # A stage is runnable when its inputs are approved AND the FSM
                # would accept the move — a stage mid-generation must not offer
                # a second Generate.
                can_generate=(
                    not unmet
                    and stage.produces in STAGE_TASKS
                    and (
                        state is None
                        or can_regenerate(state)
                        or state is ArtifactState.PENDING
                    )
                ),
            )
        )
    return summaries


def _rank(artifact: Artifact) -> int:
    order = {
        ArtifactState.FAILED: 0,
        ArtifactState.PENDING: 1,
        ArtifactState.GENERATING: 2,
        ArtifactState.REJECTED: 3,
        ArtifactState.AWAITING_APPROVAL: 4,
        ArtifactState.APPROVED: 5,
    }
    return order[ArtifactState(artifact.state)]


class JobResponse(BaseModel):
    id: str
    status: str
    task_name: str
    queue: str
    attempt: int
    max_attempts: int
    error: dict[str, Any] | None = None
    queued_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None

    @classmethod
    def of(cls, job: GenerationJob) -> JobResponse:
        return cls(
            id=job.id,
            status=job.status.value,
            task_name=job.task_name,
            queue=job.queue,
            attempt=job.attempt,
            max_attempts=job.max_attempts,
            error=job.error,
            queued_at=job.queued_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
        )


# --------------------------------------------------------------------------- #
# Series branding (M3-06, and the API half of M3-13)
# --------------------------------------------------------------------------- #


class CreateCharacterRequest(BaseModel):
    """A new character version.

    ``immutable_traits`` and ``variable_traits`` are open objects rather than a
    fixed schema: the useful vocabulary for a reductive character convention is
    not known yet (R7), and pinning it into Pydantic now would mean a migration
    every time an operator finds a trait that helps. The *split* is the part
    that matters and it is enforced by having two fields.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    immutable_traits: dict[str, Any] = Field(default_factory=dict)
    variable_traits: dict[str, Any] = Field(default_factory=dict)


class CreateStyleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    fields: dict[str, Any] = Field(default_factory=dict)


class ApproveCharacterRequest(BaseModel):
    """Approving a character optionally chooses its canonical reference sheet.

    Optional because a character can be approved before any sheets exist —
    which is the ordinary path today, with M3-04 unbuilt. Once sheets exist,
    approving without naming a group leaves the character with no reference
    images, and image generation then falls back to text alone.
    """

    model_config = ConfigDict(extra="forbid")

    reference_group_id: str | None = None


class SeriesSummary(BaseModel):
    id: str
    title: str
    created_at: datetime

    @classmethod
    def of(cls, series: Series) -> SeriesSummary:
        return cls(id=series.id, title=series.title, created_at=series.created_at)


class ReferenceSummary(BaseModel):
    """One reference sheet image.

    Carries the storage key rather than bytes or a signed URL: assets are
    served by nginx via X-Accel-Redirect (ADR-011), so the client builds
    ``/api/assets/{bucket}/{key}`` and never receives image data through the
    API.
    """

    id: str
    index: int
    storage_key: str
    pose: str
    angle: str
    expression: str
    shot_type: str

    @classmethod
    def of(cls, reference: CharacterReference) -> ReferenceSummary:
        return cls(
            id=reference.id,
            index=reference.index,
            storage_key=reference.storage_key,
            pose=reference.pose,
            angle=reference.angle,
            expression=reference.expression,
            shot_type=reference.shot_type,
        )


class CharacterSummary(BaseModel):
    id: str
    version_no: int
    name: str
    status: str
    immutable_traits: dict[str, Any] = Field(default_factory=dict)
    variable_traits: dict[str, Any] = Field(default_factory=dict)
    approved_reference_group_id: str | None = None
    created_at: datetime

    @classmethod
    def of(cls, character: SeriesCharacter) -> CharacterSummary:
        return cls(
            id=character.id,
            version_no=character.version_no,
            name=character.name,
            status=BrandingStatus(character.status).value,
            immutable_traits=dict(character.immutable_traits or {}),
            variable_traits=dict(character.variable_traits or {}),
            approved_reference_group_id=character.approved_reference_group_id,
            created_at=character.created_at,
        )


class StyleSummary(BaseModel):
    id: str
    version_no: int
    name: str
    status: str
    fields: dict[str, Any] = Field(default_factory=dict)
    #: The compiled block, exposed so the editor can show what the fields
    #: actually become. Read-only: it is derived by ``compile_style_block`` and
    #: a client that sent one back would be writing prompt text by hand, which
    #: is the thing structured fields exist to prevent.
    prompt_block: str = ""
    created_at: datetime

    @classmethod
    def of(cls, style: SeriesStyle) -> StyleSummary:
        return cls(
            id=style.id,
            version_no=style.version_no,
            name=style.name,
            status=BrandingStatus(style.status).value,
            fields=dict(style.fields or {}),
            prompt_block=style.prompt_block,
            created_at=style.created_at,
        )


class BrandingDetail(BaseModel):
    """A series' branding as the settings screen needs it.

    ``ready`` is the server's answer to "can this series generate images yet?"
    — the same question ``admission.resolve_branding`` answers on the write
    path, so the UI never has to reimplement it. Same contract as
    ``capabilities`` on an artifact, one level up.
    """

    series_id: str
    character: CharacterSummary | None = None
    style: StyleSummary | None = None
    references: list[ReferenceSummary] = Field(default_factory=list)
    characters: list[CharacterSummary] = Field(default_factory=list)
    styles: list[StyleSummary] = Field(default_factory=list)
    ready: bool = False
    #: Why not, when ``ready`` is False. "Waiting on: an approved style" beats
    #: a disabled button with no explanation — the reason S11's `unmet` exists
    #: on `StageSummary`.
    missing: list[str] = Field(default_factory=list)


class ContactTile(BaseModel):
    """One cell of the contact sheet (M3-09).

    Everything the grid needs for one scene, in one row, so a twenty-scene
    sheet is one request rather than twenty. In particular ``asset_url`` is
    built here from the version's ``storage_key``: which bucket media lives in
    is a server fact (ADR-011), and a client composing that path would be a
    second place that has to change when it moves.
    """

    scene_id: str
    scene_index: int
    #: What the scene is about, for a caption under the thumbnail. A grid of
    #: twenty pictures with no labels is a puzzle.
    narration: str
    artifact_id: str | None = None
    state: str | None = None
    stale_since: datetime | None = None
    version_id: str | None = None
    version_no: int | None = None
    #: ``None`` when the scene has no image yet — a hole in the sheet, which is
    #: itself the useful signal.
    asset_url: str | None = None
    capabilities: dict[str, bool] = Field(default_factory=dict)


class ContactSheet(BaseModel):
    """The whole per-scene set of one kind, as a grid (M3-09, risk R9).

    ``pending_version_ids`` is the batch the "approve all remaining" button
    submits. Computed **here** rather than in TypeScript for the reason
    ``capabilities`` is: which versions may be approved is the FSM's answer,
    and a client that filtered the list itself would be a second copy of the
    rule that drifts the first time the machine changes.
    """

    kind: str
    tiles: list[ContactTile] = Field(default_factory=list)
    total: int = 0
    #: How many tiles are waiting on a human right now.
    pending: int = 0
    pending_version_ids: list[str] = Field(default_factory=list)


class ApproveManyRequest(BaseModel):
    """``POST /projects/{id}/reviews/approve-remaining`` (M3-09).

    ``version_ids`` is required and explicit: the client sends what it actually
    displayed. A server-side "approve everything pending" would sweep up a
    scene that regenerated while the reviewer was scrolling — the failure
    ``expected_version_no`` prevents on the single-item path, reintroduced
    twenty at a time.
    """

    model_config = ConfigDict(extra="forbid")

    version_ids: list[str] = Field(min_length=1, max_length=200)
    comment: str | None = Field(default=None, max_length=4000)


class SkippedApprovalDetail(BaseModel):
    version_id: str
    reason: str


class BatchReviewResult(BaseModel):
    """Partial success, stated rather than hidden.

    A stale tile skips with its reason and the rest still land; the alternative
    — failing the whole batch — makes one raced scene cost the reviewer the
    entire pass.
    """

    approved: int = 0
    skipped: list[SkippedApprovalDetail] = Field(default_factory=list)
