"""Probe ElevenLabs ``eleven_v3`` for the two facts ADR-019 rests on.

1. Does ``/with-timestamps`` return the **written** alignment one-to-one with a
   *tagged* request? If yes, the voice stage strips tags by offset
   (``tag_strip_method = "offset"``). If not, it falls back to a bracket scan,
   and this script says what came back instead.
2. Does it accept ``language_code``? The voice stage sends it whenever
   ``CONTENT_LANGUAGE`` is set.

Costs a few hundred characters of credit. Two checks are opt-in:
``--check-limit`` sends ~8,000 characters, and ``--check-speed`` (~800)
asks whether v3 honours ``voice_settings.speed``.

    ELEVENLABS_API_KEY=... PROVIDERS__VOICE__VOICE_ID=... \\
        uv run python scripts/elevenlabs-v3-probe.py \\
        [--check-speed] [--check-limit]
"""

from __future__ import annotations

import os
import sys
import time

import httpx

from videoforge_domain.voice_direction import (
    DirectedScene,
    SceneText,
    Tag,
    compose,
    strip_tag_characters,
)

_BASE = "https://api.elevenlabs.io/v1"
_MODEL = "eleven_v3"


def _call(
    text: str,
    *,
    language_code: str | None = None,
    speed: float | None = None,
) -> httpx.Response:
    body: dict[str, object] = {"text": text, "model_id": _MODEL}
    if language_code:
        body["language_code"] = language_code
    if speed is not None:
        body["voice_settings"] = {"speed": speed}
    return httpx.post(
        f"{_BASE}/text-to-speech/{os.environ['PROVIDERS__VOICE__VOICE_ID']}"
        "/with-timestamps",
        headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]},
        json=body,
        # The voice stage's own budget (VoiceRequest.timeout_s); long v3
        # requests take minutes, and a shorter wait reports nothing useful.
        timeout=300,
    )


_SPEED_TEXT = (
    "Você já se perguntou por que algumas pessoas guardam cartas por décadas? "
    "Nenhuma delas foi escrita para ser lida por estranhos. Mesmo assim, cada "
    "uma conta uma história que a família preferiu esquecer."
)


def _spoken_seconds(response: httpx.Response) -> float:
    """From the first character to the last: the reading itself, without
    trailing silence."""
    alignment = response.json().get("alignment") or {}
    starts = alignment.get("character_start_times_seconds") or [0.0]
    ends = alignment.get("character_end_times_seconds") or [0.0]
    return float(ends[-1]) - float(starts[0])


def _check_speed() -> None:
    """Does v3 honour ``voice_settings.speed``? The same paragraph at several
    speeds: if the durations do not shrink, it is ignored."""
    baseline: float | None = None
    for speed in (1.0, 1.1, 1.2):
        response = _call(_SPEED_TEXT, language_code="pt", speed=speed)
        if response.status_code != 200:
            print(f"speed {speed}: {response.status_code} {response.text[:200]}")
            continue
        seconds = _spoken_seconds(response)
        baseline = baseline or seconds
        print(f"speed {speed}: {seconds:.2f}s ({seconds / baseline:.0%} of 1.0)")
    out_of_range = _call(_SPEED_TEXT, language_code="pt", speed=1.3)
    print(
        f"speed 1.3 accepted: {out_of_range.status_code == 200} "
        f"({out_of_range.status_code})"
    )


def main() -> int:
    for name in ("ELEVENLABS_API_KEY", "PROVIDERS__VOICE__VOICE_ID"):
        if not os.environ.get(name):
            print(f"set {name}", file=sys.stderr)
            return 2

    scenes = [
        DirectedScene(
            SceneText("a", 1, "Nobody knew what was inside the box."),
            (Tag(0, "whispers"),),
        ),
        DirectedScene(
            SceneText("b", 2, "Until now. It held 762 letters!"),
            (Tag(0, "excited"), Tag(6, "laughs")),
        ),
    ]
    composed = compose(scenes)
    print(f"request: {composed.text!r}")

    response = _call(composed.text)
    print(f"status:  {response.status_code}")
    if response.status_code != 200:
        print(response.text[:500])
        return 1

    alignment = response.json().get("alignment") or {}
    chars = alignment.get("characters") or []
    starts = alignment.get("character_start_times_seconds") or []
    ends = alignment.get("character_end_times_seconds") or []
    written = "".join(chars)

    print(f"aligned: {written!r}")
    print(f"one-to-one with the request: {written == composed.text}")
    kept, _, _, method = strip_tag_characters(composed, chars, starts, ends)
    print(f"strip method: {method}")
    print(f"after stripping: {''.join(kept)!r}")
    clean = " ".join(s.scene.narration for s in scenes)
    print(f"words match the narration: {''.join(kept).split() == clean.split()}")

    lang = _call("[curious] Olá, tudo bem?", language_code="pt")
    print(f"language_code accepted: {lang.status_code == 200} ({lang.status_code})")

    if "--check-speed" in sys.argv:
        _check_speed()

    if "--check-limit" in sys.argv:
        for size in (3_000, 5_000):
            text = ("The tide comes in. " * (size // 19 + 1))[:size]
            started = time.monotonic()
            try:
                status = str(_call(text).status_code)
            except httpx.TimeoutException:
                status = "timed out"
            print(f"{size} characters: {status} ({time.monotonic() - started:.0f}s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
