"""M4-10 — checking the rendered picture, frame by frame.

**What this catches that nothing else can.** ffprobe confirms a video is
*a* video: right length, right codecs, right box order. It cannot tell you
that scene 3 is on screen during scene 2's narration, that a crossfade never
blended, or that a caption is sitting off the bottom of the frame. Those are
the failures that reach a viewer, and they are only visible in pixels.

----

**Committed pixel goldens were considered and rejected**, which is a change
from how this ticket was originally written ("extract frames and pixel-diff
them"). Two measurements decided it:

* the encode is **deterministic on one machine** — the same input encoded
  twice gives one sha256 — so a golden would be perfectly stable locally;
* the CI runner is ``ubuntu-latest`` (**x86_64**) and the tooling image here
  is **aarch64**. libx264 takes different assembly paths on the two, and its
  output is not guaranteed identical between them.

A golden committed from one architecture would therefore go red on the other
on its first run, and its failure message would be "these bytes differ" —
which is exactly the uninformative red this ticket warned against. So the
comparison is made against **properties of the picture** rather than against
stored bytes: what colour is on screen, when, and where the white pixels are.
Every assertion below survives a re-encode; none survives an offset error.

The frames come from the **production graph** — ``video_filter_graph`` and
``render_command``, not a hand-written ffmpeg line — so a change to either is
what these tests are actually watching.
"""

from __future__ import annotations

import io
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from PIL import Image

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
from videoforge_workers.subtitles import CAPTION_BAND, CaptionLine, ass_document

pytestmark = pytest.mark.integration

_W, _H, _FPS = 320, 568, 30
_FADE = 400

#: Saturated and far apart in every channel, so "which scene is this?" is a
#: question about one pixel rather than an image-similarity problem. Nothing
#: about the assertions depends on the *values*; only on their being distinct.
_COLOURS = {1: (220, 30, 30), 2: (30, 190, 60), 3: (40, 60, 210)}

#: Scene windows, chosen so both boundaries carry a real crossfade.
_SPANS = {1: (0, 2000), 2: (2000, 4000), 3: (4000, 6000)}
_TOTAL_MS = 6500

_CUES = (
    CaptionCue(text="first caption", start_ms=300, end_ms=1500),
    CaptionCue(text="second caption", start_ms=2300, end_ms=3500),
    CaptionCue(text="third caption", start_ms=4300, end_ms=5500),
)


def _timeline() -> Timeline:
    lead = _FADE // 2
    trail = _FADE - lead
    clips = []
    for index, (start, end) in _SPANS.items():
        clips.append(
            Clip(
                scene_id=f"s{index}",
                scene_index=index,
                kind=SceneKind.ILLUSTRATION,
                storage_key=f"k{index}",
                start_ms=0 if index == 1 else start - lead,
                end_ms=_TOTAL_MS if index == 3 else end + trail,
            )
        )
    transitions = tuple(
        Transition(
            kind=TransitionKind.CROSSFADE,
            from_clip=position,
            start_ms=_SPANS[position + 1][1] - lead,
            duration_ms=_FADE,
        )
        for position in range(len(clips) - 1)
    )
    return Timeline(
        project_id="golden",
        total_ms=_TOTAL_MS,
        tail_ms=500,
        video=TimelineVideo(width=_W, height=_H, fps=_FPS),
        clips=tuple(clips),
        transitions=transitions,
        captions=_CUES,
        audio=(
            AudioTrack(
                role=AudioRole.NARRATION,
                storage_key="a",
                start_ms=0,
                duration_ms=6000,
                gain=(GainPoint(at_ms=0, gain_db=0.0),),
            ),
        ),
        source=TimelineSource(scene_set_version_id="ss", voice_version_id="vv"),
    )


@pytest.fixture(scope="module")
def rendered() -> Iterator[tuple[Path, Timeline]]:
    """One render, reused by every assertion below.

    Module-scoped because the encode is the expensive part and every test here
    asks a different question of the *same* video — which is also the honest
    shape: these are checks on one artifact, not six independent renders.
    """
    timeline = _timeline()
    with tempfile.TemporaryDirectory(prefix="videoforge-golden-") as tmp:
        root = Path(tmp)

        frames = []
        for index, colour in _COLOURS.items():
            path = root / f"f{index}.png"
            Image.new("RGB", (_W, _H), colour).save(path)
            frames.append(str(path))

        audio = root / "a.wav"
        subprocess.run(  # noqa: S603
            [
                "ffmpeg",
                "-hide_banner",
                "-y",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=6",
                str(audio),
            ],
            shell=False,
            capture_output=True,
            timeout=60,
        )

        ass = root / "c.ass"
        ass.write_text(
            ass_document(
                [CaptionLine(c.text, c.start_ms, c.end_ms) for c in timeline.captions],
                width=_W,
                height=_H,
            ),
            encoding="utf-8",
        )

        out = root / "golden.mp4"
        graph = video_filter_graph(timeline, ass_path=str(ass))
        result = subprocess.run(  # noqa: S603
            render_command(
                timeline,
                frame_paths=frames,
                audio_paths=[str(audio)],
                graph=graph,
                out_path=str(out),
            ),
            shell=False,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert result.returncode == 0, result.stderr[-2000:]
        yield out, timeline


def _frame(path: Path, milliseconds: int) -> Image.Image:
    """The frame on screen at a given moment.

    ``-ss`` before ``-i`` seeks the *input*, which for a short file is exact
    and much faster than decoding to the timestamp.
    """
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "f.png"
        subprocess.run(  # noqa: S603
            [
                "ffmpeg",
                "-hide_banner",
                "-y",
                "-ss",
                f"{milliseconds / 1000:.3f}",
                "-i",
                str(path),
                "-frames:v",
                "1",
                str(target),
            ],
            shell=False,
            capture_output=True,
            timeout=60,
        )
        return Image.open(io.BytesIO(target.read_bytes())).convert("RGB")


def _corner(image: Image.Image) -> tuple[int, int, int]:
    """A pixel no caption can reach, so "which scene" never depends on text."""
    return image.getpixel((6, 6))  # type: ignore[return-value]


def _distance(left: tuple[int, int, int], right: tuple[int, int, int]) -> float:
    return sum(abs(a - b) for a, b in zip(left, right, strict=True)) / 3


def _white_rows(image: Image.Image) -> list[int]:
    """Rows containing near-white pixels — the caption fill.

    Near-white rather than exactly white: h264 is lossy, so a burned caption
    comes back at 250-ish rather than 255. The scene colours are saturated and
    nowhere near white, so the discriminator holds with room to spare.
    """
    data = list(image.getdata())
    rows = []
    for y in range(0, image.height, 2):
        offset = y * image.width
        for x in range(0, image.width, 2):
            r, g, b = data[offset + x]
            if r > 230 and g > 230 and b > 230:
                rows.append(y)
                break
    return rows


class TestTheRightSceneAtTheRightTime:
    """The class of bug ffprobe is blind to."""

    @pytest.mark.parametrize("scene", [1, 2, 3])
    def test_each_scene_owns_its_marked_window(
        self, rendered: tuple[Path, Timeline], scene: int
    ) -> None:
        """**The sync test.** An offset error moves a picture without changing
        the file's duration, so a video that passes every check in M4-09 can
        still show scene 3 while scene 2 is being narrated.

        Sampled at the midpoint of the window the mark says the scene owns.
        """
        path, timeline = rendered
        mark = next(m for m in scene_marks(timeline) if m["scene_index"] == scene)
        midpoint = (int(str(mark["start_ms"])) + int(str(mark["end_ms"]))) // 2
        assert _distance(_corner(_frame(path, midpoint)), _COLOURS[scene]) < 12

    def test_the_last_frame_holds_after_the_narration(
        self, rendered: tuple[Path, Timeline]
    ) -> None:
        """``tail_ms`` exists so a video does not end on its final consonant.
        If the tail were dropped, this frame would not exist at all."""
        path, _ = rendered
        assert _distance(_corner(_frame(path, _TOTAL_MS - 100)), _COLOURS[3]) < 12


class TestTransitions:
    def test_a_crossfade_actually_blends(self, rendered: tuple[Path, Timeline]) -> None:
        """A blend that silently became a cut keeps every duration correct.
        The midpoint frame must be **neither** source and **between** both."""
        path, timeline = rendered
        transition = timeline.transitions[0]
        middle = transition.start_ms + transition.duration_ms // 2
        pixel = _corner(_frame(path, middle))

        assert _distance(pixel, _COLOURS[1]) > 25
        assert _distance(pixel, _COLOURS[2]) > 25
        for channel in range(3):
            low = min(_COLOURS[1][channel], _COLOURS[2][channel])
            high = max(_COLOURS[1][channel], _COLOURS[2][channel])
            assert low - 12 <= pixel[channel] <= high + 12

    def test_the_blend_is_finished_before_the_next_scene_speaks(
        self, rendered: tuple[Path, Timeline]
    ) -> None:
        """M4-04 caps each crossfade at the pause it sits in, so by the time
        the incoming scene's narration starts its picture is fully up."""
        path, timeline = rendered
        transition = timeline.transitions[0]
        after = transition.start_ms + transition.duration_ms + 60
        assert _distance(_corner(_frame(path, after)), _COLOURS[2]) < 12


class TestCaptions:
    def test_a_caption_sits_in_the_measured_band(
        self, rendered: tuple[Path, Timeline]
    ) -> None:
        """§1.0.2's 57%. A caption drifting to the bottom of the frame is
        invisible to ffprobe and obvious to a viewer."""
        path, _ = rendered
        rows = _white_rows(_frame(path, 800))
        assert rows, "no caption found on a frame that should carry one"

        band = _H * CAPTION_BAND
        assert min(rows) > band - _H * 0.12
        assert max(rows) < band + _H * 0.12

    def test_a_caption_is_gone_once_its_cue_ends(
        self, rendered: tuple[Path, Timeline]
    ) -> None:
        """The first cue ends at 1500ms and the next starts at 2300ms. A
        caption still up at 1900ms means the ASS end times are not being
        honoured — which the file's duration cannot show."""
        path, _ = rendered
        assert _white_rows(_frame(path, 1900)) == []

    def test_every_cue_is_actually_burned(
        self, rendered: tuple[Path, Timeline]
    ) -> None:
        """One sample inside each cue. A caption track that stopped after the
        first line would pass the band test above and fail here."""
        path, timeline = rendered
        for cue in timeline.captions:
            middle = (cue.start_ms + cue.end_ms) // 2
            assert _white_rows(_frame(path, middle)), f"no caption during {cue.text!r}"
