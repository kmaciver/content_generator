"""Request-scoped wiring: one transaction per request, one dispatcher per app.

SADD §10.1's unit of work is "one transaction per use case", and for the API a
use case is a request. The engine and the dispatcher are process-wide (building
either per request would be pure waste); the *session* is not.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from flask import current_app
from sqlalchemy.orm import Session, sessionmaker

from videoforge.services.dispatch import TaskDispatcher
from videoforge_persistence.uow import UnitOfWork, unit_of_work

__all__ = ["SESSION_FACTORY_KEY", "TASK_DISPATCHER_KEY", "dispatcher", "transaction"]

SESSION_FACTORY_KEY = "VIDEOFORGE_SESSIONS"
TASK_DISPATCHER_KEY = "VIDEOFORGE_DISPATCHER"


@contextmanager
def transaction() -> Iterator[UnitOfWork]:
    """A unit of work for the current request.

    Commits on success, rolls back on any exception — including the ones the
    error handlers turn into problem+json. A view that raised after writing
    half a use case must not leave the half behind.
    """
    factory: sessionmaker[Session] = current_app.config[SESSION_FACTORY_KEY]
    with unit_of_work(factory) as uow:
        yield uow


def dispatcher() -> TaskDispatcher:
    """The app's task dispatcher.

    Reached through ``current_app.config`` rather than a module global so a
    test can substitute ``RecordingDispatcher`` by building the app, with no
    patching and no import-order games.
    """
    result: TaskDispatcher = current_app.config[TASK_DISPATCHER_KEY]
    return result
