"""Image Studio endpoints (ADR-018).

Free-form image generation for a person rather than a pipeline: a prompt, a
model, an aspect ratio, optional reference images, and a gallery of what came
back. **No series character, style or prompt template is applied** — that is
the point of the surface.

The same rules as everywhere else in the API hold:

* **The API never generates.** ``POST /studio/generations`` writes a request
  and a job and answers 202; ``worker-media`` (the only service holding the
  Google key) makes the pictures, and the client polls.
* **Bytes do not flow through the API on the way out.** Images are served by
  the asset route (ADR-011); responses carry the URL, not the data. Uploads
  are the one exception, and they go straight to the object store.
"""

from __future__ import annotations

import base64
import binascii
import logging

from flask import Blueprint, Response, current_app, jsonify, request
from pydantic import BaseModel, ValidationError

from videoforge.api.deps import dispatcher, transaction
from videoforge.api.errors import ApiError
from videoforge.config import AppSettings
from videoforge.dto.studio import (
    CreateStudioGenerationRequest,
    StudioGenerationSummary,
    StudioOptions,
    StudioPage,
    StudioReferenceResponse,
    UploadStudioReferenceRequest,
)
from videoforge.services.jobs import JobService
from videoforge_shared.hashing import sha256_from_key
from videoforge_shared.storage import StorageClient
from videoforge_shared.studio import (
    STUDIO_MAX_REFERENCE_BYTES,
    STUDIO_REFERENCE_MIME_TYPES,
)
from videoforge_shared.tasks import STUDIO_GENERATE

logger = logging.getLogger(__name__)

studio_blueprint = Blueprint("studio", __name__)

_DEFAULT_PAGE = 24
_TOO_LARGE = (
    f"reference images may be at most {STUDIO_MAX_REFERENCE_BYTES // 1_048_576} MB"
)
_MAX_PAGE = 100


def _body[T: BaseModel](model: type[T]) -> T:
    payload = request.get_json(silent=True)
    if payload is None:
        raise ApiError(400, "Invalid request", "expected a JSON object body")
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0]
        location = ".".join(str(p) for p in first["loc"]) or "body"
        raise ApiError(400, "Invalid request", f"{location}: {first['msg']}") from exc


def _ok(model: BaseModel, status: int = 200) -> tuple[Response, int]:
    return jsonify(model.model_dump(mode="json")), status


def _settings() -> AppSettings:
    settings: AppSettings = current_app.config["VIDEOFORGE_SETTINGS"]
    return settings


def _storage() -> StorageClient:
    storage: StorageClient = current_app.config["VIDEOFORGE_STORAGE"]
    return storage


def _bucket() -> str:
    return _settings().minio.bucket_assets


def _looks_like(mime_type: str, data: bytes) -> bool:
    """Check the magic bytes agree with the declared type.

    The declared type decides the stored extension, and the extension decides
    the Content-Type the asset route serves. Trusting the claim would let any
    bytes be served back as an image.
    """
    if mime_type == "image/png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if mime_type == "image/jpeg":
        return data.startswith(b"\xff\xd8\xff")
    if mime_type == "image/webp":
        return len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    return False


@studio_blueprint.get("/studio/options")
def get_options() -> tuple[Response, int]:
    """What the form may offer. Served rather than hardcoded in TypeScript so
    the choices and the validation come from one place."""
    return _ok(StudioOptions.current())


@studio_blueprint.post("/studio/references")
def upload_reference() -> tuple[Response, int]:
    """Store one reference image and return its key — 201."""
    body = _body(UploadStudioReferenceRequest)

    # Reject on the encoded length first, so an oversized upload is not
    # decoded into memory just to be measured.
    if len(body.data_base64) * 3 // 4 > STUDIO_MAX_REFERENCE_BYTES + 3:
        raise ApiError(
            413,
            "Payload too large",
            _TOO_LARGE,
        )
    try:
        data = base64.b64decode(body.data_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ApiError(400, "Invalid request", "data_base64: not valid base64") from exc
    if len(data) > STUDIO_MAX_REFERENCE_BYTES:
        raise ApiError(
            413,
            "Payload too large",
            _TOO_LARGE,
        )
    if not _looks_like(body.mime_type, data):
        raise ApiError(
            400, "Invalid request", f"the data is not a valid {body.mime_type} image"
        )

    extension = STUDIO_REFERENCE_MIME_TYPES[body.mime_type]
    stored = _storage().put_bytes(_bucket(), data, f"studio-reference{extension}")
    logger.info(
        "studio reference stored",
        extra={"bytes": len(data), "deduplicated": stored.deduplicated},
    )
    return _ok(
        StudioReferenceResponse(
            storage_key=stored.key,
            asset_url=f"/assets/{_bucket()}/{stored.key}",
        ),
        201,
    )


@studio_blueprint.post("/studio/generations")
def create_generation() -> tuple[Response, int]:
    """Request images — 202 with the job id and the new generation."""
    body = _body(CreateStudioGenerationRequest)

    # References must be content-addressed keys that exist in the assets
    # bucket. The shape check rejects traversal and arbitrary paths before the
    # store is asked anything; the existence check turns a stale key into a
    # 400 now rather than a failed job in thirty seconds.
    storage, bucket = _storage(), _bucket()
    for key in body.references:
        if sha256_from_key(key) is None or key.count("/") != 2:
            raise ApiError(400, "Invalid request", f"references: malformed key {key!r}")
        if not storage.exists(bucket, key):
            raise ApiError(
                400,
                "Invalid request",
                "references: an attached image is no longer available; "
                "remove it and upload it again",
            )

    with transaction() as uow:
        generation = uow.studio.create_generation(
            prompt=body.prompt,
            model=body.model,
            aspect_ratio=body.aspect_ratio,
            count=body.count,
            references=body.references,
        )
        uow.flush()
        service = JobService(uow, dispatcher())
        reserved = service.request_studio_job(
            generation_id=generation.id, spec=STUDIO_GENERATE
        )
        uow.flush()
        entry = uow.studio.entry(generation.id)
        assert entry is not None
        summary = StudioGenerationSummary.of(entry, bucket=bucket)
        job_id = reserved.job.id

    service.dispatch_pending()
    return (
        jsonify({"job_id": job_id, "generation": summary.model_dump(mode="json")}),
        202,
    )


@studio_blueprint.get("/studio/generations")
def list_generations() -> tuple[Response, int]:
    """The gallery, newest first. ``?limit=&before=<generation id>``."""
    try:
        limit = int(request.args.get("limit", _DEFAULT_PAGE))
    except ValueError as exc:
        raise ApiError(400, "Invalid request", "limit: must be an integer") from exc
    limit = max(1, min(limit, _MAX_PAGE))
    before = request.args.get("before") or None

    bucket = _bucket()
    with transaction() as uow:
        # One extra row says whether there is a next page without a count query.
        entries = uow.studio.page(limit=limit + 1, before=before)
        items = [StudioGenerationSummary.of(e, bucket=bucket) for e in entries[:limit]]
    next_cursor = items[-1].id if len(entries) > limit else None
    return _ok(StudioPage(items=items, next_cursor=next_cursor))


@studio_blueprint.get("/studio/generations/<generation_id>")
def get_generation(generation_id: str) -> tuple[Response, int]:
    with transaction() as uow:
        entry = uow.studio.entry(generation_id)
        if entry is None:
            raise ApiError(404, "Not found", f"no studio generation {generation_id}")
        return _ok(StudioGenerationSummary.of(entry, bucket=_bucket()))
