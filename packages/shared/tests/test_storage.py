"""Unit tests for the storage client (M0-05).

Run against an in-memory fake of the four S3 operations the client uses —
fast, offline, and behaviour-focused. The client also runs against real MinIO
in the compose stack (exercised from M0-09 onward and by CI's e2e), so the
fake only needs to be honest about the S3 *interface*, not its internals.
"""

from __future__ import annotations

import hashlib
import io
from typing import TYPE_CHECKING, Any, cast

import pytest
from botocore.exceptions import ClientError

from videoforge_shared.storage import (
    IntegrityError,
    ObjectNotFoundError,
    StorageClient,
)

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client


class FakeS3:
    """The slice of the S3 client surface StorageClient touches."""

    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.content_types: dict[tuple[str, str], str] = {}
        self.buckets: set[str] = set()
        self.put_calls = 0
        self.copy_calls = 0

    def put_object(
        self, *, Bucket: str, Key: str, Body: bytes, ContentType: str = ""
    ) -> dict[str, Any]:
        self.put_calls += 1
        self.buckets.add(Bucket)
        self.objects[(Bucket, Key)] = Body
        self.content_types[(Bucket, Key)] = ContentType
        return {}

    def head_bucket(self, *, Bucket: str) -> dict[str, Any]:
        if Bucket not in self.buckets:
            raise ClientError({"Error": {"Code": "404"}}, "HeadBucket")
        return {}

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        return {"ContentType": self.content_types.get((Bucket, Key), "")}

    def copy_object(
        self,
        *,
        Bucket: str,
        Key: str,
        CopySource: dict[str, str],
        ContentType: str,
        MetadataDirective: str,
    ) -> dict[str, Any]:
        self.copy_calls += 1
        self.content_types[(Bucket, Key)] = ContentType
        return {}

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:
        if (Bucket, Key) not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def generate_presigned_url(
        self, operation: str, *, Params: dict[str, str], ExpiresIn: int
    ) -> str:
        return (
            f"http://fake/{Params['Bucket']}/{Params['Key']}"
            f"?X-Amz-Expires={ExpiresIn}"
        )


@pytest.fixture()
def fake() -> FakeS3:
    return FakeS3()


@pytest.fixture()
def client(fake: FakeS3) -> StorageClient:
    return StorageClient(cast("S3Client", fake))


class TestPut:
    def test_put_returns_content_address(self, client: StorageClient) -> None:
        data = b"illustration bytes"
        stored = client.put_bytes("artifacts", data, "scene0.png")
        digest = hashlib.sha256(data).hexdigest()
        assert stored.sha256 == digest
        assert stored.key == f"{digest[:2]}/{digest}/scene0.png"
        assert stored.size == len(data)
        assert stored.deduplicated is False

    def test_identical_bytes_upload_once(
        self, client: StorageClient, fake: FakeS3
    ) -> None:
        first = client.put_bytes("artifacts", b"same", "a.png")
        second = client.put_bytes("artifacts", b"same", "a.png")
        assert fake.put_calls == 1
        assert second.deduplicated is True
        assert first.key == second.key

    def test_content_type_guessed_from_filename(
        self, client: StorageClient, fake: FakeS3
    ) -> None:
        mp4 = client.put_bytes("artifacts", b"vid", "final.mp4")
        png = client.put_bytes("artifacts", b"img", "scene0.png")
        unknown = client.put_bytes("artifacts", b"??", "mystery.zzz")
        assert fake.content_types[("artifacts", mp4.key)] == "video/mp4"
        assert fake.content_types[("artifacts", png.key)] == "image/png"
        assert fake.content_types[("artifacts", unknown.key)] == (
            "application/octet-stream"
        )

    def test_dedup_repairs_stale_content_type(
        self, client: StorageClient, fake: FakeS3
    ) -> None:
        """Regression for the M0-12 exit-test finding: identical bytes skip the
        upload, so a metadata fix would never reach objects stored earlier."""
        stored = client.put_bytes("artifacts", b"vid", "final.mp4")
        # Simulate an object written before content types were set.
        fake.content_types[("artifacts", stored.key)] = "binary/octet-stream"

        again = client.put_bytes("artifacts", b"vid", "final.mp4")

        assert again.deduplicated is True
        assert fake.put_calls == 1, "bytes must not be re-uploaded"
        assert fake.copy_calls == 1, "metadata should be repaired in place"
        assert fake.content_types[("artifacts", stored.key)] == "video/mp4"

    def test_dedup_does_not_touch_correct_metadata(
        self, client: StorageClient, fake: FakeS3
    ) -> None:
        client.put_bytes("artifacts", b"vid", "final.mp4")
        client.put_bytes("artifacts", b"vid", "final.mp4")
        assert fake.copy_calls == 0, "no rewrite when the type already matches"

    def test_different_bytes_never_collide(self, client: StorageClient) -> None:
        one = client.put_bytes("artifacts", b"v1", "img.png")
        two = client.put_bytes("artifacts", b"v2", "img.png")
        assert one.key != two.key  # immutability by construction


class TestGet:
    def test_round_trip(self, client: StorageClient) -> None:
        stored = client.put_bytes("artifacts", b"payload", "f.bin")
        assert client.get_bytes("artifacts", stored.key) == b"payload"

    def test_missing_key_raises_specifically(self, client: StorageClient) -> None:
        with pytest.raises(ObjectNotFoundError, match="artifacts/nope"):
            client.get_bytes("artifacts", "nope")

    def test_verified_get_accepts_intact_content(self, client: StorageClient) -> None:
        stored = client.put_bytes("artifacts", b"good bytes", "f.bin")
        assert client.get_bytes_verified("artifacts", stored.key) == b"good bytes"

    def test_verified_get_rejects_corruption(
        self, client: StorageClient, fake: FakeS3
    ) -> None:
        stored = client.put_bytes("artifacts", b"original", "f.bin")
        fake.objects[("artifacts", stored.key)] = b"tampered"
        with pytest.raises(IntegrityError, match="does not match"):
            client.get_bytes_verified("artifacts", stored.key)

    def test_verified_get_skips_non_addressed_keys(
        self, client: StorageClient, fake: FakeS3
    ) -> None:
        # Library assets (music, fonts) are not content-addressed; verification
        # must pass them through rather than reject them.
        fake.objects[("assets", "music/calm-01.mp3")] = b"mp3bytes"
        assert client.get_bytes_verified("assets", "music/calm-01.mp3") == b"mp3bytes"


class TestExistsAndPresign:
    def test_exists(self, client: StorageClient) -> None:
        stored = client.put_bytes("artifacts", b"x", "f.bin")
        assert client.exists("artifacts", stored.key) is True
        assert client.exists("artifacts", "absent") is False

    def test_bucket_exists(self, client: StorageClient) -> None:
        client.put_bytes("artifacts", b"x", "f.bin")
        assert client.bucket_exists("artifacts") is True
        assert client.bucket_exists("never-created") is False

    def test_presigned_url_carries_expiry(self, client: StorageClient) -> None:
        stored = client.put_bytes("artifacts", b"x", "f.bin")
        url = client.presigned_get_url("artifacts", stored.key, expires_s=300)
        assert stored.key in url
        assert "X-Amz-Expires=300" in url
