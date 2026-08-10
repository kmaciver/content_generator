"""The Timeline schema — one definition, Pydantic (M4-03).

A timeline is the contract between the compiler and the renderer, and the
*only* thing the renderer is given. Everything it needs is resolved here, in
absolute milliseconds, so that rendering is application rather than judgement.

**S8 is withdrawn, not deferred.** §7.7 planned JSON Schema → Pydantic + TS
codegen for exactly this artifact, and the entire motivation was drift between
a Python compiler and a TypeScript renderer. D4 made the renderer Python, so
the timeline crosses no language boundary; the frontend reads it, if at all,
read-only. Plain Pydantic is the single definition and `datamodel-code-generator`
/ `json-schema-to-typescript` never enter the build. Recorded here so it is not
relitigated when someone finds the SADD section.

**The renderer-neutrality rule** (§2.5) is what keeps D4 reversible after the
fact, and it is a review rule applied to every field in this file:

* transitions are named **abstractly** (``crossfade``), never as an engine's
  filter (``xfade``);
* gains are **baked envelopes** in dB at absolute times (S3), never
  ``duck: true`` for the renderer to interpret;
* every time is an **absolute millisecond offset** from the start of the
  video, never a fraction, a frame index, or an offset relative to something
  else that has to be looked up first.

Anything shaped like one engine's interpolation config is out. The test is
simple: could a second renderer implement this without reading the first one?

----

Two decisions this schema had to make rather than leave to the compiler,
because a compiler that decides them by accident produces a video nobody can
explain.

**1. A crossfade is centred on the scene boundary.**

Scene spans from ``voice.generate`` are contiguous: scene *i* runs to exactly
where scene *i+1* begins. Call that boundary B. A crossfade of duration *t*
occupies ``[B - t/2, B + t/2]``.

The alternative considered first — the blend *beginning* at B — is asymmetric:
the incoming scene pays the whole cost, and its opening words play over an
image that still belongs to the previous scene. Centring splits the cost
evenly, and puts the 50/50 point exactly on the word boundary where the
narration changes subject, which is the one instant at which neither image is
more correct than the other.

The consequence that matters more than the aesthetics: **clips overlap**. Clip
*i* and clip *i+1* both carry material across ``[B - t/2, B + t/2]``, so
``clip.start_ms``/``end_ms`` below describe when a clip's material is on
screen **at all**, blends included — not when it is the only thing on screen.
Consecutive clips therefore overlap by exactly the transition duration, and
:meth:`Timeline._check_clips` asserts it. A schema where clips merely abutted
would force the renderer to re-derive the overlap, which is precisely the kind
of hidden arithmetic the neutrality rule exists to prevent.

**2. The video outlives the audio by ``tail_ms``.**

The last frame holds after the narration stops. Without it a video ends on the
same millisecond as its final consonant, which reads as a file that was cut
off. It is stated as an explicit field rather than a renderer default so that
``total_ms`` is a number the compiler committed to, not one the renderer
happened to produce.

Note that narration audio is usually slightly *longer* than the last word's
end — measured at 98.038 s of MP3 against a 97.989 s final word on
2026-08-09, which is MP3 end padding. The compiler resolves that; the schema
only insists (:meth:`Timeline._check_audio`) that no audio track runs past
``total_ms``, because audio the renderer would have to truncate is a mix
nobody designed.

----

**Music is out of v1** (M4-07, decided 2026-08-09). :class:`AudioTrack` still
carries a gain envelope, and the narration track carries a constant one — so
the mix path and S3's shape are real and exercised rather than sketched. There
is no music track and no ducking computation to get wrong. Adding music later
adds a second track and a non-constant envelope; it does not change this file.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from videoforge_shared.enums import SceneKind

__all__ = [
    "AudioRole",
    "AudioTrack",
    "CaptionCue",
    "Clip",
    "GainPoint",
    "Timeline",
    "TimelineSource",
    "TimelineVideo",
    "Transition",
    "TransitionKind",
]

#: Bumped when a change would make an older renderer misread a newer timeline.
#: Adding an optional field does not; changing what an existing field *means*
#: does. Stored on the artifact, so a timeline compiled months ago still says
#: what it was compiled against.
SCHEMA_VERSION: Literal[1] = 1


class TransitionKind(StrEnum):
    """Abstract names. ``xfade`` is FFmpeg's word for it, not ours."""

    CUT = "cut"
    CROSSFADE = "crossfade"


class AudioRole(StrEnum):
    """What a track is *for*, which is what decides how it is mixed."""

    NARRATION = "narration"
    #: Unused in v1 (M4-07). Declared because the mix is a list of roles rather
    #: than a narration field plus a music field — a shape that would have to
    #: change to add anything, which is the shape that ages badly.
    MUSIC = "music"


class _Frozen(BaseModel):
    """Immutable, and strict about unknown fields.

    ``extra="forbid"`` for the reason M1-08 gives about the API: a timeline
    carrying a field the renderer silently ignores is worse than one that
    fails to parse, because the video renders and is quietly wrong.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class TimelineVideo(_Frozen):
    """The output format. Fixed for a project, but stated rather than assumed —
    a renderer that read these from its own configuration could produce a
    1080×1920 video from frames normalised for something else."""

    width: int = Field(gt=0)
    height: int = Field(gt=0)
    fps: int = Field(gt=0, le=120)


class Clip(_Frozen):
    """One scene's frame, and the window its material is on screen.

    ``start_ms``/``end_ms`` include the blends at either end (see the module
    docstring): consecutive clips overlap by exactly the transition between
    them. The clip is the *only* thing on screen from the end of its incoming
    transition to the start of its outgoing one.
    """

    scene_id: str
    scene_index: int = Field(gt=0)
    #: Provenance, not instruction. The renderer composites a card and an
    #: illustration identically — both are PNG or JPEG frames by the time they
    #: reach it (§1.0 D4: "the renderer never learns what a card is") — but
    #: "why is scene 7 blank" is a much shorter investigation with this here.
    kind: SceneKind
    #: Content-addressed (ADR-004). The renderer fetches through
    #: ``get_bytes_verified``, so a corrupted frame fails the job rather than
    #: the video.
    storage_key: str
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def _check_order(self) -> Clip:
        if self.end_ms <= self.start_ms:
            raise ValueError(
                f"clip for scene {self.scene_index} ends at or before it starts"
            )
        return self


class Transition(_Frozen):
    """A blend between two consecutive clips, at an absolute time.

    A ``CUT`` has ``duration_ms == 0`` and is still emitted. Making the
    no-transition case an explicit entry rather than a gap in the list means
    the renderer walks one sequence instead of reasoning about which
    boundaries have entries and which do not.
    """

    kind: TransitionKind
    #: Index of the outgoing clip in ``Timeline.clips``. The incoming clip is
    #: the next one — a transition is always between neighbours, and carrying
    #: both indices would allow expressing something that cannot be rendered.
    from_clip: int = Field(ge=0)
    start_ms: int = Field(ge=0)
    duration_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def _check_kind(self) -> Transition:
        if self.kind is TransitionKind.CUT and self.duration_ms != 0:
            raise ValueError("a cut has no duration")
        if self.kind is TransitionKind.CROSSFADE and self.duration_ms <= 0:
            raise ValueError("a crossfade with no duration is a cut; say so")
        return self


class CaptionCue(_Frozen):
    """One caption, on screen for a window.

    Text is carried **unescaped**. ASS escaping belongs to the writer that
    emits the subtitle file (M4-05), because it is that format's concern —
    putting it here would make the timeline unreadable and would bake one
    renderer's syntax into the neutral contract.
    """

    text: str = Field(min_length=1)
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)

    @model_validator(mode="after")
    def _check_order(self) -> CaptionCue:
        if self.end_ms <= self.start_ms:
            raise ValueError(f"caption {self.text!r} ends at or before it starts")
        return self


class GainPoint(_Frozen):
    """One point on a **baked** gain envelope (S3), in dB at an absolute time.

    dB rather than a linear multiplier because that is the unit a mix is
    reasoned about in, and 0.0 reads unambiguously as "unchanged" where 1.0
    does not.
    """

    at_ms: int = Field(ge=0)
    gain_db: float


class AudioTrack(_Frozen):
    """One audio source and the envelope applied to it.

    The envelope is **resolved at compile time** — this is S3's whole point.
    The renderer interpolates between the points it is given; it never decides
    that narration is playing and music should therefore drop.
    """

    role: AudioRole
    storage_key: str
    start_ms: int = Field(ge=0)
    duration_ms: int = Field(gt=0)
    #: At least one point. A track with an empty envelope would leave the
    #: renderer to choose a default gain, which is a decision.
    gain: tuple[GainPoint, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _check_gain(self) -> AudioTrack:
        times = [point.at_ms for point in self.gain]
        if times != sorted(times):
            raise ValueError(f"{self.role.value} gain envelope is out of order")
        if len(set(times)) != len(times):
            raise ValueError(
                f"{self.role.value} gain envelope has two points at one time"
            )
        return self


class TimelineSource(_Frozen):
    """What this timeline was compiled from (§10.3 rule 4).

    Without it, "why is scene 4 three seconds long?" has no answer once the
    voice artifact moves on. Version ids rather than content, because the
    versions are immutable and already stored.
    """

    scene_set_version_id: str
    voice_version_id: str
    #: scene id → the image version composited for it. A map rather than a
    #: list so a regenerated single scene is visibly one changed entry.
    image_version_ids: dict[str, str] = Field(default_factory=dict)


class Timeline(_Frozen):
    """The compiled video, ready to render.

    Every invariant below is asserted here rather than in the compiler: the
    compiler is one producer, and a hand-written or edited timeline must not
    be able to describe a video that cannot exist.
    """

    schema_version: Literal[1] = SCHEMA_VERSION
    project_id: str
    total_ms: int = Field(gt=0)
    #: How long the last frame holds after the audio stops. Explicit so that
    #: ``total_ms`` is a number the compiler committed to.
    tail_ms: int = Field(ge=0)
    video: TimelineVideo
    clips: tuple[Clip, ...] = Field(min_length=1)
    #: Exactly one fewer than ``clips`` — one per boundary, cuts included.
    transitions: tuple[Transition, ...] = ()
    captions: tuple[CaptionCue, ...] = ()
    audio: tuple[AudioTrack, ...] = Field(min_length=1)
    source: TimelineSource

    @model_validator(mode="after")
    def _check(self) -> Timeline:
        self._check_clips()
        self._check_transitions()
        self._check_captions()
        self._check_audio()
        return self

    def _check_clips(self) -> None:
        """Ordered, starting at zero, ending at ``total_ms``, and overlapping
        by exactly the transition between each pair.

        The overlap check is the one that earns its keep. It is the single
        place the centred-crossfade decision is enforced, and a compiler that
        drifted to abutting clips would otherwise produce a video shorter than
        its own audio by the sum of every transition — a failure that looks
        like a sync bug and is arithmetic.
        """
        if self.clips[0].start_ms != 0:
            raise ValueError("the first clip must start at 0")
        if self.clips[-1].end_ms != self.total_ms:
            raise ValueError(
                f"the last clip ends at {self.clips[-1].end_ms}, "
                f"but total_ms is {self.total_ms}"
            )
        indexes = [clip.scene_index for clip in self.clips]
        if indexes != sorted(indexes):
            raise ValueError("clips are not in scene order")

    def _check_transitions(self) -> None:
        if len(self.transitions) != len(self.clips) - 1:
            raise ValueError(
                f"{len(self.clips)} clips need {len(self.clips) - 1} transitions, "
                f"got {len(self.transitions)}"
            )
        for position, transition in enumerate(self.transitions):
            if transition.from_clip != position:
                raise ValueError(
                    f"transition {position} says it follows clip "
                    f"{transition.from_clip}"
                )
            outgoing, incoming = self.clips[position], self.clips[position + 1]
            overlap = outgoing.end_ms - incoming.start_ms
            if overlap != transition.duration_ms:
                raise ValueError(
                    f"clips {position} and {position + 1} overlap by {overlap}ms "
                    f"but their transition lasts {transition.duration_ms}ms"
                )
            if transition.start_ms != incoming.start_ms:
                raise ValueError(
                    f"transition {position} starts at {transition.start_ms} "
                    f"but the incoming clip starts at {incoming.start_ms}"
                )

    def _check_captions(self) -> None:
        """In order, non-overlapping, and inside the video.

        Overlapping cues are refused rather than tolerated: two captions on
        screen at once is not a thing this format does, and libass would
        render both stacked without complaint.
        """
        previous = 0
        for cue in self.captions:
            if cue.start_ms < previous:
                raise ValueError(f"caption {cue.text!r} overlaps the one before it")
            if cue.end_ms > self.total_ms:
                raise ValueError(f"caption {cue.text!r} runs past the end of the video")
            previous = cue.end_ms

    def _check_audio(self) -> None:
        roles = [track.role for track in self.audio]
        if AudioRole.NARRATION not in roles:
            raise ValueError("a timeline with no narration is not this format")
        if len(roles) != len(set(roles)):
            raise ValueError("two tracks share one role")
        for track in self.audio:
            end = track.start_ms + track.duration_ms
            if end > self.total_ms:
                raise ValueError(
                    f"{track.role.value} runs to {end}ms, past the video's "
                    f"{self.total_ms}ms — the renderer would have to truncate a "
                    "mix nobody designed"
                )
            for point in track.gain:
                if point.at_ms > track.duration_ms:
                    raise ValueError(
                        f"{track.role.value} has a gain point at {point.at_ms}ms, "
                        f"past its own {track.duration_ms}ms"
                    )
