"""Engine and session factories.

The only place a SQLAlchemy engine is constructed — the DB-side twin of the
storage client's "one factory knows the endpoint" rule.
"""

from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from videoforge_shared.settings import PostgresSettings


def create_engine_from_settings(
    postgres: PostgresSettings, *, connect_timeout_s: int = 5
) -> Engine:
    """Build a sync engine.

    ``pool_pre_ping`` because uWSGI workers and Celery workers are long-lived
    while Postgres connections are not guaranteed to be — a recycled container
    or a server-side timeout would otherwise surface as a mid-request
    ``OperationalError`` on a stale pooled connection.
    """
    return create_engine(
        postgres.sqlalchemy_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=5,
        connect_args={"connect_timeout": connect_timeout_s},
    )


def session_factory(engine: Engine) -> sessionmaker[Session]:
    """Sessionmaker with the unit-of-work defaults the service layer expects:
    explicit commits only (SADD §10.1 — one transaction per use case)."""
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
