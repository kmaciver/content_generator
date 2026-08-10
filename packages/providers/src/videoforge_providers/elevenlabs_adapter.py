"""ElevenLabs text-to-speech with alignment (M3-12).

Chosen because word timing is a hard capability gate (B3/S5) and this is the
mainstream TTS API that supplies it. **Verified against the live API on
2026-08-09**, and three facts from that probe shape everything downstream:

* **Timings are per character, not per word.** ``alignment`` is three parallel
  arrays — ``characters``, ``character_start_times_seconds``,
  ``character_end_times_seconds``. Word timings do not exist in the response;
  ``videoforge_domain.timing`` groups them. This is why ``VoiceCaps`` asks
  whether word timings can be *derived* rather than whether words are returned.
* **Audio is MP3**, not WAV — the payload begins ``ID3``.
* **Two alignments come back**, and the choice between them is not cosmetic.
  ``alignment`` maps character-for-character to the text as written (25 entries
  for a 25-character script); ``normalized_alignment`` describes what was
  actually spoken and is padded, and can expand tokens. §1.0.2 found the
  reference videos caption a bare numeral as its own frame, and the probe put
  ``762`` at 0.743-1.533 s as one written token. Captioning from the normalised
  stream would turn it into four words nobody wrote, so this adapter returns
  the **written** alignment and never exposes the other.

Thin, like the other adapters: ``httpx`` rather than the vendor SDK. The whole
surface used here is one POST and three arrays, and a dependency whose own
release cadence can break a worker is a poor trade for that.
"""

from __future__ import annotations

import base64
import time
from typing import Any

from videoforge_providers.models import (
    ProviderError,
    ProviderTimeoutError,
    Usage,
    VoiceCaps,
    VoiceRequest,
    VoiceResult,
)

__all__ = ["DEFAULT_MODEL", "ElevenLabsVoiceProvider"]

#: Chosen by the operator on 2026-08-09 and verified on the same day: it
#: answers ``/with-timestamps`` with the identical alignment shape as
#: ``eleven_multilingual_v2``, for less money and lower latency.
DEFAULT_MODEL = "eleven_turbo_v2_5"

_BASE = "https://api.elevenlabs.io/v1"

#: The documented ceiling for a single request on the turbo models. Declared so
#: the gate can refuse a script too long for one call — B3 requires the whole
#: narration in one synthesis, so silently splitting would give back exactly the
#: concatenated-monotone audio that finding exists to prevent.
_MAX_CHARACTERS = 40_000


class ElevenLabsVoiceProvider:
    """One call, whole script, audio plus per-character alignment."""

    name = "elevenlabs"

    def __init__(
        self,
        *,
        api_key: str,
        voice_id: str,
        model: str = DEFAULT_MODEL,
    ) -> None:
        if not voice_id:
            # Caught here rather than at the first call: a missing voice is a
            # configuration mistake, and the failure should land at startup
            # beside the other configuration failures.
            raise ValueError(
                "elevenlabs requires PROVIDERS__VOICE__VOICE_ID; without it the "
                "API has no voice to speak in"
            )
        self._api_key = api_key
        self._voice_id = voice_id
        self._model = model

    def capabilities(self) -> VoiceCaps:
        """Declared from the live probe, not from the documentation."""
        return VoiceCaps(
            word_timings=True,
            mime_type="audio/mpeg",
            max_characters=_MAX_CHARACTERS,
        )

    def synthesise(self, req: VoiceRequest) -> VoiceResult:
        import httpx

        text = req.text.strip()
        if not text:
            raise ProviderError("nothing to speak", provider=self.name)

        started = time.monotonic()
        try:
            response = httpx.post(
                f"{_BASE}/text-to-speech/{req.voice_id or self._voice_id}"
                "/with-timestamps",
                headers={
                    "xi-api-key": self._api_key,
                    "Content-Type": "application/json",
                },
                json={"text": text, "model_id": req.model_hint or self._model},
                timeout=float(req.timeout_s),
            )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(str(exc), provider=self.name) from exc
        except httpx.HTTPError as exc:
            raise ProviderError(str(exc), provider=self.name, retryable=True) from exc

        if response.status_code != 200:
            # 429 and 5xx are worth retrying; a 4xx is a request this code will
            # keep getting wrong. The middleware owns the policy, so all this
            # does is classify.
            retryable = response.status_code == 429 or response.status_code >= 500
            raise ProviderError(
                f"elevenlabs returned {response.status_code}: {response.text[:300]}",
                provider=self.name,
                retryable=retryable,
            )

        payload: dict[str, Any] = response.json()
        alignment = payload.get("alignment") or {}
        characters = tuple(alignment.get("characters") or ())
        starts = tuple(
            float(v) for v in alignment.get("character_start_times_seconds") or ()
        )
        ends = tuple(
            float(v) for v in alignment.get("character_end_times_seconds") or ()
        )

        if not characters or not starts or not ends:
            # The capability gate promised timings. Their absence at call time
            # means the promise was wrong, and continuing would produce audio
            # nothing downstream can place against a scene.
            raise ProviderError(
                "elevenlabs returned no alignment; captions and scene "
                "boundaries cannot be derived from this response",
                provider=self.name,
            )

        audio = base64.b64decode(payload["audio_base64"])
        return VoiceResult(
            audio=audio,
            mime_type="audio/mpeg",
            characters=characters,
            character_starts_s=starts,
            character_ends_s=ends,
            latency_ms=int((time.monotonic() - started) * 1000),
            usage=Usage(
                # Billed per character of input, which is exactly what was sent.
                unit_cost_estimate=_estimate_cost(len(text), self._model),
            ),
            provider_meta={
                "provider": self.name,
                "model": req.model_hint or self._model,
                "voice_id": req.voice_id or self._voice_id,
                "characters": len(text),
            },
        )


#: **UNVERIFIED.** Per-character list prices vary by plan and are not published
#: per request, so this is an order-of-magnitude placeholder in the same spirit
#: as the image prices — it exists so the S10 daily cap sees a non-zero number,
#: not so anyone can reconcile a bill from it.
_PER_CHARACTER_USD: dict[str, float] = {
    "eleven_turbo_v2_5": 0.00003,
    "eleven_multilingual_v2": 0.00006,
}


def _estimate_cost(characters: int, model: str) -> float:
    return round(characters * _PER_CHARACTER_USD.get(model, 0.00006), 6)
