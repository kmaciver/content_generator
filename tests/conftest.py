"""Integration-test harness: a real PostgreSQL, per session (SADD §22).

Some properties only exist in the database and cannot be unit-tested against a
fake: immutability triggers that must *raise*, `UNIQUE NULLS NOT DISTINCT`,
optimistic locking, the `artifact_version_status` view, and whether a migration
actually applies. From M1-01 those are the substance of the schema, so the
harness lands first.

**How this runs.** Tests execute inside the tooling container (ADR-014), so
testcontainers spawns Postgres as a *sibling* through the mounted docker socket
rather than a child. The sibling publishes on a host port, which the tooling
container reaches via `host.docker.internal` — hence the socket mount,
`--add-host`, and `TESTCONTAINERS_HOST_OVERRIDE` in the Makefile's `TOOL`.

Ryuk (testcontainers' reaper sidecar) is disabled there too: it assumes it can
see the containers it reaps, which does not hold in the sibling arrangement.
The fixture's own teardown stops the container instead.

Anything using these fixtures must be marked `@pytest.mark.integration`, so a
machine without a docker socket can still run the unit suite with
`-m "not integration"`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Pinned to match the compose stack — testing against a different major
#: version than production would defeat the point of testing against a real one.
POSTGRES_IMAGE = "postgres:16.14-bookworm"


def _docker_available() -> bool:
    """True when a docker socket is reachable, so the skip reason is honest."""
    try:
        import docker

        docker.from_env().ping()
    except Exception:
        return False
    return True


@pytest.fixture(scope="session")
def postgres_url() -> Iterator[str]:
    """A throwaway PostgreSQL, once per test session.

    Session-scoped because container startup is a few seconds and per-test
    isolation is better achieved with transaction rollback (see `db_session`
    in M1-03) than by paying that cost repeatedly.
    """
    if not _docker_available():
        pytest.skip("no docker socket — integration tests need one")

    # testcontainers.postgres is deprecated in 4.x in favour of the
    # community namespace; fall back so this keeps working either way.
    try:
        from testcontainers.community.postgres import PostgresContainer
    except ImportError:  # older testcontainers
        from testcontainers.postgres import PostgresContainer

    with PostgresContainer(
        POSTGRES_IMAGE,
        username="videoforge",
        password="videoforge-test",
        dbname="videoforge_test",
        driver="psycopg",
    ) as container:
        yield container.get_connection_url()


@pytest.fixture()
def migrated_postgres_url(postgres_url: str) -> Iterator[str]:
    """`postgres_url`, with every Alembic migration applied.

    Runs the real migration chain rather than `metadata.create_all()`, because
    the thing worth testing is what production will actually execute —
    including the raw-SQL triggers and views that `create_all` never sees.
    """
    from alembic import command
    from alembic.config import Config

    config = Config(str(REPO_ROOT / "database" / "alembic.ini"))
    config.set_main_option(
        "script_location", str(REPO_ROOT / "database" / "migrations")
    )

    # env.py builds its URL from PostgresSettings, so point those at the
    # container for the duration and restore afterwards.
    saved = {k: os.environ.get(k) for k in ("POSTGRES_URL_OVERRIDE",)}
    os.environ["POSTGRES_URL_OVERRIDE"] = postgres_url
    try:
        command.upgrade(config, "head")
        yield postgres_url
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@pytest.fixture()
def db_engine(migrated_postgres_url: str) -> Iterator[Engine]:
    """An engine against the migrated database, **truncated on teardown**.

    ``db_session`` isolates by rollback, which covers every test that goes
    through it. Tests that take the engine directly do not get that: they open
    their own sessions and genuinely commit, and whatever they commit outlives
    them.

    That was invisible until M3-06 added ``tests/test_admission.py``, which
    sorts *before* ``test_api.py``. Every previously-committing file
    (``test_pipeline_stages``, ``test_projection``, ``test_double_delivery``)
    happened to sort after it, so their leftovers were never seen by the one
    fixture that cares — ``workspaces.sole()``, which uses ``one_or_none()``
    and therefore **raises** rather than picking arbitrarily once a second
    workspace exists. Twenty-six tests errored at setup, none of them from the
    change that exposed it.

    Truncating here rather than re-migrating keeps the fast path fast (the
    migration chain is the expensive part) and makes collection order
    irrelevant, which is the property that was actually missing.
    """
    engine = create_engine(migrated_postgres_url, future=True)
    try:
        yield engine
    finally:
        _truncate_all(engine)
        engine.dispose()


def _truncate_all(engine: Engine) -> None:
    """Empty every mapped table in one statement.

    ``CASCADE`` because the tables form a graph with foreign keys in both
    directions (``artifact`` ↔ ``scene`` via ``scene_ref``, added in M2-01),
    so no ordering of individual TRUNCATEs is valid. One statement listing them
    all also means one lock acquisition rather than fifteen.

    Best-effort: a test that already tore the schema down leaves nothing to
    truncate, and failing teardown would mask the real failure with a
    confusing second one.
    """
    from sqlalchemy import text

    from videoforge_persistence.base import Base

    tables = ", ".join(f'"{name}"' for name in Base.metadata.tables)
    if not tables:
        return
    try:
        with engine.begin() as connection:
            connection.execute(
                text(f"TRUNCATE TABLE {tables} RESTART IDENTITY CASCADE")
            )
    except Exception:  # pragma: no cover - teardown must not mask a failure
        logging.getLogger(__name__).warning(
            "could not truncate test tables", exc_info=True
        )


@pytest.fixture()
def db_session(db_engine: Engine) -> Iterator[Session]:
    """A session whose work is **always rolled back**.

    Isolation by rollback rather than by re-migrating: the container and the
    migration chain are the expensive part, and a test that leaves rows behind
    is indistinguishable from one that does not if nothing is ever committed.

    The session joins an outer transaction on a single connection, so even a
    ``session.commit()`` inside the test only ends a nested SAVEPOINT — the
    outer ``rollback()`` in the teardown still discards everything. Tests that
    genuinely need to commit (the double-delivery test in M1-04) therefore
    still get a clean database afterwards.
    """
    connection = db_engine.connect()
    transaction = connection.begin()
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
