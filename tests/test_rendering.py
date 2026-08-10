"""M4-09 — the filter graph and the encode argv.

Pure, and the reason this module was split out of the stage: an offset a few
hundred milliseconds wrong produces a video that plays perfectly and is out of
sync, which no exception will ever tell you about.

**Verified against real ffmpeg on 2026-08-09**, at 480×854 with three clips —
one crossfade and one cut. Duration came back at exactly 8.000 s; the frame at
the middle of the blend measured (124, 62, 0) between a red (251, 0, 0) and a
green (0, 126, 0) source, so the blend is real rather than a jump; the frame
after the cut was pure blue with its caption burned; and ``moov`` sat at byte
36 with ``mdat`` at 10117 on the actual bytes.
"""

from __future__ import annotations

import pytest

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
from videoforge_workers.rendering import (
    render_command,
    scene_marks,
    video_filter_graph,
)


def _clip(index: int, start: int, end: int, kind: SceneKind | None = None) -> Clip:
    return Clip(
        scene_id=f"s{index}",
        scene_index=index,
        kind=kind or SceneKind.ILLUSTRATION,
        storage_key=f"ab/abc/{index}.png",
        start_ms=start,
        end_ms=end,
    )


def _timeline(**overrides: object) -> Timeline:
    """Three clips: crossfade, then a cut."""
    fields: dict[str, object] = {
        "project_id": "p",
        "total_ms": 8000,
        "tail_ms": 500,
        "video": TimelineVideo(width=1080, height=1920, fps=30),
        "clips": (_clip(1, 0, 2400), _clip(2, 2000, 5200), _clip(3, 5200, 8000)),
        "transitions": (
            Transition(
                kind=TransitionKind.CROSSFADE,
                from_clip=0,
                start_ms=2000,
                duration_ms=400,
            ),
            Transition(
                kind=TransitionKind.CUT, from_clip=1, start_ms=5200, duration_ms=0
            ),
        ),
        "captions": (CaptionCue(text="hello", start_ms=100, end_ms=900),),
        "audio": (
            AudioTrack(
                role=AudioRole.NARRATION,
                storage_key="cd/cde/n.mp3",
                start_ms=0,
                duration_ms=7500,
                gain=(GainPoint(at_ms=0, gain_db=0.0),),
            ),
        ),
        "source": TimelineSource(scene_set_version_id="ss", voice_version_id="vv"),
    }
    fields.update(overrides)
    return Timeline(**fields)  # type: ignore[arg-type]


def _graph(**overrides: object) -> str:
    return video_filter_graph(_timeline(**overrides), ass_path="/tmp/c.ass")


class TestVideoChain:
    def test_every_branch_is_normalised_before_it_is_joined(self) -> None:
        """``xfade`` refuses inputs whose sample aspect ratios disagree and
        ``concat`` refuses ones whose resolution or frame rate do — both with
        errors that name the filter rather than the frame that caused it."""
        graph = _graph()
        for index in range(3):
            assert f"[{index}:v]scale=1080:1920,setsar=1,fps=30[v{index}]" in graph

    def test_a_crossfade_uses_the_timelines_own_offset(self) -> None:
        """Nothing here recomputes timing. Because consecutive clips overlap by
        exactly the transition duration (M4-03), the accumulated stream's clock
        is the timeline's at every step, so ``offset`` is simply
        ``transition.start_ms``."""
        assert (
            "[v0][v1]xfade=transition=fade:duration=0.400:offset=2.000[x1]" in _graph()
        )

    def test_a_cut_is_a_concat_not_a_zero_length_xfade(self) -> None:
        """**The one that would have been silently wrong.** ``xfade`` requires
        a positive duration, and the obvious workaround — a one-frame blend —
        makes clips overlap in the render that do not overlap in the timeline,
        so the video comes out a frame shorter per cut than the artifact says.
        ``concat``'s output is the sum of its inputs, which is what a cut is."""
        assert "[x1][v2]concat=n=2:v=1:a=0[x2]" in _graph()

    def test_captions_are_burned_after_every_join(self) -> None:
        """A cue may sit across a blend. Burning per branch would fade the
        caption in and out with the picture underneath it."""
        graph = _graph()
        assert graph.index("subtitles=") > graph.index("xfade=")
        assert graph.index("subtitles=") > graph.index("concat=")

    def test_the_output_is_yuv420p(self) -> None:
        """Without it libx264 picks a chroma format Safari and QuickTime
        refuse, and the review screen shows a black rectangle that ffprobe
        insists is a valid video."""
        assert "[sub]format=yuv420p[vout]" in _graph()

    def test_a_single_clip_needs_no_joins(self) -> None:
        timeline = _timeline(
            clips=(_clip(1, 0, 8000),),
            transitions=(),
        )
        graph = video_filter_graph(timeline, ass_path="/tmp/c.ass")
        assert "xfade" not in graph
        assert "concat" not in graph
        assert "[v0]subtitles=" in graph


class TestAudio:
    def test_the_audio_input_follows_every_still(self) -> None:
        """Three clips means the narration is input 3. An off-by-one here maps
        a still to the audio filter and ffmpeg's error names neither."""
        assert "[3:a]volume=" in _graph()

    def test_the_mix_is_padded_to_the_video(self) -> None:
        assert "apad=whole_dur=8.000" in _graph()


class TestPathEscaping:
    def test_colons_in_the_subtitle_path_are_escaped(self) -> None:
        """A filtergraph splits options on ``:``, so an unescaped one truncates
        the ``subtitles`` filter and ffmpeg reports the failure against
        whatever follows it."""
        graph = video_filter_graph(_timeline(), ass_path="/tmp/od:d/c.ass")
        assert "filename=/tmp/od\\:d/c.ass" in graph

    def test_commas_in_the_subtitle_path_are_escaped(self) -> None:
        graph = video_filter_graph(_timeline(), ass_path="/tmp/a,b/c.ass")
        assert "filename=/tmp/a\\,b/c.ass" in graph


class TestCommand:
    def _argv(self) -> list[str]:
        return render_command(
            _timeline(),
            frame_paths=["/t/1.png", "/t/2.png", "/t/3.png"],
            audio_paths=["/t/a.mp3"],
            graph="G",
            out_path="/t/out.mp4",
        )

    def test_it_is_a_list_never_a_string(self) -> None:
        """The standing rule for every subprocess here, and doubly so with a
        filter graph full of shell metacharacters."""
        argv = self._argv()
        assert isinstance(argv, list)
        assert all(isinstance(part, str) for part in argv)

    def test_each_still_is_bounded_by_its_own_window(self) -> None:
        """An image input is infinite without ``-t``, and the bound must be the
        clip's window — the graph's offsets assume each branch is that long."""
        argv = self._argv()
        text = " ".join(argv)
        assert "-loop 1 -t 2.400 -i /t/1.png" in text
        assert "-loop 1 -t 3.200 -i /t/2.png" in text
        assert "-loop 1 -t 2.800 -i /t/3.png" in text

    def test_it_asks_for_faststart(self) -> None:
        assert "+faststart" in self._argv()

    def test_it_maps_both_named_outputs(self) -> None:
        argv = self._argv()
        assert argv[argv.index("-map") + 1] == "[vout]"
        assert "[aout]" in argv

    def test_the_wrong_number_of_frames_is_refused(self) -> None:
        """Caught here rather than by ffmpeg, which would report a missing
        input stream several filters into the graph."""
        with pytest.raises(ValueError, match="3 clips need 3 frames"):
            render_command(
                _timeline(),
                frame_paths=["/t/1.png"],
                audio_paths=["/t/a.mp3"],
                graph="G",
                out_path="/t/out.mp4",
            )

    def test_the_wrong_number_of_audio_files_is_refused(self) -> None:
        with pytest.raises(ValueError, match="audio tracks need as many files"):
            render_command(
                _timeline(),
                frame_paths=["/t/1.png", "/t/2.png", "/t/3.png"],
                audio_paths=[],
                graph="G",
                out_path="/t/out.mp4",
            )


class TestSceneMarks:
    """Where each scene sits in the finished file, for the review player."""

    def test_a_mark_is_the_window_a_clip_owns(self) -> None:
        """**Not the raw clip window.** Clips overlap by the transition, so
        seeking to `clip.start_ms` would land a reviewer mid-dissolve on a
        scene they had not asked for. The mark is the span where the clip is
        the only thing on screen."""
        marks = scene_marks(_timeline())
        # clip 2 runs 2000–5200 and shares a 400ms blend at its start and a
        # cut (0ms) at its end.
        assert (marks[1]["start_ms"], marks[1]["end_ms"]) == (2200, 5200)

    def test_the_first_and_last_marks_reach_the_ends(self) -> None:
        marks = scene_marks(_timeline())
        assert marks[0]["start_ms"] == 0
        assert marks[-1]["end_ms"] == 8000

    def test_marks_do_not_overlap(self) -> None:
        """The property the player relies on to say which scene is on screen."""
        marks = scene_marks(_timeline())
        starts = [int(str(mark["start_ms"])) for mark in marks]
        ends = [int(str(mark["end_ms"])) for mark in marks]
        for earlier_end, later_start in zip(ends, starts[1:], strict=False):
            assert later_start >= earlier_end

    def test_a_card_is_marked_as_one(self) -> None:
        """ "Scene 7 looks wrong" has a very different answer when scene 7 was
        never drawn."""
        timeline = _timeline(
            clips=(
                _clip(1, 0, 2400),
                _clip(2, 2000, 5200, SceneKind.CARD),
                _clip(3, 5200, 8000),
            )
        )
        assert scene_marks(timeline)[1]["kind"] == "card"
