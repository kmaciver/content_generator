"""Alembic environment.

The S9 concern, resolved: this file imports the application's metadata, so the
image running migrations must have the workspace packages installed — which
the shared application image does (M0-02). ``target_metadata`` comes from
``videoforge_persistence`` (NOT the backend: workers write to the same schema,
so the data layer is app-neutral), and the connection URL comes from the same
``PostgresSettings`` every service resolves, so migrations run against
whatever the environment says with no second source of truth.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

# Imported for the side effect of registering every model on ``Base.metadata``
# — autogenerate compares that against the live database, so an unimported
# model module is a table Alembic believes should be DROPPED. Importing the
# package namespace (rather than each module) means adding a model in M2 needs
# no edit here, only an entry in ``models/__init__.py``.
import videoforge_persistence.models  # noqa: E402,F401  (side-effecting import)
from videoforge_persistence.base import Base
from videoforge_shared.settings import PostgresSettings

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """Connection URL for migrations.

    ``POSTGRES_URL_OVERRIDE`` exists for the integration harness, which points
    the migration chain at a throwaway testcontainer. Absent that (i.e. always,
    in production), the URL comes from the same PostgresSettings every service
    resolves — one source of truth, no credentials in any ini file.
    """
    override = os.environ.get("POSTGRES_URL_OVERRIDE")
    if override:
        return override
    return PostgresSettings().sqlalchemy_url


def run_migrations_offline() -> None:
    """Emit SQL to stdout instead of executing (``alembic upgrade --sql``)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # NullPool: the migrate service is a one-shot process; holding pooled
    # connections open past the last migration would only delay its exit.
    engine = create_engine(_database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
