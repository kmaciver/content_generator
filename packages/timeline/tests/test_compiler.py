"""M4-04 — compiling approved media into a timeline.

Pure: no database, no object store, no ffmpeg, no clock. Every test here runs
in microseconds and costs nothing, which is the point — this is the component
that decides how long every frame is on screen, and being wrong here is far
cheaper to find in a unit test than in a render.

The golden file is a regression net, not the specification. The assertions
above it are the specification; the golden catches the changes nobody meant to
make.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from videoforge_domain.timing import WordTiming
from videoforge_shared.enums import SceneKind
from videoforge_timeline import (
    CompileOptions,
    Frame,
    Span,
    Timeline,
    TimelineSource,
    TransitionKind,
    compile_timeline,
)

_GOLDEN = Path(__file__).parent / "golden" / "two_scenes.json"

_SOURCE = TimelineSource(
    scene_set_version_id="01SSVERSION",
    voice_version_id="01VVVERSION",
    image_version_ids={"scene-1": "01IV1", "scene-2": "01IV2"},
)


def _words(raw: tuple[tuple[str, int, int], ...]) -> tuple[WordTiming, ...]:
    return tuple(
        WordTiming(text=text, start_ms=start, end_ms=end, offset=index)
        for index, (text, start, end) in enumerate(raw)
    )


_SCENE_1_WORDS = _words(
    (
        ("You", 0, 174),
        ("give", 174, 336),
        ("your", 336, 511),
        ("kid", 511, 731),
        ("five", 801, 1080),
        ("bucks", 1080, 1289),
        ("for", 1289, 1486),
        ("doing", 1567, 1798),
        ("the", 1798, 1904),
        ("dishes.", 1904, 2612),
    )
)
# Scene 2 opens 400ms after scene 1's last word — a real pause, of the size
# measured between real scenes (175–441ms). The fixture used to be contiguous,
# which is a shape ``scene_spans`` has never produced.
_SCENE_2_WORDS = _words(
    (
        ("It", 3012, 3221),
        ("feels", 3221, 3488),
        ("responsible.", 3488, 4394),
    )
)

#: Where scene 1's narration ends and scene 2's begins.
_PAUSE = (2612, 3012)


def _frame(index: int, kind: SceneKind = SceneKind.ILLUSTRATION) -> Frame:
    return Frame(
        scene_id=f"scene-{index}",
        scene_index=index,
        kind=kind,
        storage_key=f"ab/abcdef/{index:03d}.png",
        version_id=f"01IV{index}",
    )


def _compile(**overrides: object) -> Timeline:
    fields: dict[str, object] = {
        "project_id": "01PROJECT",
        "frames": [_frame(1), _frame(2)],
        "spans": [
            Span(scene_id="scene-1", start_ms=0, end_ms=2612, words=_SCENE_1_WORDS),
            Span(scene_id="scene-2", start_ms=3012, end_ms=4394, words=_SCENE_2_WORDS),
        ],
        "narration_storage_key": "cd/cdefgh/narration.mp3",
        "narration_duration_ms": 4442,
        "source": _SOURCE,
    }
    fields.update(overrides)
    return compile_timeline(**fields)  # type: ignore[arg-type]


class TestGeometry:
    def test_the_video_is_the_audio_plus_the_tail(self) -> None:
        timeline = _compile()
        assert timeline.total_ms == 4442 + 500

    def test_the_audio_decides_the_length_not_the_last_word(self) -> None:
        """MP3 end padding makes the file longer than its final word —
        measured at 98.038 s against 97.989 s. Cutting the video at the word
        would clip the file's own tail."""
        assert _compile().total_ms > 4394 + 500

    def test_the_crossfade_sits_inside_the_pause(self) -> None:
        """The two decisions together. The boundary is the middle of the
        2612–3012 pause, and the blend is capped at the pause's own length —
        so it starts exactly where the speech stops and ends exactly where it
        resumes."""
        transition = _compile().transitions[0]
        assert transition.kind is TransitionKind.CROSSFADE
        assert transition.start_ms == _PAUSE[0]
        assert transition.start_ms + transition.duration_ms == _PAUSE[1]

    def test_clips_overlap_by_exactly_the_transition(self) -> None:
        """The invariant that stops the video being shorter than its audio."""
        first, second = _compile().clips
        assert first.end_ms - second.start_ms == 400

    def test_the_first_clip_starts_at_zero_and_the_last_absorbs_the_tail(
        self,
    ) -> None:
        timeline = _compile()
        assert timeline.clips[0].start_ms == 0
        assert timeline.clips[-1].end_ms == timeline.total_ms

    def test_an_odd_transition_still_overlaps_exactly(self) -> None:
        """Halving an odd duration as a float lands the two sides half a
        millisecond apart and the overlap check fails on a rounding artefact.
        Integer lead/trail is why this passes."""
        timeline = _compile(options=CompileOptions(transition_ms=333))
        first, second = timeline.clips
        assert first.end_ms - second.start_ms == 333

    def test_a_short_pause_shortens_the_crossfade(self) -> None:
        """**Regression, found against real data.**

        ``transition_ms`` is a maximum, not a length. With a fixed 400 ms
        blend, six of nine boundaries in the real narration faded over speech
        — both images half-visible while the incoming scene's opening words
        played — because the pauses there are 175–441 ms.

        Here the pause is 150 ms, so the crossfade is 150 ms. It cannot be
        otherwise: a blend never extends past the silence it sits in.
        """
        timeline = _compile(
            spans=[
                Span(scene_id="scene-1", start_ms=0, end_ms=2612, words=_SCENE_1_WORDS),
                Span(scene_id="scene-2", start_ms=2762, end_ms=4394),
            ]
        )
        transition = timeline.transitions[0]
        assert transition.duration_ms == 150
        assert transition.start_ms == 2612
        assert transition.start_ms + transition.duration_ms == 2762

    def test_touching_spans_become_a_cut(self) -> None:
        """No pause, nowhere for a blend to go. A cut is the honest answer for
        narration that runs straight through — and the cut this format uses
        constantly anyway."""
        timeline = _compile(
            spans=[
                Span(scene_id="scene-1", start_ms=0, end_ms=2612, words=_SCENE_1_WORDS),
                Span(scene_id="scene-2", start_ms=2612, end_ms=4394),
            ]
        )
        assert timeline.transitions[0].kind is TransitionKind.CUT

    def test_no_crossfade_ever_covers_speech(self) -> None:
        """The rule, stated as the property it exists for."""
        timeline = _compile()
        for transition in timeline.transitions:
            assert transition.start_ms >= _PAUSE[0]
            assert transition.start_ms + transition.duration_ms <= _PAUSE[1]

    def test_zero_transition_produces_cuts(self) -> None:
        timeline = _compile(options=CompileOptions(transition_ms=0))
        assert timeline.transitions[0].kind is TransitionKind.CUT
        assert timeline.clips[0].end_ms == timeline.clips[1].start_ms


class TestCaptions:
    def test_captions_are_grouped_phrases(self) -> None:
        assert [cue.text for cue in _compile().captions] == [
            "You give your kid",
            "five bucks for",
            "doing the dishes.",
            "It feels responsible.",
        ]

    def test_no_cue_spans_a_scene_boundary(self) -> None:
        """A caption that outlived its scene would stay on screen while the
        image changed underneath it. Enforced by construction — grouping runs
        per scene — rather than by a rule that could be forgotten."""
        timeline = _compile()
        assert not any(
            cue.start_ms < _PAUSE[0] and cue.end_ms > _PAUSE[1]
            for cue in timeline.captions
        )

    def test_cards_carry_no_captions(self) -> None:
        """A card is already text on screen. A caption over it puts two
        competing pieces of writing in one frame, and the card is the one the
        scene exists to show."""
        timeline = _compile(frames=[_frame(1), _frame(2, SceneKind.CARD)])
        assert all(cue.end_ms <= _PAUSE[0] for cue in timeline.captions)

    def test_cards_can_be_captioned_if_asked(self) -> None:
        timeline = _compile(
            frames=[_frame(1), _frame(2, SceneKind.CARD)],
            options=CompileOptions(caption_cards=True),
        )
        assert any(cue.start_ms >= _PAUSE[1] for cue in timeline.captions)

    def test_the_grouping_is_tunable(self) -> None:
        tight = _compile(options=CompileOptions(caption_max_characters=12))
        assert len(tight.captions) > len(_compile().captions)


class TestRefusals:
    """Loud, not silent. Each of these is a video somebody would have had to
    watch to discover."""

    def test_no_spans_is_refused(self) -> None:
        with pytest.raises(ValueError, match="no voice spans"):
            _compile(spans=[])

    def test_a_span_with_no_frame_is_refused(self) -> None:
        """Otherwise the scene renders as whatever was on screen before it."""
        with pytest.raises(ValueError, match="no approved image"):
            _compile(frames=[_frame(1)])

    def test_overlapping_spans_are_refused(self) -> None:
        """Two scenes claiming the same audio cannot be rendered: one frame
        would have to be on screen while another was too."""
        with pytest.raises(ValueError, match="both claim the narration"):
            _compile(
                spans=[
                    Span(scene_id="scene-1", start_ms=0, end_ms=4000),
                    Span(scene_id="scene-2", start_ms=3500, end_ms=6000),
                ]
            )

    def test_an_inverted_span_is_refused(self) -> None:
        with pytest.raises(ValueError, match="ends at or before it starts"):
            _compile(
                spans=[
                    Span(scene_id="scene-1", start_ms=0, end_ms=0),
                    Span(scene_id="scene-2", start_ms=0, end_ms=4394),
                ]
            )

    def test_spans_out_of_order_are_sorted_not_refused(self) -> None:
        """Order is a serialisation detail; a gap is a defect. Sorting first
        means the contiguity check reports the real problem rather than the
        order it happened to arrive in."""
        timeline = _compile(
            spans=[
                Span(scene_id="scene-2", start_ms=3012, end_ms=4394),
                Span(scene_id="scene-1", start_ms=0, end_ms=2612),
            ]
        )
        assert [clip.scene_index for clip in timeline.clips] == [1, 2]


class TestProvenance:
    def test_the_source_versions_are_carried(self) -> None:
        """§10.3 rule 4. Without it, "why is scene 4 three seconds long?" has
        no answer once the voice artifact moves on."""
        assert _compile().source.voice_version_id == "01VVVERSION"

    def test_narration_carries_a_constant_envelope(self) -> None:
        """M4-07: no music in v1, so nothing to duck under. The envelope is
        real and exercised rather than sketched."""
        gain = _compile().audio[0].gain
        assert [(point.at_ms, point.gain_db) for point in gain] == [(0, 0.0)]


class TestGolden:
    def test_it_matches_the_golden_file(self) -> None:
        """Regression net. Any change to the compiled shape shows up here as a
        diff a human has to accept, which is the whole value — the assertions
        above cover what the compiler is *for*, and this covers what it does
        that nobody thought to assert.

        Regenerate deliberately with ``WRITE_GOLDEN=1``, never reflexively.
        """
        produced = json.loads(_compile().model_dump_json())
        if not _GOLDEN.exists():
            pytest.fail(f"golden missing: {_GOLDEN}")
        assert produced == json.loads(_GOLDEN.read_text())
