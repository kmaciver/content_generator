"""A timeline → an FFmpeg filter graph and argv (M4-09).

Pure. Building the graph is where this milestone is most likely to be subtly
wrong — an offset a few hundred milliseconds out produces a video that plays
fine and is out of sync — so it is separated from the task that runs it and
tested without ffmpeg, exactly as ``imaging`` is separated from ``images``.

**The chain is uniform in shape and not in filter.** Each boundary takes the
accumulated stream and the next clip and produces a new accumulated stream:

    [prev][vN]xfade=transition=fade:duration=D:offset=O[xN]   # crossfade
    [prev][vN]concat=n=2:v=1:a=0[xN]                          # cut

``concat`` rather than a zero-length ``xfade`` because **xfade requires a
positive duration**. The obvious workaround — a one-frame blend — would make
clips overlap in the render that do not overlap in the timeline, so the
finished video would drift a frame shorter per cut than the artifact says it
is. ``concat`` is exact: its output is the sum of its inputs, which is what a
cut means.

**Why the offsets are simply the timeline's.** ``xfade``'s ``offset`` is
measured from the start of the accumulated stream, and its output runs for
``a + b − duration``. Because M4-03 makes consecutive clips overlap by exactly
the transition duration, feeding each clip in at its own window length makes
the accumulated stream's clock identical to the timeline's at every step. So
``offset`` is ``transition.start_ms`` and nothing here recomputes anything —
which is the point of the compiler having resolved it all already.

**Every branch is normalised before it is joined.** ``scale``, ``setsar=1``
and ``fps`` on each input: ``xfade`` refuses inputs whose sample aspect ratios
disagree, and ``concat`` refuses ones whose resolution or frame rate do — both
with errors that name the filter rather than the frame that caused it. M0-09
learned this on two stills; it matters more on twenty.
"""

from __future__ import annotations

from collections.abc import Sequence

from videoforge_timeline import Timeline, TransitionKind
from videoforge_workers.mixing import GainStop, MixTrack, audio_filter_chain

__all__ = [
    "audio_input_index",
    "render_command",
    "scene_marks",
    "video_filter_graph",
]

#: FFmpeg's crossfade. Abstract in the timeline (``crossfade``), named here —
#: this module is where the neutral contract stops being neutral.
_XFADE_TRANSITION = "fade"


def audio_input_index(timeline: Timeline) -> int:
    """Where the narration sits in the argv: after every still."""
    return len(timeline.clips)


def video_filter_graph(timeline: Timeline, *, ass_path: str) -> str:
    """The whole graph — video chain, subtitle burn, and the audio mix."""
    width, height, fps = (
        timeline.video.width,
        timeline.video.height,
        timeline.video.fps,
    )

    parts: list[str] = [
        f"[{index}:v]scale={width}:{height},setsar=1,fps={fps}[v{index}]"
        for index in range(len(timeline.clips))
    ]

    accumulated = "v0"
    for position, transition in enumerate(timeline.transitions):
        incoming = f"v{position + 1}"
        label = f"x{position + 1}"
        if transition.kind is TransitionKind.CROSSFADE:
            parts.append(
                f"[{accumulated}][{incoming}]"
                f"xfade=transition={_XFADE_TRANSITION}"
                f":duration={_seconds(transition.duration_ms)}"
                f":offset={_seconds(transition.start_ms)}[{label}]"
            )
        else:
            parts.append(f"[{accumulated}][{incoming}]concat=n=2:v=1:a=0[{label}]")
        accumulated = label

    # Captions are burned *after* every join, not per clip: a cue may sit
    # across a blend, and burning per branch would fade the caption in and out
    # with the picture underneath it.
    parts.append(f"[{accumulated}]subtitles=filename={_escape_path(ass_path)}[sub]")
    # yuv420p last. Without it libx264 picks a chroma format that Safari and
    # QuickTime refuse, and the review screen shows a black rectangle that
    # ffprobe insists is a valid video.
    parts.append("[sub]format=yuv420p[vout]")

    parts.append(
        audio_filter_chain(
            [
                MixTrack(
                    input_index=audio_input_index(timeline) + offset,
                    start_ms=track.start_ms,
                    gain=tuple(
                        GainStop(at_ms=point.at_ms, gain_db=point.gain_db)
                        for point in track.gain
                    ),
                )
                for offset, track in enumerate(timeline.audio)
            ],
            total_ms=timeline.total_ms,
        )
    )
    return ";".join(parts)


def render_command(
    timeline: Timeline,
    *,
    frame_paths: Sequence[str],
    audio_paths: Sequence[str],
    graph: str,
    out_path: str,
) -> list[str]:
    """The encode argv. **A list, never a string** — the standing rule for
    every subprocess in this codebase, and doubly so here where filenames come
    from content-addressed keys.

    ``-loop 1 -t <window>`` per still: an image input is infinite without a
    bound, and the bound must be the clip's own window — the graph's offsets
    assume each branch is exactly that long.
    """
    if len(frame_paths) != len(timeline.clips):
        raise ValueError(
            f"{len(timeline.clips)} clips need {len(timeline.clips)} frames, "
            f"got {len(frame_paths)}"
        )
    if len(audio_paths) != len(timeline.audio):
        raise ValueError(
            f"{len(timeline.audio)} audio tracks need as many files, "
            f"got {len(audio_paths)}"
        )

    argv = ["ffmpeg", "-hide_banner", "-y"]
    for clip, path in zip(timeline.clips, frame_paths, strict=True):
        argv += [
            "-loop",
            "1",
            "-t",
            _seconds(clip.end_ms - clip.start_ms),
            "-i",
            path,
        ]
    for path in audio_paths:
        argv += ["-i", path]

    argv += [
        "-filter_complex",
        graph,
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        # Belt and braces with `apad`: the mix is padded to the video's length,
        # and this stops a rounding difference in the last frame extending the
        # file past what the timeline promised.
        "-t",
        _seconds(timeline.total_ms),
        "-c:v",
        "libx264",
        "-preset",
        "medium",
        "-crf",
        "20",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "160k",
        # `moov` ahead of `mdat`, so the review screen can start playing before
        # the whole file arrives. M0-10 proved the serving path; this is what
        # makes it worth having. Verified on the actual bytes, not trusted.
        "-movflags",
        "+faststart",
        out_path,
    ]
    return argv


def scene_marks(timeline: Timeline) -> list[dict[str, object]]:
    """Where each scene is in the finished video, for the review player.

    Stored on the render version rather than re-derived by the client — the
    rule M3-12 followed for voice spans, and for the same reason: the player
    and the encoder must agree about where scene 7 is, and they agree by
    reading one number rather than by computing the same thing twice.

    The window reported is the one a clip **owns**: its span minus the half of
    each blend it shares with a neighbour. The raw clip window overlaps the
    next one, so seeking to it would land a reviewer mid-dissolve on a scene
    they had not asked for.
    """
    marks: list[dict[str, object]] = []
    for position, clip in enumerate(timeline.clips):
        incoming = timeline.transitions[position - 1].duration_ms if position > 0 else 0
        outgoing = (
            timeline.transitions[position].duration_ms
            if position < len(timeline.transitions)
            else 0
        )
        marks.append(
            {
                "scene_index": clip.scene_index,
                "scene_id": clip.scene_id,
                "kind": clip.kind.value,
                "start_ms": clip.start_ms + incoming // 2,
                "end_ms": clip.end_ms - outgoing // 2,
            }
        )
    return marks


def _seconds(milliseconds: int) -> str:
    """Milliseconds as seconds, to the millisecond.

    Three decimals rather than more: FFmpeg parses further, but a timeline is
    integer milliseconds and printing 4.200000001 would suggest a precision
    that does not exist.
    """
    return f"{milliseconds / 1000:.3f}"


def _escape_path(path: str) -> str:
    """Make a path safe inside a filtergraph argument.

    A filtergraph splits options on ``:`` and filters on ``,``, so a path
    containing either truncates the ``subtitles`` filter and FFmpeg reports the
    failure against whatever follows. Paths here are ours — a temporary
    directory — but the escape costs nothing and the failure it prevents is
    extremely hard to read.
    """
    return path.replace("\\", "/").replace(":", "\\:").replace(",", "\\,")
