"""Backend configuration surface.

A deliberate re-export, not a home: the models live in ``videoforge_shared``
because workers need them too and the apps must never import each other.

Only the App-scoped names are re-exported. ``WorkerSettings``, ``ProviderKeys``
and ``get_worker_settings`` are intentionally absent — the backend has no
business holding provider configuration or credentials (NF8, SADD §21.3), and
``tests/test_secret_isolation.py`` fails if backend code ever names them.
"""

from videoforge_shared.settings import (
    AppSettings,
    Environment,
    LogFormat,
    LogLevel,
    get_app_settings,
    load_app_settings,
)

__all__ = [
    "AppSettings",
    "Environment",
    "LogFormat",
    "LogLevel",
    "get_app_settings",
    "load_app_settings",
]
