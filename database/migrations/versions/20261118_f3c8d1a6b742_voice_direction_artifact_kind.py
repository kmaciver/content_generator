"""voice_direction artifact kind

ADR-019. Emotion tags for ``eleven_v3`` become their own reviewable stage
between the scene set and the narration, and therefore a new label on
``artifact_kind``.

**No new ``project_phase`` label.** The stage reports ``SCENING`` while
generating and ``SCENES_REVIEW`` while awaiting review, alongside ``prompt``:
it is reviewed next to the scenes it tags, and the stage rail already says
which of the two is waiting.

**``ADD VALUE`` inside Alembic's transaction is fine**, for the reason
``b6d2e7c14f38`` gives: Postgres allows it provided the label is not used in
the same transaction, and nothing here writes a row.

Irreversible by nature: Postgres cannot drop an enum value. See ``downgrade``.

Revision ID: f3c8d1a6b742
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "f3c8d1a6b742"
down_revision: str | None = "e4b7a2c91d05"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # After `prompt`, so the type's declaration order matches the pipeline's
    # (cosmetic, as in b6d2e7c14f38 — nothing sorts on it).
    op.execute(
        "ALTER TYPE artifact_kind ADD VALUE IF NOT EXISTS "
        "'voice_direction' AFTER 'prompt'"
    )


def downgrade() -> None:
    """A no-op, stated rather than silently omitted.

    Postgres has no ``ALTER TYPE ... DROP VALUE``; removing the label would mean
    recreating ``artifact_kind`` and deleting every ``voice_direction`` row
    first. An unused label is harmless once the stage is gone from
    ``pipeline.yaml``.
    """
