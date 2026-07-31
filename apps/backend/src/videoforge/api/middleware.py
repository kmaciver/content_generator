"""Request middleware: correlation-id lifecycle.

One id per request, from the edge to the last log line:

* nginx forwards (or mints) ``X-Request-Id`` — see docker/nginx/nginx.conf;
* ``before_request`` binds it into the shared contextvar, so every log line
  emitted while handling the request carries it automatically;
* ``after_request`` echoes it on the response, so a client error report can be
  joined against server logs;
* ``teardown_request`` restores the previous binding — uWSGI workers are
  long-lived, and a leaked binding would stamp this request's id onto the next.
"""

from __future__ import annotations

from flask import Flask, Response, g, request

from videoforge_shared.correlation import (
    CORRELATION_HEADER,
    bind_correlation_id,
    extract_correlation_id,
    get_correlation_id,
    unbind_correlation_id,
)
from videoforge_shared.ids import new_ulid


def register_request_middleware(app: Flask) -> None:
    @app.before_request
    def bind_correlation() -> None:
        incoming = extract_correlation_id(dict(request.headers))
        cid = incoming if incoming is not None else new_ulid()
        g.correlation_token = bind_correlation_id(cid)

    @app.after_request
    def echo_correlation(response: Response) -> Response:
        cid = get_correlation_id()
        if cid is not None:
            response.headers[CORRELATION_HEADER] = cid
        return response

    @app.teardown_request
    def unbind_correlation(_exc: BaseException | None) -> None:
        token = g.pop("correlation_token", None)
        if token is not None:
            unbind_correlation_id(token)
