"""Backend test fixtures: an app wired to constructed settings, never to the
host environment."""

from __future__ import annotations

import pytest
from flask import Flask
from flask.testing import FlaskClient

from videoforge.app import create_app
from videoforge_shared.settings import (
    AppSettings,
    CelerySettings,
    CoreSettings,
    MinioSettings,
    PostgresSettings,
    RedisSettings,
    RenderSettings,
)


@pytest.fixture()
def settings() -> AppSettings:
    return AppSettings(
        core=CoreSettings(),
        postgres=PostgresSettings(),
        redis=RedisSettings(),
        celery=CelerySettings(),
        minio=MinioSettings(),
        render=RenderSettings(),
    )


@pytest.fixture()
def app(settings: AppSettings) -> Flask:
    application = create_app(settings)
    application.config["TESTING"] = True
    return application


@pytest.fixture()
def client(app: Flask) -> FlaskClient:
    return app.test_client()
