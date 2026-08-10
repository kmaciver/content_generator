"""Google Gemini image generation — the "Nano Banana" family, behind ``ImageProvider``.

Chosen for one capability: **reference-image conditioning**. Risk R7 says style
and character consistency across ~20 hard cuts is what separates professional
output from a slideshow, and ADR-016's capability gate refuses at boot any
adapter that cannot accept references — because a provider that ignores them
produces twenty plausible images of twenty different characters, at full price,
diagnosable only by looking at them.

Imagen was the alternative in the same account and was not chosen. It has
native seed control, which this family appears to lack; it does not do
character consistency from references, which is the requirement. Reference
images beat seeds for R7, so ``supports_seed`` here is **False** and reference
conditioning carries the whole consistency story.

**No SDK type crosses this boundary** (SADD §15.2). ``google.genai`` is
imported inside this module and nowhere else, lazily, so a mock deployment
never needs the package installed.

Named ``google_adapter`` rather than ``google``: a module shadowing the
namespace package it imports is a debugging session nobody needs. Same reason
as ``anthropic_adapter`` and ``prompts_stage``.

**Verified against the live API on 2026-08-07**, ``gemini-3.1-flash-image``:
``generate_content`` with ``response_modalities=["IMAGE"]``,
``image_config.aspect_ratio`` and ``Part.from_bytes`` returned 200. Three
observations from that call that the rest of the pipeline depends on:

* **Output is JPEG, not PNG.** The mock emits PNG, so the offline path and the
  real one differ in format — anything downstream that assumes PNG (thumbnails,
  the contact sheet, M3-08's normalisation) has to read ``mime_type`` rather
  than the file extension. It also means references fed back into a later
  generation are lossy, so a character sheet re-encoded through several
  regenerations will drift; storing the *original* bytes content-addressed
  (ADR-004) rather than a re-encode is what keeps that from compounding.
* **Dimensions are approximate, not exact.** A ``9:16`` request came back
  768×1376, which is 24:43 — about 0.8% narrower than 9:16. So B2's
  normalisation (M3-08) is a real crop-or-pad decision against 1080×1920, not a
  scale, and it must work from ``GeneratedImage.width/height`` rather than from
  what was asked for.
* **~6 s per image.** Twenty scenes serially is about two minutes, comfortably
  inside §14.2's 600 s image-queue ceiling, and a four-candidate run is ~24 s.

The ``AFC is enabled`` line the SDK logs is automatic function calling, which
does nothing here because no tools are declared. Left alone deliberately: this
call signature is verified working, and perturbing it for log cosmetics is a
bad trade.
"""

from __future__ import annotations

import logging
import struct
import time
from typing import Any

from videoforge_providers.models import (
    GeneratedImage,
    ImageCaps,
    ImageRequest,
    ImageResult,
    ProviderError,
    ProviderTimeoutError,
    Usage,
)
from videoforge_providers.pricing import estimate_image_cost

logger = logging.getLogger(__name__)

__all__ = ["DEFAULT_MODEL", "GoogleImageProvider"]

#: "Nano Banana 2". The **stable** id, not the ``-preview`` sibling: a preview
#: id can be withdrawn, and a pipeline that pins branding for reproducibility
#: (§10.3 rule 4) should not depend on one. Overridable per deployment via
#: ``PROVIDERS__IMAGE__MODEL``.
DEFAULT_MODEL = "gemini-3.1-flash-image"

#: Ratios the model renders. Declared rather than assumed universal, so an
#: exotic ``ImageRequest.aspect_ratio`` is a caller error the normalisation step
#: (B2, M3-08) has to resolve rather than a silent square crop.
#:
#: **These are honoured approximately.** A ``9:16`` request measured 768×1376 —
#: 24:43, ~0.8% off. Treat the value as a request, and the returned
#: ``width``/``height`` as the truth.
_ASPECT_RATIOS: tuple[str, ...] = ("1:1", "2:3", "3:2", "3:4", "4:3", "9:16", "16:9")

#: How many reference images to accept. Generous rather than exact: the family
#: takes several, and the gate only needs to know it takes *at least one*.
_MAX_REFERENCES = 8


class GoogleImageProvider:
    """Gemini image generation via the official ``google-genai`` SDK."""

    name = "google"

    def __init__(
        self,
        *,
        api_key: str = "",
        model: str = "",
        timeout_s: int = 180,
        client: Any = None,
    ) -> None:
        """Build against the real SDK, or against an injected client.

        ``client`` exists for the same reason ``AnthropicLLMProvider``'s does:
        the translation logic should be testable with no key and no network. A
        seam beats a test that reaches into private attributes — that test
        passes until the attribute is renamed and then fails for a reason
        unrelated to the behaviour it was checking.
        """
        self._model = model or DEFAULT_MODEL
        self._timeout_s = timeout_s

        if client is not None:
            self._client: Any = client
            return

        from google import genai

        if not api_key:
            # Fails at construction — configuration time — rather than several
            # seconds into a user's first generation. Same reasoning as
            # `UnknownAdapterError`.
            raise ProviderError(
                "google adapter selected but GOOGLE_API_KEY is empty",
                provider=self.name,
            )
        self._client = genai.Client(api_key=api_key)

    def capabilities(self) -> ImageCaps:
        """What this adapter supports. No I/O — it runs during startup.

        ``supports_seed=False`` is the conservative declaration, not a measured
        one. If the family does expose a seed, saying so here later is a
        one-line change; claiming it now and being wrong would let M3-07 build
        a reproducibility story on a parameter the provider ignores.
        """
        return ImageCaps(
            max_reference_images=_MAX_REFERENCES,
            supports_seed=False,
            # Negative prompts are not a first-class parameter on this family
            # the way they are on diffusion APIs. The prompt builder folds
            # exclusions into the positive text instead — see `_compose`.
            supports_negative_prompt=False,
            aspect_ratios=_ASPECT_RATIOS,
            max_images_per_call=1,
        )

    def generate(self, req: ImageRequest) -> ImageResult:
        """Render ``req.n`` images.

        **One API call per image, deliberately.** ``generate_content`` returns a
        single candidate for this family, and without a seed there is no way to
        ask for four *deterministic* variations in one call. M3-04 wants 4–8
        genuinely different candidates, so N calls is what that costs — and
        making it explicit here beats a caller assuming batching is free.
        """
        started = time.monotonic()
        model = req.model_hint or self._model

        images: list[GeneratedImage] = []
        metas: list[dict[str, Any]] = []
        for _ in range(max(1, req.n)):
            response = self._call(model, req)
            image, meta = _extract(response)
            images.append(image)
            metas.append(meta)

        return ImageResult(
            images=tuple(images),
            usage=Usage(
                images=len(images),
                unit_cost_estimate=estimate_image_cost(model, len(images)),
            ),
            provider_meta={
                "provider": self.name,
                "model": model,
                "aspect_ratio": req.aspect_ratio,
                "references": len(req.references),
                "calls": len(images),
                # Per-call detail (finish reason, safety verdicts) kept as a
                # list: with N calls there is no single answer, and collapsing
                # them would hide the one that was filtered.
                "per_call": metas,
            },
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    def _call(self, model: str, req: ImageRequest) -> Any:
        """One API call, with vendor exceptions translated to a retry decision.

        The split is the usual one: transport trouble and rate limits are worth
        another go, a malformed request or a bad key never is. Retrying those
        burns budget and delays the fix — and on images "burns budget" is
        literal rather than theoretical.
        """
        from google.genai import errors as genai_errors
        from google.genai import types

        parts: list[Any] = [types.Part.from_text(text=_compose(req))]
        for reference in req.references:
            # References follow the prompt. The model reads them as "make it
            # look like this", and a reference before the instruction reads as
            # the subject of a question instead.
            parts.append(
                types.Part.from_bytes(
                    data=reference.data, mime_type=reference.mime_type
                )
            )

        config = types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            image_config=types.ImageConfig(aspect_ratio=req.aspect_ratio),
            http_options=types.HttpOptions(timeout=self._timeout_s * 1000),
        )

        try:
            return self._client.models.generate_content(
                model=model,
                contents=[types.Content(role="user", parts=parts)],
                config=config,
            )
        except genai_errors.APIError as exc:
            status = getattr(exc, "code", 0) or 0
            if status == 408 or status == 504:
                raise ProviderTimeoutError(str(exc), provider=self.name) from exc
            raise ProviderError(
                f"google returned {status}: {exc}",
                provider=self.name,
                retryable=status == 429 or status >= 500,
            ) from exc
        except Exception as exc:  # transport, DNS, TLS — no vendor class
            raise ProviderError(
                f"google call failed: {exc}", provider=self.name, retryable=True
            ) from exc


def _compose(req: ImageRequest) -> str:
    """The prompt text actually sent.

    This family has no separate negative-prompt parameter, so exclusions are
    appended as an instruction. Phrased as "Do not include" rather than
    "Avoid:" — a bare noun list after "Avoid" is a well-known way to *get* the
    nouns, because the model reads the list and not the instruction.
    """
    if not req.negative_prompt.strip():
        return req.prompt
    return f"{req.prompt}\n\nDo not include any of: {req.negative_prompt.strip()}."


def _extract(response: Any) -> tuple[GeneratedImage, dict[str, Any]]:
    """Pull the image bytes out of a ``generate_content`` response.

    Parts are **scanned**, not indexed: the response legitimately carries a text
    part alongside the image (the model narrating what it drew), and assuming
    ``parts[0]`` is the picture works right up until it does not.
    """
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        raise ProviderError(
            "google returned no candidates; the prompt was most likely "
            "blocked by a safety filter",
            provider="google",
            # Not retryable: the same prompt will be blocked again, and three
            # attempts would bill three times for one refusal.
            retryable=False,
        )

    candidate = candidates[0]
    finish_reason = getattr(candidate, "finish_reason", None)
    content = getattr(candidate, "content", None)
    parts = getattr(content, "parts", None) or []

    for part in parts:
        inline = getattr(part, "inline_data", None)
        data = getattr(inline, "data", None) if inline else None
        if not data:
            continue
        mime_type = getattr(inline, "mime_type", "image/png") or "image/png"
        width, height = _dimensions(data, mime_type)
        return (
            GeneratedImage(
                data=data,
                mime_type=mime_type,
                width=width,
                height=height,
                # No seed to echo — see `capabilities()`.
                seed=None,
            ),
            {"finish_reason": str(finish_reason) if finish_reason else None},
        )

    raise ProviderError(
        f"google returned no image part (finish_reason={finish_reason!r}); "
        "the request was answered but produced no picture",
        provider="google",
        retryable=True,
    )


def _dimensions(data: bytes, mime_type: str) -> tuple[int, int]:
    """Read pixel dimensions from the image header.

    Parsed rather than assumed, because ``GeneratedImage.width/height`` mean
    *what the provider actually produced* — which is not necessarily what was
    asked for — and B2's normalisation (M3-08) has to make its decision from
    real numbers. Header-only: decoding the pixels to count them would pull in
    an image library for two integers.

    Unknown formats return ``(0, 0)`` rather than raising. A provider that
    starts returning AVIF should not fail a generation that is otherwise fine;
    it should show up as an unnormalisable image downstream, where there is a
    human looking.
    """
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            # IHDR is always the first chunk: 8 signature + 4 length + 4 type.
            width, height = struct.unpack(">II", data[16:24])
            return int(width), int(height)
        if data[:2] == b"\xff\xd8":
            return _jpeg_dimensions(data)
    except (struct.error, IndexError, ValueError):
        logger.warning("could not read image dimensions", extra={"mime": mime_type})
    return 0, 0


def _jpeg_dimensions(data: bytes) -> tuple[int, int]:
    """Walk JPEG segments to the frame header.

    JPEG has no fixed dimension offset — the size lives in a Start-Of-Frame
    marker that appears after a variable number of other segments, so the only
    correct way to find it is to walk them.
    """
    index = 2
    # ``index + 9 <= len`` rather than ``index < len - 9``: the latter is off by
    # one and skips a frame header that ends exactly at the end of the buffer.
    # Real JPEGs carry scan data after the header so it never bit in practice —
    # which is precisely why it needed a test to find.
    while index + 9 <= len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        # SOF0-SOF15 carry the dimensions; C4 (Huffman), C8 (JPG extension) and
        # CC (arithmetic conditioning) share the range and do not.
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            return int(width), int(height)
        segment_length = struct.unpack(">H", data[index + 2 : index + 4])[0]
        index += 2 + segment_length
    return 0, 0
