"""Record and replay provider calls (M2-07, SADD §15.3).

The problem this solves: the moment a real adapter exists, CI has exactly three
options — call the vendor (costs money, needs a key in CI, fails when the vendor
is down), run only against the mock (so the real adapter is never exercised
until production), or replay captured traffic. Only the third tests the adapter.

    PROVIDERS__MODE=record   real calls, every one written to a fixture
    PROVIDERS__MODE=replay   fixtures only — no key, no network, no cost

**Replay must never reach the network.** It does not wrap a real adapter at
all; it wraps nothing. That is a structural guarantee rather than a promise:
there is no client to call, so a missing fixture raises instead of quietly
falling through to the vendor and billing someone.

Fixtures are keyed by a **stable hash of the request** — sha256 of its canonical
JSON, not ``hash()``, which is salted per process and would give the recording
worker and the replaying test different keys for identical requests. Same
reasoning as ``MockLLMProvider._seed``.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path

from videoforge_providers.models import LLMRequest, LLMResult, ProviderError
from videoforge_providers.protocols import LLMProvider

logger = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_FIXTURES_DIR",
    "FIXTURES_DIR_ENV",
    "MissingFixtureError",
    "RecordingLLMProvider",
    "ReplayLLMProvider",
    "fixture_key",
    "fixtures_dir",
]

#: Committed to git, like any other test input. Reviewing a diff here is
#: reviewing a change in provider behaviour, which is the point.
DEFAULT_FIXTURES_DIR = "tests/fixtures/providers/llm"

FIXTURES_DIR_ENV = "VIDEOFORGE_PROVIDER_FIXTURES"


def fixtures_dir() -> Path:
    """Where fixtures live. Mirrors ``VIDEOFORGE_PIPELINE_FILE``'s convention."""
    return Path(os.environ.get(FIXTURES_DIR_ENV) or DEFAULT_FIXTURES_DIR)


class MissingFixtureError(ProviderError):
    """Replay was asked for a call nobody recorded.

    Deliberately loud, and deliberately *not* a fallback to a real call. The
    message names the key and the re-record command, because the person hitting
    this is usually someone who changed a prompt and has no idea why CI is red.
    """

    def __init__(self, key: str, path: Path) -> None:
        super().__init__(
            f"no recorded fixture {key} at {path}. The request changed — "
            f"re-record with PROVIDERS__MODE=record and commit the fixture.",
            provider="replay",
        )
        self.key = key
        self.path = path


def fixture_key(req: LLMRequest) -> str:
    """A stable digest of everything that could change the answer.

    ``timeout_s`` is excluded on purpose: it affects whether a call *completes*,
    never what comes back, and including it would invalidate every fixture the
    day someone tunes a timeout.
    """
    canonical = req.model_dump(mode="json", exclude={"timeout_s"})
    blob = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


class RecordingLLMProvider:
    """Passes calls through to a real adapter and writes down what came back."""

    def __init__(self, inner: LLMProvider, *, directory: Path | None = None) -> None:
        self._inner = inner
        self._dir = directory or fixtures_dir()
        self.name = f"record:{inner.name}"

    def complete(self, req: LLMRequest) -> LLMResult:
        result = self._inner.complete(req)
        key = fixture_key(req)
        self._dir.mkdir(parents=True, exist_ok=True)
        path = self._dir / f"{key}.json"
        path.write_text(
            json.dumps(
                {
                    # The request is stored for humans, never read back: the key
                    # is the identity. Without it a fixture directory is 40
                    # opaque hashes and no way to tell which prompt is which.
                    "request": req.model_dump(mode="json"),
                    "result": result.model_dump(mode="json"),
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        logger.info("recorded provider fixture", extra={"key": key, "path": str(path)})
        return result


class ReplayLLMProvider:
    """Serves recorded results. Holds no client and cannot make a call."""

    name = "replay"

    def __init__(self, *, directory: Path | None = None) -> None:
        self._dir = directory or fixtures_dir()

    def complete(self, req: LLMRequest) -> LLMResult:
        key = fixture_key(req)
        path = self._dir / f"{key}.json"
        if not path.is_file():
            raise MissingFixtureError(key, path)
        payload = json.loads(path.read_text(encoding="utf-8"))
        return LLMResult.model_validate(payload["result"])
