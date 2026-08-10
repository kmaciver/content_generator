"""scene kind: illustration or card

M4-01, from §1.0.3. Not every scene is a generated illustration — the
reference videos intercut artwork with typographic cards ("Step 5" on cream
paper), and the schema had no way to say so. A ``card`` scene renders locally
from a template: no provider call, no cost, no style drift, and byte-identical
between runs.

**The default is what makes this safe on live data.** Every scene row written
before this migration was an illustration, and ``server_default`` states that
once in the schema instead of leaving each reader to assume it. Scene rows are
immutable by design, so a nullable column would be permanently ambiguous for
the rows that already exist — there is no backfill pass that could ever run.

**Two CHECKs, both directions.** ``card_text`` must be present exactly when the
kind is ``card``. A card with no text renders an empty frame; an illustration
carrying card text means the stage that wrote the row disagreed with itself.
Both are silent failures downstream — the first produces a blank frame in a
finished video, the second produces text nobody ever sees — so they are
refused here rather than discovered in a render.

The length bound is a product claim, not a storage one: a card is a display
frame at ~1080px wide in a large marker font, and 60 characters is already
past what stays legible. Text longer than that is a scene that should have
been an illustration.

**The enum type is created explicitly.** Alembic does not autogenerate
``CREATE TYPE`` (learned in M3-02, where two ``subject_type`` labels had to be
added by hand), and a missed enum surfaces as a migration that fails halfway
through on a fresh database rather than on the developer's.

Revision ID: a1f4c8d90e21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1f4c8d90e21"
down_revision: str | None = "7c1e4a09b2d3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCENE_KIND = sa.Enum("illustration", "card", name="scene_kind")


def upgrade() -> None:
    _SCENE_KIND.create(op.get_bind(), checkfirst=False)
    op.add_column(
        "scene",
        sa.Column(
            "kind",
            _SCENE_KIND,
            nullable=False,
            server_default="illustration",
        ),
    )
    op.add_column("scene", sa.Column("card_text", sa.Text(), nullable=True))
    op.create_check_constraint(
        "scene_card_text_matches_kind",
        "scene",
        "(kind = 'card') = (card_text IS NOT NULL)",
    )
    op.create_check_constraint(
        "scene_card_text_fits_a_card",
        "scene",
        "card_text IS NULL OR char_length(card_text) BETWEEN 1 AND 60",
    )


def downgrade() -> None:
    """Lossy, and worth naming: dropping ``kind`` turns every card back into an
    illustration, so a re-upgrade would send scenes to an image provider that
    were deliberately kept away from one. The scene text survives — only the
    classification is lost."""
    op.drop_constraint("scene_card_text_fits_a_card", "scene", type_="check")
    op.drop_constraint("scene_card_text_matches_kind", "scene", type_="check")
    op.drop_column("scene", "card_text")
    op.drop_column("scene", "kind")
    _SCENE_KIND.drop(op.get_bind(), checkfirst=False)
