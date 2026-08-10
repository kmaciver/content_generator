"""structured rejection reasons

M3-10. A rejection recorded only as free text is a note to a human: the next
generation runs against exactly the prompt that just failed, and nothing the
reviewer noticed reaches the model. This column is what makes a rejection
*machine-readable*, so it can become a correction the next attempt carries —
and countable, which prose is not.

**jsonb, not a Postgres enum array.** The vocabulary will grow as new failure
modes are found, and every change to a Postgres enum is an ``ALTER TYPE`` that
Alembic does not autogenerate and that cannot share a transaction with other
DDL — a cost paid on every future edit to a list the domain already owns.
Unknown values read back are ignored rather than raising, so a reason retired
later never makes an old artifact impossible to regenerate.

``NOT NULL DEFAULT '[]'`` rather than nullable: "no reasons given" and "the
column predates this feature" are the same thing to every reader, and an empty
array says it without a null check at each use.

Note ``review_decision`` carries an append-only trigger. Adding a column is
DDL, not an UPDATE, so the trigger is untouched — existing rows take the
server default without a row rewrite on PostgreSQL 11+.

Revision ID: 7c1e4a09b2d3
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "7c1e4a09b2d3"
down_revision: str | None = "40baa694d406"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "review_decision",
        sa.Column(
            "reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )


def downgrade() -> None:
    """Reversible, and lossy in the way that matters is stated: dropping this
    discards every structured reason ever recorded. The free-text ``comment``
    survives, so the audit trail keeps whatever the reviewer wrote."""
    op.drop_column("review_decision", "reasons")
