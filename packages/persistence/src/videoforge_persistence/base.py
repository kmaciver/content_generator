"""Declarative base — the single MetaData every table and migration hangs off.

The naming convention is configured NOW, while zero tables exist, because it
cannot be retrofitted: constraint names are baked into the database at create
time, and changing the convention later means a rename migration for every
constraint in every table. With it, Alembic autogenerate produces stable,
predictable names (``uq_artifact_project_id_kind`` instead of a
server-generated one), which is what makes migration diffs reviewable
(SADD §10.4's "autogenerate + mandatory human review").
"""

from __future__ import annotations

from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """Every ORM model inherits from this; ``Base.metadata`` is what Alembic's
    env.py compares against the migration history (S9)."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)
