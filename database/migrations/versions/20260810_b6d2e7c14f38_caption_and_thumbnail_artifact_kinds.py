"""caption and thumbnail artifact kinds

M5-01 and M5-02. Two new stages at the end of the pipeline, and therefore two
new labels on ``artifact_kind``.

**Why they are stages and not fields on the package.** F10 puts caption text,
hashtags and a thumbnail inside the publishing zip, and the cheap shape is to
have the package worker produce them on its way past. That makes the review
unit a **zip file**, which no human can meaningfully review, and it makes
"rewrite the caption" mean "rebuild the archive". Both of these are things a
person will want to look at and change on their own — the caption is literally
the text that gets published — so they are versioned, reviewable artifacts like
every other stage here.

**No new ``project_phase`` labels.** Both stages report ``PACKAGING`` while
generating *and* while awaiting review, joining ``package`` itself. §12.4 calls
the phase *coarse* and derived, and it is: the stage rail already tells a
reviewer exactly which of the three is waiting on them. Inventing
``CAPTIONING`` and ``THUMBNAILING`` would add two labels to a user-facing
vocabulary to say something the screen beside it already says better.

**``ADD VALUE`` runs inside Alembic's transaction, and that is fine here.**
Postgres has allowed it since 12 provided the new label is not *used* in the
same transaction — and nothing here writes a row. A ``CREATE TYPE`` would have
had to be spelled out by hand (M3-02, M4-01); an ``ADD VALUE`` is simpler but
still not something Alembic autogenerates, because the enum lives in
``videoforge_shared`` and the comparison only sees the column type.

Irreversible by nature: Postgres cannot drop a value from an enum. See
``downgrade``.

Revision ID: b6d2e7c14f38
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "b6d2e7c14f38"
down_revision: str | None = "a1f4c8d90e21"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Added after ``render`` so the type's own ordering matches the pipeline's.
#: Cosmetic — nothing sorts on it — but an enum whose declaration order
#: contradicts the process it describes is a small lie in ``\dT+`` output.
_NEW_KINDS = (("caption", "render"), ("thumbnail", "caption"))


def upgrade() -> None:
    for value, after in _NEW_KINDS:
        op.execute(
            "ALTER TYPE artifact_kind ADD VALUE IF NOT EXISTS "
            f"'{value}' AFTER '{after}'"
        )


def downgrade() -> None:
    """A no-op, stated rather than silently omitted.

    Postgres has no ``ALTER TYPE ... DROP VALUE``. Removing these would mean
    recreating ``artifact_kind`` and rewriting every column that uses it, while
    any surviving ``caption`` or ``thumbnail`` row would have to be deleted
    first — a data-destroying operation that a downgrade must not perform on
    its own. Leaving two unused labels behind is harmless: nothing produces
    them once the stages are gone from ``pipeline.yaml``.
    """
