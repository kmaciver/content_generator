"""live-only idempotency key: a dead job must not hold it forever

Found the first time a real provider returned 529 Overloaded.

The key is derived from the version a job will produce (`task:artifact:vN`), and
a failed job produces no version — so the key never changes. With a plain
UNIQUE constraint the dead job held it forever, `reserve` kept returning that
job with `created=False`, and the artifact's Regenerate button became a silent
no-op. One transient provider blip made a project permanently unrecoverable.

The invariant was always "at most one **live** job per key". A partial unique
index says exactly that. SUCCEEDED is deliberately NOT released: that is the
double-delivery guarantee (§14.3, M1-04) — a redelivered task whose twin
already produced a version must still find the key taken.

Revision ID: 16dae6463a8a
Revises: d9f8efce2586
Create Date: 2026-08-03 15:13:38.560730
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "16dae6463a8a"
down_revision: str | None = "d9f8efce2586"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        op.f("uq_generation_job_idempotency_key"), "generation_job", type_="unique"
    )
    op.create_index(
        "uq_generation_job_live_idempotency_key",
        "generation_job",
        ["idempotency_key"],
        unique=True,
        postgresql_where=sa.text("status NOT IN ('FAILED', 'CANCELLED', 'ORPHANED')"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_generation_job_live_idempotency_key",
        table_name="generation_job",
        postgresql_where=sa.text("status NOT IN ('FAILED', 'CANCELLED', 'ORPHANED')"),
    )
    op.create_unique_constraint(
        op.f("uq_generation_job_idempotency_key"),
        "generation_job",
        ["idempotency_key"],
        postgresql_nulls_not_distinct=False,
    )
