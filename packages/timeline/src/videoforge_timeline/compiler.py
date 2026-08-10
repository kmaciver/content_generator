"""Approved artifacts → :class:`Timeline` (M4-04).

**Pure.** No database, no object store, no ffmpeg, no clock. Everything it
needs arrives as arguments and everything it decides comes out in the return
value, which is why its tests run in milliseconds and cost nothing. That is
not a stylistic preference: this is the component that decides how long every
frame is on screen, and it is far cheaper to be wrong here in a unit test than
in a render.

It also explains a decision made in M4-02. Card scenes are pre-rendered to
PNG during the image stage rather than drawn here, so this compiler never
opens a file — if it did, none of the above would be true.

**The three inputs, and where they come from.**

* :class:`Frame` — one per scene, from ``artifact.kind='image'`` rows joined to
  their approved version. Cards and illustrations are indistinguishable here,
  as they are to the renderer.
* :class:`Span` — from ``voice.generate``'s stored ``meta['spans']``. A span
  runs from its scene's first word to its last, so **consecutive spans do not
  touch**: the pause between two sentences belongs to neither scene.
  ``scene_spans`` says so explicitly, and this compiler originally asserted
  the opposite — it was written against contiguous spans and refused the first
  real narration it was given, whose gaps measured 175–441 ms.
* narration key and duration — the audio the whole timeline is pinned to.

That correction improved the design rather than patching it. **The scene
boundary is the midpoint of the pause**, so a crossfade happens during silence
and no word is ever spoken over a half-faded image. Contiguous spans are the
degenerate case of the same rule — the midpoint of a zero-length gap is the
shared instant — so the arithmetic below has one form, not two.

**Failing loudly is the policy.** A scene with no span, a span with no frame, a
span out of order: all raise. The alternative is a video with a frame that
flashes past or a silent gap, discovered by watching it. ``scene_spans`` in
``videoforge_domain.timing`` already follows this rule one layer down; this is
the same rule at the layer that can still do something about it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from videoforge_domain.captions import group_into_cues
from videoforge_domain.timing import WordTiming
from videoforge_shared.enums import SceneKind
from videoforge_timeline.models import (
    AudioRole,
    AudioTrack,
    CaptionCue,
    Clip,
    GainPoint,
    Timeline,
    TimelineSource,
    TimelineVideo,
    Transition,
    TransitionKind,
)

__all__ = [
    "CompileOptions",
    "Frame",
    "Span",
    "compile_timeline",
]

#: Long enough to read as a deliberate dissolve, short enough that a 4-second
#: scene does not spend a fifth of itself blending. Matches the M0-09 spike,
#: which is the only value this pipeline has ever produced a real frame at.
DEFAULT_TRANSITION_MS = 400

#: How long the last frame holds after the audio stops. Without it a video
#: ends on its final consonant, which reads as a file that was cut off.
DEFAULT_TAIL_MS = 500


@dataclass(frozen=True, slots=True)
class Frame:
    """One scene's approved image, whatever produced it."""

    scene_id: str
    scene_index: int
    kind: SceneKind
    storage_key: str
    version_id: str


@dataclass(frozen=True, slots=True)
class Span:
    """One scene's window in the narration, with its words."""

    scene_id: str
    start_ms: int
    end_ms: int
    words: tuple[WordTiming, ...] = ()


@dataclass(frozen=True, slots=True)
class CompileOptions:
    width: int = 1080
    height: int = 1920
    fps: int = 30
    #: The **longest** a crossfade may be, not its length. Each blend is
    #: capped at the pause it sits in, so none ever covers speech — see
    #: :func:`_transition_durations`.
    transition_ms: int = DEFAULT_TRANSITION_MS
    tail_ms: int = DEFAULT_TAIL_MS
    #: Captions are suppressed over card scenes. A card *is* text on screen,
    #: and a caption over it puts two competing pieces of writing in one frame
    #: — the card carries the beat, which is why the scene is a card.
    caption_cards: bool = False
    caption_max_characters: int | None = None
    caption_target_dwell_ms: int | None = None


@dataclass(frozen=True, slots=True)
class _Plan:
    """A clip's window and the boundary that follows it."""

    clip: Clip
    boundary_ms: int | None
    transition_ms: int = 0
    words: tuple[WordTiming, ...] = field(default_factory=tuple)
    kind: SceneKind = SceneKind.ILLUSTRATION


def compile_timeline(
    *,
    project_id: str,
    frames: list[Frame] | tuple[Frame, ...],
    spans: list[Span] | tuple[Span, ...],
    narration_storage_key: str,
    narration_duration_ms: int,
    source: TimelineSource,
    options: CompileOptions | None = None,
) -> Timeline:
    """Compile one project's approved media into a renderable timeline."""
    opts = options or CompileOptions()
    if not spans:
        raise ValueError(f"project {project_id} has no voice spans to compile")

    by_scene = {frame.scene_id: frame for frame in frames}
    ordered = _ordered(spans)

    missing = [span.scene_id for span in ordered if span.scene_id not in by_scene]
    if missing:
        raise ValueError(
            f"project {project_id} has narration for scenes with no approved "
            f"image: {missing}"
        )

    # The video outlives the audio. ``max`` rather than the last span's end
    # because MP3 end padding makes the file slightly longer than its final
    # word — measured at 98.038s of audio against a 97.989s last word — and
    # cutting the video at the word would clip the file's own tail.
    body_ms = max(ordered[-1].end_ms, narration_duration_ms)
    total_ms = body_ms + opts.tail_ms

    plans = _plan_clips(ordered, by_scene, total_ms, opts)

    return Timeline(
        project_id=project_id,
        total_ms=total_ms,
        tail_ms=opts.tail_ms,
        video=TimelineVideo(width=opts.width, height=opts.height, fps=opts.fps),
        clips=tuple(plan.clip for plan in plans),
        transitions=_transitions(plans),
        captions=_captions(plans, opts),
        audio=(
            AudioTrack(
                role=AudioRole.NARRATION,
                storage_key=narration_storage_key,
                start_ms=0,
                duration_ms=narration_duration_ms,
                # Constant: music is out of v1 (M4-07), so there is nothing to
                # duck under. The envelope is real and exercised rather than
                # sketched, and adding music later changes this value, not the
                # shape around it.
                gain=(GainPoint(at_ms=0, gain_db=0.0),),
            ),
        ),
        source=source,
    )


def _ordered(spans: list[Span] | tuple[Span, ...]) -> tuple[Span, ...]:
    """Spans in time order, checked for the one thing they must not do.

    **Gaps are expected**; overlaps are not. A gap is the pause between two
    sentences, which ``scene_spans`` deliberately gives to neither scene. An
    overlap means two scenes claim the same audio, which no correct span set
    can produce and no timeline can express — one frame would have to be on
    screen while another was too.
    """
    ordered = tuple(sorted(spans, key=lambda span: span.start_ms))
    for span in ordered:
        if span.end_ms <= span.start_ms:
            raise ValueError(
                f"scene {span.scene_id} has a span that ends at or before it starts"
            )
    for previous, following in zip(ordered, ordered[1:], strict=False):
        if following.start_ms < previous.end_ms:
            raise ValueError(
                f"scenes {previous.scene_id} and {following.scene_id} both claim "
                f"the narration at {following.start_ms}ms; spans must not overlap"
            )
    return ordered


def _plan_clips(
    spans: tuple[Span, ...],
    by_scene: dict[str, Frame],
    total_ms: int,
    opts: CompileOptions,
) -> tuple[_Plan, ...]:
    """Clip windows, with each crossfade centred on its scene boundary.

    The arithmetic, once, so nothing downstream repeats it. The boundary
    between two scenes is the **midpoint of the pause** between them — which
    puts the blend inside the silence, where no word is spoken over a
    half-faded image. For a boundary B and a transition of *t*, the blend runs
    ``[B - lead, B + trail]`` where ``lead + trail == t``. A clip therefore
    carries material from ``lead`` before its window to ``trail`` after it, and
    consecutive clips overlap by exactly *t* — the invariant :class:`Timeline`
    asserts, and the reason the finished video is as long as its audio plus the
    tail rather than shorter by every transition put together.

    ``lead``/``trail`` are split with integer division rather than by halving
    a float: an odd transition duration otherwise lands the two halves half a
    millisecond apart and the overlap check fails on a rounding artefact.
    """
    boundaries = _boundaries(spans)
    durations = _transition_durations(spans, opts.transition_ms)

    plans: list[_Plan] = []
    last = len(spans) - 1
    for position, span in enumerate(spans):
        frame = by_scene[span.scene_id]
        if position == 0:
            start = 0
        else:
            incoming = durations[position - 1]
            start = boundaries[position - 1] - incoming // 2
        # The last clip runs to the end of the video, absorbing the tail. Every
        # other one ends `trail` past its boundary so the next clip can blend
        # into it.
        if position == last:
            end = total_ms
        else:
            outgoing = durations[position]
            end = boundaries[position] + (outgoing - outgoing // 2)
        plans.append(
            _Plan(
                clip=Clip(
                    scene_id=span.scene_id,
                    scene_index=frame.scene_index,
                    kind=frame.kind,
                    storage_key=frame.storage_key,
                    start_ms=start,
                    end_ms=end,
                ),
                boundary_ms=None if position == last else boundaries[position],
                transition_ms=0 if position == last else durations[position],
                words=span.words,
                kind=frame.kind,
            )
        )
    return tuple(plans)


def _transition_durations(spans: tuple[Span, ...], configured: int) -> tuple[int, ...]:
    """How long each crossfade may be: **as long as its pause allows**.

    ``CompileOptions.transition_ms`` is a maximum, not a length. Measured on
    the first real narration, gaps between scenes ran 175–441 ms, so a fixed
    400 ms crossfade blended over speech at six boundaries out of nine — both
    images half-visible while the incoming scene's opening words play.

    Capping each blend at its own pause makes that impossible: no transition
    can extend past the silence it sits in. Where two spans touch there is no
    silence, so the blend has nowhere to go and the boundary becomes a **cut**
    — which is the honest answer for narration that runs straight through, and
    the cut this format uses constantly anyway.
    """
    ceiling = max(0, configured)
    return tuple(
        min(ceiling, max(0, following.start_ms - previous.end_ms))
        for previous, following in zip(spans, spans[1:], strict=False)
    )


def _boundaries(spans: tuple[Span, ...]) -> tuple[int, ...]:
    """Where each scene hands over to the next: the middle of the pause.

    Measured gaps on the first real narration were 175–441 ms, so a 400 ms
    crossfade usually falls entirely or almost entirely inside silence. Where
    two spans do touch, the midpoint is the shared instant and this reduces to
    a blend centred on the cut — one rule covering both.
    """
    return tuple(
        (previous.end_ms + following.start_ms) // 2
        for previous, following in zip(spans, spans[1:], strict=False)
    )


def _transitions(plans: tuple[_Plan, ...]) -> tuple[Transition, ...]:
    """One per boundary, cuts included.

    A cut is emitted rather than omitted so the renderer walks one sequence
    instead of reasoning about which boundaries have entries.
    """
    transitions: list[Transition] = []
    for position, plan in enumerate(plans[:-1]):
        boundary = plan.boundary_ms
        assert boundary is not None  # only the last plan has none
        duration = plan.transition_ms
        lead = duration // 2
        transitions.append(
            Transition(
                kind=TransitionKind.CROSSFADE if duration else TransitionKind.CUT,
                from_clip=position,
                start_ms=boundary - lead,
                duration_ms=duration,
            )
        )
    return tuple(transitions)


def _captions(plans: tuple[_Plan, ...], opts: CompileOptions) -> tuple[CaptionCue, ...]:
    """Cues, grouped per scene so none can span a scene change.

    Card scenes are skipped by default (:attr:`CompileOptions.caption_cards`):
    a card is already text on screen, and a caption over it competes with the
    words the scene exists to show.
    """
    kwargs = {}
    if opts.caption_max_characters is not None:
        kwargs["max_characters"] = opts.caption_max_characters
    if opts.caption_target_dwell_ms is not None:
        kwargs["target_dwell_ms"] = opts.caption_target_dwell_ms

    cues: list[CaptionCue] = []
    for plan in plans:
        if plan.kind is SceneKind.CARD and not opts.caption_cards:
            continue
        for cue in group_into_cues(plan.words, **kwargs):
            cues.append(
                CaptionCue(text=cue.text, start_ms=cue.start_ms, end_ms=cue.end_ms)
            )
    return tuple(cues)
