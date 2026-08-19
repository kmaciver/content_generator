"""publishing_package

M5-03. The table SADD §10.2 has listed since the first draft — ``publishing_
package(…, zip_key, manifest jsonb)`` — and which nothing has needed until
there was a packager to write it.

**An artifact_version extension**, hanging off ``artifact_version_id`` like
``scene_set``, and for the reason that model's docstring argues at length:
artifact is identity, version is content, and an archive is unambiguously
content. Regenerating a package after rewording the caption produces a second
version and a second row, never an UPDATE. The UNIQUE constraint is what makes
"the archive of version N" a lookup rather than a query with an ordering guess.

**CASCADE on delete**, matching every other extension table: this row is
immutable, ``SET NULL`` is an UPDATE (finding M1-04a), and a package cannot
outlive the version that produced it.

The ``manifest`` column duplicates what the stage also writes into
``artifact_version.meta``, and that is deliberate rather than an oversight —
``meta`` is provider debris with no schema, read by a reviewer; this column is
queryable content, for the support question "which packages contain scene 7's
image?". Neither is the other's cache: both are written once, together, and
never change.

Revision ID: c9a3f1e57b24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "c9a3f1e57b24"
down_revision: str | None = "b6d2e7c14f38"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "publishing_package",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("artifact_version_id", sa.String(length=26), nullable=False),
        sa.Column("zip_key", sa.Text(), nullable=False),
        sa.Column(
            "manifest",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["artifact_version_id"],
            ["artifact_version.id"],
            name=op.f("fk_publishing_package_artifact_version_id_artifact_version"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_publishing_package")),
        sa.UniqueConstraint(
            "artifact_version_id",
            name=op.f("uq_publishing_package_artifact_version_id"),
        ),
    )

    # Append-only, like every other extension table (§10.3). The guard function
    # has existed since the core schema; only the trigger is new. FOR EACH
    # STATEMENT, matching the seven tables before it: the statement is never
    # legitimate, so there is no reason to pay per row, and a zero-row UPDATE
    # should still be rejected.
    op.execute(
        "CREATE TRIGGER publishing_package_forbid_update "
        "BEFORE UPDATE ON publishing_package "
        "FOR EACH STATEMENT EXECUTE FUNCTION videoforge_forbid_update();"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS publishing_package_forbid_update "
        "ON publishing_package;"
    )
    op.drop_table("publishing_package")
