"""Cross-cutting adapter middleware (SADD §15.4).

Every adapter gets the same treatment — timing, bounded retry, logging — and
none of them implement it. An adapter that hand-rolled its own retry would
retry differently from its neighbours, and the difference would only surface
during an outage.

Wrapping is by *shape*, not by base class: the wrapper satisfies the same
Protocol as the thing it wraps, so it is transparent to callers and wrappers
compose in any order.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable

from videoforge_providers.models import (
    ImageCaps,
    ImageRequest,
    ImageResult,
    LLMRequest,
    LLMResult,
    ProviderError,
    ProviderResult,
)
from videoforge_providers.protocols import ImageProvider, LLMProvider

logger = logging.getLogger(__name__)

__all__ = [
    "ImageUsageRecorder",
    "RetryingImageProvider",
    "RetryingLLMProvider",
    "UsageRecorder",
    "with_standard_image_middleware",
    "with_standard_middleware",
]

#: Attempts, not retries: 3 means one call plus two more.
DEFAULT_ATTEMPTS = 3
#: Base for exponential backoff. Kept short because the caller is a Celery task
#: with its own retry budget above this one — deep backoff here would burn the
#: task's soft time limit and turn a retryable blip into a hard failure.
DEFAULT_BACKOFF_S = 0.5


def _with_retry[Req, Res](
    call: Callable[[Req], Res],
    req: Req,
    *,
    provider: str,
    attempts: int,
    backoff_s: float,
    sleep: Callable[[float], None],
) -> Res:
    """The retry **policy**, defined once for every modality.

    M3-01 added image generation, and the tempting move was to copy the loop
    below into a second class. Two loops is two policies the moment somebody
    tunes one of them — and the difference would only ever surface during an
    outage, which is the worst possible time to discover that images and
    completions disagree about what "retryable" means.

    ``ProviderError.retryable`` decides, not the exception class: a 429 and a
    502 are worth retrying and a 400 never is, and only the adapter knows
    which it got. Retrying a malformed request would burn the budget and
    produce the same error three times.
    """
    last: ProviderError | None = None
    for attempt in range(1, attempts + 1):
        try:
            return call(req)
        except ProviderError as exc:
            last = exc
            if not exc.retryable or attempt == attempts:
                raise
            delay = backoff_s * (2 ** (attempt - 1))
            logger.warning(
                "provider call failed; retrying",
                extra={
                    "provider": provider,
                    "attempt": attempt,
                    "attempts": attempts,
                    "delay_s": delay,
                    "error": str(exc),
                },
            )
            sleep(delay)
    raise last if last else RuntimeError("unreachable")  # pragma: no cover


class RetryingLLMProvider:
    """Bounded retry on *retryable* failures only. Policy in :func:`_with_retry`."""

    def __init__(
        self,
        inner: LLMProvider,
        *,
        attempts: int = DEFAULT_ATTEMPTS,
        backoff_s: float = DEFAULT_BACKOFF_S,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._inner = inner
        self._attempts = attempts
        self._backoff_s = backoff_s
        # Injected so tests exercise the backoff *sequence* without waiting
        # for it — a retry test that really sleeps is a test people delete.
        self._sleep = sleep
        self.name = inner.name

    def complete(self, req: LLMRequest) -> LLMResult:
        return _with_retry(
            self._inner.complete,
            req,
            provider=self.name,
            attempts=self._attempts,
            backoff_s=self._backoff_s,
            sleep=self._sleep,
        )


class RetryingImageProvider:
    """The image half. Same policy, different call."""

    def __init__(
        self,
        inner: ImageProvider,
        *,
        attempts: int = DEFAULT_ATTEMPTS,
        backoff_s: float = DEFAULT_BACKOFF_S,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._inner = inner
        self._attempts = attempts
        self._backoff_s = backoff_s
        self._sleep = sleep
        self.name = inner.name

    def capabilities(self) -> ImageCaps:
        """Passed straight through — capabilities are a static declaration, so
        there is nothing here to retry or to meter."""
        return self._inner.capabilities()

    def generate(self, req: ImageRequest) -> ImageResult:
        return _with_retry(
            self._inner.generate,
            req,
            provider=self.name,
            attempts=self._attempts,
            backoff_s=self._backoff_s,
            sleep=self._sleep,
        )


def _measure[Res: ProviderResult](result: Res, started: float) -> Res:
    """Stamp the wall-clock latency the caller actually waited for.

    Trusted over the adapter's self-report because this includes the adapter's
    own retries and serialisation — which is exactly what an operator watching
    for a degraded provider needs to see.
    """
    return result.model_copy(
        update={"latency_ms": int((time.monotonic() - started) * 1000)}
    )


class UsageRecorder:
    """Captures the usage of every call for the caller to persist.

    Deliberately does **not** write to the database. This package has no
    persistence dependency and must not gain one — the worker owns the
    transaction, and a provider that wrote its own rows would land usage
    outside the caller's unit of work, so a rolled-back job would still be
    billed.
    """

    def __init__(self, inner: LLMProvider) -> None:
        self._inner = inner
        self.name = inner.name
        self.calls: list[LLMResult] = []

    def complete(self, req: LLMRequest) -> LLMResult:
        started = time.monotonic()
        measured = _measure(self._inner.complete(req), started)
        self.calls.append(measured)
        logger.info(
            "provider call",
            extra={
                "provider": self.name,
                "latency_ms": measured.latency_ms,
                "input_tokens": measured.usage.input_tokens,
                "output_tokens": measured.usage.output_tokens,
                "cost_estimate": measured.usage.unit_cost_estimate,
            },
        )
        return measured


class ImageUsageRecorder:
    """The image half. Same contract, and the same refusal to touch a session."""

    def __init__(self, inner: ImageProvider) -> None:
        self._inner = inner
        self.name = inner.name
        self.calls: list[ImageResult] = []

    def capabilities(self) -> ImageCaps:
        return self._inner.capabilities()

    def generate(self, req: ImageRequest) -> ImageResult:
        started = time.monotonic()
        measured = _measure(self._inner.generate(req), started)
        self.calls.append(measured)
        logger.info(
            "provider call",
            extra={
                "provider": self.name,
                "latency_ms": measured.latency_ms,
                "images": measured.usage.images,
                "cost_estimate": measured.usage.unit_cost_estimate,
            },
        )
        return measured


def with_standard_middleware(
    provider: LLMProvider, *, attempts: int = DEFAULT_ATTEMPTS
) -> UsageRecorder:
    """The stack every adapter gets, in the order that makes it meaningful.

    ``UsageRecorder(RetryingLLMProvider(adapter))`` — recorder *outside*, so
    the latency it records covers the retries. Inverted, a call that succeeded
    on its third attempt would be recorded as fast, and the one number an
    operator uses to spot a degraded provider would be the one number that
    hides it.
    """
    return UsageRecorder(RetryingLLMProvider(provider, attempts=attempts))


def with_standard_image_middleware(
    provider: ImageProvider, *, attempts: int = DEFAULT_ATTEMPTS
) -> ImageUsageRecorder:
    """Same stack, same order, same reason."""
    return ImageUsageRecorder(RetryingImageProvider(provider, attempts=attempts))
