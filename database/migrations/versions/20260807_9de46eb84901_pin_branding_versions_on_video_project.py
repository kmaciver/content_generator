"""pin branding versions on video_project

M3-06, and the load-bearing half of ADR-016. A project records which character
version and which style version it was generated against, and that pin never
moves. Without it, approving character v2 would retroactively invalidate every
episode built from v1 — a staleness cascade across the entire back catalogue,
triggered by an ordinary tweak.

**Deliberately not foreign keys.** These are a provenance record and must
outlive their subjects: deleting a series cascades its branding rows away while
``video_project.series_id`` goes SET NULL, so a FK would either block the delete
or (with SET NULL) erase the record of what the video was actually made from.
Same reasoning as ``state_transition.subject_id`` and
``character_reference.generation_job_id`` — history holds ids, not references.

Nullable with no backfill: existing projects predate branding entirely and
never generated an image against one. NULL means "not pinned yet", which is
exactly true of every row here today, and ``ProjectRepository.pin_branding``
keys its write-once guard off that NULL.

Revision ID: 9de46eb84901
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9de46eb84901"
down_revision: str | None = "cb5ec31b3165"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "video_project",
        sa.Column("character_version_id", sa.String(length=26), nullable=True),
    )
    op.add_column(
        "video_project",
        sa.Column("style_version_id", sa.String(length=26), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("video_project", "style_version_id")
    op.drop_column("video_project", "character_version_id")
