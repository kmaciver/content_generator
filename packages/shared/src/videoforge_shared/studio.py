"""Image Studio limits and choices — shared by the API that validates a request
and the worker that runs it (ADR-018).

Here rather than in either app because both must agree, and the backend may not
import the providers package (the API never generates). The prices are quoted
to the person choosing a model; ``tests/test_studio.py`` pins them to
``videoforge_providers.pricing`` so the page and the spend meter cannot
disagree.

**Imagen is deliberately absent.** It is served by a different Google endpoint
(``generate_images``) from the one ``GoogleImageProvider`` calls
(``generate_content``), so offering it would hand the user a model that fails
on every request.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "STUDIO_ASPECT_RATIOS",
    "STUDIO_MAX_COUNT",
    "STUDIO_MAX_PROMPT_CHARS",
    "STUDIO_MAX_REFERENCES",
    "STUDIO_MAX_REFERENCE_BYTES",
    "STUDIO_REFERENCE_MIME_TYPES",
    "StudioModel",
    "STUDIO_MODELS",
]


@dataclass(frozen=True, slots=True)
class StudioModel:
    label: str
    description: str
    #: A decimal string, so it serialises without float rounding.
    price_per_image: str


#: Insertion order is display order; the first entry is the default.
STUDIO_MODELS: dict[str, StudioModel] = {
    "gemini-3.1-flash-image": StudioModel(
        label="Flash",
        description="Fast and inexpensive. Good for most images.",
        price_per_image="0.04",
    ),
    "gemini-3-pro-image": StudioModel(
        label="Pro",
        description="Slower, higher quality, better with text and detail.",
        price_per_image="0.14",
    ),
}

#: The ratios ``GoogleImageProvider`` declares it renders.
STUDIO_ASPECT_RATIOS: tuple[str, ...] = (
    "1:1",
    "3:4",
    "4:3",
    "2:3",
    "3:2",
    "9:16",
    "16:9",
)

#: Images per request. Each is a separate API call (~6 s, billed each), so the
#: ceiling keeps one click from turning into a surprise.
STUDIO_MAX_COUNT = 4

STUDIO_MAX_PROMPT_CHARS = 4000

#: Matches the adapter's ``_MAX_REFERENCES``.
STUDIO_MAX_REFERENCES = 8

#: Per uploaded reference. Phone photos are typically 2–6 MB.
STUDIO_MAX_REFERENCE_BYTES = 10 * 1024 * 1024

#: Accepted upload types, mapped to the extension used in the storage key —
#: which is also what the asset route derives the served Content-Type from.
STUDIO_REFERENCE_MIME_TYPES: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
}
