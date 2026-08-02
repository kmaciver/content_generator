"""Database access for workers.

One engine per worker *process*, built lazily. Not at import time: Celery
prefork forks after the module is imported, and a connection pool created
before the fork is inherited by every child — several processes then issue
statements over the same socket, which corrupts the protocol in ways that
surface as unrelated errors much later.

Building on first use means each child creates its own pool after forking.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from videoforge_persistence.engine import create_engine_from_settings, session_factory
from videoforge_persistence.uow import UnitOfWork, unit_of_work
from videoforge_shared.settings import get_app_settings

_engine: Engine | None = None
_sessions: sessionmaker[Session] | None = None

__all__ = ["dispose_engine", "get_session_factory", "worker_unit_of_work"]


def get_session_factory() -> sessionmaker[Session]:
    """The process-local sessionmaker, created on first call."""
    global _engine, _sessions
    if _sessions is None:
        _engine = create_engine_from_settings(get_app_settings().postgres)
        _sessions = session_factory(_engine)
    return _sessions


def dispose_engine() -> None:
    """Drop the pool. Used by tests, and by any future fork hook."""
    global _engine, _sessions
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _sessions = None


@contextmanager
def worker_unit_of_work() -> Iterator[UnitOfWork]:
    """A transaction scoped to one piece of worker work."""
    with unit_of_work(get_session_factory()) as uow:
        yield uow
