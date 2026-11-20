"""Narration playback speed, applied after synthesis.

``eleven_v3`` reads noticeably slower than the turbo model it replaced (a
157-word narration went from 45.7 s to 54.8 s of speech), and a probe of
``voice_settings.speed`` on v3 gave no consistent effect. So the speed is set
here instead, where it does not depend on the model: ffmpeg's ``atempo``
changes speed without changing pitch, and every timing from the provider's
alignment is divided by the same factor.

The voice stage applies it **before** storing anything, so the stored audio,
its per-word timings and ``duration_ms`` always agree. Captions, scene spans
and the render read them unchanged.

ffmpeg runs as a list argv with ``shell=False``, the same boundary
``render.py`` states.
"""

from __future__ import annotations

import subprocess
import tempfile
from collections.abc import Sequence
from pathlib import Path

__all__ = ["TempoError", "change_tempo", "retime"]

_TIMEOUT_S = 60


class TempoError(RuntimeError):
    """ffmpeg could not re-time the narration."""


def retime(seconds: Sequence[float], tempo: float) -> tuple[float, ...]:
    """Timings for audio played ``tempo`` times faster."""
    if tempo <= 0:
        raise ValueError(f"tempo must be positive, got {tempo}")
    return tuple(value / tempo for value in seconds)


def change_tempo(audio: bytes, tempo: float) -> bytes:
    """``audio`` (MP3) played ``tempo`` times faster, pitch kept, as MP3.

    A single ``atempo`` stage covers 0.5-2.0, far wider than the setting
    allows. Re-encoded at 44.1 kHz / 128 kbps, ElevenLabs' default output.
    """
    if tempo <= 0:
        raise ValueError(f"tempo must be positive, got {tempo}")
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        source, destination = root / "in.mp3", root / "out.mp3"
        source.write_bytes(audio)
        try:
            result = subprocess.run(  # noqa: S603 — list argv, shell=False
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(source),
                    "-filter:a",
                    f"atempo={tempo:.4f}",
                    "-codec:a",
                    "libmp3lame",
                    "-b:a",
                    "128k",
                    "-ar",
                    "44100",
                    str(destination),
                ],
                shell=False,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT_S,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise TempoError(f"ffmpeg could not run: {exc}") from exc
        if result.returncode != 0 or not destination.is_file():
            raise TempoError(
                f"ffmpeg atempo={tempo} failed rc={result.returncode}: "
                f"{result.stderr[-500:]}"
            )
        return destination.read_bytes()
