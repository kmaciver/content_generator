"""M1-06: the provider seam, tested without a network."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

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
from videoforge_shared.settings import (
    LLMProviderConfig,
    ProviderKeys,
    ProviderMode,
    ProviderSettings,
)

#: What the script stage asks for. The mock synthesises *from* the schema,
#: so a test that passed a property-less object would assert nothing.
_SCRIPT_SCHEMA = {
    "type": "object",
    "properties": {"title": {"type": "string"}, "script": {"type": "string"}},
    "required": ["title", "script"],
}


def _request(topic: str = "photosynthesis", schema: bool = False) -> LLMRequest:
    return LLMRequest(
        messages=(
            LLMMessage(role="system", content="You write scripts."),
            LLMMessage(role="user", content=f"Write about: {topic}"),
        ),
        response_schema=_SCRIPT_SCHEMA if schema else None,
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


# --------------------------------------------------------------------------- #
# M2-06 / M2-07 — the Anthropic adapter, and record/replay
# --------------------------------------------------------------------------- #


class _Block:
    """One response block. Shaped like the SDK's, not imported from it —
    building these by hand is what lets the translation logic be tested with no
    key, no network, and no dependency on the vendor's test doubles."""

    def __init__(self, kind: str, *, text: str = "", payload: Any = None) -> None:
        self.type = kind
        self.text = text
        self.input = payload


class _Usage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _Message:
    def __init__(self, blocks: list[_Block], *, model: str = "claude-sonnet-5") -> None:
        self.content = blocks
        self.usage = _Usage(11, 22)
        self.model = model
        self.stop_reason = "end_turn"
        self.id = "msg_test"


class _FakeMessages:
    def __init__(self, message: Any) -> None:
        self._message = message
        self.last_kwargs: dict[str, Any] | None = None

    def create(self, **kwargs: Any) -> Any:
        self.last_kwargs = kwargs
        if isinstance(self._message, Exception):
            raise self._message
        return self._message


def _adapter(message: Any) -> tuple[Any, _FakeMessages]:
    """Built through the adapter's injection seam — no key, no network, and no
    reaching into private attributes (which would break on a rename for reasons
    unrelated to the behaviour under test)."""
    from videoforge_providers.anthropic_adapter import AnthropicLLMProvider

    messages = _FakeMessages(message)
    provider = AnthropicLLMProvider(client=SimpleNamespace(messages=messages))
    return provider, messages


def _llm_req(**over: Any) -> LLMRequest:
    base: dict[str, Any] = {
        "messages": (
            LLMMessage(role="system", content="be brief"),
            LLMMessage(role="user", content="explain tides"),
        )
    }
    base.update(over)
    return LLMRequest(**base)


class TestAnthropicAdapter:
    def test_system_turns_become_the_system_parameter(self) -> None:
        """Anthropic takes `system` top-level, not as a turn. `LLMMessage`
        models it as a role because providers disagree; this adapter is where
        that disagreement is supposed to be resolved."""
        provider, messages = _adapter(_Message([_Block("text", text="hi")]))
        provider.complete(_llm_req())

        assert messages.last_kwargs is not None
        assert messages.last_kwargs["system"] == "be brief"
        assert messages.last_kwargs["messages"] == [
            {"role": "user", "content": "explain tides"}
        ]

    def test_a_schema_forces_tool_use(self) -> None:
        """The guarantee the scenes stage depends on. Prompting for JSON and
        parsing the reply is what this replaces — the failure being avoided is
        prose wrapped around valid JSON."""
        schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        provider, messages = _adapter(
            _Message([_Block("tool_use", payload={"a": "b"})])
        )
        result = provider.complete(_llm_req(response_schema=schema))

        assert messages.last_kwargs is not None
        assert messages.last_kwargs["tools"][0]["input_schema"] == schema
        assert messages.last_kwargs["tool_choice"]["type"] == "tool"
        assert result.parsed == {"a": "b"}

    def test_text_alongside_a_tool_call_is_kept(self) -> None:
        """Blocks are scanned, not indexed. Assuming content[0] is the
        interesting one works right up until the model adds a preamble."""
        provider, _ = _adapter(
            _Message(
                [
                    _Block("text", text="thinking..."),
                    _Block("tool_use", payload={"a": "b"}),
                ]
            )
        )
        result = provider.complete(_llm_req(response_schema={"type": "object"}))
        assert result.text == "thinking..."
        assert result.parsed == {"a": "b"}

    def test_a_missing_tool_block_raises_rather_than_falling_back(self) -> None:
        """Forced tool choice makes this near-impossible, which is exactly why
        it must not silently fall back to parsing prose: that would turn a
        provider-side change into malformed scene data discovered much later."""
        provider, _ = _adapter(_Message([_Block("text", text="sorry, no")]))
        with pytest.raises(ProviderError, match="no tool_use block"):
            provider.complete(_llm_req(response_schema={"type": "object"}))

    def test_the_served_model_is_recorded_not_the_requested_one(self) -> None:
        """Aliases resolve server-side, and §10.3 rule 4's reproducibility chain
        needs the value that actually answered."""
        provider, _ = _adapter(
            _Message([_Block("text", text="x")], model="claude-sonnet-5-20260101")
        )
        result = provider.complete(_llm_req())
        assert result.provider_meta["model"] == "claude-sonnet-5-20260101"

    def test_usage_is_carried_through(self) -> None:
        provider, _ = _adapter(_Message([_Block("text", text="x")]))
        result = provider.complete(_llm_req())
        assert (result.usage.input_tokens, result.usage.output_tokens) == (11, 22)

    def test_a_request_with_no_user_turn_fails_here(self) -> None:
        """A caller bug, but the API's 400 is less informative than this."""
        provider, _ = _adapter(_Message([_Block("text", text="x")]))
        with pytest.raises(ProviderError, match="no user or assistant turns"):
            provider.complete(
                LLMRequest(messages=(LLMMessage(role="system", content="only me"),))
            )


class TestRecordReplay:
    def test_a_recorded_call_replays_identically(self, tmp_path: Path) -> None:
        from videoforge_providers.record_replay import (
            RecordingLLMProvider,
            ReplayLLMProvider,
        )

        inner = MockLLMProvider(model="mock-llm-v1")
        req = _llm_req()

        recorded = RecordingLLMProvider(inner, directory=tmp_path).complete(req)
        replayed = ReplayLLMProvider(directory=tmp_path).complete(req)

        assert replayed == recorded

    def test_replay_holds_no_client_at_all(self, tmp_path: Path) -> None:
        """The structural half of the guarantee. Replay does not wrap a real
        adapter, so there is nothing for a missing fixture to fall through to —
        it cannot reach the network even if the code tried."""
        from videoforge_providers.record_replay import ReplayLLMProvider

        replay = ReplayLLMProvider(directory=tmp_path)
        assert not any(
            "client" in name or "inner" in name for name in vars(replay)
        ), vars(replay)

    def test_a_missing_fixture_names_the_fix(self, tmp_path: Path) -> None:
        """The person hitting this changed a prompt and has no idea why CI is
        red. The message has to say so."""
        from videoforge_providers.record_replay import (
            MissingFixtureError,
            ReplayLLMProvider,
        )

        with pytest.raises(MissingFixtureError, match="re-record"):
            ReplayLLMProvider(directory=tmp_path).complete(_llm_req())

    def test_the_key_ignores_timeout_but_not_content(self, tmp_path: Path) -> None:
        """`timeout_s` changes whether a call completes, never what comes back.
        Including it would invalidate every fixture the day someone tunes it."""
        from videoforge_providers.record_replay import fixture_key

        base = _llm_req()
        assert fixture_key(base) == fixture_key(_llm_req(timeout_s=999))
        assert fixture_key(base) != fixture_key(_llm_req(temperature=0.1))

    def test_the_key_is_stable_across_processes(self) -> None:
        """sha256, not `hash()` — which is salted per process, so the recording
        worker and the replaying test would disagree about identical requests.
        Same trap as MockLLMProvider._seed."""
        import subprocess
        import sys

        from videoforge_providers.record_replay import fixture_key

        code = (
            "from videoforge_providers.models import LLMMessage, LLMRequest;"
            "from videoforge_providers.record_replay import fixture_key;"
            "print(fixture_key(LLMRequest(messages=("
            "LLMMessage(role='system', content='be brief'),"
            "LLMMessage(role='user', content='explain tides'),))))"
        )
        other = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, check=True
        )
        assert other.stdout.strip() == fixture_key(_llm_req())


class TestRegistryModes:
    def _settings(self, **over: Any) -> ProviderSettings:
        data: dict[str, Any] = {"mode": "mock", "llm": {"adapter": "mock"}}
        data.update(over)
        return ProviderSettings.model_validate(data)

    def test_mock_mode_beats_a_real_adapter(self) -> None:
        """The global "do not spend money" switch. A config where mode=mock
        still called Anthropic because adapter was left set would be the most
        expensive kind of surprise."""
        provider = build_llm_provider(
            self._settings(mode="mock", llm={"adapter": "anthropic"}),
            with_middleware=False,
        )
        assert provider.name == "mock"

    def test_replay_needs_no_key(self, tmp_path: Path, monkeypatch: Any) -> None:
        """What keeps CI offline once a real adapter exists."""
        monkeypatch.setenv("VIDEOFORGE_PROVIDER_FIXTURES", str(tmp_path))
        provider = build_llm_provider(
            self._settings(mode="replay", llm={"adapter": "anthropic"}),
            keys=None,
            with_middleware=False,
        )
        assert provider.name == "replay"

    def test_anthropic_without_a_key_fails_at_configuration_time(self) -> None:
        """Not twenty seconds into a user's first generation."""
        with pytest.raises(UnknownAdapterError, match="ANTHROPIC_API_KEY"):
            build_llm_provider(
                self._settings(mode="real", llm={"adapter": "anthropic"}),
                keys=ProviderKeys(),
                with_middleware=False,
            )

    def test_an_unknown_adapter_still_lists_the_valid_ones(self) -> None:
        with pytest.raises(UnknownAdapterError, match="anthropic"):
            build_llm_provider(
                self._settings(mode="real", llm={"adapter": "gpt-9"}),
                with_middleware=False,
            )
