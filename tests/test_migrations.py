"""Migration integration tests (M1-00).

These prove the harness works AND give M1-01 a regression net before it writes
a single table: from here on, any migration that fails to apply, fails to
reverse, or drifts from the models fails CI rather than a colleague's laptop.
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, inspect, text

pytestmark = pytest.mark.integration


def test_migrations_apply_to_an_empty_database(migrated_postgres_url: str) -> None:
    """The whole chain runs against a genuinely empty database.

    `metadata.create_all()` would not do: it never executes the raw-SQL
    triggers and views that M1-01 depends on, so it would pass while
    production's actual migration path was broken.
    """
    engine = create_engine(migrated_postgres_url)
    try:
        with engine.connect() as conn:
            version = conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one()
        assert version, "alembic_version should record the applied head"
    finally:
        engine.dispose()


def test_single_head(migrated_postgres_url: str) -> None:
    """Exactly one head (SADD §10.4).

    A branched history is the migration failure that hurts most: it applies
    fine on the machine that created it and breaks everywhere else.
    """
    engine = create_engine(migrated_postgres_url)
    try:
        with engine.connect() as conn:
            heads = conn.execute(text("SELECT version_num FROM alembic_version")).all()
        assert len(heads) == 1, f"expected one head, found {len(heads)}: {heads}"
    finally:
        engine.dispose()


def test_models_match_migrations(migrated_postgres_url: str) -> None:
    """No drift between the ORM metadata and the migrated schema.

    The same property `make migrate-check` enforces, asserted here so it is
    caught by the unit-test job rather than only by the stack job.
    """
    from alembic.autogenerate import compare_metadata
    from alembic.runtime.migration import MigrationContext

    from videoforge_persistence.base import Base

    engine = create_engine(migrated_postgres_url)
    try:
        with engine.connect() as conn:
            context = MigrationContext.configure(conn)
            diff = compare_metadata(context, Base.metadata)
        assert diff == [], f"models and migrations disagree: {diff}"
    finally:
        engine.dispose()


def test_harness_gives_a_genuinely_isolated_database(
    migrated_postgres_url: str,
) -> None:
    """Sanity check on the harness itself: this is a throwaway container, not
    the developer's stack. Writing here must never touch real data."""
    engine = create_engine(migrated_postgres_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE harness_probe (id int primary key)"))
            conn.execute(text("INSERT INTO harness_probe VALUES (1)"))
            count = conn.execute(text("SELECT count(*) FROM harness_probe")).scalar()
            assert count == 1

        assert "harness_probe" in inspect(engine).get_table_names()

        with engine.begin() as conn:
            conn.execute(text("DROP TABLE harness_probe"))
    finally:
        engine.dispose()
