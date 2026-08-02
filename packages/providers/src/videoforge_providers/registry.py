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

from videoforge_providers.middleware import with_standard_middleware
from videoforge_providers.mock import MockLLMProvider
from videoforge_providers.protocols import LLMProvider
from videoforge_shared.settings import ProviderKeys, ProviderMode, ProviderSettings

logger = logging.getLogger(__name__)

__all__ = ["UnknownAdapterError", "build_llm_provider"]


class UnknownAdapterError(ValueError):
    """Raised at configuration time, not at first call.

    A typo in ``providers.yaml`` should stop the worker at boot with the list
    of valid names, not surface twenty seconds into a user's first generation
    as an ``AttributeError`` from somewhere unrelated.
    """


#: Adapters that exist today. Real ones land in M2 (LLM) and M3 (image, voice);
#: the mapping is here so an unknown name fails with a list rather than a
#: KeyError.
_LLM_ADAPTERS = frozenset({"mock"})


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

    if adapter not in _LLM_ADAPTERS:
        raise UnknownAdapterError(
            f"unknown LLM adapter {adapter!r}; available: "
            f"{', '.join(sorted(_LLM_ADAPTERS))}"
        )

    provider: LLMProvider = MockLLMProvider(model=providers.llm.model or "mock-llm-v1")

    # `keys` is unused while `mock` is the only adapter. It stays in the
    # signature because the alternative — adding it when the first real
    # adapter lands — is the moment someone reaches for a module-level
    # `ProviderKeys()` instead and quietly reads secrets in the API process.
    _ = keys

    return with_standard_middleware(provider) if with_middleware else provider
