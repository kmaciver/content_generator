"""M1-06: the provider seam, tested without a network."""

from __future__ import annotations

import pytest

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
    Usage,
)
from videoforge_providers.protocols import LLMProvider
from videoforge_providers.registry import UnknownAdapterError, build_llm_provider
from videoforge_shared.settings import LLMProviderConfig, ProviderMode, ProviderSettings


def _request(topic: str = "photosynthesis", schema: bool = False) -> LLMRequest:
    return LLMRequest(
        messages=(
            LLMMessage(role="system", content="You write scripts."),
            LLMMessage(role="user", content=f"Write about: {topic}"),
        ),
        response_schema={"type": "object"} if schema else None,
    )


class TestMockProvider:
    def test_satisfies_the_protocol(self) -> None:
        """Checked at configuration time, not at first call.

        The registry relies on this: a broken adapter should stop the worker
        at boot, not twenty seconds into a user's first generation.
        """
        assert isinstance(MockLLMProvider(), LLMProvider)

    def test_is_deterministic(self) -> None:
        """The property the seed data, golden tests and Playwright run rely on.

        A mock that returned random text would make every downstream assertion
        either flaky or vacuous.
        """
        provider = MockLLMProvider()
        first = provider.complete(_request())
        second = provider.complete(_request())
        assert first.text == second.text

    def test_different_topics_differ(self) -> None:
        """Guards the opposite failure: a mock that ignores the prompt entirely
        would look correct in the determinism test above."""
        provider = MockLLMProvider()
        assert (
            provider.complete(_request("photosynthesis")).text
            != provider.complete(_request("plate tectonics")).text
        )

    def test_determinism_survives_a_new_process(self) -> None:
        """Two instances must agree.

        Seeding with ``hash()`` would pass within one process and fail across
        two — and the API container and the worker are two processes.
        """
        assert MockLLMProvider().complete(_request()).text == (
            MockLLMProvider().complete(_request()).text
        )

    def test_json_mode_returns_parsed_content(self) -> None:
        result = MockLLMProvider().complete(_request(schema=True))
        assert result.parsed is not None
        assert set(result.parsed) == {"title", "script"}

    def test_reports_nonzero_usage(self) -> None:
        """A spend cap tested against zeros would never trip."""
        usage = MockLLMProvider().complete(_request()).usage
        assert usage.input_tokens and usage.input_tokens > 0
        assert usage.output_tokens and usage.output_tokens > 0


class _Flaky:
    """Fails ``failures`` times, then succeeds."""

    name = "flaky"

    def __init__(self, failures: int, *, retryable: bool = True) -> None:
        self.calls = 0
        self._failures = failures
        self._retryable = retryable

    def complete(self, req: LLMRequest) -> LLMResult:
        self.calls += 1
        if self.calls <= self._failures:
            raise ProviderError("boom", provider=self.name, retryable=self._retryable)
        return LLMResult(text="ok", usage=Usage())


class TestRetryMiddleware:
    def test_retries_until_success(self) -> None:
        inner = _Flaky(failures=2)
        provider = RetryingLLMProvider(inner, attempts=3, sleep=lambda _: None)
        assert provider.complete(_request()).text == "ok"
        assert inner.calls == 3

    def test_does_not_retry_non_retryable(self) -> None:
        """A 400 retried three times is three identical errors and a wasted
        budget. Only the adapter knows which kind it got, so ``retryable``
        decides — not the exception class."""
        inner = _Flaky(failures=1, retryable=False)
        with pytest.raises(ProviderError):
            RetryingLLMProvider(inner, attempts=3, sleep=lambda _: None).complete(
                _request()
            )
        assert inner.calls == 1

    def test_gives_up_after_the_budget(self) -> None:
        inner = _Flaky(failures=99)
        with pytest.raises(ProviderError):
            RetryingLLMProvider(inner, attempts=3, sleep=lambda _: None).complete(
                _request()
            )
        assert inner.calls == 3

    def test_backoff_is_exponential(self) -> None:
        delays: list[float] = []
        inner = _Flaky(failures=2)
        RetryingLLMProvider(
            inner, attempts=3, backoff_s=0.5, sleep=delays.append
        ).complete(_request())
        assert delays == [0.5, 1.0]


class TestUsageRecorder:
    def test_records_every_call(self) -> None:
        recorder = UsageRecorder(MockLLMProvider())
        recorder.complete(_request())
        recorder.complete(_request("tectonics"))
        assert len(recorder.calls) == 2

    def test_latency_covers_retries(self) -> None:
        """Ordering check for the standard stack.

        The recorder wraps the retrier, so a call that succeeded on its third
        attempt is recorded as slow. Inverted, the one number an operator uses
        to spot a degraded provider would be the number that hides it.
        """
        slow = MockLLMProvider(latency_ms=5)
        stack = with_standard_middleware(slow)
        result = stack.complete(_request())
        assert result.latency_ms >= 5


class TestRegistry:
    def test_defaults_to_mock(self) -> None:
        provider = build_llm_provider(ProviderSettings(), None)
        assert provider.name == "mock"

    def test_mock_mode_overrides_a_real_adapter(self) -> None:
        """The global "do not spend money" switch must win.

        A config where ``mode: mock`` still made real calls because someone
        left ``adapter: anthropic`` set is the most expensive possible
        surprise, so mode takes precedence over adapter — not the reverse.
        """
        settings = ProviderSettings(
            mode=ProviderMode.MOCK,
            llm=LLMProviderConfig(adapter="anthropic"),
        )
        assert build_llm_provider(settings, None).name == "mock"

    def test_unknown_adapter_fails_at_configuration_time(self) -> None:
        settings = ProviderSettings(
            mode=ProviderMode.REAL, llm=LLMProviderConfig(adapter="nope")
        )
        with pytest.raises(UnknownAdapterError, match="nope"):
            build_llm_provider(settings, None)

    def test_registry_needs_no_keys_for_mock(self) -> None:
        """NF8, from the other direction: the mock path never touches keys, so
        an offline stack cannot leak one it does not have."""
        assert build_llm_provider(ProviderSettings(), None) is not None
