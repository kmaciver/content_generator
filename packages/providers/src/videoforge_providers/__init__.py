"""Provider protocols and adapters — the only external boundary in the system.

Everything that leaves the machine goes through here, which is why this package
has exactly one job and no others: no persistence, no Celery, no Flask. A
provider that wrote to the database would land its usage row outside the
caller's transaction, so a rolled-back job would still be billed.

The default is ``mock`` (``config/providers.yaml``), so the whole pipeline runs
offline with no API key and nothing can cost money until that is deliberately
changed. CI runs on the same path, which keeps the offline route the
best-exercised one rather than a branch that rots.
"""

from videoforge_providers.middleware import (
    RetryingLLMProvider,
    UsageRecorder,
    with_standard_middleware,
)
from videoforge_providers.mock import MockLLMProvider
from videoforge_providers.models import (
    LLMMessage,
    LLMRequest,
    LLMResult,
    ProviderError,
    ProviderTimeoutError,
    Usage,
)
from videoforge_providers.protocols import ImageProvider, LLMProvider, VoiceProvider
from videoforge_providers.registry import UnknownAdapterError, build_llm_provider

__all__ = [
    "ImageProvider",
    "LLMMessage",
    "LLMProvider",
    "LLMRequest",
    "LLMResult",
    "MockLLMProvider",
    "ProviderError",
    "ProviderTimeoutError",
    "RetryingLLMProvider",
    "UnknownAdapterError",
    "Usage",
    "UsageRecorder",
    "VoiceProvider",
    "build_llm_provider",
    "with_standard_middleware",
]
