"""Anthropic adapter — Claude, behind ``LLMProvider`` (M2-06).

The first adapter that can spend money, so the interesting parts are the edges
rather than the happy path: how a vendor's exceptions become a retry decision,
and how "give me JSON" stops being a hope.

**Structured output uses tool use, not prompting.** ``LLMRequest.response_schema``
is turned into a single-tool definition with ``tool_choice`` forcing it, so the
model must answer through the schema. Asking for JSON in the prompt and parsing
the reply is the failure mode ``models.py`` calls out — prose wrapped around
valid JSON, or a fenced code block, or an apology. The scenes stage (M2-11)
consumes structured output directly, so this has to be a guarantee.

**No SDK type crosses this module's boundary** (SADD §15.2). ``anthropic``
imports live here and nowhere else; everything returned is a Pydantic model
from ``models.py``.

The module is deliberately *not* named ``anthropic.py``: a module shadowing the
package it imports is a debugging session nobody needs.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from videoforge_providers.models import (
    LLMMessage,
    LLMRequest,
    LLMResult,
    ProviderError,
    ProviderTimeoutError,
    Usage,
)

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_MODEL", "AnthropicLLMProvider"]

#: Claude Sonnet: the quality/latency/cost point that suits research, script and
#: scene generation. Overridable per call via ``LLMRequest.model_hint`` and per
#: deployment via ``PROVIDERS__LLM__MODEL``.
DEFAULT_MODEL = "claude-sonnet-5"

#: Name of the synthetic tool used to force structured output. Arbitrary, but
#: it appears in ``provider_meta``, so it should read as deliberate.
_STRUCTURED_TOOL = "emit_result"


class AnthropicLLMProvider:
    """Claude via the official SDK."""

    name = "anthropic"

    def __init__(
        self,
        *,
        api_key: str = "",
        model: str = "",
        timeout_s: int = 120,
        client: Any = None,
    ) -> None:
        """Build against the real SDK, or against an injected client.

        ``client`` exists so the translation logic — system-turn splitting,
        tool-forced JSON, error classification — can be tested with no key and
        no network. A seam beats a test that reaches into private attributes:
        that test passes until the attribute is renamed, and then fails for a
        reason unrelated to the behaviour it was checking.

        The client is untyped (``Any``) because the SDK is imported lazily, so
        there is no module-level name to annotate with. That laziness is the
        point: importing ``videoforge_providers`` must not require the SDK,
        since a mock or replay deployment never installs a reason to use it.
        """
        self._model = model or DEFAULT_MODEL

        if client is not None:
            self._client: Any = client
            return

        from anthropic import Anthropic

        if not api_key:
            # Fails at construction — which is configuration time — rather than
            # at the first call, several seconds into a user's first
            # generation. Same reasoning as `UnknownAdapterError`.
            raise ProviderError(
                "anthropic adapter selected but ANTHROPIC_API_KEY is empty",
                provider=self.name,
            )
        self._client = Anthropic(api_key=api_key, timeout=float(timeout_s))

    def complete(self, req: LLMRequest) -> LLMResult:
        started = time.monotonic()
        system, turns = _split_system(req.messages)

        kwargs: dict[str, Any] = {
            "model": req.model_hint or self._model,
            "max_tokens": req.max_tokens,
            "messages": turns,
        }
        # Omitted unless the caller asked for one. Newer Claude models reject
        # the parameter entirely rather than ignoring it, so sending a default
        # turns every call into a 400. "Unset" has to reach the API as absent,
        # not as a number somebody picked.
        if req.temperature is not None:
            kwargs["temperature"] = req.temperature
        if system:
            kwargs["system"] = system
        if req.response_schema is not None:
            kwargs["tools"] = [
                {
                    "name": _STRUCTURED_TOOL,
                    "description": "Return the result in the required structure.",
                    "input_schema": req.response_schema,
                }
            ]
            kwargs["tool_choice"] = {"type": "tool", "name": _STRUCTURED_TOOL}

        message = self._call(kwargs)
        text, parsed = _extract(message, structured=req.response_schema is not None)

        return LLMResult(
            text=text,
            parsed=parsed,
            usage=Usage(
                input_tokens=getattr(message.usage, "input_tokens", None),
                output_tokens=getattr(message.usage, "output_tokens", None),
            ),
            provider_meta={
                "provider": self.name,
                # The model the API *actually* served, not the one requested —
                # aliases resolve server-side, and §10.3 rule 4's
                # reproducibility chain needs the resolved value.
                "model": getattr(message, "model", kwargs["model"]),
                "stop_reason": getattr(message, "stop_reason", None),
                "message_id": getattr(message, "id", None),
                "structured": req.response_schema is not None,
            },
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    def _call(self, kwargs: dict[str, Any]) -> Any:
        """One API call, with vendor exceptions translated to a retry decision.

        ``ProviderError.retryable`` exists so the middleware never has to guess
        from an exception class. The split is the usual one: transport trouble
        and rate limits are worth another go, a malformed request or a bad key
        never is — retrying those burns the budget and delays a fix.
        """
        from anthropic import APIConnectionError, APIStatusError, APITimeoutError

        try:
            return self._client.messages.create(**kwargs)
        except APITimeoutError as exc:
            raise ProviderTimeoutError(str(exc), provider=self.name) from exc
        except APIConnectionError as exc:
            raise ProviderError(str(exc), provider=self.name, retryable=True) from exc
        except APIStatusError as exc:
            status = getattr(exc, "status_code", 0)
            retryable = status == 429 or status >= 500
            raise ProviderError(
                f"anthropic returned {status}: {exc}",
                provider=self.name,
                retryable=retryable,
            ) from exc


def _split_system(messages: tuple[LLMMessage, ...]) -> tuple[str, list[dict[str, str]]]:
    """Anthropic takes ``system`` as a top-level parameter, not a turn.

    ``LLMMessage`` models it as a role because providers disagree about which
    it is, and this is the module where that disagreement is resolved. Multiple
    system turns are joined rather than dropped — silently losing instructions
    is worse than a slightly long system prompt.
    """
    system_parts = [m.content for m in messages if m.role == "system"]
    turns = [
        {"role": m.role, "content": m.content} for m in messages if m.role != "system"
    ]
    if not turns:
        # The API requires at least one turn. An all-system request is a caller
        # bug, but failing here with a clear message beats a 400 from the API.
        raise ProviderError(
            "request has no user or assistant turns", provider="anthropic"
        )
    return "\n\n".join(system_parts), turns


def _extract(message: Any, *, structured: bool) -> tuple[str, dict[str, Any] | None]:
    """Pull text and, when a schema was requested, the structured payload.

    Blocks are scanned rather than indexed: the response may legitimately carry
    a text block alongside the tool call, and assuming ``content[0]`` is the
    interesting one works right up until it does not.
    """
    text_parts: list[str] = []
    parsed: dict[str, Any] | None = None

    for block in getattr(message, "content", []) or []:
        kind = getattr(block, "type", None)
        if kind == "text":
            text_parts.append(getattr(block, "text", ""))
        elif kind == "tool_use" and parsed is None:
            payload = getattr(block, "input", None)
            if isinstance(payload, dict):
                parsed = payload

    if structured and parsed is None:
        # Forced tool choice makes this close to impossible, which is exactly
        # why it must raise rather than fall back to parsing the prose: a
        # silent fallback would turn a provider-side change into malformed
        # scene data appearing much later.
        raise ProviderError(
            "structured output was requested but no tool_use block came back",
            provider="anthropic",
            retryable=True,
        )

    return "\n".join(p for p in text_parts if p), parsed
