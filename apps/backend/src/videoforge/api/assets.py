"""Asset serving — the API half of the X-Accel-Redirect handshake (B4/ADR-011).

The flow, end to end:

1. Browser requests ``/assets/{bucket}/{key}`` — a **stable, content-addressed
   URL**, which is what makes ``Cache-Control: immutable`` genuinely work.
2. nginx rewrites that to this endpoint. Flask authorizes, then answers with an
   empty body and ``X-Accel-Redirect: /internal-assets/{presigned path+query}``.
3. nginx intercepts the header, switches to the ``internal;`` location (which
   outside requests cannot reach), and proxies the *presigned* URL to MinIO —
   so MinIO still validates a real SigV4 signature and no bucket is ever
   anonymous, yet nginx never has to compute a signature itself, and the
   short-lived query string never reaches the browser's cache key.

The presign here is an internal implementation detail with a short TTL; the
public URL never changes. Bytes stream nginx→browser directly — Flask handles
authorization only, so a 2GB video costs the API worker microseconds (NF2).

Authorization today is bucket allowlist + key hygiene + existence; the bearer
token gate (SADD §21.1) arrives with M1's auth work and slots in front of this
handler without changing the handshake.
"""

from __future__ import annotations

import mimetypes
from urllib.parse import urlsplit

from flask import Blueprint, Response, current_app

from videoforge.api.errors import ApiError
from videoforge.config import AppSettings
from videoforge_shared.storage import StorageClient

assets_blueprint = Blueprint("assets", __name__)

#: Must match the `internal;` location in docker/nginx/nginx.conf exactly.
INTERNAL_LOCATION = "/internal-assets"


def _servable_buckets(settings: AppSettings) -> set[str]:
    """tmp-render is deliberately absent: scratch space is never servable."""
    return {
        settings.minio.bucket_artifacts,
        settings.minio.bucket_packages,
        settings.minio.bucket_assets,
    }


@assets_blueprint.get("/assets/<bucket>/<path:key>")
def serve_asset(bucket: str, key: str) -> Response:
    settings: AppSettings = current_app.config["VIDEOFORGE_SETTINGS"]
    storage: StorageClient = current_app.config["VIDEOFORGE_STORAGE"]

    if bucket not in _servable_buckets(settings):
        raise ApiError(403, "Forbidden", f"bucket {bucket!r} is not servable")
    if ".." in key or key.startswith("/") or not key:
        raise ApiError(400, "Bad Request", "malformed asset key")
    if not storage.exists(bucket, key):
        raise ApiError(404, "Not Found", "no such asset")

    presigned = storage.presigned_get_url(bucket, key)
    parts = urlsplit(presigned)
    accel_target = f"{INTERNAL_LOCATION}{parts.path}?{parts.query}"

    # Empty body on purpose: nginx discards it and re-runs the request against
    # the internal location; the browser's response comes from MinIO's stream.
    #
    # Content-Type is set HERE, not left to the proxy: on an X-Accel redirect
    # nginx carries the type from the authorizing response, and Flask's default
    # (`text/html; charset=utf-8`) otherwise smears a charset parameter onto
    # video/mp4. Declared from the filename, same guess the upload made.
    guessed = mimetypes.guess_type(key)[0] or "application/octet-stream"
    response = Response(status=200, mimetype=guessed)
    response.headers["X-Accel-Redirect"] = accel_target
    return response
