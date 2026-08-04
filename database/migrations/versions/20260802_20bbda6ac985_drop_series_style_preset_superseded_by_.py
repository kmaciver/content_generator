"""drop series.style_preset (superseded by ADR-016)

``series.style_preset`` was a free-form jsonb column holding "the look".
ADR-016 replaces it with a series-scoped style table carrying versions,
approval, and the pin each project records — so the column is now a second
source of truth for the same thing, which is the drift that record exists to
prevent.

Dropped **now** rather than in M3 alongside the replacement table, because
today nothing reads it. The seed wrote a value; no code path ever read one.
That asymmetry is the whole argument: a written-but-never-read column cannot
break anything when it disappears, so removal is a migration and two call
sites. Once a reader attaches — M2's prompt rendering is the likely first —
removal becomes a data migration out of unvalidated jsonb, plus a decision
about which source wins when the two disagree. Dropping it here also closes
the window in which such a reader could attach.

The downgrade restores the column, not its contents. Any style data seeded
before this revision is gone, which is acceptable for a column with no
readers.

Revision ID: 20bbda6ac985
Revises: 83fa1410a747
Create Date: 2026-08-02 15:56:55.284447
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20bbda6ac985"
down_revision: str | None = "83fa1410a747"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_column("series", "style_preset")


def downgrade() -> None:
    # `postgresql` is imported explicitly above: autogenerate emits this
    # reference without the import, so the file it writes does not run.
    op.add_column(
        "series",
        sa.Column(
            "style_preset",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            autoincrement=False,
            nullable=False,
        ),
    )
