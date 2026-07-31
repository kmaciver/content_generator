"""Flask application factory.

The app object is assembled here and only here: settings resolved, logging
configured, middleware installed, error handlers bound, blueprints registered.
``wsgi.py`` calls this once for uWSGI; tests call it with injected settings.

The factory stays thin by design — anything resembling business logic belongs
in services, and anything resembling transport detail belongs in ``api/``.
"""

from __future__ import annotations

from flask import Flask

from videoforge.api.assets import assets_blueprint
from videoforge.api.errors import register_error_handlers
from videoforge.api.health import health_blueprint
from videoforge.api.middleware import register_request_middleware
from videoforge.config import AppSettings, get_app_settings
from videoforge_shared.logging import configure_logging
from videoforge_shared.storage import storage_client_from_settings

API_PREFIX = "/api/v1"


def create_app(settings: AppSettings | None = None) -> Flask:
    """Build the Flask app.

    ``settings=None`` (production path) resolves from the environment once and
    caches; tests pass a constructed :class:`AppSettings` and get an app wired
    to it with no environment coupling.
    """
    resolved = settings if settings is not None else get_app_settings()

    configure_logging(
        level=resolved.core.log_level.value,
        fmt=resolved.core.log_format.value,
    )

    app = Flask("videoforge")
    # Settings and the storage client ride on Flask's config so views reach
    # them through `current_app`. One storage client per process: building a
    # boto3 client per request would be pure waste, and tests swap the config
    # entry for a stub without touching any global.
    app.config["VIDEOFORGE_SETTINGS"] = resolved
    app.config["VIDEOFORGE_STORAGE"] = storage_client_from_settings(resolved.minio)

    register_request_middleware(app)
    register_error_handlers(app)
    app.register_blueprint(health_blueprint, url_prefix=API_PREFIX)
    app.register_blueprint(assets_blueprint, url_prefix=API_PREFIX)

    return app
