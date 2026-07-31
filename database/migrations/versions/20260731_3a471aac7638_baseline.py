"""baseline

Revision ID: 3a471aac7638
Revises:
Create Date: 2026-07-31 01:43:06.629910
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa  # noqa: F401  (used by most generated migrations)
from alembic import op  # noqa: F401

revision: str = "3a471aac7638"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
