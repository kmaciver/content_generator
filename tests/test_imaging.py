"""M3-08 / finding B2: closing the gap between provider pixels and render pixels.

Split deliberately in two. The **geometry** is a pure function and is tested
everywhere, including the tooling image, which has no ffmpeg. The **encode**
needs a real ffmpeg and is skipped where there isn't one — so the arithmetic
that decides what a frame loses is never the part that goes untested.
"""

from __future__ import annotations

import shutil

import pytest

from videoforge_workers.imaging import CropPlan, crop_plan, normalise, png_dimensions

#: What Gemini actually returned for a ``9:16`` request on 2026-08-07, and what
#: the renderer wants. 768/1376 is 24:43, not 9:16 — which is the whole reason
#: this module exists.
MEASURED = (768, 1376)
RENDER = (1080, 1920)

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed in this image"
)


class TestGeometry:
    def test_the_measured_case(self) -> None:
        """768×1376 → 1080×1920 loses 15 rows and nothing else."""
        plan = crop_plan(*MEASURED, *RENDER)
        assert (plan.scale_width, plan.scale_height) == (1080, 1935)
        assert (plan.offset_x, plan.offset_y) == (0, 7)
        assert plan.discarded < 0.01

    def test_it_covers_rather_than_fits(self) -> None:
        """Both scaled dimensions reach the target, so the crop only ever
        removes pixels. Fitting instead would leave gaps needing bars, and bars
        in a full-bleed vertical video are worse than losing a strip of floor.
        """
        for source in (MEASURED, (1024, 1024), (1920, 1080), (100, 3000)):
            plan = crop_plan(*source, *RENDER)
            assert plan.scale_width >= plan.target_width
            assert plan.scale_height >= plan.target_height

    def test_the_crop_is_centred(self) -> None:
        plan = crop_plan(1024, 1024, *RENDER)
        assert plan.offset_y == 0
        assert plan.offset_x == (plan.scale_width - 1080) // 2

    def test_an_exact_match_is_identity(self) -> None:
        """A provider that natively offers the render size must cost nothing —
        no re-encode, no generation of quality."""
        plan = crop_plan(*RENDER, *RENDER)
        assert plan.is_identity
        assert plan.discarded == 0.0

    def test_a_square_source_reports_a_large_discard(self) -> None:
        """Not an error — the frame is still the best available — but the
        number is recorded so a badly cropped episode is visible rather than
        silent."""
        plan = crop_plan(1024, 1024, *RENDER)
        assert plan.discarded > 0.4

    def test_rounding_never_leaves_the_scale_short(self) -> None:
        """A scaled dimension one pixel under the target makes the crop
        impossible and ffmpeg fail. A rounding error must cost a wasted row,
        never a failed job."""
        for width in range(97, 130):
            for height in range(101, 134):
                plan = crop_plan(width, height, *RENDER)
                assert plan.scale_width >= plan.target_width
                assert plan.scale_height >= plan.target_height
                assert plan.offset_x >= 0
                assert plan.offset_y >= 0

    @pytest.mark.parametrize("source", [(0, 100), (100, 0), (-1, 10)])
    def test_a_source_with_no_area_is_rejected(self, source: tuple[int, int]) -> None:
        with pytest.raises(ValueError, match="no area"):
            crop_plan(*source, *RENDER)


class TestPngDimensions:
    def test_reads_ihdr(self) -> None:
        from videoforge_providers.mock import MockImageProvider
        from videoforge_providers.models import ImageRequest

        image = MockImageProvider().generate(ImageRequest(prompt="x")).images[0]
        assert png_dimensions(image.data) == (image.width, image.height)

    def test_rejects_something_that_is_not_a_png(self) -> None:
        """Called on ffmpeg's output as a self-check, so it has to actually
        check rather than read four bytes of whatever it was given."""
        with pytest.raises(ValueError, match="not a PNG"):
            png_dimensions(b"\xff\xd8\xff\xe0" + b"\x00" * 40)


@needs_ffmpeg
class TestNormalise:
    def _source(self, aspect: str) -> tuple[bytes, int, int]:
        from videoforge_providers.mock import MockImageProvider
        from videoforge_providers.models import ImageRequest

        image = (
            MockImageProvider()
            .generate(ImageRequest(prompt="x", aspect_ratio=aspect))
            .images[0]
        )
        return image.data, image.width, image.height

    def test_produces_exactly_the_render_size(self) -> None:
        data, width, height = self._source("9:16")
        frame = normalise(
            data, mime_type="image/png", width=width, height=height, target=RENDER
        )
        assert (frame.width, frame.height) == RENDER
        assert png_dimensions(frame.data) == RENDER
        assert frame.mime_type == "image/png"

    def test_a_square_source_still_lands_on_the_render_size(self) -> None:
        data, width, height = self._source("1:1")
        frame = normalise(
            data, mime_type="image/png", width=width, height=height, target=RENDER
        )
        assert png_dimensions(frame.data) == RENDER
        assert frame.plan.discarded > 0.4

    def test_an_exact_match_returns_the_original_bytes(self) -> None:
        """Identity must be byte-identical, not a re-encode that happens to be
        the same size — otherwise every frame pays a generation of quality for
        nothing."""
        data, width, height = self._source("9:16")
        frame = normalise(
            data,
            mime_type="image/png",
            width=width,
            height=height,
            target=(width, height),
        )
        assert frame.data is data
        assert frame.plan.is_identity

    def test_garbage_input_fails_loudly(self) -> None:
        """The renderer composites these 1:1; a frame that is quietly the wrong
        shape becomes a broken video found much later."""
        with pytest.raises(RuntimeError, match="ffmpeg failed"):
            normalise(
                b"not an image at all",
                mime_type="image/png",
                width=768,
                height=1376,
                target=RENDER,
            )


class TestCropPlanArithmetic:
    def test_discarded_is_area_not_a_single_axis(self) -> None:
        """A reviewer asking "how much did we lose" means area."""
        plan = CropPlan(
            source_width=100,
            source_height=100,
            scale_width=200,
            scale_height=100,
            offset_x=50,
            offset_y=0,
            target_width=100,
            target_height=100,
        )
        assert plan.discarded == pytest.approx(0.5)
