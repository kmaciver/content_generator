"""image studio

ADR-018. Free-form image generation that belongs to no project and no series:
two new tables, and ``generation_job`` learns a third scope.

**The scope constraint is replaced, not added to.** ``ck_generation_job_scope``
said "exactly one of project or series"; it now says "exactly one of project,
series or studio generation", written with ``num_nonnulls`` so a fourth scope
would not need a pairwise rewrite. Every existing row has exactly one of the
first two set, so the new constraint validates against real data.

**Constraint names are given in full with ``op.f``.** The metadata naming
convention prefixes check constraints with ``ck_<table>_``, and the model's
``name="ck_generation_job_scope"`` was prefixed *again* — the live constraint
is ``ck_generation_job_ck_generation_job_scope``. Passing that exact string
through ``op.f`` stops the convention applying a third time.

Revision ID: e4b7a2c91d05
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "e4b7a2c91d05"
down_revision: str | None = "c9a3f1e57b24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCOPE = "ck_generation_job_ck_generation_job_scope"


def upgrade() -> None:
    op.create_table(
        "studio_generation",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("aspect_ratio", sa.Text(), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column(
            "references",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "count > 0", name=op.f("ck_studio_generation_count_positive")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_studio_generation")),
    )
    op.create_index(
        "ix_studio_generation_created_at",
        "studio_generation",
        ["created_at"],
        unique=False,
    )

    op.create_table(
        "studio_image",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("generation_id", sa.String(length=26), nullable=False),
        sa.Column("index", sa.Integer(), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column(
            "provider_meta",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["generation_id"],
            ["studio_generation.id"],
            name=op.f("fk_studio_image_generation_id_studio_generation"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_studio_image")),
        sa.UniqueConstraint(
            "generation_id",
            "index",
            name=op.f("uq_studio_image_generation_id_index"),
        ),
    )
    op.create_index(
        "ix_studio_image_generation_id",
        "studio_image",
        ["generation_id"],
        unique=False,
    )

    op.add_column(
        "generation_job",
        sa.Column("studio_generation_id", sa.String(length=26), nullable=True),
    )
    op.create_index(
        "ix_generation_job_studio_generation_id",
        "generation_job",
        ["studio_generation_id"],
        unique=False,
    )
    op.create_foreign_key(
        op.f("fk_generation_job_studio_generation_id_studio_generation"),
        "generation_job",
        "studio_generation",
        ["studio_generation_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.drop_constraint(op.f(_SCOPE), "generation_job", type_="check")
    op.create_check_constraint(
        op.f(_SCOPE),
        "generation_job",
        "num_nonnulls(project_id, series_id, studio_generation_id) = 1",
    )


def downgrade() -> None:
    """Reversible, with the same caveat as the series-scope migration.

    Restoring the two-scope constraint **fails if any studio job exists**:
    such a row has neither a project nor a series. Deleting the studio tables
    first would cascade those jobs away — which is why the tables go *after*
    the constraint is restored, so the failure is loud rather than a silent
    deletion of job history.
    """
    op.drop_constraint(op.f(_SCOPE), "generation_job", type_="check")
    op.create_check_constraint(
        op.f(_SCOPE),
        "generation_job",
        "(project_id IS NULL) <> (series_id IS NULL)",
    )
    op.drop_constraint(
        op.f("fk_generation_job_studio_generation_id_studio_generation"),
        "generation_job",
        type_="foreignkey",
    )
    op.drop_index("ix_generation_job_studio_generation_id", table_name="generation_job")
    op.drop_column("generation_job", "studio_generation_id")
    op.drop_index("ix_studio_image_generation_id", table_name="studio_image")
    op.drop_table("studio_image")
    op.drop_index("ix_studio_generation_created_at", table_name="studio_generation")
    op.drop_table("studio_generation")
