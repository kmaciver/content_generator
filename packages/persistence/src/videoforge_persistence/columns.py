"""Column primitives every model reuses.

Three things are centralised here because getting them subtly different across
thirteen tables is the kind of inconsistency that only shows up in production:
ULID primary keys, timezone-aware timestamps, and the mapping from a Python
enum to a native Postgres enum type.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from videoforge_shared.ids import ULID_LENGTH, new_ulid

#: ULIDs are fixed-width 26-char Crockford base32, so ``CHAR(26)`` is honest
#: about the shape and lets Postgres skip the length header. Not ``uuid``:
#: the sortable timestamp prefix is the entire reason for choosing ULIDs
#: (SADD §10.2), and a uuid column would throw the ordering away.
ULIDType = sa.String(ULID_LENGTH)

#: Every timestamp in the system is ``timestamptz``. A naive timestamp column
#: is a bug waiting for the first container running in a non-UTC locale.
TimestampType = sa.TIMESTAMP(timezone=True)


def pg_enum(enum_cls: type[StrEnum], name: str, metadata: sa.MetaData) -> sa.Enum:
    """A native Postgres ENUM storing member **values**, not member names.

    ``values_callable`` is load-bearing. Without it SQLAlchemy persists
    ``ArtifactKind.SCENE_SET`` as the *name* ``"SCENE_SET"`` while the SADD
    (§10.2), the API, and every fixture say ``"scene_set"``. The mismatch
    would not surface until something outside SQLAlchemy read the column.

    Native rather than a ``VARCHAR`` + CHECK because §10.4 commits to explicit
    ``ALTER TYPE`` migrations for enum changes — that is only available on a
    real enum type, and it makes an invalid value a database error rather than
    an application one.

    ``metadata`` is required, not optional. A ``sa.Enum`` constructed inline
    inside a column is owned by *that table*, so a type used by two tables —
    ``subject_type`` is used by both ``state_transition`` and ``audit_event``
    — emits ``CREATE TYPE`` twice and the second one fails. Binding to the
    MetaData makes the type a first-class schema object created once, before
    any table that references it.
    """
    return sa.Enum(
        enum_cls,
        name=name,
        native_enum=True,
        values_callable=lambda cls: [member.value for member in cls],
        metadata=metadata,
        # No redundant CHECK constraint alongside a native enum — the type
        # already rejects anything outside its labels.
        create_constraint=False,
    )


def ulid_pk() -> Mapped[str]:
    """Primary key column, generated application-side.

    Client-side generation (rather than a database default) is deliberate: a
    service needs the id *before* it commits, to write the matching
    ``outbox_event`` and ``audit_event`` rows in the same transaction without
    a round-trip or a flush in the middle.
    """
    return mapped_column(ULIDType, primary_key=True, default=new_ulid)


def created_at_col() -> Mapped[datetime]:
    """``created_at`` with a server-side default.

    ``server_default=now()`` rather than a Python default so rows inserted by
    migrations, seeds, or psql all get one, and so the clock is the database's
    — comparing timestamps written by three different containers against each
    other otherwise measures clock skew.

    Deliberately **not** indexed here. An automatic index on all thirteen
    tables would cost a write on every insert to buy a lookup nothing
    performs: the real queries are all scoped first (``project_id, created_at``,
    ``subject_type, subject_id, created_at``) and are served by the composite
    indexes the models declare. Where a bare ``created_at`` scan is genuinely
    the access path — the outbox drain — the model declares a partial index
    sized to the backlog instead.
    """
    return mapped_column(TimestampType, nullable=False, server_default=sa.func.now())


def updated_at_col() -> Mapped[datetime]:
    """``updated_at`` for mutable tables only.

    Immutable tables (SADD §10.3) must NOT have this column — its presence is
    a claim that rows change, which the immutability trigger makes false.
    """
    return mapped_column(
        TimestampType,
        nullable=False,
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
    )


def jsonb_col() -> Mapped[dict[str, Any]]:
    """A non-null ``JSONB`` object defaulting to ``{}``.

    ``JSONB`` rather than ``json``: it is indexable, and it normalises on
    write so two equal objects compare equal. Defaulting to an empty object
    rather than allowing NULL removes the "missing or empty?" question from
    every read site — these columns are all bags of settings, and an absent
    bag and an empty bag mean the same thing.
    """
    return mapped_column(
        JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb")
    )


def nullable_jsonb_col() -> Mapped[dict[str, Any] | None]:
    """``JSONB`` where NULL carries meaning distinct from ``{}``.

    Used for ``generation_job.error`` and ``comment.anchor``, where "no error"
    and "an error with no fields" are genuinely different facts.
    """
    return mapped_column(JSONB, nullable=True, default=None)
