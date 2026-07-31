"""Error contract and correlation middleware tests (M0-06)."""

from __future__ import annotations

from flask import Flask
from flask.testing import FlaskClient

from videoforge.api.errors import PROBLEM_CONTENT_TYPE, ApiError
from videoforge_shared.correlation import get_correlation_id
from videoforge_shared.ids import is_ulid


class TestProblemJson:
    def test_404_is_problem_json(self, client: FlaskClient) -> None:
        response = client.get("/api/v1/definitely-not-a-route")
        assert response.status_code == 404
        assert response.content_type == PROBLEM_CONTENT_TYPE
        body = response.get_json()
        assert body["title"] == "Not Found"
        assert body["status"] == 404
        assert body["instance"] == "/api/v1/definitely-not-a-route"
        assert "correlation_id" in body

    def test_api_problem_carries_safe_detail(self, app: Flask) -> None:
        @app.get("/boom-problem")
        def boom_problem() -> str:
            raise ApiError(409, "Conflict", "expected_version_no is stale")

        response = app.test_client().get("/boom-problem")
        assert response.status_code == 409
        body = response.get_json()
        assert body["title"] == "Conflict"
        assert body["detail"] == "expected_version_no is stale"

    def test_unexpected_exception_is_opaque_500(self, app: Flask) -> None:
        @app.get("/boom-internal")
        def boom_internal() -> str:
            raise RuntimeError("secret internal state: db password is hunter2")

        app.config["TESTING"] = False  # let the handler run instead of re-raising
        response = app.test_client().get("/boom-internal")
        assert response.status_code == 500
        assert response.content_type == PROBLEM_CONTENT_TYPE
        body = response.get_json()
        # The internal message must never reach the client (SADD §21).
        assert "hunter2" not in response.get_data(as_text=True)
        assert body["title"] == "Internal Server Error"
        assert "correlation_id" in body


class TestCorrelationMiddleware:
    def test_incoming_id_is_echoed(self, client: FlaskClient) -> None:
        response = client.get("/api/v1/health", headers={"X-Request-Id": "cid-abc"})
        assert response.headers["X-Request-Id"] == "cid-abc"

    def test_id_minted_when_absent(self, client: FlaskClient) -> None:
        response = client.get("/api/v1/health")
        assert is_ulid(response.headers["X-Request-Id"])

    def test_binding_does_not_leak_across_requests(self, app: Flask) -> None:
        seen: list[str | None] = []

        @app.get("/capture-cid")
        def capture() -> str:
            seen.append(get_correlation_id())
            return "ok"

        test_client = app.test_client()
        test_client.get("/capture-cid", headers={"X-Request-Id": "cid-first"})
        # After the request completes, the worker context must be unbound —
        # this is the guard against one request's id stamping the next's logs.
        assert get_correlation_id() is None
        test_client.get("/capture-cid")
        assert seen[0] == "cid-first"
        assert seen[1] != "cid-first"
