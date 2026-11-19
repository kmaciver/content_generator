"""Image Studio request and response shapes (ADR-018).

A module of its own rather than more of ``dto/__init__.py``: nothing else in
the API refers to these, and the studio is meant to stay separable from the
pipeline.

Validation is here, against ``videoforge_shared.studio`` — the client's form
offers the same choices, but the server is the one that has to be right.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from videoforge_persistence.repositories import StudioEntry
from videoforge_shared.enums import JobStatus
from videoforge_shared.studio import (
    STUDIO_ASPECT_RATIOS,
    STUDIO_MAX_COUNT,
    STUDIO_MAX_PROMPT_CHARS,
    STUDIO_MAX_REFERENCE_BYTES,
    STUDIO_MAX_REFERENCES,
    STUDIO_MODELS,
    STUDIO_REFERENCE_MIME_TYPES,
)

__all__ = [
    "CreateStudioGenerationRequest",
    "StudioGenerationSummary",
    "StudioImageSummary",
    "StudioModelOption",
    "StudioOptions",
    "StudioPage",
    "StudioReferenceResponse",
    "UploadStudioReferenceRequest",
]

#: What the gallery shows. Collapses the job's statuses into the four a person
#: cares about; ORPHANED and CANCELLED both read as "it did not work".
StudioStatus = Literal["QUEUED", "RUNNING", "SUCCEEDED", "FAILED"]

_STATUS: dict[JobStatus, StudioStatus] = {
    JobStatus.QUEUED: "QUEUED",
    JobStatus.RUNNING: "RUNNING",
    JobStatus.SUCCEEDED: "SUCCEEDED",
}


class StudioModelOption(BaseModel):
    id: str
    label: str
    description: str
    price_per_image: str


class StudioOptions(BaseModel):
    models: list[StudioModelOption]
    aspect_ratios: list[str]
    max_count: int
    max_references: int
    max_reference_bytes: int
    reference_mime_types: list[str]

    @classmethod
    def current(cls) -> StudioOptions:
        return cls(
            models=[
                StudioModelOption(
                    id=model_id,
                    label=model.label,
                    description=model.description,
                    price_per_image=model.price_per_image,
                )
                for model_id, model in STUDIO_MODELS.items()
            ],
            aspect_ratios=list(STUDIO_ASPECT_RATIOS),
            max_count=STUDIO_MAX_COUNT,
            max_references=STUDIO_MAX_REFERENCES,
            max_reference_bytes=STUDIO_MAX_REFERENCE_BYTES,
            reference_mime_types=list(STUDIO_REFERENCE_MIME_TYPES),
        )


class UploadStudioReferenceRequest(BaseModel):
    """A reference image as base64 inside JSON.

    JSON rather than multipart because the BFF forwards bodies as text with a
    JSON content type — one transport for every call. The ~33% base64 overhead
    on a few megabytes is not worth a second proxy path.
    """

    model_config = ConfigDict(extra="forbid")

    mime_type: str
    data_base64: str = Field(min_length=1)

    @field_validator("mime_type")
    @classmethod
    def _known_type(cls, value: str) -> str:
        if value not in STUDIO_REFERENCE_MIME_TYPES:
            allowed = ", ".join(STUDIO_REFERENCE_MIME_TYPES)
            raise ValueError(f"must be one of {allowed}")
        return value


class StudioReferenceResponse(BaseModel):
    storage_key: str
    asset_url: str


class CreateStudioGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prompt: str = Field(max_length=STUDIO_MAX_PROMPT_CHARS)
    model: str
    aspect_ratio: str
    count: int = Field(ge=1, le=STUDIO_MAX_COUNT)
    references: list[str] = Field(
        default_factory=list, max_length=STUDIO_MAX_REFERENCES
    )

    @field_validator("prompt")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped

    @field_validator("model")
    @classmethod
    def _known_model(cls, value: str) -> str:
        if value not in STUDIO_MODELS:
            raise ValueError(f"must be one of {', '.join(STUDIO_MODELS)}")
        return value

    @field_validator("aspect_ratio")
    @classmethod
    def _known_ratio(cls, value: str) -> str:
        if value not in STUDIO_ASPECT_RATIOS:
            raise ValueError(f"must be one of {', '.join(STUDIO_ASPECT_RATIOS)}")
        return value


class StudioImageSummary(BaseModel):
    id: str
    index: int
    storage_key: str
    mime_type: str
    width: int
    height: int
    asset_url: str


class StudioGenerationSummary(BaseModel):
    id: str
    prompt: str
    model: str
    aspect_ratio: str
    count: int
    references: list[str]
    reference_urls: list[str]
    status: StudioStatus
    #: The provider's message when it failed — e.g. a safety block — so the
    #: person can change the prompt rather than retry blindly.
    error: str | None
    created_at: datetime
    images: list[StudioImageSummary]

    @classmethod
    def of(cls, entry: StudioEntry, *, bucket: str) -> StudioGenerationSummary:
        generation, job = entry.generation, entry.job
        status = _STATUS.get(job.status, "FAILED") if job is not None else "FAILED"
        error = None
        if status == "FAILED":
            message = (job.error or {}).get("message") if job is not None else None
            error = str(message) if message else "generation did not complete"
        return cls(
            id=generation.id,
            prompt=generation.prompt,
            model=generation.model,
            aspect_ratio=generation.aspect_ratio,
            count=generation.count,
            references=list(generation.references),
            reference_urls=[f"/assets/{bucket}/{key}" for key in generation.references],
            status=status,
            error=error,
            created_at=generation.created_at,
            images=[
                StudioImageSummary(
                    id=image.id,
                    index=image.index,
                    storage_key=image.storage_key,
                    mime_type=image.mime_type,
                    width=image.width,
                    height=image.height,
                    asset_url=f"/assets/{bucket}/{image.storage_key}",
                )
                for image in entry.images
            ],
        )


class StudioPage(BaseModel):
    items: list[StudioGenerationSummary]
    #: Pass back as ``before`` for the next page; ``None`` on the last one.
    next_cursor: str | None
