"""Request and result models for every provider (SADD §15.2).

**No SDK type ever crosses this boundary.** An ``anthropic.types.Message`` or an
``openai.ChatCompletion`` reaching a worker would put a vendor's schema in the
middle of the pipeline, and swapping providers would then mean rewriting the
consumer rather than the adapter. Everything in and out is a Pydantic model
defined here.

M1 defined only the LLM shapes. M3-01 adds the image ones; voice follows in
M3-12.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "GeneratedImage",
    "ImageCaps",
    "ImageReference",
    "ImageRequest",
    "ImageResult",
    "LLMMessage",
    "LLMRequest",
    "LLMResult",
    "ProviderError",
    "ProviderResult",
    "ProviderTimeoutError",
    "Usage",
]


class LLMMessage(BaseModel):
    """One turn. ``system`` is a role, not a separate parameter, because
    providers disagree about which it is and the adapter is the right place
    for that disagreement to be resolved."""

    model_config = ConfigDict(frozen=True)

    role: Literal["system", "user", "assistant"]
    content: str


class LLMRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    messages: tuple[LLMMessage, ...]
    #: A *hint*: the adapter resolves it against what the vendor actually
    #: offers. Empty means "the adapter's default", which is what keeps
    #: ``config/providers.yaml`` able to say ``model: ""`` and still work.
    model_hint: str = ""
    #: ``None`` means "whatever the provider defaults to", which is not the same as
    #: any particular number. Newer Claude models **reject** ``temperature``
    #: outright (`400: temperature is deprecated for this model`), so a
    #: non-optional field with a default made every call to them fail — the
    #: adapter had no way to tell "caller wants 0.7" from "caller said nothing".
    temperature: float | None = None
    max_tokens: int = 4096
    #: JSON-mode schema. When set, ``LLMResult.parsed`` carries the decoded
    #: object and the consumer never parses free text — the failure mode being
    #: avoided is a model that returns prose around valid JSON.
    response_schema: dict[str, Any] | None = None
    timeout_s: int = 120


class Usage(BaseModel):
    """What a call consumed. Feeds ``provider_usage`` and the S10 spend cap.

    Every field is optional because the meaning differs per modality — an
    image call has no tokens, a voice call has no images — and a zero would
    claim knowledge the adapter does not have.
    """

    model_config = ConfigDict(frozen=True)

    input_tokens: int | None = None
    output_tokens: int | None = None
    images: int | None = None
    audio_seconds: float | None = None
    #: Locally estimated, never billed. Named ``estimate`` so no one mistakes
    #: it for an invoice.
    unit_cost_estimate: float = 0.0


class ProviderResult(BaseModel):
    """What every modality's result carries, regardless of what it produced.

    Extracted in M3-01 so the middleware can meter an image call the same way
    it meters a completion. The three fields here are exactly what
    ``UsageRecorder`` and ``complete_generation`` read; anything modality-
    specific belongs on the subclass.
    """

    model_config = ConfigDict(frozen=True)

    usage: Usage = Field(default_factory=Usage)
    #: Adapter-specific detail — model actually used, finish reason, request
    #: id. Lands in ``artifact_version.meta`` for reproducibility (§10.3
    #: rule 4). Untyped on purpose: it is evidence, not contract.
    provider_meta: dict[str, Any] = Field(default_factory=dict)
    latency_ms: int = 0


class LLMResult(ProviderResult):
    text: str
    parsed: dict[str, Any] | None = None


class ImageCaps(BaseModel):
    """What an image adapter can actually do (ADR-016, the B3/S5 precedent).

    Checked at **configuration time** by ``registry.build_image_provider``, not
    at the first call. A provider that cannot accept reference images cannot do
    character consistency at all (R7), and discovering that twenty images into
    a user's first video is the outcome this exists to prevent.

    Conservative defaults: an adapter declares what it *has*, and one that
    forgets to declare anything is treated as capable of nothing rather than
    silently assumed to support everything.
    """

    model_config = ConfigDict(frozen=True)

    #: How many reference images may be sent with one request. Zero means the
    #: adapter cannot do reference-guided generation.
    max_reference_images: int = 0
    #: Deterministic seeds. The other half of style consistency (R7) — the
    #: same seed plus the same prompt should reproduce an image.
    supports_seed: bool = False
    supports_negative_prompt: bool = False
    #: Ratios the provider renders natively, e.g. ``("1:1", "9:16")``. Empty
    #: means the adapter makes no claim and the caller must normalise (B2).
    aspect_ratios: tuple[str, ...] = ()
    max_images_per_call: int = 1


class ImageReference(BaseModel):
    """One reference image handed to the provider.

    Bytes rather than a URL or a storage key: this package has no storage
    dependency and must not gain one (the same rule ``UsageRecorder`` follows
    for the database). The worker reads from ``StorageClient`` and passes the
    bytes in.
    """

    model_config = ConfigDict(frozen=True)

    #: ``repr=False`` so a logged request never dumps a megabyte of PNG into
    #: the log pipeline. The field is still there; it is just not printable.
    data: bytes = Field(repr=False)
    mime_type: str = "image/png"
    #: What this reference is *for* — pose, angle, expression, shot type.
    #: M3-07 selects references by these; the adapter may pass it through as a
    #: label or ignore it.
    role: str = ""


class ImageRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    prompt: str
    negative_prompt: str = ""
    #: An aspect **ratio**, not a pixel size. Providers render at their own
    #: native sizes, and asking for the nearest native ratio then deriving
    #: 1080×1920 (B2, M3-08) beats asking for pixels no provider offers and
    #: getting an upscale.
    aspect_ratio: str = "9:16"
    n: int = 1
    seed: int | None = None
    references: tuple[ImageReference, ...] = ()
    model_hint: str = ""
    #: Longer than the LLM default: image generation is slow, and the value
    #: mirrors SADD §14.2's 600s image queue ceiling with room to spare under
    #: it.
    timeout_s: int = 180


class GeneratedImage(BaseModel):
    model_config = ConfigDict(frozen=True)

    data: bytes = Field(repr=False)
    mime_type: str = "image/png"
    #: What the provider *actually* produced, which is not necessarily what was
    #: asked for — the normalisation step (B2) needs the real numbers.
    width: int = 0
    height: int = 0
    #: The seed actually used. Echoed back even when the caller supplied one,
    #: because reproducing an image later needs the value the provider used
    #: rather than the value we hoped it would.
    seed: int | None = None


class ImageResult(ProviderResult):
    images: tuple[GeneratedImage, ...] = ()


class ProviderError(RuntimeError):
    """An adapter failed in a way the caller may want to distinguish.

    Carries ``retryable`` because the retry decision belongs to the caller's
    policy, not to the exception type — a 429 and a 500 are both worth
    retrying, a 400 never is, and hiding that behind a class hierarchy makes
    the middleware guess.
    """

    def __init__(self, message: str, *, provider: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.provider = provider
        self.retryable = retryable


class ProviderTimeoutError(ProviderError):
    def __init__(self, message: str, *, provider: str) -> None:
        super().__init__(message, provider=provider, retryable=True)


class VoiceCaps(BaseModel):
    """What a voice adapter can do (M3-12, findings B3/S5).

    Checked at **configuration time** by ``registry.build_voice_provider``.
    Word timing is the one non-negotiable: scene boundaries and the caption
    track both derive from it, and discovering its absence after a narration
    has been synthesised means the audio is unusable and already paid for.

    Conservative defaults, matching ``ImageCaps``: an adapter declares what it
    has, and one that forgets is treated as capable of nothing.
    """

    model_config = ConfigDict(frozen=True)

    #: Whether timings precise enough to place individual words come back with
    #: the audio. **Not** "returns word objects" — measured against ElevenLabs
    #: on 2026-08-09, the response is one entry per *character*, and words are
    #: grouped from those by ``videoforge_domain.timing``. A capability defined
    #: as "returns words" would disqualify the provider that does this best.
    word_timings: bool = False
    #: Output container the adapter emits, e.g. ``audio/mpeg``. Recorded rather
    #: than assumed: the measured response is **MP3**, not WAV.
    mime_type: str = ""
    #: Longest script the adapter will accept in one call, in characters. Zero
    #: means unknown. B3 requires one call for the whole script, so a limit
    #: below a full narration is a disqualifying fact and not a detail.
    max_characters: int = 0


class VoiceRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    #: The **whole script**, not one scene (B3 revised). Twenty sentences read
    #: in isolation each get a complete intonation contour, terminal fall
    #: included, and concatenate into a list of statements rather than a
    #: narration.
    text: str
    voice_id: str = ""
    model_hint: str = ""
    #: Generous: a full narration is far more audio than a single image, and
    #: SADD §14.2 puts the voice queue's ceiling well above this.
    timeout_s: int = 300


class VoiceResult(ProviderResult):
    """Audio plus the timings the rest of the pipeline is built on."""

    #: ``repr=False`` so a logged result never dumps a megabyte of MP3.
    audio: bytes = Field(repr=False)
    mime_type: str = "audio/mpeg"
    #: Per-character alignment **of the text as written**, not as normalised.
    #: §1.0.2 found the reference displays a bare numeral as its own caption
    #: frame; a probe on 2026-08-09 put ``762`` at 0.743-1.533 s as one written
    #: token, where the normalised stream would have made it four words nobody
    #: wrote. The adapter picks the written alignment and never exposes the
    #: other one.
    characters: tuple[str, ...] = ()
    character_starts_s: tuple[float, ...] = ()
    character_ends_s: tuple[float, ...] = ()

    @property
    def duration_ms(self) -> int:
        """Total spoken length, from the last character's end."""
        if not self.character_ends_s:
            return 0
        return int(round(self.character_ends_s[-1] * 1000))
