"""The mock LLM — a first-class citizen, not a stub.

It is the **default** in ``config/providers.yaml``, which is what makes the
README's promise true: clone, ``make up-prod``, and the whole pipeline runs
with no API key and no possibility of spending money. CI runs on it too, so
the offline path is the one exercised most often rather than a neglected
branch that rots.

Determinism is the design constraint. The same request must produce the same
script, because the seed data, the golden tests and the Playwright run all
depend on it — a mock that returned random text would make every downstream
assertion either flaky or vacuous.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from videoforge_providers.models import LLMRequest, LLMResult, Usage

__all__ = ["MockLLMProvider"]

#: Sentences the fake script is assembled from. Deliberately about the *shape*
#: of an educational short — a hook, some beats, a closing line — because the
#: review UI, the scene splitter (M2) and the caption renderer are all judged
#: against realistic proportions, and lorem ipsum would hide layout problems
#: until a real provider was wired in.
_BEATS = (
    "Here is something most people get wrong about {topic}.",
    "It starts with a simple observation that turns out to matter enormously.",
    "The first piece of the puzzle is easy to overlook.",
    "But once you see it, the rest follows almost immediately.",
    "There is a catch, and it is the reason this took so long to figure out.",
    "The resolution is more elegant than you would expect.",
    "And that is why {topic} works the way it does.",
)


class MockLLMProvider:
    """Deterministic, offline, and shaped like the real thing."""

    name = "mock"

    def __init__(self, *, model: str = "mock-llm-v1", latency_ms: int = 0) -> None:
        self._model = model
        self._latency_ms = latency_ms

    def complete(self, req: LLMRequest) -> LLMResult:
        started = time.monotonic()
        if self._latency_ms:
            # Opt-in only. Off by default so the test suite stays fast; the
            # knob exists because a UI that looks fine against a 0ms provider
            # can still have no loading state at all.
            time.sleep(self._latency_ms / 1000)

        topic = self._topic(req)
        seed = self._seed(req)

        # Vary length by seed so two different topics do not produce
        # identically-shaped output — otherwise a bug that ignores the prompt
        # entirely would look correct.
        beat_count = 4 + (seed % 4)
        body = " ".join(beat.format(topic=topic) for beat in _BEATS[:beat_count])

        parsed: dict[str, Any] | None = None
        if req.response_schema is not None:
            parsed = {"title": f"{topic.title()}, explained", "script": body}
            text = json.dumps(parsed, separators=(",", ":"))
        else:
            text = body

        return LLMResult(
            text=text,
            parsed=parsed,
            usage=Usage(
                # Rough but non-zero: a spend cap tested against zeros would
                # never trip, and the cap's own test needs something to add up.
                input_tokens=sum(len(m.content) for m in req.messages) // 4,
                output_tokens=len(text) // 4,
                unit_cost_estimate=0.0,
            ),
            provider_meta={
                "provider": self.name,
                "model": req.model_hint or self._model,
                "seed": seed,
                "deterministic": True,
            },
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    @staticmethod
    def _topic(req: LLMRequest) -> str:
        """Best-effort topic, taken from the last user turn.

        The mock reads the prompt rather than ignoring it so that a caller who
        forgets to interpolate the topic gets visibly generic output instead of
        something that looks right.
        """
        for message in reversed(req.messages):
            if message.role == "user" and message.content.strip():
                return message.content.strip().splitlines()[-1][:80]
        return "the subject"

    @staticmethod
    def _seed(req: LLMRequest) -> int:
        """Stable hash of the request. ``hash()`` would not do — it is salted
        per process, so the "deterministic" mock would differ between the API
        container and the worker."""
        blob = "|".join(f"{m.role}:{m.content}" for m in req.messages)
        digest = hashlib.sha256(blob.encode()).hexdigest()
        return int(digest[:8], 16)
