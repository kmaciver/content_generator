"""Probe ElevenLabs ``eleven_v3`` for the two facts ADR-019 rests on.

1. Does ``/with-timestamps`` return the **written** alignment one-to-one with a
   *tagged* request? If yes, the voice stage strips tags by offset
   (``tag_strip_method = "offset"``). If not, it falls back to a bracket scan,
   and this script says what came back instead.
2. Does it accept ``language_code``? The voice stage sends it whenever
   ``CONTENT_LANGUAGE`` is set.

Costs a few hundred characters of credit. The character-limit check is opt-in
(``--check-limit``) because it sends ~8,000 characters.

    ELEVENLABS_API_KEY=... PROVIDERS__VOICE__VOICE_ID=... \\
        uv run python scripts/elevenlabs-v3-probe.py [--check-limit]
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


def _call(text: str, *, language_code: str | None = None) -> httpx.Response:
    body: dict[str, object] = {"text": text, "model_id": _MODEL}
    if language_code:
        body["language_code"] = language_code
    return httpx.post(
        f"{_BASE}/text-to-speech/{os.environ['PROVIDERS__VOICE__VOICE_ID']}"
        "/with-timestamps",
        headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]},
        json=body,
        # The voice stage's own budget (VoiceRequest.timeout_s); long v3
        # requests take minutes, and a shorter wait reports nothing useful.
        timeout=300,
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
