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

from videoforge_providers.models import LLMRequest, LLMResult, ProviderError
from videoforge_providers.protocols import LLMProvider

logger = logging.getLogger(__name__)

__all__ = ["RetryingLLMProvider", "UsageRecorder", "with_standard_middleware"]

#: Attempts, not retries: 3 means one call plus two more.
DEFAULT_ATTEMPTS = 3
#: Base for exponential backoff. Kept short because the caller is a Celery task
#: with its own retry budget above this one — deep backoff here would burn the
#: task's soft time limit and turn a retryable blip into a hard failure.
DEFAULT_BACKOFF_S = 0.5


class RetryingLLMProvider:
    """Bounded retry on *retryable* failures only.

    ``ProviderError.retryable`` decides, not the exception class: a 429 and a
    502 are worth retrying and a 400 never is, and only the adapter knows
    which it got. Retrying a malformed request would burn the budget and
    produce the same error three times.
    """

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
        last: ProviderError | None = None
        for attempt in range(1, self._attempts + 1):
            try:
                return self._inner.complete(req)
            except ProviderError as exc:
                last = exc
                if not exc.retryable or attempt == self._attempts:
                    raise
                delay = self._backoff_s * (2 ** (attempt - 1))
                logger.warning(
                    "provider call failed; retrying",
                    extra={
                        "provider": self.name,
                        "attempt": attempt,
                        "attempts": self._attempts,
                        "delay_s": delay,
                        "error": str(exc),
                    },
                )
                self._sleep(delay)
        raise last if last else RuntimeError("unreachable")  # pragma: no cover


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
        result = self._inner.complete(req)
        # Trust the wall clock here over the adapter's self-report: this
        # measures what the worker actually waited for, including the adapter's
        # own retries and serialisation.
        measured = result.model_copy(
            update={"latency_ms": int((time.monotonic() - started) * 1000)}
        )
        self.calls.append(measured)
        logger.info(
            "provider call",
            extra={
                "provider": self.name,
                "latency_ms": measured.latency_ms,
                "input_tokens": measured.usage.input_tokens,
                "output_tokens": measured.usage.output_tokens,
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
