"""WSGI entrypoint — what uWSGI's ``module = videoforge.wsgi:app`` imports.

Nothing lives here but the factory call. The M0-00 spike endpoints (/slow and
its uWSGI introspection) are gone with the spike; anything the app serves is
registered through :func:`videoforge.app.create_app`.
"""

from __future__ import annotations

from videoforge.app import create_app

app = create_app()
