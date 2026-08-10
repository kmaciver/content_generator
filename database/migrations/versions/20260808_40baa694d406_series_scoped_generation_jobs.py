"""series-scoped generation jobs

M3-04b. **Not every job is about a project.** Reference-sheet generation belongs
to a *series* (ADR-016) and produces branding every episode consumes, so forcing
it to name one project would be a lie visible in that project's job list.

The alternative — running branding generation with no job row — loses
idempotency (a double-click costs another eight images), the audit trail, and,
because ``provider_usage.job_id`` is NOT NULL, spend metering entirely. Putting
the most expensive operation in the system outside the S10 cap is the wrong
trade, so the job table learns a second scope instead.

**Hand-corrected after autogenerate**, which produced the column, the
nullability change, the index and the foreign key correctly but **not the CHECK
constraint** — Alembic does not autogenerate ``CheckConstraint``. Without it,
making ``project_id`` nullable would buy one real case and one nonsense one: a
job scoped to neither, or to both.

Revision ID: 40baa694d406
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "40baa694d406"
down_revision: str | None = "9de46eb84901"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "generation_job", sa.Column("series_id", sa.String(length=26), nullable=True)
    )
    op.alter_column(
        "generation_job",
        "project_id",
        existing_type=sa.VARCHAR(length=26),
        nullable=True,
    )
    op.create_index(
        "ix_generation_job_series_id", "generation_job", ["series_id"], unique=False
    )
    op.create_foreign_key(
        op.f("fk_generation_job_series_id_series"),
        "generation_job",
        "series",
        ["series_id"],
        ["id"],
        ondelete="CASCADE",
    )
    # Exactly one scope. Added *after* the column exists and while every
    # existing row still has a project_id, so it validates against real data
    # rather than being trusted.
    op.create_check_constraint(
        "ck_generation_job_scope",
        "generation_job",
        "(project_id IS NULL) <> (series_id IS NULL)",
    )


def downgrade() -> None:
    """Reversible, with one caveat stated rather than hidden.

    ``project_id`` goes back to NOT NULL, which **fails if any series-scoped
    job exists**. That is correct: those rows have no project to fall back to,
    and inventing one would corrupt the audit trail more quietly than an error
    does. Delete them first if you genuinely mean to go back.
    """
    op.drop_constraint("ck_generation_job_scope", "generation_job", type_="check")
    op.drop_constraint(
        op.f("fk_generation_job_series_id_series"), "generation_job", type_="foreignkey"
    )
    op.drop_index("ix_generation_job_series_id", table_name="generation_job")
    op.alter_column(
        "generation_job",
        "project_id",
        existing_type=sa.VARCHAR(length=26),
        nullable=False,
    )
    op.drop_column("generation_job", "series_id")
