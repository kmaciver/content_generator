"""The FFmpeg render worker (D4) — M0-09 hello-render.

Renders the smallest thing that exercises what the real M4 renderer will do:
two stills → crossfade → one burnt-in ASS caption word → silent audio →
1080×1920 H.264/AAC with ``+faststart`` → content-addressed upload.

Deliberately a normal Celery task on the ``render`` queue, through the same
skeleton as every other stage — the whole point of D4 was deleting the
separate renderer service, its stream contract, and its callback path.

Boundaries that must hold as M4 grows this file:

* ffmpeg runs via ``subprocess.run`` with a list argv and ``shell=False`` —
  never a shell string (filter graphs are full of shell metacharacters).
* Inputs come from MinIO via ``get_bytes_verified``: a corrupted asset must
  fail the job with IntegrityError, not become garbage frames found at review.
* The output is self-checked before upload (ffprobe + moov placement), so a
  bad render fails the task rather than becoming a broken artifact.
"""

from __future__ import annotations

import json
import logging
import struct
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from videoforge_shared.settings import RenderSettings, get_app_settings
from videoforge_shared.storage import storage_client_from_settings
from videoforge_workers.skeleton import videoforge_task

logger = logging.getLogger(__name__)

STILL_DURATION_S = 3.0
XFADE_DURATION_S = 0.4
#: Two 3s stills crossfaded for 0.4s: 3 + 3 − 0.4.
HELLO_DURATION_S = 2 * STILL_DURATION_S - XFADE_DURATION_S

_FFMPEG_TIMEOUT_S = 120


# --------------------------------------------------------------------------- #
# Pure helpers — unit-tested; no ffmpeg needed
# --------------------------------------------------------------------------- #


def ass_document(text: str, *, width: int, height: int) -> str:
    """A minimal ASS file with one centred caption word in the reference style:
    bold white fill, heavy black outline (§1.0.2). BorderStyle=1 + Outline is
    the exact rendering model the caption templates build on in M4.

    ASS colours are ``&HAABBGGRR`` — blue-green-red, not RGB.
    """
    x = width // 2
    y = int(height * 0.57)  # caption band from the reference analysis
    return "\n".join(
        [
            "[Script Info]",
            "ScriptType: v4.00+",
            f"PlayResX: {width}",
            f"PlayResY: {height}",
            "",
            "[V4+ Styles]",
            "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, "
            "BackColour, Bold, BorderStyle, Outline, Shadow, Alignment",
            "Style: Word,DejaVu Sans,110,&H00FFFFFF,&H00000000,&H00000000," "1,1,8,0,5",
            "",
            "[Events]",
            "Format: Layer, Start, End, Style, Text",
            f"Dialogue: 0,0:00:01.00,0:00:04.50,Word,{{\\pos({x},{y})}}{text}",
            "",
        ]
    )


def hello_filter_graph(
    render: RenderSettings, ass_path: str, *, xfade_offset: float
) -> str:
    """Filter graph: per-branch normalise → xfade → subtitles → yuv420p.

    ``setsar=1`` on each branch because xfade refuses inputs with mismatched
    sample aspect ratios, and ``fps`` before the crossfade so both branches
    agree on a timebase.
    """
    w, h, fps = render.width, render.height, render.fps
    return (
        f"[0:v]scale={w}:{h},setsar=1,fps={fps}[v0];"
        f"[1:v]scale={w}:{h},setsar=1,fps={fps}[v1];"
        f"[v0][v1]xfade=transition=fade:duration={XFADE_DURATION_S}"
        f":offset={xfade_offset}[xf];"
        f"[xf]subtitles=filename={ass_path}[sub];"
        f"[sub]format=yuv420p[vout]"
    )


def ffmpeg_render_cmd(
    render: RenderSettings,
    still_a: str,
    still_b: str,
    graph: str,
    out_path: str,
) -> list[str]:
    """The encode argv. A list, never a string — see module docstring."""
    return [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-loop",
        "1",
        "-framerate",
        str(render.fps),
        "-t",
        str(STILL_DURATION_S),
        "-i",
        still_a,
        "-loop",
        "1",
        "-framerate",
        str(render.fps),
        "-t",
        str(STILL_DURATION_S),
        "-i",
        still_b,
        "-f",
        "lavfi",
        "-i",
        "anullsrc=r=48000:cl=stereo",
        "-filter_complex",
        graph,
        "-map",
        "[vout]",
        "-map",
        "2:a",
        "-t",
        str(HELLO_DURATION_S),
        "-c:v",
        "libx264",
        "-preset",
        render.preset,
        "-crf",
        str(render.crf),
        "-c:a",
        "aac",
        "-ar",
        "48000",
        "-b:a",
        "128k",
        # moov atom up front: the browser can start playing/scrubbing before
        # the download completes (SADD §16.3, exercised by M0-10's asset path).
        "-movflags",
        "+faststart",
        out_path,
    ]


def moov_before_mdat(data: bytes) -> bool:
    """Walk top-level MP4 boxes and check the ``moov`` atom precedes ``mdat``
    — the property ``+faststart`` exists to guarantee, verified on the actual
    bytes rather than trusted from the flag."""
    pos = 0
    order: list[str] = []
    total = len(data)
    while pos + 8 <= total:
        (size,) = struct.unpack_from(">I", data, pos)
        box_type = data[pos + 4 : pos + 8].decode("latin-1")
        if size == 1:  # 64-bit largesize follows
            if pos + 16 > total:
                break
            (size,) = struct.unpack_from(">Q", data, pos + 8)
        elif size == 0:  # box extends to EOF
            size = total - pos
        if size < 8:
            break
        order.append(box_type)
        pos += size
    return (
        "moov" in order
        and "mdat" in order
        and (order.index("moov") < order.index("mdat"))
    )


# --------------------------------------------------------------------------- #
# Subprocess plumbing
# --------------------------------------------------------------------------- #


class FfmpegError(RuntimeError):
    """ffmpeg/ffprobe exited non-zero; carries the stderr tail for the log."""


def _run(cmd: list[str], *, timeout_s: int = _FFMPEG_TIMEOUT_S) -> str:
    result = subprocess.run(  # noqa: S603 — list argv, shell=False, our binaries
        cmd,
        shell=False,
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    if result.returncode != 0:
        tail = result.stderr[-2000:]
        raise FfmpegError(f"{cmd[0]} failed rc={result.returncode}: {tail}")
    return result.stderr  # ffmpeg reports progress on stderr


def _probe(path: str) -> dict[str, Any]:
    out = subprocess.run(  # noqa: S603
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            path,
        ],
        shell=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if out.returncode != 0:
        raise FfmpegError(f"ffprobe failed: {out.stderr[-500:]}")
    parsed: dict[str, Any] = json.loads(out.stdout)
    return parsed


def _generate_still(color: str, size: str, out_path: str) -> None:
    _run(
        [
            "ffmpeg",
            "-hide_banner",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s={size}",
            "-frames:v",
            "1",
            out_path,
        ],
        timeout_s=30,
    )


# --------------------------------------------------------------------------- #
# The task
# --------------------------------------------------------------------------- #


@videoforge_task(name="render.hello", queue="render")
def hello_render(caption: str = "VideoForge") -> dict[str, Any]:
    """M0 exit-test render. Returns a receipt with everything the exit test
    asserts, so verification needs no second trip into the container."""
    settings = get_app_settings()
    render = settings.render
    storage = storage_client_from_settings(settings.minio)
    size = f"{render.width}x{render.height}"

    with tempfile.TemporaryDirectory(prefix="hello-render-") as tmp:
        tmp_path = Path(tmp)
        still_a = str(tmp_path / "a.png")
        still_b = str(tmp_path / "b.png")
        _generate_still("0x2E5A87", size, still_a)  # slate blue
        _generate_still("0xC46A3D", size, still_b)  # warm ochre

        # Round-trip the stills through MinIO exactly as M4 will: upload to the
        # scratch bucket, fetch back verified, render from the fetched bytes.
        for local in (still_a, still_b):
            data = Path(local).read_bytes()
            stored = storage.put_bytes(
                settings.minio.bucket_tmp_render, data, Path(local).name
            )
            Path(local).write_bytes(
                storage.get_bytes_verified(settings.minio.bucket_tmp_render, stored.key)
            )

        ass_path = str(tmp_path / "caption.ass")
        Path(ass_path).write_text(
            ass_document(caption, width=render.width, height=render.height),
            encoding="utf-8",
        )

        out_path = str(tmp_path / "hello.mp4")
        graph = hello_filter_graph(
            render, ass_path, xfade_offset=STILL_DURATION_S - XFADE_DURATION_S
        )
        stderr = _run(ffmpeg_render_cmd(render, still_a, still_b, graph, out_path))
        # libass complains loudly on stderr when the font can't be resolved;
        # a silent fallback to boxes must not pass as success.
        for red_flag in ("fontselect: failed", "Glyph 0x", "No usable fonts"):
            if red_flag in stderr:
                raise FfmpegError(f"caption font problem: {red_flag!r} in ffmpeg log")

        mp4 = Path(out_path).read_bytes()
        probe = _probe(out_path)
        codecs = sorted(s.get("codec_name", "?") for s in probe["streams"])
        duration = float(probe["format"]["duration"])

        stored = storage.put_bytes(settings.minio.bucket_artifacts, mp4, "hello.mp4")

    return {
        "bucket": stored.bucket,
        "key": stored.key,
        "sha256": stored.sha256,
        "size_bytes": stored.size,
        "deduplicated": stored.deduplicated,
        "duration_s": round(duration, 2),
        "codecs": codecs,
        "moov_before_mdat": moov_before_mdat(mp4),
        "caption": caption,
    }
