"""The Gemini image adapter's translation logic (M3-04).

Everything here runs through an **injected client** — no key, no network. What
is under test is the part this repository owns: how references are packed, how
parts are read back, how vendor errors become a retry decision, and how pixel
dimensions are recovered. The one thing these cannot cover is whether the SDK
call signature is right, which the first real call answers immediately.
"""

from __future__ import annotations

import struct
import zlib
from typing import Any

import pytest

from videoforge_providers.google_adapter import (
    DEFAULT_MODEL,
    GoogleImageProvider,
    _compose,
    _dimensions,
)
from videoforge_providers.models import (
    ImageReference,
    ImageRequest,
    ProviderError,
)
from videoforge_providers.protocols import ImageProvider


def _png(width: int, height: int) -> bytes:
    """A real PNG of known size, for the dimension parser to read back."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + b"\x40\x80\xc0" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 1))
        + chunk(b"IEND", b"")
    )


class _Part:
    def __init__(self, *, data: bytes | None = None, mime: str = "image/png") -> None:
        self.inline_data = _Inline(data, mime) if data is not None else None
        self.text = None if data is not None else "here is your picture"


class _Inline:
    def __init__(self, data: bytes, mime: str) -> None:
        self.data = data
        self.mime_type = mime


class _Candidate:
    def __init__(self, parts: list[Any], finish_reason: str = "STOP") -> None:
        self.content = type("C", (), {"parts": parts})()
        self.finish_reason = finish_reason


class _Response:
    def __init__(self, candidates: list[Any]) -> None:
        self.candidates = candidates


class _FakeModels:
    """Records what was sent and returns what it was told to."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def generate_content(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class _FakeClient:
    def __init__(self, responses: list[Any]) -> None:
        self.models = _FakeModels(responses)


def _provider(responses: list[Any]) -> tuple[GoogleImageProvider, _FakeClient]:
    client = _FakeClient(responses)
    return GoogleImageProvider(client=client), client


def _ok_response(width: int = 64, height: int = 114) -> _Response:
    return _Response([_Candidate([_Part(), _Part(data=_png(width, height))])])


class TestCapabilities:
    def test_satisfies_the_protocol(self) -> None:
        assert isinstance(GoogleImageProvider(client=_FakeClient([])), ImageProvider)

    def test_declares_reference_support(self) -> None:
        """The whole reason this vendor was chosen — and the thing ADR-016's
        gate checks at boot."""
        caps = GoogleImageProvider(client=_FakeClient([])).capabilities()
        assert caps.max_reference_images >= 1

    def test_declares_no_seed_support(self) -> None:
        """Conservative, not measured. Claiming a seed this family may ignore
        would let M3-07 build a reproducibility story on a no-op parameter."""
        assert (
            GoogleImageProvider(client=_FakeClient([])).capabilities().supports_seed
            is False
        )

    def test_passes_the_registry_gate(self) -> None:
        from videoforge_providers.registry import require_image_caps

        require_image_caps(
            GoogleImageProvider(client=_FakeClient([])).capabilities(), adapter="google"
        )

    def test_capabilities_performs_no_io(self) -> None:
        """It runs during startup, before anything is known to be reachable."""
        provider, client = _provider([])
        provider.capabilities()
        assert client.models.calls == []


class TestGeneration:
    def test_returns_the_image_bytes(self) -> None:
        provider, _ = _provider([_ok_response()])
        result = provider.generate(ImageRequest(prompt="a pale circle"))
        assert result.images[0].data.startswith(b"\x89PNG")

    def test_reads_the_real_dimensions_back(self) -> None:
        """``width``/``height`` mean what the provider *produced*, not what was
        asked for — B2's normalisation decides from these."""
        provider, _ = _provider([_ok_response(width=72, height=128)])
        image = provider.generate(ImageRequest(prompt="x")).images[0]
        assert (image.width, image.height) == (72, 128)

    def test_scans_parts_rather_than_indexing(self) -> None:
        """The response legitimately carries a text part alongside the image;
        assuming ``parts[0]`` is the picture works until it does not."""
        provider, _ = _provider([_ok_response()])
        assert provider.generate(ImageRequest(prompt="x")).images

    def test_sends_references_after_the_prompt(self) -> None:
        """The model reads them as "make it look like this". A reference before
        the instruction reads as the subject of a question instead."""
        provider, client = _provider([_ok_response()])
        provider.generate(
            ImageRequest(
                prompt="a pale circle",
                references=(
                    ImageReference(data=_png(8, 8), role="front"),
                    ImageReference(data=_png(8, 8), role="side"),
                ),
            )
        )
        parts = client.models.calls[0]["contents"][0].parts
        assert len(parts) == 3  # prompt + two references
        assert result_text(parts[0]) == "a pale circle"

    def test_n_becomes_n_calls(self) -> None:
        """No seed and one candidate per response, so four variations cost four
        calls. Explicit here so no caller assumes batching is free."""
        provider, client = _provider([_ok_response() for _ in range(4)])
        result = provider.generate(ImageRequest(prompt="x", n=4))
        assert len(result.images) == 4
        assert len(client.models.calls) == 4

    def test_meters_every_image(self) -> None:
        provider, _ = _provider([_ok_response() for _ in range(3)])
        usage = provider.generate(ImageRequest(prompt="x", n=3)).usage
        assert usage.images == 3
        assert usage.unit_cost_estimate > 0

    def test_uses_the_configured_model_by_default(self) -> None:
        provider, client = _provider([_ok_response()])
        provider.generate(ImageRequest(prompt="x"))
        assert client.models.calls[0]["model"] == DEFAULT_MODEL

    def test_model_hint_overrides(self) -> None:
        provider, client = _provider([_ok_response()])
        provider.generate(ImageRequest(prompt="x", model_hint="gemini-3-pro-image"))
        assert client.models.calls[0]["model"] == "gemini-3-pro-image"


class TestNegativePrompt:
    def test_is_folded_into_the_text(self) -> None:
        """This family has no negative-prompt parameter, so exclusions have to
        travel in the prompt."""
        composed = _compose(
            ImageRequest(prompt="a pale circle", negative_prompt="photorealism, text")
        )
        assert "photorealism, text" in composed

    def test_is_phrased_as_an_instruction_not_a_list(self) -> None:
        """A bare noun list after "Avoid:" is a well-known way to *get* the
        nouns — the model reads the list, not the instruction."""
        composed = _compose(ImageRequest(prompt="x", negative_prompt="hats"))
        assert "Do not include" in composed
        assert not composed.strip().endswith("Avoid: hats")

    def test_an_empty_negative_leaves_the_prompt_alone(self) -> None:
        assert _compose(ImageRequest(prompt="just this")) == "just this"


class TestErrors:
    def _api_error(self, code: int) -> Exception:
        from google.genai import errors as genai_errors

        error = genai_errors.APIError.__new__(genai_errors.APIError)
        error.code = code
        error.message = f"boom {code}"
        error.args = (f"boom {code}",)
        return error

    @pytest.mark.parametrize("code", [429, 500, 503])
    def test_rate_limits_and_server_errors_are_retryable(self, code: int) -> None:
        provider, _ = _provider([self._api_error(code)])
        with pytest.raises(ProviderError) as caught:
            provider.generate(ImageRequest(prompt="x"))
        assert caught.value.retryable is True

    @pytest.mark.parametrize("code", [400, 401, 403])
    def test_client_errors_are_not_retryable(self, code: int) -> None:
        """On images this is not academic: three attempts bill three times for
        one rejection."""
        provider, _ = _provider([self._api_error(code)])
        with pytest.raises(ProviderError) as caught:
            provider.generate(ImageRequest(prompt="x"))
        assert caught.value.retryable is False

    def test_a_transport_failure_is_retryable(self) -> None:
        provider, _ = _provider([OSError("connection reset")])
        with pytest.raises(ProviderError) as caught:
            provider.generate(ImageRequest(prompt="x"))
        assert caught.value.retryable is True

    def test_a_safety_block_is_not_retryable(self) -> None:
        """No candidates means the prompt was refused. The same prompt will be
        refused again, and retrying bills three times for one refusal."""
        provider, _ = _provider([_Response([])])
        with pytest.raises(ProviderError) as caught:
            provider.generate(ImageRequest(prompt="x"))
        assert caught.value.retryable is False
        assert "safety" in str(caught.value)

    def test_a_response_with_no_image_part_is_retryable(self) -> None:
        """Answered but produced no picture — a transient model mood rather
        than a refusal, so worth one more go."""
        provider, _ = _provider([_Response([_Candidate([_Part()])])])
        with pytest.raises(ProviderError) as caught:
            provider.generate(ImageRequest(prompt="x"))
        assert caught.value.retryable is True

    def test_a_missing_key_fails_at_construction(self) -> None:
        """Configuration time, not several seconds into a user's first video."""
        with pytest.raises(ProviderError, match="GOOGLE_API_KEY"):
            GoogleImageProvider(api_key="")


class TestDimensionParsing:
    def test_reads_png(self) -> None:
        assert _dimensions(_png(120, 240), "image/png") == (120, 240)

    def test_reads_jpeg(self) -> None:
        """JPEG has no fixed offset — the size lives in a SOF marker after a
        variable number of segments, so the parser has to walk them."""
        sof = (
            b"\xff\xc0" + struct.pack(">H", 17) + b"\x08" + struct.pack(">HH", 200, 100)
        )
        data = b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 4) + b"JF" + sof
        assert _dimensions(data, "image/jpeg") == (100, 200)

    def test_an_unknown_format_returns_zeros_rather_than_raising(self) -> None:
        """A provider that starts returning AVIF should surface as an
        unnormalisable image downstream, where a human is looking — not as a
        failed generation."""
        assert _dimensions(b"RIFF????WEBPVP8 ", "image/webp") == (0, 0)

    def test_truncated_data_returns_zeros(self) -> None:
        assert _dimensions(b"\x89PNG\r\n\x1a\n", "image/png") == (0, 0)


def result_text(part: Any) -> str:
    """The text a ``Part`` carries, however the SDK spells it."""
    return getattr(part, "text", "") or ""
