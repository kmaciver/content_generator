"""Structured logging (NF9): JSON lines in containers, readable lines in dev.

Built on stdlib ``logging`` rather than structlog — the whole requirement is
"every line is a JSON object carrying the correlation id and any ``extra=``
fields", which is a formatter, not a framework.

Usage:

    configure_logging(level="INFO", fmt="json")
    logger = logging.getLogger(__name__)
    logger.info("job started", extra={"job_id": job_id})

The correlation id is read from the contextvar at format time, so any log line
emitted inside a bound :func:`~videoforge_shared.correlation.correlation_context`
carries it with no cooperation from the call site.
"""

from __future__ import annotations

import io
import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from videoforge_shared.correlation import get_correlation_id

#: Attributes every LogRecord carries; anything else came in via ``extra=``.
_STANDARD_RECORD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)

#: Marker attribute so reconfiguration replaces our handler instead of stacking.
_HANDLER_MARKER = "_videoforge_handler"


def _record_extras(record: logging.LogRecord) -> dict[str, Any]:
    return {
        key: value
        for key, value in record.__dict__.items()
        if key not in _STANDARD_RECORD_ATTRS and not key.startswith("_")
    }


class JsonFormatter(logging.Formatter):
    """One JSON object per line; ``default=str`` so an unserialisable extra
    degrades to its repr instead of killing the log call."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(
                timespec="milliseconds"
            ),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        correlation_id = get_correlation_id()
        if correlation_id is not None:
            payload["correlation_id"] = correlation_id
        payload.update(_record_extras(record))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class PrettyFormatter(logging.Formatter):
    """Development format: aligned, greppable, extras as trailing k=v pairs."""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.fromtimestamp(record.created, tz=UTC).strftime("%H:%M:%S")
        message = record.getMessage()
        line = f"{timestamp} {record.levelname:<8} [{record.name}] {message}"
        correlation_id = get_correlation_id()
        if correlation_id is not None:
            line += f"  cid={correlation_id}"
        for key, value in _record_extras(record).items():
            line += f"  {key}={value}"
        if record.exc_info:
            line += "\n" + self.formatException(record.exc_info)
        return line


def configure_logging(
    level: str = "INFO",
    fmt: str = "json",
    *,
    stream: io.TextIOBase | Any = None,
) -> logging.Handler:
    """Install the platform log handler on the root logger.

    Idempotent: a previous handler installed by this function is replaced, so
    calling from several entry points (wsgi, celery worker init, tests) stacks
    nothing. Returns the handler, which tests use to capture output.
    """
    formatter: logging.Formatter = (
        PrettyFormatter() if fmt == "pretty" else JsonFormatter()
    )

    handler = logging.StreamHandler(stream if stream is not None else sys.stdout)
    handler.setFormatter(formatter)
    setattr(handler, _HANDLER_MARKER, True)

    root = logging.getLogger()
    for existing in list(root.handlers):
        if getattr(existing, _HANDLER_MARKER, False):
            root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())
    return handler
