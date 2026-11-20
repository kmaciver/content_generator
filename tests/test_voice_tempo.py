"""``PROVIDERS__VOICE__TEMPO`` — narration sped up after synthesis.

The contract is that the stored audio, its word timings and ``duration_ms``
describe the same recording. Speeding up the audio without its timings would
put every caption late by a growing amount.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from sqlalchemy.orm import Session, sessionmaker
from tests.test_timeline_stage import _approve, _run
from tests.test_voice_direction_stage import (  # noqa: F401 — fixture
    _voice_meta,
    scened,
)

from videoforge_shared.enums import ArtifactKind
from videoforge_shared.settings import VoiceProviderConfig
from videoforge_workers.tempo import change_tempo, retime

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed in this image"
)


def _seconds(audio: bytes) -> float:
    with tempfile.NamedTemporaryFile(suffix=".mp3") as handle:
        Path(handle.name).write_bytes(audio)
        out = subprocess.run(  # noqa: S603
            ["ffprobe", "-v", "error", "-print_format", "json", "-show_format"]
            + [handle.name],
            capture_output=True,
            text=True,
            check=True,
        )
    return float(json.loads(out.stdout)["format"]["duration"])


def _tone(seconds: float) -> bytes:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "tone.mp3"
        subprocess.run(  # noqa: S603
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi"]
            + ["-i", f"sine=frequency=220:duration={seconds}", str(path)],
            check=True,
        )
        return path.read_bytes()


class TestRetime:
    def test_timings_shrink_by_the_tempo(self) -> None:
        assert retime((0.0, 1.0, 2.5), 1.25) == (0.0, 0.8, 2.0)

    def test_a_non_positive_tempo_is_refused(self) -> None:
        with pytest.raises(ValueError):
            retime((1.0,), 0)


class TestSetting:
    def test_the_default_leaves_audio_alone(self) -> None:
        assert VoiceProviderConfig().tempo == 1.0

    @pytest.mark.parametrize("tempo", [0.5, 1.31, 2.0])
    def test_tempos_that_distort_speech_are_refused(self, tempo: float) -> None:
        with pytest.raises(ValidationError):
            VoiceProviderConfig(tempo=tempo)


@needs_ffmpeg
class TestChangeTempo:
    def test_audio_gets_shorter_by_the_tempo(self) -> None:
        faster = change_tempo(_tone(4.0), 1.25)
        assert _seconds(faster) == pytest.approx(3.2, abs=0.1)


@needs_ffmpeg
@pytest.mark.integration
class TestVoiceStage:
    def test_timings_and_duration_follow_the_audio(
        self,
        monkeypatch: pytest.MonkeyPatch,
        sessions: sessionmaker[Session],
        scened: str,  # noqa: F811 - the imported fixture
    ) -> None:
        import videoforge_workers.voice as voice

        stored: list[bytes] = []
        real_storage = voice.storage

        def _recording_storage() -> Any:
            client = real_storage()
            original = client.put_bytes

            def _put(bucket: str, data: bytes, name: str) -> Any:
                stored.append(data)
                return original(bucket, data, name)

            monkeypatch.setattr(client, "put_bytes", _put)
            return client

        monkeypatch.setattr(voice, "storage", _recording_storage)

        _run(monkeypatch, sessions, scened, ArtifactKind.VOICE_DIRECTION)
        _approve(sessions, scened, ArtifactKind.VOICE_DIRECTION)
        _run(monkeypatch, sessions, scened, ArtifactKind.VOICE)
        normal = _voice_meta(sessions, scened)

        monkeypatch.setenv("PROVIDERS__VOICE__TEMPO", "1.25")
        _run(monkeypatch, sessions, scened, ArtifactKind.VOICE, regenerate=True)
        faster = _voice_meta(sessions, scened)

        assert normal["tempo"] == 1.0 and faster["tempo"] == 1.25
        assert faster["duration_ms"] == round(normal["duration_ms"] / 1.25)

        def last_word_end(meta: dict[str, Any]) -> int:
            return int(meta["spans"][-1]["words"][-1]["end_ms"])

        assert last_word_end(faster) == pytest.approx(
            last_word_end(normal) / 1.25, abs=2
        )
        # The stored audio really is the shorter recording, not the original
        # with relabelled timings.
        assert len(stored) == 2
        assert _seconds(stored[1]) == pytest.approx(
            _seconds(stored[0]) / 1.25, rel=0.05
        )
