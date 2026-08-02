"""Provider protocols (SADD §15.2) — the system's only external boundary.

``typing.Protocol`` rather than an ABC: adapters are written against a shape,
not a base class, so a test fake, a record/replay wrapper and a real vendor
adapter are interchangeable without any of them inheriting from anything. The
middleware in ``middleware.py`` depends on this too — it wraps *a shape*, which
is why one implementation can wrap every provider kind.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from videoforge_providers.models import LLMRequest, LLMResult

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


class ImageProvider(Protocol):
    """Declared now, implemented in M3. Present so the seam is visible in the
    package that owns it rather than appearing later as a surprise."""

    name: str


class VoiceProvider(Protocol):
    """Declared now, implemented in M3.

    When it lands, ``capabilities()`` gates adoption: **word-level timestamps
    are a hard requirement** (findings B3/S5). Scene boundaries and karaoke
    captions both derive from them, and an adapter that cannot supply them
    fails the contract test at configuration time rather than producing a
    video with unsynchronised captions.
    """

    name: str
