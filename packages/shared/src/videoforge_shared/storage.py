"""The storage client — the single choke point for all artifact I/O.

Every byte the platform persists goes through this module; nothing else may
speak S3. That sentence is the entire cloud-migration story (SADD §24): move
from MinIO to S3 and only :func:`storage_client_from_settings` changes.

Writes are content-addressed (SADD §18): ``put_bytes`` hashes the payload,
derives the key, and skips the upload entirely when the key already exists —
identical regenerations are free, and nothing can ever be overwritten because
different content cannot produce the same key.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from botocore.exceptions import ClientError

from videoforge_shared.hashing import content_key, sha256_bytes, sha256_from_key

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

    from videoforge_shared.settings import MinioSettings

_NOT_FOUND_CODES = {"404", "NoSuchKey", "NotFound"}

#: SADD §19.3 — presigned URLs are short-lived; content-addressing makes
#: re-signing cheap because the underlying object never changes.
DEFAULT_PRESIGN_EXPIRY_S = 900


class StorageError(Exception):
    """Base class for storage failures."""


class ObjectNotFoundError(StorageError):
    """The requested key does not exist in the bucket."""


class IntegrityError(StorageError):
    """Fetched bytes do not match the digest their key claims (NF5 violation —
    this should never happen and always warrants investigation)."""


@dataclass(frozen=True, slots=True)
class StoredObject:
    """Receipt for a persisted payload; what artifact_version rows record."""

    bucket: str
    key: str
    sha256: str
    size: int
    deduplicated: bool
    """True when the bytes were already present and no upload happened."""


class StorageClient:
    """Thin, typed wrapper over an S3-compatible client.

    Takes the boto3 client as a constructor argument rather than building it,
    so tests substitute an in-memory fake and the factory below stays the only
    place that knows where the object store lives.
    """

    def __init__(self, s3: S3Client) -> None:
        self._s3 = s3

    # -- writes -------------------------------------------------------------- #

    def put_bytes(self, bucket: str, data: bytes, filename: str) -> StoredObject:
        """Persist a payload under its content address.

        Existing key ⇒ no upload: same key means same bytes by construction,
        so re-uploading could only waste bandwidth or (worse) mask a hash
        collision bug. ``deduplicated`` on the receipt says which path ran.

        The content type is guessed from the filename and stored with the
        object — the asset-serving path (M0-10) streams objects to browsers,
        and a ``video/mp4`` served as ``application/octet-stream`` downloads
        instead of playing.
        """
        digest = sha256_bytes(data)
        key = content_key(digest, filename)
        content_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"

        existing = self._head(bucket, key)
        if existing is not None:
            # Dedup hit. The BYTES are necessarily identical — that is what the
            # key means — but the METADATA may predate a change in how we derive
            # it, and skipping the upload would preserve the stale value
            # forever. Found by the M0 exit test: an mp4 stored before content
            # types were set kept serving as octet-stream, which makes browsers
            # download the file instead of playing it.
            if existing.get("ContentType") != content_type:
                self._s3.copy_object(
                    Bucket=bucket,
                    Key=key,
                    CopySource={"Bucket": bucket, "Key": key},
                    ContentType=content_type,
                    MetadataDirective="REPLACE",
                )
            return StoredObject(bucket, key, digest, len(data), deduplicated=True)

        self._s3.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)
        return StoredObject(bucket, key, digest, len(data), deduplicated=False)

    # -- reads --------------------------------------------------------------- #

    def get_bytes(self, bucket: str, key: str) -> bytes:
        try:
            response = self._s3.get_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            if _is_not_found(exc):
                raise ObjectNotFoundError(f"{bucket}/{key}") from exc
            raise StorageError(f"get failed for {bucket}/{key}") from exc
        body: bytes = response["Body"].read()
        return body

    def get_bytes_verified(self, bucket: str, key: str) -> bytes:
        """Fetch and verify against the digest embedded in the key.

        The render worker uses this before feeding assets to FFmpeg — a
        corrupted image should fail the job with :class:`IntegrityError`, not
        become ten seconds of garbage frames discovered at review.
        """
        data = self.get_bytes(bucket, key)
        claimed = sha256_from_key(key)
        if claimed is not None and sha256_bytes(data) != claimed:
            raise IntegrityError(f"content of {bucket}/{key} does not match its key")
        return data

    def exists(self, bucket: str, key: str) -> bool:
        return self._head(bucket, key) is not None

    def bucket_exists(self, bucket: str) -> bool:
        """True when the bucket itself exists and is reachable with our
        credentials. Health checks and bootstrap verification use this."""
        try:
            self._s3.head_bucket(Bucket=bucket)
        except ClientError as exc:
            if _is_not_found(exc):
                return False
            raise StorageError(f"head_bucket failed for {bucket}") from exc
        return True

    def presigned_get_url(
        self, bucket: str, key: str, *, expires_s: int = DEFAULT_PRESIGN_EXPIRY_S
    ) -> str:
        """Time-limited GET URL. Surfaced only through authenticated API
        responses (SADD §21.5) — never logged, never stored."""
        return self._s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=expires_s,
        )

    # -- internals ----------------------------------------------------------- #

    def _head(self, bucket: str, key: str) -> dict[str, Any] | None:
        """Object metadata, or None when absent. Returns the response rather
        than a bool so callers can inspect stored metadata without a second
        round trip."""
        try:
            response = self._s3.head_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            if _is_not_found(exc):
                return None
            raise StorageError(f"head failed for {bucket}/{key}") from exc
        return dict(response)


def _is_not_found(exc: ClientError) -> bool:
    code = exc.response.get("Error", {}).get("Code")
    return str(code) in _NOT_FOUND_CODES


def storage_client_from_settings(minio: MinioSettings) -> StorageClient:
    """The one place that knows where the object store is.

    Path-style addressing because MinIO serves buckets as paths, not vhosts;
    the region is a formality boto3 requires. Point ``MINIO_ENDPOINT`` at real
    S3 (and drop path-style) and everything above this line is untouched.

    ``signature_version="s3v4"`` is required, not optional: boto3 otherwise
    emits legacy SigV2 presigned URLs (``AWSAccessKeyId=...``), which modern
    S3 regions reject outright — precisely the kind of local-only quirk the
    migration story (§24) exists to prevent. Found live against MinIO in M0-05.
    """
    import boto3
    from botocore.config import Config

    s3 = boto3.client(
        "s3",
        endpoint_url=minio.endpoint,
        aws_access_key_id=minio.root_user,
        aws_secret_access_key=minio.root_password.get_secret_value(),
        region_name="us-east-1",
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            # Local object store: if it hasn't answered in seconds it is down,
            # and callers (health checks, request handlers under NF2) must
            # find out quickly rather than inherit boto3's 60s defaults.
            connect_timeout=3,
            read_timeout=10,
            retries={"max_attempts": 2},
        ),
    )
    return StorageClient(s3)
