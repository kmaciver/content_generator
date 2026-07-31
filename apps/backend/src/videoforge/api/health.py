"""Health endpoints (NF9, SADD §19.1).

Two tiers with different jobs:

* ``GET /api/v1/health`` — liveness. No dependencies touched, always fast;
  this is what container healthchecks and nginx probes hit, so it must never
  block on a slow store.
* ``GET /api/v1/health/deep`` — the component map. Probes postgres, redis, and
  MinIO with short timeouts; 200 when everything answers, 503 with per-
  component detail when anything doesn't. A diagnostics endpoint, not a
  liveness one — it is deliberately NOT wired into any container healthcheck,
  because a store outage restarting the API would only add chaos to an outage.

The check registry is a plain dict so tests monkeypatch entries and future
components (broker reachability, etc.) are one line each.
"""

from __future__ import annotations

import importlib.metadata
import os
import socket
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlparse

from flask import Blueprint, Response, current_app, jsonify
from sqlalchemy import text

from videoforge.config import AppSettings
from videoforge_persistence.engine import create_engine_from_settings
from videoforge_shared.storage import storage_client_from_settings

health_blueprint = Blueprint("health", __name__)

_PROBE_TIMEOUT_S = 1.0


@dataclass(frozen=True, slots=True)
class ComponentStatus:
    ok: bool
    latency_ms: int
    error: str | None = None


def _timed(probe: Callable[[], None]) -> ComponentStatus:
    started = time.monotonic()
    try:
        probe()
    except Exception as exc:  # per-component isolation: one failure, one entry
        latency = int((time.monotonic() - started) * 1000)
        return ComponentStatus(ok=False, latency_ms=latency, error=str(exc))
    return ComponentStatus(ok=True, latency_ms=int((time.monotonic() - started) * 1000))


def check_postgres(settings: AppSettings) -> ComponentStatus:
    """Real `SELECT 1` through the driver — proves reachability, credentials,
    and that the database accepts connections, not merely that a port is open.
    (Upgraded from a TCP probe when M0-07 landed SQLAlchemy.)"""

    def probe() -> None:
        engine = create_engine_from_settings(
            settings.postgres, connect_timeout_s=int(_PROBE_TIMEOUT_S)
        )
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        finally:
            # A health probe must not hold pooled connections between calls.
            engine.dispose()

    return _timed(probe)


def check_redis(settings: AppSettings) -> ComponentStatus:
    """RESP inline PING — real protocol round-trip, no client library needed."""

    def probe() -> None:
        parsed = urlparse(settings.redis.url)
        host = parsed.hostname or "redis"
        port = parsed.port or 6379
        with socket.create_connection((host, port), timeout=_PROBE_TIMEOUT_S) as conn:
            conn.settimeout(_PROBE_TIMEOUT_S)
            conn.sendall(b"PING\r\n")
            reply = conn.recv(64)
        if not reply.startswith(b"+PONG"):
            raise ConnectionError(f"unexpected redis reply: {reply[:32]!r}")

    return _timed(probe)


def check_minio(settings: AppSettings) -> ComponentStatus:
    """Bucket existence via the storage client — proves endpoint, credentials,
    and that bootstrap actually created the artifacts bucket."""

    def probe() -> None:
        client = storage_client_from_settings(settings.minio)
        if not client.bucket_exists(settings.minio.bucket_artifacts):
            raise RuntimeError(
                f"bucket {settings.minio.bucket_artifacts!r} missing — "
                "did minio-bootstrap run?"
            )

    return _timed(probe)


#: name → check. A dict on purpose: tests monkeypatch entries, and future
#: components (celery broker reachability, etc.) are one line each.
COMPONENT_CHECKS: dict[str, Callable[[AppSettings], ComponentStatus]] = {
    "postgres": check_postgres,
    "redis": check_redis,
    "minio": check_minio,
}


def _service_info() -> dict[str, Any]:
    try:
        version = importlib.metadata.version("videoforge-backend")
    except importlib.metadata.PackageNotFoundError:  # unbuilt source tree
        version = "unknown"
    return {
        "service": "videoforge-api",
        "version": version,
        "python": sys.version.split()[0],
        "pid": os.getpid(),
    }


@health_blueprint.get("/health")
def health() -> Response:
    return jsonify(status="ok", **_service_info())


@health_blueprint.get("/health/deep")
def health_deep() -> tuple[Response, int]:
    settings: AppSettings = current_app.config["VIDEOFORGE_SETTINGS"]
    components = {name: check(settings) for name, check in COMPONENT_CHECKS.items()}
    all_ok = all(status.ok for status in components.values())
    body = jsonify(
        status="ok" if all_ok else "degraded",
        components={name: asdict(status) for name, status in components.items()},
        **_service_info(),
    )
    return body, 200 if all_ok else 503
