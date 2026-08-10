"""Provider selection (SADD §15.3).

One place decides which adapter a worker gets, from settings that resolve
``code defaults → config/providers.yaml → environment``. Workers ask for "the
LLM provider" and never name a vendor, which is what makes swapping one a
config change.

**NF8.** This module takes ``ProviderKeys`` as an argument rather than
resolving it. Only ``WorkerSettings`` carries that model, so the API — which
builds ``AppSettings`` — cannot construct a real adapter even by mistake. The
secret boundary is enforced by what each process is able to *build*, not by
remembering not to.
"""

from __future__ import annotations

import logging

from videoforge_providers.anthropic_adapter import AnthropicLLMProvider
from videoforge_providers.elevenlabs_adapter import ElevenLabsVoiceProvider
from videoforge_providers.google_adapter import GoogleImageProvider
from videoforge_providers.middleware import (
    with_standard_image_middleware,
    with_standard_middleware,
)
from videoforge_providers.mock import (
    MockImageProvider,
    MockLLMProvider,
    MockVoiceProvider,
)
from videoforge_providers.models import ImageCaps, VoiceCaps
from videoforge_providers.protocols import ImageProvider, LLMProvider, VoiceProvider
from videoforge_providers.record_replay import (
    RecordingLLMProvider,
    ReplayLLMProvider,
)
from videoforge_shared.settings import ProviderKeys, ProviderMode, ProviderSettings

logger = logging.getLogger(__name__)

__all__ = [
    "CapabilityError",
    "UnknownAdapterError",
    "build_image_provider",
    "require_voice_caps",
    "build_voice_provider",
    "build_llm_provider",
    "require_image_caps",
]


class UnknownAdapterError(ValueError):
    """Raised at configuration time, not at first call.

    A typo in ``providers.yaml`` should stop the worker at boot with the list
    of valid names, not surface twenty seconds into a user's first generation
    as an ``AttributeError`` from somewhere unrelated.
    """


class CapabilityError(ValueError):
    """An adapter is valid but cannot do what this deployment needs (ADR-016).

    Separate from :class:`UnknownAdapterError` because the remedies differ: a
    typo is fixed by correcting the name, an incapable provider is fixed by
    choosing a different vendor or by turning the requirement off.
    """


#: Adapters that exist today. Voice lands in M3-12; the mapping is here so an
#: unknown name fails with a list rather than a KeyError.
_LLM_ADAPTERS = frozenset({"mock", "anthropic"})

#: Gemini's "Nano Banana" family, chosen for reference-image conditioning —
#: the one capability ADR-016's gate exists to require (R7).
_IMAGE_ADAPTERS = frozenset({"mock", "google"})

#: M3-12. ElevenLabs is here because word timing is a hard gate (B3/S5) and
#: it is the mainstream TTS API that supplies it.
_VOICE_ADAPTERS = frozenset({"mock", "elevenlabs"})


def build_llm_provider(
    providers: ProviderSettings,
    keys: ProviderKeys | None = None,
    *,
    with_middleware: bool = True,
) -> LLMProvider:
    """Build the configured LLM provider, wrapped in the standard middleware.

    ``mode=mock`` overrides the adapter choice entirely. That precedence is
    deliberate: ``mode`` is the global "do not touch the network or spend
    money" switch, and a config where ``mode: mock`` still made real calls
    because someone left ``adapter: anthropic`` set would be the most
    expensive kind of surprise.
    """
    adapter = providers.llm.adapter.strip().lower() or "mock"

    if providers.mode is ProviderMode.MOCK:
        if adapter != "mock":
            logger.info(
                "provider mode is mock; ignoring configured adapter",
                extra={"configured_adapter": adapter},
            )
        adapter = "mock"

    # REPLAY short-circuits before the adapter is even resolved. It needs no
    # key, no SDK and no network — which is what lets CI exercise the real
    # adapter's *output* on a machine that could not reach the vendor if it
    # tried. Checked ahead of the adapter name so `replay` also works after a
    # vendor is renamed or removed.
    if providers.mode is ProviderMode.REPLAY:
        return _wrap(ReplayLLMProvider(), with_middleware)

    if adapter not in _LLM_ADAPTERS:
        raise UnknownAdapterError(
            f"unknown LLM adapter {adapter!r}; available: "
            f"{', '.join(sorted(_LLM_ADAPTERS))}"
        )

    provider: LLMProvider
    if adapter == "anthropic":
        # `keys` is required here, and this is the line NF8 protects: only
        # `WorkerSettings` can produce a `ProviderKeys`, so the API process
        # cannot reach this branch even by misconfiguration.
        if keys is None or keys.anthropic_api_key is None:
            raise UnknownAdapterError(
                "adapter 'anthropic' needs ANTHROPIC_API_KEY, which reaches "
                "worker containers only (NF8). Set it in .env, or use "
                "PROVIDERS__MODE=mock / replay."
            )
        provider = AnthropicLLMProvider(
            api_key=keys.anthropic_api_key.get_secret_value(),
            model=providers.llm.model,
            timeout_s=providers.llm.timeout_s,
        )
    else:
        provider = MockLLMProvider(model=providers.llm.model or "mock-llm-v1")

    # RECORD wraps the real adapter rather than replacing it: the call still
    # happens and still costs money — recording is a side effect, not a mode of
    # its own. Wrapped *inside* the middleware so what gets written down is the
    # provider's own answer, not one shaped by retries or metering.
    if providers.mode is ProviderMode.RECORD:
        provider = RecordingLLMProvider(provider)

    return _wrap(provider, with_middleware)


def _wrap(provider: LLMProvider, with_middleware: bool) -> LLMProvider:
    return with_standard_middleware(provider) if with_middleware else provider


# --------------------------------------------------------------------------- #
# Images (M3-01)
# --------------------------------------------------------------------------- #

#: How many reference images a deployment must be able to send for character
#: consistency to be possible at all.
#:
#: One, not zero: a provider that accepts a single reference can hold a
#: character across scenes, which is the requirement R7 actually states. More
#: is better and several providers accept a whole sheet, but demanding a number
#: nobody publishes would disqualify capable adapters for no gain.
REQUIRED_REFERENCE_IMAGES = 1


def require_image_caps(
    caps: ImageCaps, *, adapter: str, min_references: int = REQUIRED_REFERENCE_IMAGES
) -> None:
    """Fail configuration when an adapter cannot do character consistency.

    **This is the ADR-016 gate, and it fires at boot.** The alternative — a
    provider that quietly ignores the ``references`` field — produces twenty
    plausible images of twenty different characters, which costs real money and
    is only diagnosable by looking at them. The ``VoiceCaps`` precedent
    (B3/S5) is the same shape for the same reason: a capability the product
    depends on is checked where it can still be changed cheaply.

    ``min_references=0`` turns the check off, deliberately. An operator doing
    non-character work — abstract or diagram-only videos — has a legitimate
    reason to run a provider without reference support, and forcing them to
    patch the source to do it would be the kind of rule people route around.
    """
    if min_references <= 0:
        logger.warning(
            "image reference-capability check disabled; character consistency "
            "(R7) is not enforceable in this configuration",
            extra={"adapter": adapter},
        )
        return
    if caps.max_reference_images < min_references:
        raise CapabilityError(
            f"image adapter {adapter!r} accepts {caps.max_reference_images} "
            f"reference image(s), but character consistency needs at least "
            f"{min_references} (ADR-016). Choose an adapter with reference "
            f"support, or set PROVIDERS__IMAGE__MIN_REFERENCE_IMAGES=0 to "
            f"generate without a recurring character."
        )


def build_image_provider(
    providers: ProviderSettings,
    keys: ProviderKeys | None = None,
    *,
    with_middleware: bool = True,
) -> ImageProvider:
    """Build the configured image provider, gated and wrapped.

    Mirrors :func:`build_llm_provider` — including ``mode=mock`` overriding the
    adapter choice, for the same reason: ``mode`` is the global "do not spend
    money" switch, and images are where that stops being theoretical.
    """
    adapter = providers.image.adapter.strip().lower() or "mock"

    if providers.mode is ProviderMode.MOCK:
        if adapter != "mock":
            logger.info(
                "provider mode is mock; ignoring configured image adapter",
                extra={"configured_adapter": adapter},
            )
        adapter = "mock"

    if providers.mode in (ProviderMode.RECORD, ProviderMode.REPLAY):
        # Not silently downgraded to the mock. Replay's whole value is that CI
        # exercises a *real* adapter's output offline, and quietly substituting
        # a different provider's pictures would make a passing run meaningless.
        # Lands with M3-04, when there is a real adapter whose bytes are worth
        # recording.
        raise UnknownAdapterError(
            f"provider mode {providers.mode.value!r} is not implemented for "
            "images yet (M3-04). Use PROVIDERS__MODE=mock or real."
        )

    if adapter not in _IMAGE_ADAPTERS:
        raise UnknownAdapterError(
            f"unknown image adapter {adapter!r}; available: "
            f"{', '.join(sorted(_IMAGE_ADAPTERS))}"
        )

    provider: ImageProvider
    if adapter == "google":
        # `keys` is required here, and this is the line NF8 protects: only
        # `WorkerSettings` can produce a `ProviderKeys`, so the API process
        # cannot reach this branch even by misconfiguration.
        if keys is None or keys.google_api_key is None:
            raise UnknownAdapterError(
                "adapter 'google' needs GOOGLE_API_KEY, which reaches worker "
                "containers only (NF8). Set it in .env, or use "
                "PROVIDERS__MODE=mock."
            )
        provider = GoogleImageProvider(
            api_key=keys.google_api_key.get_secret_value(),
            model=providers.image.model,
        )
    else:
        provider = MockImageProvider(model=providers.image.model or "mock-image-v1")

    # Gate on the *adapter's* declaration, before any wrapper — the middleware
    # passes `capabilities()` through, so checking here or after is equivalent
    # today, and checking here keeps it true if a wrapper ever transforms them.
    require_image_caps(
        provider.capabilities(),
        adapter=adapter,
        min_references=providers.image.min_reference_images,
    )

    return with_standard_image_middleware(provider) if with_middleware else provider


def require_voice_caps(caps: VoiceCaps, *, adapter: str) -> None:
    """Fail configuration when an adapter cannot place words in time.

    **The B3/S5 gate, and it fires at boot.** Scene boundaries and the caption
    track both derive from word timings, so an adapter without them produces a
    narration that is audible and unusable — you cannot say which image it
    belongs under, and you cannot show a word when it is spoken. Discovering
    that after synthesis means paying for audio twice.

    No opt-out, unlike ``min_references``. A series can legitimately have no
    recurring character; no video in this product has captions that are
    optional, because §1.0.2 found single-word captions are the format.
    """
    if not caps.word_timings:
        raise CapabilityError(
            f"voice adapter {adapter!r} does not supply word timings (B3/S5). "
            "Scene boundaries and captions are both derived from them, so an "
            "adapter without them is disqualified rather than degraded."
        )


def build_voice_provider(
    providers: ProviderSettings, keys: ProviderKeys | None = None
) -> VoiceProvider:
    """Build the configured voice provider, gated (M3-12).

    Mirrors the image builder, including ``mode=mock`` overriding the adapter:
    ``mode`` is the global "do not spend money" switch, and a full narration is
    the largest single unit of provider spend in the pipeline.

    No middleware wrapper yet. The retry helpers are typed for request/response
    pairs and voice is one call per project — the place a retry would matter is
    a transient 5xx, which the adapter already classifies as retryable for
    whenever that wrapper lands.
    """
    adapter = providers.voice.adapter.strip().lower() or "mock"

    if providers.mode is ProviderMode.MOCK:
        if adapter != "mock":
            logger.info(
                "provider mode is mock; ignoring configured voice adapter",
                extra={"configured_adapter": adapter},
            )
        adapter = "mock"

    if providers.mode in (ProviderMode.RECORD, ProviderMode.REPLAY):
        raise UnknownAdapterError(
            f"provider mode {providers.mode.value!r} is not implemented for "
            "voice yet. Use PROVIDERS__MODE=mock or real."
        )

    if adapter not in _VOICE_ADAPTERS:
        raise UnknownAdapterError(
            f"unknown voice adapter {adapter!r}; available: "
            f"{', '.join(sorted(_VOICE_ADAPTERS))}"
        )

    provider: VoiceProvider
    if adapter == "elevenlabs":
        # NF8 again: only `WorkerSettings` can produce a `ProviderKeys`, so the
        # API process cannot reach this branch even by misconfiguration.
        if keys is None or keys.elevenlabs_api_key is None:
            raise UnknownAdapterError(
                "adapter 'elevenlabs' needs ELEVENLABS_API_KEY, which reaches "
                "worker containers only (NF8). Set it in .env, or use "
                "PROVIDERS__MODE=mock."
            )
        provider = ElevenLabsVoiceProvider(
            api_key=keys.elevenlabs_api_key.get_secret_value(),
            voice_id=providers.voice.voice_id,
        )
    else:
        provider = MockVoiceProvider()

    require_voice_caps(provider.capabilities(), adapter=adapter)
    return provider
