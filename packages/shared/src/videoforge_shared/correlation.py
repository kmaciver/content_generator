"""Correlation ids — one value tying an HTTP request to every log line, job,
and render it causes (NF9, SADD §21.8).

The id is born at the edge (nginx mints one when the caller didn't send
``X-Request-Id``) and travels: HTTP header → this contextvar → outgoing
headers/Celery task headers → the worker re-binds it → its logs carry it.

This module is transport-agnostic on purpose: it knows about mappings of
headers, not about Flask or Celery. The framework glue (M0-06, M0-08) calls
:func:`extract_correlation_id` on the way in and :func:`correlation_headers`
on the way out, and nothing else needs to understand how propagation works.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar, Token

from videoforge_shared.ids import new_ulid

#: Canonical header name. nginx already forwards/mints this (M0-00's config).
CORRELATION_HEADER = "X-Request-Id"

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def get_correlation_id() -> str | None:
    """The current context's correlation id, or None outside any request/job."""
    return _correlation_id.get()


def bind_correlation_id(correlation_id: str) -> Token[str | None]:
    """Bind an id and return the restore token.

    The token-based pair (this + :func:`unbind_correlation_id`) exists for
    frameworks whose request lifecycle is split across callbacks — Flask's
    before/teardown hooks can't wrap a ``with`` block around a request. Code
    that *can* use a ``with`` block should prefer :func:`correlation_context`.
    """
    return _correlation_id.set(correlation_id)


def unbind_correlation_id(token: Token[str | None]) -> None:
    """Restore the binding that was live before the matching bind."""
    _correlation_id.reset(token)


def ensure_correlation_id() -> str:
    """The current id, minting and binding a fresh one if absent.

    For entry points that may be reached without an upstream id (beat ticks,
    manual shell invocations) — everything downstream can then rely on one
    existing.
    """
    current = _correlation_id.get()
    if current is not None:
        return current
    fresh = new_ulid()
    _correlation_id.set(fresh)
    return fresh


def extract_correlation_id(
    headers: Mapping[str, str], *, header: str = CORRELATION_HEADER
) -> str | None:
    """Pull a correlation id out of a header mapping, case-insensitively.

    WSGI, Celery, and plain dicts all disagree about header casing; this is the
    one place that disagreement is absorbed.
    """
    wanted = header.lower()
    for key, value in headers.items():
        if key.lower() == wanted and value:
            return value
    return None


def correlation_headers() -> dict[str, str]:
    """Headers to attach to any outgoing call so the id keeps travelling.

    Empty when no id is bound — never invents one, because an id minted at
    send time would claim a lineage that doesn't exist.
    """
    current = _correlation_id.get()
    if current is None:
        return {}
    return {CORRELATION_HEADER: current}


@contextmanager
def correlation_context(correlation_id: str | None = None) -> Iterator[str]:
    """Bind an id for the duration of a block, restoring the previous binding.

    The restore matters: worker processes are long-lived and handle many jobs,
    so a leaked binding would stamp one job's id onto the next job's logs.
    """
    cid = correlation_id if correlation_id is not None else new_ulid()
    token = _correlation_id.set(cid)
    try:
        yield cid
    finally:
        _correlation_id.reset(token)
