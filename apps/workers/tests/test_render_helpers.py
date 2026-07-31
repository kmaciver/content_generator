"""Unit tests for the render worker's pure helpers (M0-09).

The tooling container has no ffmpeg, so execution is the live verification's
job; these pin the parts that are just data — the ASS document, the filter
graph, the argv shape, and the moov-atom parser.
"""

from __future__ import annotations

import struct

from videoforge_shared.settings import RenderSettings
from videoforge_workers.render import (
    HELLO_DURATION_S,
    STILL_DURATION_S,
    XFADE_DURATION_S,
    ass_document,
    ffmpeg_render_cmd,
    hello_filter_graph,
    moov_before_mdat,
)


class TestAssDocument:
    def test_reference_style_essentials(self) -> None:
        doc = ass_document("VideoForge", width=1080, height=1920)
        assert "PlayResX: 1080" in doc
        assert "PlayResY: 1920" in doc
        assert "DejaVu Sans" in doc  # the font the image installs (M0-02)
        assert "&H00FFFFFF" in doc  # white fill
        assert ",1,1,8,0,5" in doc  # bold, BorderStyle=1, outline 8, centred
        assert "VideoForge" in doc

    def test_caption_positioned_in_reference_band(self) -> None:
        doc = ass_document("word", width=1080, height=1920)
        # 57% of 1920 — the caption band measured off the reference (§1.0.2).
        assert "\\pos(540,1094)" in doc


class TestFilterGraph:
    def test_graph_wires_the_full_chain(self) -> None:
        graph = hello_filter_graph(RenderSettings(), "/tmp/x/cap.ass", xfade_offset=2.6)
        assert "scale=1080:1920" in graph
        assert "setsar=1" in graph  # xfade refuses mismatched SAR
        assert "fps=30" in graph
        assert "xfade=transition=fade:duration=0.4:offset=2.6" in graph
        assert "subtitles=filename=/tmp/x/cap.ass" in graph
        assert graph.endswith("format=yuv420p[vout]")

    def test_durations_are_consistent(self) -> None:
        assert HELLO_DURATION_S == 2 * STILL_DURATION_S - XFADE_DURATION_S


class TestFfmpegCommand:
    def test_argv_is_a_list_with_no_shell(self) -> None:
        cmd = ffmpeg_render_cmd(RenderSettings(), "a.png", "b.png", "GRAPH", "o.mp4")
        assert isinstance(cmd, list)
        assert cmd[0] == "ffmpeg"
        assert all(isinstance(part, str) for part in cmd)

    def test_faststart_and_codecs_present(self) -> None:
        cmd = ffmpeg_render_cmd(RenderSettings(), "a.png", "b.png", "G", "o.mp4")
        joined = " ".join(cmd)
        assert "-movflags +faststart" in joined
        assert "libx264" in joined
        assert "aac" in joined
        assert "anullsrc=r=48000:cl=stereo" in joined


def _box(box_type: bytes, payload: bytes = b"") -> bytes:
    return struct.pack(">I", 8 + len(payload)) + box_type + payload


class TestMoovParser:
    def test_faststart_layout_detected(self) -> None:
        mp4 = _box(b"ftyp", b"isom") + _box(b"moov", b"x" * 32) + _box(b"mdat", b"d")
        assert moov_before_mdat(mp4) is True

    def test_non_faststart_layout_rejected(self) -> None:
        mp4 = _box(b"ftyp", b"isom") + _box(b"mdat", b"d" * 64) + _box(b"moov", b"x")
        assert moov_before_mdat(mp4) is False

    def test_garbage_is_not_faststart(self) -> None:
        assert moov_before_mdat(b"not an mp4 at all") is False
        assert moov_before_mdat(b"") is False

    def test_largesize_box_is_walked(self) -> None:
        # size==1 means a 64-bit largesize follows the type.
        mdat_payload = b"d" * 16
        large_mdat = (
            struct.pack(">I", 1)
            + b"mdat"
            + struct.pack(">Q", 16 + len(mdat_payload))
            + mdat_payload
        )
        mp4 = _box(b"ftyp") + _box(b"moov", b"m" * 8) + large_mdat
        assert moov_before_mdat(mp4) is True
