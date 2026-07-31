"""Asset endpoint tests (M0-10) — the API half of the X-Accel handshake."""

from __future__ import annotations

import pytest
from flask import Flask
from flask.testing import FlaskClient


class StubStorage:
    """Only what the endpoint touches: exists + presign."""

    def __init__(self) -> None:
        self.known: set[tuple[str, str]] = set()

    def exists(self, bucket: str, key: str) -> bool:
        return (bucket, key) in self.known

    def presigned_get_url(self, bucket: str, key: str) -> str:
        return (
            f"http://minio:9000/{bucket}/{key}"
            "?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=stub"
        )


@pytest.fixture()
def storage(app: Flask) -> StubStorage:
    stub = StubStorage()
    app.config["VIDEOFORGE_STORAGE"] = stub
    return stub


KEY = "ab/abcdef0123/hello.mp4"


class TestAccelHandshake:
    def test_known_asset_returns_accel_redirect_and_no_body(
        self, client: FlaskClient, storage: StubStorage
    ) -> None:
        storage.known.add(("artifacts", KEY))
        response = client.get(f"/api/v1/assets/artifacts/{KEY}")
        assert response.status_code == 200
        assert response.data == b""  # nginx discards it; bytes come from MinIO
        accel = response.headers["X-Accel-Redirect"]
        assert accel.startswith(f"/internal-assets/artifacts/{KEY}?")
        # The presigned query must ride along or MinIO rejects the fetch.
        assert "X-Amz-Signature=" in accel

    def test_missing_asset_is_404(
        self, client: FlaskClient, storage: StubStorage
    ) -> None:
        response = client.get(f"/api/v1/assets/artifacts/{KEY}")
        assert response.status_code == 404
        assert "X-Accel-Redirect" not in response.headers


class TestAuthorization:
    def test_scratch_bucket_is_never_servable(
        self, client: FlaskClient, storage: StubStorage
    ) -> None:
        storage.known.add(("tmp-render", KEY))
        response = client.get(f"/api/v1/assets/tmp-render/{KEY}")
        assert response.status_code == 403

    def test_unknown_bucket_is_403(self, client: FlaskClient) -> None:
        assert client.get(f"/api/v1/assets/secrets/{KEY}").status_code == 403

    def test_traversal_is_rejected_before_storage_is_touched(
        self, client: FlaskClient, storage: StubStorage
    ) -> None:
        response = client.get("/api/v1/assets/artifacts/../../etc/passwd")
        # Either Flask/werkzeug normalises it away from the route (404) or the
        # handler rejects it (400); what must never happen is a redirect out.
        assert response.status_code in (400, 404)
        assert "X-Accel-Redirect" not in response.headers
