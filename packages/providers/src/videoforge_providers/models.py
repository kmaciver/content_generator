"""Request and result models for every provider (SADD §15.2).

**No SDK type ever crosses this boundary.** An ``anthropic.types.Message`` or an
``openai.ChatCompletion`` reaching a worker would put a vendor's schema in the
middle of the pipeline, and swapping providers would then mean rewriting the
consumer rather than the adapter. Everything in and out is a Pydantic model
defined here.

M1 defines only the LLM shapes; image and voice arrive with M3, and their
protocols are declared in ``protocols.py`` now so the seam is visible.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "LLMMessage",
    "LLMRequest",
    "LLMResult",
    "ProviderError",
    "ProviderTimeoutError",
    "Usage",
]


class LLMMessage(BaseModel):
    """One turn. ``system`` is a role, not a separate parameter, because
    providers disagree about which it is and the adapter is the right place
    for that disagreement to be resolved."""

    model_config = ConfigDict(frozen=True)

    role: Literal["system", "user", "assistant"]
    content: str


class LLMRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    messages: tuple[LLMMessage, ...]
    #: A *hint*: the adapter resolves it against what the vendor actually
    #: offers. Empty means "the adapter's default", which is what keeps
    #: ``config/providers.yaml`` able to say ``model: ""`` and still work.
    model_hint: str = ""
    temperature: float = 0.7
    max_tokens: int = 4096
    #: JSON-mode schema. When set, ``LLMResult.parsed`` carries the decoded
    #: object and the consumer never parses free text — the failure mode being
    #: avoided is a model that returns prose around valid JSON.
    response_schema: dict[str, Any] | None = None
    timeout_s: int = 120


class Usage(BaseModel):
    """What a call consumed. Feeds ``provider_usage`` and the S10 spend cap.

    Every field is optional because the meaning differs per modality — an
    image call has no tokens, a voice call has no images — and a zero would
    claim knowledge the adapter does not have.
    """

    model_config = ConfigDict(frozen=True)

    input_tokens: int | None = None
    output_tokens: int | None = None
    images: int | None = None
    audio_seconds: float | None = None
    #: Locally estimated, never billed. Named ``estimate`` so no one mistakes
    #: it for an invoice.
    unit_cost_estimate: float = 0.0


class LLMResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    text: str
    parsed: dict[str, Any] | None = None
    usage: Usage = Field(default_factory=Usage)
    #: Adapter-specific detail — model actually used, finish reason, request
    #: id. Lands in ``artifact_version.meta`` for reproducibility (§10.3
    #: rule 4). Untyped on purpose: it is evidence, not contract.
    provider_meta: dict[str, Any] = Field(default_factory=dict)
    latency_ms: int = 0


class ProviderError(RuntimeError):
    """An adapter failed in a way the caller may want to distinguish.

    Carries ``retryable`` because the retry decision belongs to the caller's
    policy, not to the exception type — a 429 and a 500 are both worth
    retrying, a 400 never is, and hiding that behind a class hierarchy makes
    the middleware guess.
    """

    def __init__(self, message: str, *, provider: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.provider = provider
        self.retryable = retryable


class ProviderTimeoutError(ProviderError):
    def __init__(self, message: str, *, provider: str) -> None:
        super().__init__(message, provider=provider, retryable=True)
