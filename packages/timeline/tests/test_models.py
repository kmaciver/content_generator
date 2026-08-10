"""M4-03 — the Timeline schema and its invariants.

Each test names the *video* the invariant prevents, not the rule it restates.
A schema whose tests only prove that a valid object validates has tested
Pydantic, not the design.

Pure: no database, no provider, no ffmpeg. That is the property the whole
compiler is built on, and it starts here.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from videoforge_shared.enums import SceneKind
from videoforge_timeline import (
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

_FADE = 400
_SOURCE = TimelineSource(
    scene_set_version_id="01SS", voice_version_id="01VV", image_version_ids={}
)


def _clip(index: int, start: int, end: int) -> Clip:
    return Clip(
        scene_id=f"scene-{index}",
        scene_index=index,
        kind=SceneKind.ILLUSTRATION,
        storage_key=f"ab/abc/{index}.png",
        start_ms=start,
        end_ms=end,
    )


def _narration(duration: int) -> AudioTrack:
    return AudioTrack(
        role=AudioRole.NARRATION,
        storage_key="cd/cde/narration.mp3",
        start_ms=0,
        duration_ms=duration,
        # Constant, and deliberately so: music is out of v1 (M4-07), so there
        # is nothing to duck under. The envelope exists, is exercised, and
        # stays the shape a real mix would use.
        gain=(GainPoint(at_ms=0, gain_db=0.0),),
    )


def _timeline(**overrides: object) -> Timeline:
    """Two scenes, one centred crossfade, 8s of narration, 500ms of tail.

    Scene 1 owns 0–4000, scene 2 owns 4000–8000, so the boundary is 4000 and
    a 400ms crossfade runs 3800–4200. Both clips carry material across it,
    which is why they overlap by exactly 400ms.
    """
    fields: dict[str, object] = {
        "project_id": "01PR",
        "total_ms": 8500,
        "tail_ms": 500,
        "video": TimelineVideo(width=1080, height=1920, fps=30),
        "clips": (_clip(1, 0, 4200), _clip(2, 3800, 8500)),
        "transitions": (
            Transition(
                kind=TransitionKind.CROSSFADE,
                from_clip=0,
                start_ms=3800,
                duration_ms=_FADE,
            ),
        ),
        "captions": (CaptionCue(text="Water rises", start_ms=0, end_ms=900),),
        "audio": (_narration(8000),),
        "source": _SOURCE,
    }
    fields.update(overrides)
    return Timeline(**fields)  # type: ignore[arg-type]


class TestTheHappyPath:
    def test_a_two_scene_timeline_validates(self) -> None:
        assert _timeline().total_ms == 8500

    def test_it_round_trips_through_json(self) -> None:
        """The artifact is stored as jsonb and read back by the renderer. A
        model that serialised to something it could not parse would fail in
        the one place there is no test — between two processes."""
        timeline = _timeline()
        assert Timeline.model_validate_json(timeline.model_dump_json()) == timeline


class TestCrossfadeGeometry:
    """The decision recorded in the module docstring, enforced."""

    def test_clips_must_overlap_by_exactly_the_transition(self) -> None:
        """**The invariant that stops a sync bug.**

        Abutting clips (scene 1 ending at 4000, scene 2 starting at 4000) look
        entirely reasonable and produce a video shorter than its own audio by
        the sum of every transition — 3.6s over ten scenes. That reads as a
        drifting sync bug and is arithmetic.
        """
        with pytest.raises(ValidationError, match="overlap by 0ms"):
            _timeline(clips=(_clip(1, 0, 4000), _clip(2, 4000, 8500)))

    def test_the_transition_must_start_where_the_incoming_clip_does(self) -> None:
        with pytest.raises(ValidationError, match="transition 0 starts at"):
            _timeline(
                transitions=(
                    Transition(
                        kind=TransitionKind.CROSSFADE,
                        from_clip=0,
                        start_ms=3900,
                        duration_ms=_FADE,
                    ),
                )
            )

    def test_a_cut_is_still_an_entry(self) -> None:
        """Cuts are emitted, not omitted. The renderer walks one sequence
        rather than reasoning about which boundaries have entries."""
        timeline = _timeline(
            clips=(_clip(1, 0, 4000), _clip(2, 4000, 8500)),
            transitions=(
                Transition(
                    kind=TransitionKind.CUT, from_clip=0, start_ms=4000, duration_ms=0
                ),
            ),
        )
        assert timeline.transitions[0].duration_ms == 0

    def test_a_crossfade_with_no_duration_is_refused(self) -> None:
        """Say ``cut``. Two ways to express one thing is two code paths in
        every consumer."""
        with pytest.raises(ValidationError, match="is a cut; say so"):
            Transition(
                kind=TransitionKind.CROSSFADE, from_clip=0, start_ms=0, duration_ms=0
            )

    def test_a_cut_with_a_duration_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="a cut has no duration"):
            Transition(
                kind=TransitionKind.CUT, from_clip=0, start_ms=0, duration_ms=200
            )

    def test_every_boundary_needs_a_transition(self) -> None:
        with pytest.raises(ValidationError, match="need 1 transitions, got 0"):
            _timeline(transitions=())


class TestSpan:
    def test_the_first_clip_must_start_at_zero(self) -> None:
        """Otherwise the video opens on nothing, for however long."""
        with pytest.raises(ValidationError, match="must start at 0"):
            _timeline(clips=(_clip(1, 200, 4200), _clip(2, 3800, 8500)))

    def test_the_last_clip_must_reach_total_ms(self) -> None:
        """A gap at the end is a black frame the compiler did not intend and
        the renderer would fill silently."""
        with pytest.raises(ValidationError, match="but total_ms is"):
            _timeline(total_ms=9000)

    def test_clips_must_be_in_scene_order(self) -> None:
        with pytest.raises(ValidationError, match="not in scene order"):
            _timeline(clips=(_clip(2, 0, 4200), _clip(1, 3800, 8500)))

    def test_a_clip_cannot_end_before_it_starts(self) -> None:
        with pytest.raises(ValidationError, match="ends at or before it starts"):
            _clip(1, 4000, 4000)


class TestCaptions:
    def test_captions_may_not_overlap(self) -> None:
        """libass would render both, stacked, without complaining."""
        with pytest.raises(ValidationError, match="overlaps the one before it"):
            _timeline(
                captions=(
                    CaptionCue(text="one", start_ms=0, end_ms=900),
                    CaptionCue(text="two", start_ms=800, end_ms=1500),
                )
            )

    def test_captions_may_not_run_past_the_video(self) -> None:
        with pytest.raises(ValidationError, match="past the end of the video"):
            _timeline(captions=(CaptionCue(text="one", start_ms=8000, end_ms=9000),))

    def test_caption_text_is_carried_unescaped(self) -> None:
        """ASS escaping belongs to the writer that emits the subtitle file
        (M4-05). Baking one renderer's syntax into the neutral contract is
        exactly what the neutrality rule forbids."""
        cue = CaptionCue(text="{not an override}", start_ms=0, end_ms=500)
        assert cue.text == "{not an override}"

    def test_a_timeline_may_have_no_captions(self) -> None:
        assert _timeline(captions=()).captions == ()


class TestAudio:
    def test_narration_is_required(self) -> None:
        """A silent video is not this format."""
        music = AudioTrack(
            role=AudioRole.MUSIC,
            storage_key="ef/efg/bed.mp3",
            start_ms=0,
            duration_ms=8000,
            gain=(GainPoint(at_ms=0, gain_db=-18.0),),
        )
        with pytest.raises(ValidationError, match="no narration"):
            _timeline(audio=(music,))

    def test_audio_may_not_run_past_the_video(self) -> None:
        """The renderer would have to truncate a mix nobody designed."""
        with pytest.raises(ValidationError, match="past the video's"):
            _timeline(audio=(_narration(9000),))

    def test_the_tail_lets_the_last_frame_outlive_the_audio(self) -> None:
        """The reason ``tail_ms`` is a field: a video that ends on its final
        consonant reads as a file that was cut off."""
        timeline = _timeline()
        narration = timeline.audio[0]
        assert timeline.total_ms - narration.duration_ms == timeline.tail_ms

    def test_an_empty_gain_envelope_is_refused(self) -> None:
        """It would leave the renderer to choose a default gain, which is a
        decision — and S3 exists to move decisions to compile time."""
        with pytest.raises(ValidationError):
            AudioTrack(
                role=AudioRole.NARRATION,
                storage_key="k",
                start_ms=0,
                duration_ms=1000,
                gain=(),
            )

    def test_a_gain_envelope_must_be_ordered(self) -> None:
        with pytest.raises(ValidationError, match="out of order"):
            AudioTrack(
                role=AudioRole.NARRATION,
                storage_key="k",
                start_ms=0,
                duration_ms=2000,
                gain=(
                    GainPoint(at_ms=1000, gain_db=0.0),
                    GainPoint(at_ms=0, gain_db=-6.0),
                ),
            )

    def test_a_gain_point_past_the_track_is_refused(self) -> None:
        with pytest.raises(ValidationError, match="past its own"):
            _timeline(
                audio=(
                    AudioTrack(
                        role=AudioRole.NARRATION,
                        storage_key="k",
                        start_ms=0,
                        duration_ms=8000,
                        gain=(
                            GainPoint(at_ms=0, gain_db=0.0),
                            GainPoint(at_ms=9000, gain_db=-3.0),
                        ),
                    ),
                )
            )

    def test_two_tracks_cannot_share_a_role(self) -> None:
        with pytest.raises(ValidationError, match="share one role"):
            _timeline(audio=(_narration(8000), _narration(8000)))


class TestNeutrality:
    """§2.5's rule, as far as a test can reach it."""

    def test_transitions_are_named_abstractly(self) -> None:
        """``xfade`` is FFmpeg's word. A timeline naming it would make a
        second renderer either implement FFmpeg's semantics or lie."""
        assert {kind.value for kind in TransitionKind} == {"cut", "crossfade"}

    def test_unknown_fields_are_refused(self) -> None:
        """A field the renderer silently ignores is worse than one that fails
        to parse: the video renders, and is quietly wrong."""
        with pytest.raises(ValidationError):
            _timeline(motion="ken_burns")

    def test_gain_is_baked_not_described(self) -> None:
        """S3. The envelope is dB at absolute times — there is nowhere in this
        schema to write ``duck: true`` and hope."""
        point = _timeline().audio[0].gain[0]
        assert (point.at_ms, point.gain_db) == (0, 0.0)
