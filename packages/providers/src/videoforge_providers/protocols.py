"""Provider protocols (SADD §15.2) — the system's only external boundary.

``typing.Protocol`` rather than an ABC: adapters are written against a shape,
not a base class, so a test fake, a record/replay wrapper and a real vendor
adapter are interchangeable without any of them inheriting from anything. The
middleware in ``middleware.py`` depends on this too — it wraps *a shape*, which
is why one implementation can wrap every provider kind.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from videoforge_providers.models import (
    ImageCaps,
    ImageRequest,
    ImageResult,
    LLMRequest,
    LLMResult,
    VoiceCaps,
    VoiceRequest,
    VoiceResult,
)

__all__ = ["ImageProvider", "LLMProvider", "VoiceProvider"]


@runtime_checkable
class LLMProvider(Protocol):
    """Text in, text (or parsed JSON) out.

    ``runtime_checkable`` so the registry can assert an adapter satisfies the
    protocol at *configuration* time rather than at the first call — which, on
    the LLM path, would be several seconds into a user's first generation.
    """

    name: str

    def complete(self, req: LLMRequest) -> LLMResult:
        """Run one completion. Raises ``ProviderError`` on failure."""
        ...


@runtime_checkable
class ImageProvider(Protocol):
    """Prompt (plus optional references) in, image bytes out — M3-01.

    ``capabilities()`` is part of the protocol rather than an optional extra,
    because the registry consults it at configuration time (ADR-016). Making it
    optional would mean the gate could be skipped by an adapter that simply
    did not implement it, which is the one adapter the gate exists to catch.
    """

    name: str

    def capabilities(self) -> ImageCaps:
        """What this adapter supports. Must not perform I/O — it is called
        during startup, before anything is known to be reachable."""
        ...

    def generate(self, req: ImageRequest) -> ImageResult:
        """Render one request. Raises ``ProviderError`` on failure."""
        ...


class VoiceProvider(Protocol):
    """Narration in, audio plus per-character timings out (M3-12).

    ``capabilities()`` gates adoption: **word-level timing is a hard
    requirement** (findings B3/S5). Scene boundaries and the caption track both
    derive from it, and an adapter that cannot supply it fails at configuration
    time rather than producing a video with unsynchronised captions.

    Note what the contract asks for and what it does not. It requires that
    timings *can be derived*, not that the provider returns words: measured
    against ElevenLabs on 2026-08-09, ``/with-timestamps`` returns one entry
    per **character**, and words are grouped from those by
    ``videoforge_domain.timing``. A protocol demanding word objects would have
    disqualified the provider that actually does the job best.
    """

    name: str

    def capabilities(self) -> VoiceCaps:
        """What this adapter supports. Must not perform I/O — it is called
        during startup, before anything is known to be reachable."""
        ...

    def synthesise(self, req: VoiceRequest) -> VoiceResult:
        """Speak the whole script in one call. Raises ``ProviderError``."""
        ...
