"""The image seam, its capability gate, and cost estimation (M3-01, M3-11)."""

from __future__ import annotations

import struct
import zlib
from decimal import Decimal
from typing import Any

import pytest

from videoforge_providers.middleware import with_standard_image_middleware
from videoforge_providers.mock import MockImageProvider
from videoforge_providers.models import (
    ImageCaps,
    ImageReference,
    ImageRequest,
    ImageResult,
    ProviderError,
)
from videoforge_providers.pricing import estimate_image_cost, estimate_llm_cost
from videoforge_providers.protocols import ImageProvider
from videoforge_providers.registry import (
    CapabilityError,
    UnknownAdapterError,
    build_image_provider,
    require_image_caps,
)
from videoforge_shared.settings import ProviderSettings

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _settings(**over: Any) -> ProviderSettings:
    data: dict[str, Any] = {"mode": "mock", "image": {"adapter": "mock"}}
    data.update(over)
    return ProviderSettings.model_validate(data)


class TestMockImageProvider:
    def test_satisfies_the_protocol(self) -> None:
        """``runtime_checkable``, so the registry can assert this at
        configuration time rather than at the first call."""
        assert isinstance(MockImageProvider(), ImageProvider)

    def test_emits_a_real_decodable_png(self) -> None:
        """Not a fixed byte string and not a 1x1 pixel.

        These bytes travel through content-addressed storage, the
        normalisation step (B2) and an ``<img>`` tag. Every one of those is a
        place a not-quite-an-image fails late and obscurely.
        """
        result = MockImageProvider().generate(ImageRequest(prompt="a round head"))
        image = result.images[0]

        assert image.data.startswith(PNG_SIGNATURE)

        # Parse the IHDR back out: length(4) type(4) then width, height.
        width, height = struct.unpack(">II", image.data[16:24])
        assert (width, height) == (image.width, image.height)

        # And the pixel data really is a valid zlib stream, which is the half a
        # plausible-looking header would not catch.
        idat_start = image.data.index(b"IDAT") + 4
        assert zlib.decompress(image.data[idat_start : idat_start + 1000][:-4])

    def test_is_deterministic_across_calls(self) -> None:
        """Same reasoning as the LLM mock: seeded from sha256, never from
        ``hash()`` (salted per process) or the clock."""
        req = ImageRequest(prompt="identical")
        assert (
            MockImageProvider().generate(req).images[0].data
            == MockImageProvider().generate(req).images[0].data
        )

    def test_different_prompts_differ(self) -> None:
        a = MockImageProvider().generate(ImageRequest(prompt="one")).images[0]
        b = MockImageProvider().generate(ImageRequest(prompt="two")).images[0]
        assert a.data != b.data

    def test_a_batch_returns_distinct_images(self) -> None:
        """M3-04 approves one of 4-8 *candidates*. Four identical pictures
        would give the selection UI nothing to select between."""
        result = MockImageProvider().generate(ImageRequest(prompt="x", n=4))
        assert len(result.images) == 4
        assert len({image.data for image in result.images}) == 4
        assert len({image.seed for image in result.images}) == 4

    @pytest.mark.parametrize(
        ("ratio", "portrait"),
        [("9:16", True), ("1:1", False), ("16:9", False)],
    )
    def test_honours_the_requested_aspect_ratio(
        self, ratio: str, portrait: bool
    ) -> None:
        image = (
            MockImageProvider()
            .generate(ImageRequest(prompt="x", aspect_ratio=ratio))
            .images[0]
        )
        assert (image.height > image.width) is portrait

    def test_an_unparseable_ratio_falls_back_rather_than_raising(self) -> None:
        """The mock's job is to keep the offline path running; a bad ratio
        should surface from the real adapter's validation, not the fake one."""
        image = (
            MockImageProvider()
            .generate(ImageRequest(prompt="x", aspect_ratio="banana"))
            .images[0]
        )
        assert image.height > image.width

    def test_records_that_references_were_passed(self) -> None:
        """Otherwise invisible: the mock cannot *use* references, so a caller
        that silently dropped them would look identical."""
        result = MockImageProvider().generate(
            ImageRequest(
                prompt="x",
                references=(ImageReference(data=b"\x89PNG", role="front"),),
            )
        )
        assert result.provider_meta["references"] == 1

    def test_reference_bytes_stay_out_of_the_repr(self) -> None:
        """A logged request must not dump a megabyte of PNG into the pipeline."""
        assert "PNG" not in repr(ImageReference(data=b"\x89PNG-and-more"))


class TestCapabilityGate:
    def test_rejects_an_adapter_without_reference_support(self) -> None:
        """ADR-016's gate. A provider that ignores references produces twenty
        plausible images of twenty different characters — expensive, and only
        diagnosable by looking at them."""
        with pytest.raises(CapabilityError, match="reference"):
            require_image_caps(ImageCaps(max_reference_images=0), adapter="flat")

    def test_accepts_an_adapter_with_exactly_one(self) -> None:
        """One is enough to hold a character across scenes, which is what R7
        actually asks for. Demanding a number nobody publishes would
        disqualify capable adapters for no gain."""
        require_image_caps(ImageCaps(max_reference_images=1), adapter="ok")

    def test_can_be_switched_off_deliberately(self) -> None:
        """Abstract or diagram-only videos have no recurring character. A rule
        with no escape hatch is a rule people route around."""
        require_image_caps(
            ImageCaps(max_reference_images=0), adapter="flat", min_references=0
        )

    def test_the_error_names_the_way_out(self) -> None:
        with pytest.raises(CapabilityError) as caught:
            require_image_caps(ImageCaps(), adapter="flat")
        assert "MIN_REFERENCE_IMAGES" in str(caught.value)


class TestBuildImageProvider:
    def test_builds_the_mock(self) -> None:
        assert build_image_provider(_settings(), with_middleware=False).name == "mock"

    def test_mock_mode_beats_a_real_adapter(self) -> None:
        provider = build_image_provider(
            _settings(mode="mock", image={"adapter": "some-vendor"}),
            with_middleware=False,
        )
        assert provider.name == "mock"

    def test_an_unknown_adapter_lists_the_valid_ones(self) -> None:
        with pytest.raises(UnknownAdapterError, match="mock"):
            build_image_provider(
                _settings(mode="real", image={"adapter": "midjourney"}),
                with_middleware=False,
            )

    @pytest.mark.parametrize("mode", ["record", "replay"])
    def test_record_and_replay_fail_loudly_rather_than_downgrading(
        self, mode: str
    ) -> None:
        """Replay's whole value is that CI exercises a *real* adapter's output
        offline. Quietly substituting the mock's pictures would make a passing
        run meaningless."""
        with pytest.raises(UnknownAdapterError, match="M3-04"):
            build_image_provider(_settings(mode=mode), with_middleware=False)

    def test_middleware_passes_capabilities_through(self) -> None:
        """The gate reads them through whatever wrappers are in place."""
        wrapped = with_standard_image_middleware(MockImageProvider())
        assert wrapped.capabilities().max_reference_images == 8

    def test_middleware_meters_every_call(self) -> None:
        wrapped = with_standard_image_middleware(MockImageProvider())
        wrapped.generate(ImageRequest(prompt="x", n=2))
        assert len(wrapped.calls) == 1
        assert wrapped.calls[0].usage.images == 2


class TestRetryIsSharedPolicy:
    class _Flaky:
        name = "flaky"

        def __init__(self, failures: int, *, retryable: bool) -> None:
            self.remaining = failures
            self.retryable = retryable
            self.attempts = 0

        def capabilities(self) -> ImageCaps:
            return ImageCaps(max_reference_images=1)

        def generate(self, req: ImageRequest) -> ImageResult:
            self.attempts += 1
            if self.remaining:
                self.remaining -= 1
                raise ProviderError(
                    "boom", provider=self.name, retryable=self.retryable
                )
            return ImageResult()

    def test_retries_a_retryable_failure(self) -> None:
        from videoforge_providers.middleware import RetryingImageProvider

        inner = self._Flaky(2, retryable=True)
        RetryingImageProvider(inner, backoff_s=0, sleep=lambda _: None).generate(
            ImageRequest(prompt="x")
        )
        assert inner.attempts == 3

    def test_does_not_retry_a_non_retryable_one(self) -> None:
        """Images make this expensive rather than merely slow — retrying a 400
        three times bills three times for the same rejection."""
        from videoforge_providers.middleware import RetryingImageProvider

        inner = self._Flaky(1, retryable=False)
        with pytest.raises(ProviderError):
            RetryingImageProvider(inner, backoff_s=0, sleep=lambda _: None).generate(
                ImageRequest(prompt="x")
            )
        assert inner.attempts == 1


class TestPricing:
    def test_prices_a_known_model(self) -> None:
        # 1M in + 1M out at 3.00/15.00 = 18.00
        assert estimate_llm_cost(
            "claude-sonnet-5", 1_000_000, 1_000_000
        ) == pytest.approx(18.0)

    def test_resolves_a_dated_model_id_by_prefix(self) -> None:
        """Vendors publish prices per family and date the ids; an entry per
        release would rot immediately."""
        assert estimate_llm_cost("claude-sonnet-5-20260101", 1_000_000, 0) == (
            pytest.approx(3.0)
        )

    def test_longest_prefix_wins(self) -> None:
        """``claude-opus`` must not resolve against a shorter ``claude`` entry
        depending on dict ordering."""
        assert estimate_llm_cost("claude-opus-5", 1_000_000, 0) == pytest.approx(15.0)

    def test_an_unknown_model_costs_zero_rather_than_raising(self) -> None:
        """A missing price entry must not fail a generation that is otherwise
        fine — but it warns, because silently under-counting is exactly how a
        cap stops capping."""
        assert estimate_llm_cost("gpt-9-ultra", 1_000, 1_000) == 0.0

    def test_none_token_counts_are_treated_as_zero(self) -> None:
        assert estimate_llm_cost("claude-sonnet-5", None, None) == 0.0

    def test_the_mock_is_priced_at_zero_deliberately(self) -> None:
        """A *deliberate* entry, not a missing one — so real typos stay visible
        in the unknown-model warning path."""
        assert estimate_llm_cost("mock-llm-v1", 1_000_000, 1_000_000) == 0.0
        assert estimate_image_cost("mock-image-v1", 20) == 0.0

    def test_mock_results_carry_a_cost_estimate_field(self) -> None:
        """Zero here, but the field is populated through the same table a real
        adapter uses, so the metering path is exercised offline."""
        result = MockImageProvider().generate(ImageRequest(prompt="x"))
        assert result.usage.unit_cost_estimate == 0.0
        assert result.usage.images == 1

    def test_prices_are_decimal_not_float(self) -> None:
        """Money arithmetic happens in Decimal; only the final estimate is a
        float, because that is what the column is."""
        from videoforge_providers.pricing import LLM_PRICES

        assert isinstance(LLM_PRICES["claude-sonnet"].input_per_mtok, Decimal)
