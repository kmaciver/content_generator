"""RFC-9457 problem+json error handling (SADD §19).

Every error response is the same shape:

    {"type": "...", "title": "...", "status": 4xx/5xx,
     "detail": "...", "instance": "/api/v1/...", "correlation_id": "01J..."}

Three tiers:

* :class:`ApiError` — deliberate, service-raised errors with safe detail;
* ``HTTPException`` — Flask/werkzeug routing errors (404, 405, ...), converted;
* everything else — logged with the full traceback, returned as an opaque 500.
  Internal detail never reaches the client (SADD §21; the correlation id is
  how an operator joins the client's report to the server-side traceback).
"""

from __future__ import annotations

import logging
from typing import Any

from flask import Flask, Response, jsonify, request
from werkzeug.exceptions import HTTPException

from videoforge_shared.correlation import get_correlation_id

logger = logging.getLogger(__name__)

PROBLEM_CONTENT_TYPE = "application/problem+json"


class ApiError(Exception):
    """An intentional API error. ``detail`` is client-visible — write it so."""

    def __init__(
        self,
        status: int,
        title: str,
        detail: str | None = None,
        *,
        type_uri: str = "about:blank",
    ) -> None:
        super().__init__(detail or title)
        self.status = status
        self.title = title
        self.detail = detail
        self.type_uri = type_uri


def _problem_response(
    status: int,
    title: str,
    detail: str | None,
    *,
    type_uri: str = "about:blank",
) -> Response:
    payload: dict[str, Any] = {
        "type": type_uri,
        "title": title,
        "status": status,
        "instance": request.path,
    }
    if detail:
        payload["detail"] = detail
    correlation_id = get_correlation_id()
    if correlation_id is not None:
        payload["correlation_id"] = correlation_id

    response = jsonify(payload)
    response.status_code = status
    response.content_type = PROBLEM_CONTENT_TYPE
    return response


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(ApiError)
    def handle_api_problem(exc: ApiError) -> Response:
        return _problem_response(
            exc.status, exc.title, exc.detail, type_uri=exc.type_uri
        )

    @app.errorhandler(HTTPException)
    def handle_http_exception(exc: HTTPException) -> Response:
        return _problem_response(exc.code or 500, exc.name, exc.description or None)

    @app.errorhandler(Exception)
    def handle_unexpected(exc: Exception) -> Response:
        # Full traceback server-side; opaque message client-side.
        logger.exception("unhandled error in request handler")
        return _problem_response(
            500,
            "Internal Server Error",
            "An unexpected error occurred. Reference the correlation_id "
            "when reporting.",
        )
