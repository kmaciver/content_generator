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
from videoforge_providers.middleware import with_standard_middleware
from videoforge_providers.mock import MockLLMProvider
from videoforge_providers.protocols import LLMProvider
from videoforge_providers.record_replay import (
    RecordingLLMProvider,
    ReplayLLMProvider,
)
from videoforge_shared.settings import ProviderKeys, ProviderMode, ProviderSettings

logger = logging.getLogger(__name__)

__all__ = ["UnknownAdapterError", "build_llm_provider"]


class UnknownAdapterError(ValueError):
    """Raised at configuration time, not at first call.

    A typo in ``providers.yaml`` should stop the worker at boot with the list
    of valid names, not surface twenty seconds into a user's first generation
    as an ``AttributeError`` from somewhere unrelated.
    """


#: Adapters that exist today. Image and voice land in M3; the mapping is here
#: so an unknown name fails with a list rather than a KeyError.
_LLM_ADAPTERS = frozenset({"mock", "anthropic"})


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
