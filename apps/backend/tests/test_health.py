"""Health endpoint tests (M0-06).

Deep-health tests monkeypatch the check registry — unit tests must never open
sockets toward postgres/redis/minio that don't exist on the test host.
"""

from __future__ import annotations

from flask.testing import FlaskClient
from pytest import MonkeyPatch

from videoforge.api import health as health_module
from videoforge.api.health import ComponentStatus
from videoforge_shared.settings import AppSettings


def _ok(_settings: AppSettings) -> ComponentStatus:
    return ComponentStatus(ok=True, latency_ms=1)


def _down(_settings: AppSettings) -> ComponentStatus:
    return ComponentStatus(ok=False, latency_ms=1000, error="connection refused")


class TestLiveness:
    def test_health_is_ok_and_touches_nothing(self, client: FlaskClient) -> None:
        response = client.get("/api/v1/health")
        assert response.status_code == 200
        body = response.get_json()
        assert body["status"] == "ok"
        assert body["service"] == "videoforge-api"
        assert "python" in body

    def test_health_carries_correlation_header(self, client: FlaskClient) -> None:
        response = client.get("/api/v1/health", headers={"X-Request-Id": "cid-health"})
        assert response.headers["X-Request-Id"] == "cid-health"


class TestDeepHealth:
    def test_all_components_ok(
        self, client: FlaskClient, monkeypatch: MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            health_module,
            "COMPONENT_CHECKS",
            {"postgres": _ok, "redis": _ok, "minio": _ok},
        )
        response = client.get("/api/v1/health/deep")
        assert response.status_code == 200
        body = response.get_json()
        assert body["status"] == "ok"
        assert set(body["components"]) == {"postgres", "redis", "minio"}
        assert all(c["ok"] for c in body["components"].values())

    def test_one_failure_means_degraded_503(
        self, client: FlaskClient, monkeypatch: MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            health_module,
            "COMPONENT_CHECKS",
            {"postgres": _ok, "redis": _down, "minio": _ok},
        )
        response = client.get("/api/v1/health/deep")
        assert response.status_code == 503
        body = response.get_json()
        assert body["status"] == "degraded"
        assert body["components"]["redis"]["ok"] is False
        assert body["components"]["redis"]["error"] == "connection refused"
        # Healthy components still report — the point of a component map.
        assert body["components"]["postgres"]["ok"] is True

    def test_check_isolation_survives_raising_check(
        self, client: FlaskClient, monkeypatch: MonkeyPatch
    ) -> None:
        """A probe that *raises* (rather than returning not-ok) must produce a
        component entry, not a 500 — _timed handles it, but guard the wiring."""

        def exploding(settings: AppSettings) -> ComponentStatus:
            return health_module._timed(lambda: (_ for _ in ()).throw(OSError("boom")))

        monkeypatch.setattr(health_module, "COMPONENT_CHECKS", {"pg": exploding})
        response = client.get("/api/v1/health/deep")
        assert response.status_code == 503
        assert "boom" in response.get_json()["components"]["pg"]["error"]
