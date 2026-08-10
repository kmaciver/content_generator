"""The mock LLM — a first-class citizen, not a stub.

It is the **default** in ``config/providers.yaml``, which is what makes the
README's promise true: clone, ``make up-prod``, and the whole pipeline runs
with no API key and no possibility of spending money. CI runs on it too, so
the offline path is the one exercised most often rather than a neglected
branch that rots.

Determinism is the design constraint. The same request must produce the same
script, because the seed data, the golden tests and the Playwright run all
depend on it — a mock that returned random text would make every downstream
assertion either flaky or vacuous.
"""

from __future__ import annotations

import hashlib
import json
import struct
import time
import zlib
from typing import Any

from videoforge_providers.models import (
    GeneratedImage,
    ImageCaps,
    ImageRequest,
    ImageResult,
    LLMRequest,
    LLMResult,
    Usage,
    VoiceCaps,
    VoiceRequest,
    VoiceResult,
)
from videoforge_providers.pricing import estimate_image_cost, estimate_llm_cost

__all__ = ["MockImageProvider", "MockVoiceProvider", "MockLLMProvider"]

#: Sentences the fake script is assembled from. Deliberately about the *shape*
#: of an educational short — a hook, some beats, a closing line — because the
#: review UI, the scene splitter (M2) and the caption renderer are all judged
#: against realistic proportions, and lorem ipsum would hide layout problems
#: until a real provider was wired in.
_BEATS = (
    "Here is something most people get wrong about {topic}.",
    "It starts with a simple observation that turns out to matter enormously.",
    "The first piece of the puzzle is easy to overlook.",
    "But once you see it, the rest follows almost immediately.",
    "There is a catch, and it is the reason this took so long to figure out.",
    "The resolution is more elegant than you would expect.",
    "And that is why {topic} works the way it does.",
)


class MockLLMProvider:
    """Deterministic, offline, and shaped like the real thing."""

    name = "mock"

    def __init__(self, *, model: str = "mock-llm-v1", latency_ms: int = 0) -> None:
        self._model = model
        self._latency_ms = latency_ms

    def complete(self, req: LLMRequest) -> LLMResult:
        started = time.monotonic()
        if self._latency_ms:
            # Opt-in only. Off by default so the test suite stays fast; the
            # knob exists because a UI that looks fine against a 0ms provider
            # can still have no loading state at all.
            time.sleep(self._latency_ms / 1000)

        topic = self._topic(req)
        seed = self._seed(req)

        # Vary length by seed so two different topics do not produce
        # identically-shaped output — otherwise a bug that ignores the prompt
        # entirely would look correct.
        beat_count = 4 + (seed % 4)
        body = " ".join(beat.format(topic=topic) for beat in _BEATS[:beat_count])

        parsed: dict[str, Any] | None = None
        if req.response_schema is not None:
            # Synthesised *from the schema* rather than hardcoded.
            #
            # This used to return {"title", "script"} whatever was asked for,
            # which was invisible while script was the only structured stage
            # and became a silent failure the moment scenes asked for
            # something else: the stage got a well-formed object with none of
            # its required keys, and reported "returned no scenes". A mock
            # that ignores the contract is not exercising the offline path,
            # it is exercising a different one.
            parsed = _synthesise(req.response_schema, seed=seed, topic=topic, body=body)
            text = json.dumps(parsed, separators=(",", ":"))
        else:
            text = body

        model = req.model_hint or self._model
        # Rough but non-zero: a spend cap tested against zeros would never
        # trip, and the cap's own test needs something to add up.
        input_tokens = sum(len(m.content) for m in req.messages) // 4
        output_tokens = len(text) // 4

        return LLMResult(
            text=text,
            parsed=parsed,
            usage=Usage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                # Priced through the same table as a real adapter (M3-11), so
                # the metering path is exercised offline. ``mock-llm`` is
                # priced at zero, which is a *deliberate* entry rather than a
                # missing one — see ``pricing.LLM_PRICES``.
                unit_cost_estimate=estimate_llm_cost(
                    model, input_tokens, output_tokens
                ),
            ),
            provider_meta={
                "provider": self.name,
                "model": model,
                "seed": seed,
                "deterministic": True,
            },
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    @staticmethod
    def _topic(req: LLMRequest) -> str:
        """Best-effort topic, taken from the last user turn.

        The mock reads the prompt rather than ignoring it so that a caller who
        forgets to interpolate the topic gets visibly generic output instead of
        something that looks right.
        """
        for message in reversed(req.messages):
            if message.role == "user" and message.content.strip():
                return message.content.strip().splitlines()[-1][:80]
        return "the subject"

    @staticmethod
    def _seed(req: LLMRequest) -> int:
        """Stable hash of the request. ``hash()`` would not do — it is salted
        per process, so the "deterministic" mock would differ between the API
        container and the worker."""
        blob = "|".join(f"{m.role}:{m.content}" for m in req.messages)
        digest = hashlib.sha256(blob.encode()).hexdigest()
        return int(digest[:8], 16)


def _synthesise(
    schema: dict[str, Any], *, seed: int, topic: str, body: str, depth: int = 0
) -> Any:
    """Build a deterministic value satisfying ``schema``.

    Covers the subset this pipeline's stages actually use — objects with typed
    properties, arrays of those, strings and integers. Anything unrecognised
    becomes a string, because a mock that raised on an unfamiliar schema would
    block a new stage on updating the mock first.

    Determinism is preserved by deriving every value from ``seed`` and the
    field's own name, so the same request always produces the same object and
    the golden tests, the seed data and the Playwright run stay stable.
    """
    kind = schema.get("type")

    if kind == "object":
        properties: dict[str, Any] = schema.get("properties") or {}
        required = schema.get("required") or list(properties)
        return {
            name: _synthesise(
                properties[name],
                seed=seed + _offset(name),
                topic=topic,
                body=body,
                depth=depth + 1,
            )
            for name in properties
            if name in required or depth == 0
        }

    if kind == "array":
        # Four to seven items: enough that "the third one" is meaningful, few
        # enough that a failure message stays readable.
        count = 4 + (seed % 4)
        item_schema = schema.get("items") or {"type": "string"}
        return [
            _synthesise(
                item_schema, seed=seed + i, topic=topic, body=body, depth=depth + 1
            )
            for i in range(count)
        ]

    if kind == "integer":
        # Milliseconds is the only integer these schemas ask for, so the range
        # is chosen to look like a scene: 2-6 seconds.
        return 2000 + (seed % 4000)

    if kind == "number":
        return float(2000 + (seed % 4000))

    if kind == "boolean":
        return bool(seed % 2)

    # Strings. At depth 0 the caller wants something script-shaped; nested, a
    # single beat reads more like a field value than a paragraph would.
    if depth <= 1 and len(body) > 120:
        return body
    return _BEATS[seed % len(_BEATS)].format(topic=topic)


def _offset(name: str) -> int:
    """A stable per-field nudge, so two string fields of one object differ.

    Without it every string in a scene would be identical, and a bug that
    copied narration into the visual brief would look correct.
    """
    return int(hashlib.sha256(name.encode()).hexdigest()[:4], 16)


# --------------------------------------------------------------------------- #
# Images (M3-01)
# --------------------------------------------------------------------------- #

#: Ratios the mock renders, mirroring what a real adapter would declare.
#: ``9:16`` first because it is the render target (§D4, 1080×1920).
_MOCK_RATIOS: tuple[str, ...] = ("9:16", "1:1", "16:9")

#: Short edge of the generated image, in pixels. Small on purpose — these bytes
#: pass through MinIO and the review UI in every offline run, and a mock that
#: produced megabytes would make the test suite slow for no added coverage.
_MOCK_SHORT_EDGE = 64


class MockImageProvider:
    """Deterministic images, offline, and **actually decodable**.

    It would be cheaper to return a fixed byte string, or a 1×1 pixel. Both
    were rejected: the bytes travel through content-addressed storage, the
    normalisation step (B2, M3-08) and an ``<img>`` tag in the review UI, and
    every one of those is a place a not-quite-an-image would fail late and
    obscurely. Emitting a real PNG at the requested aspect ratio means the
    offline path exercises the same code the real one will.

    Determinism follows the LLM mock's rule: colour and seed derive from a
    ``sha256`` of the request, never from ``hash()`` (salted per process) or
    from the clock.
    """

    name = "mock"

    def __init__(self, *, model: str = "mock-image-v1") -> None:
        self._model = model

    def capabilities(self) -> ImageCaps:
        """Declares full support, so the mock never fails the M3-01 gate.

        Deliberate: the gate exists to reject a *real* provider that cannot do
        character consistency (ADR-016), and a mock that failed it would make
        the offline path — the default, and the one CI runs — impossible to
        configure.
        """
        return ImageCaps(
            max_reference_images=8,
            supports_seed=True,
            supports_negative_prompt=True,
            aspect_ratios=_MOCK_RATIOS,
            max_images_per_call=8,
        )

    def generate(self, req: ImageRequest) -> ImageResult:
        started = time.monotonic()
        model = req.model_hint or self._model
        base_seed = req.seed if req.seed is not None else _image_seed(req)
        width, height = _dimensions(req.aspect_ratio)

        images = tuple(
            GeneratedImage(
                data=_solid_png(width, height, _colour(base_seed + index)),
                mime_type="image/png",
                width=width,
                height=height,
                # Each image of a batch gets its own seed, exactly as a real
                # provider does — otherwise "generate 4 candidates" would
                # return four identical pictures and the candidate-selection
                # UI (M3-04) would have nothing to select between.
                seed=base_seed + index,
            )
            for index in range(max(1, req.n))
        )

        return ImageResult(
            images=images,
            usage=Usage(
                images=len(images),
                unit_cost_estimate=estimate_image_cost(model, len(images)),
            ),
            provider_meta={
                "provider": self.name,
                "model": model,
                "seed": base_seed,
                "aspect_ratio": req.aspect_ratio,
                # Recorded so a test can assert references were *passed*, which
                # is otherwise invisible: the mock cannot use them, and a
                # caller that silently dropped them would look identical.
                "references": len(req.references),
                "deterministic": True,
            },
            latency_ms=int((time.monotonic() - started) * 1000),
        )


def _image_seed(req: ImageRequest) -> int:
    """Stable across processes — same reasoning as ``MockLLMProvider._seed``."""
    blob = f"{req.prompt}|{req.negative_prompt}|{req.aspect_ratio}"
    return int(hashlib.sha256(blob.encode()).hexdigest()[:8], 16)


def _dimensions(aspect_ratio: str) -> tuple[int, int]:
    """Pixel size for a ``w:h`` ratio, short edge pinned to ``_MOCK_SHORT_EDGE``.

    An unparseable ratio falls back to 9:16 rather than raising: the mock's job
    is to keep the offline path running, and a typo in a ratio should surface
    from the real adapter's validation, not from the fake one.
    """
    try:
        w_part, h_part = aspect_ratio.split(":")
        w_ratio, h_ratio = int(w_part), int(h_part)
        if w_ratio <= 0 or h_ratio <= 0:
            raise ValueError(aspect_ratio)
    except (ValueError, AttributeError):
        w_ratio, h_ratio = 9, 16

    if w_ratio <= h_ratio:
        return _MOCK_SHORT_EDGE, round(_MOCK_SHORT_EDGE * h_ratio / w_ratio)
    return round(_MOCK_SHORT_EDGE * w_ratio / h_ratio), _MOCK_SHORT_EDGE


def _colour(seed: int) -> tuple[int, int, int]:
    """A mid-tone RGB triple from a seed.

    Clamped away from both extremes so the result is visibly a picture in the
    review UI — a mock that rendered pure black would be indistinguishable
    from a broken image element.
    """
    return (
        64 + (seed >> 16) % 160,
        64 + (seed >> 8) % 160,
        64 + seed % 160,
    )


def _png_chunk(kind: bytes, payload: bytes) -> bytes:
    """One PNG chunk: length, type, payload, CRC32 of type+payload."""
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
    )


def _solid_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """A single-colour truecolour PNG, built by hand.

    Hand-rolled rather than via Pillow, which would be a ~3 MB dependency in
    every worker image for one solid rectangle in a fake provider. The format
    is four chunks and about fifteen lines; the real image adapter will return
    provider bytes and never come near this.

    Scanlines each carry a leading filter byte of ``0`` (None), which is what
    makes the zlib stream trivially correct rather than merely plausible.
    """
    raw = b"".join(b"\x00" + bytes(rgb) * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(
            b"IHDR",
            # bit depth 8, colour type 2 (truecolour), no interlace.
            struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0),
        )
        + _png_chunk(b"IDAT", zlib.compress(raw, 9))
        + _png_chunk(b"IEND", b"")
    )


class MockVoiceProvider:
    """Deterministic narration with **real, usable character timings** (M3-12).

    It would be cheaper to return silence and a flat array. That was rejected
    for the same reason ``MockImageProvider`` emits a decodable PNG: these
    timings travel through word grouping, scene-boundary derivation, an ASS
    caption file and the timeline compiler, and every one of those is a place
    a not-quite-plausible number fails late and obscurely.

    The audio is a valid silent MP3 frame sequence of roughly the right length,
    so anything that probes the file gets a real duration rather than zero.

    Determinism follows the LLM mock's rule — everything derives from the text,
    never from the clock.
    """

    name = "mock"

    #: Seconds per character. ~14 chars/second is ordinary narration pace, and
    #: it makes a 60-second script come out near 60 seconds rather than at some
    #: length that makes every duration assertion downstream look wrong.
    SECONDS_PER_CHAR = 1 / 14

    def __init__(self, *, model: str = "mock-voice-v1") -> None:
        self._model = model

    def capabilities(self) -> VoiceCaps:
        """Declares timings, so the offline path is configurable.

        The B3/S5 gate exists to reject a *real* provider that cannot place
        words; a mock that failed it would make the default mode impossible to
        run, which is the same argument ``MockImageProvider`` makes.
        """
        return VoiceCaps(
            word_timings=True, mime_type="audio/mpeg", max_characters=100_000
        )

    def synthesise(self, req: VoiceRequest) -> VoiceResult:
        text = req.text.strip()
        characters: list[str] = []
        starts: list[float] = []
        ends: list[float] = []

        cursor = 0.0
        for char in text:
            # Whitespace takes a little longer than a letter, which is what
            # makes word boundaries in the mock look like word boundaries in a
            # real response rather than an evenly spaced ramp.
            width = self.SECONDS_PER_CHAR * (2.2 if char.isspace() else 1.0)
            characters.append(char)
            starts.append(round(cursor, 3))
            cursor += width
            ends.append(round(cursor, 3))

        return VoiceResult(
            audio=_silent_mp3(cursor),
            mime_type="audio/mpeg",
            characters=tuple(characters),
            character_starts_s=tuple(starts),
            character_ends_s=tuple(ends),
            latency_ms=0,
            usage=Usage(unit_cost_estimate=0.0),
            provider_meta={
                "provider": self.name,
                "model": self._model,
                "characters": len(text),
            },
        )


#: One frame of MPEG-1 Layer III, 48 kbps, 44.1 kHz mono, all-zero payload.
#: Hand-built rather than pulled from a fixture file so the package ships no
#: binary blob; 26 ms per frame is the standard frame duration at 44.1 kHz.
_MP3_FRAME = b"\xff\xfb\x50\xc4" + b"\x00" * 100
_MP3_FRAME_S = 0.026


def _silent_mp3(seconds: float) -> bytes:
    """Enough frames to make the file's real duration match the timings."""
    frames = max(1, int(round(seconds / _MP3_FRAME_S)))
    return b"ID3\x03\x00\x00\x00\x00\x00\x00" + _MP3_FRAME * frames
