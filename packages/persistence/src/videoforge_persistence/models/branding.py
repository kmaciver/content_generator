"""Series-scoped branding: character, reference sheets, style (ADR-016, M3-02).

The recurring character and the visual style are what make episodes look like
the same show. ADR-016 works through where they live and lands on **series
scope with per-project pinning**; this module is that decision as tables.

**Why these are not artifacts.** The cheap option was two new `ArtifactKind`
labels, inheriting versioning, the FSM, `review_decision`, the status view and
the review UI for the cost of an enum change. It cannot express the
requirement: `artifact.project_id` is `NOT NULL`, so an artifact scoped to a
*series* is not representable, and relaxing it collides head-on with finding
S1 — `UNIQUE (project_id, kind, scene_ref) NULLS NOT DISTINCT` treats NULLs as
equal, so `(NULL, 'character', NULL)` could exist exactly once **in the entire
table**. Two series could not both have a character.

So versioning and approval are reimplemented here. ADR-016 accepts that cost
explicitly and bounds it: three tables that nothing else references, which
makes a wrong character model in M4 a rewrite of three tables rather than
surgery underneath every artifact in the system.

**These tables are mutable, on purpose.** Approving v2 supersedes v1 with an
UPDATE. That is the opposite of §10.3's append-only rule and it is why these
are absent from ``IMMUTABLE_TABLES``: the artifact tables became append-only so
that *history* could not be rewritten, and branding history is not what the
audit trail is for — `state_transition` still records every approval through
the polymorphic subject, with no foreign key needed.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from videoforge_persistence.base import Base
from videoforge_persistence.columns import (
    ULIDType,
    created_at_col,
    jsonb_col,
    ulid_pk,
    updated_at_col,
)
from videoforge_persistence.enum_types import BRANDING_STATUS
from videoforge_shared.enums import BrandingStatus

__all__ = ["CharacterReference", "SeriesCharacter", "SeriesStyle"]

#: Statuses a *live* branding row can hold. Used by the partial unique indexes
#: below: at most one APPROVED per series, with superseded rows staying
#: queryable forever so a pinned project can always explain what it used.
_APPROVED = BrandingStatus.APPROVED.value


class SeriesCharacter(Base):
    """One version of a series' recurring character.

    Each row **is** a version — there is no separate identity table. A
    character has no meaning apart from its traits, so an identity row would
    carry a name and nothing else, and every query would join through it.

    ``immutable_traits`` versus ``variable_traits`` is the R7 mechanism, not
    bookkeeping. The reference analysis (§1.0.2) found that consistency comes
    from making the character convention *radically reductive* — a pale round
    head with dot eyes is near-impossible to draw inconsistently, a detailed
    face is near-impossible to draw consistently. The immutable block is
    therefore authoritative in every generated prompt and **scene text cannot
    override it** (M3-03); variable traits are the parts a scene is allowed to
    change, like pose or expression.
    """

    __tablename__ = "series_character"

    id: Mapped[str] = ulid_pk()
    series_id: Mapped[str] = mapped_column(
        ULIDType, sa.ForeignKey("series.id", ondelete="CASCADE"), nullable=False
    )
    #: Monotonic per series. Allocated by the repository the same way artifact
    #: versions are, so two concurrent approvals cannot both claim v3.
    version_no: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: Traits no scene may contradict — the consistency anchor.
    immutable_traits: Mapped[dict[str, Any]] = jsonb_col()
    #: Traits a scene may vary: pose, expression, framing.
    variable_traits: Mapped[dict[str, Any]] = jsonb_col()
    status: Mapped[BrandingStatus] = mapped_column(
        BRANDING_STATUS, nullable=False, default=BrandingStatus.PENDING
    )
    #: Which generated reference group is the canonical sheet.
    #:
    #: A plain ULID with **no foreign key**, because it names a *group* of
    #: ``character_reference`` rows rather than one row — 4–8 candidates are
    #: approved as a set (ADR-016: "candidate groups are not versions"). A FK
    #: would need a fourth table whose only column is the group's own id.
    approved_reference_group_id: Mapped[str | None] = mapped_column(
        ULIDType, nullable=True
    )
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    __table_args__ = (
        sa.UniqueConstraint("series_id", "version_no"),
        sa.Index("ix_series_character_series_id", "series_id"),
        # At most one approved character per series, enforced by the database
        # rather than by the service that happens to write it. The same
        # instinct as ``generation_job``'s partial idempotency index: a rule
        # that only application code enforces is a rule a second writer breaks.
        sa.Index(
            "uq_series_character_one_approved",
            "series_id",
            unique=True,
            postgresql_where=sa.text(f"status = '{_APPROVED}'"),
        ),
    )


class CharacterReference(Base):
    """One generated reference image for a character version.

    Rows carry a ``group_id`` because generation produces 4–8 candidates in one
    run and the operator approves **a set**, not a picture (ADR-016). The
    winning group's id lands on
    ``SeriesCharacter.approved_reference_group_id``; the losing groups stay,
    because a rejected sheet is evidence about what the prompt produces and
    costs nothing to keep.

    The bytes are **not** here. Images go through ``StorageClient`` as
    content-addressed objects like every other binary (ADR-004) and the row
    holds the key — a jsonb or bytea column would put megabytes in every query
    that touched this table.
    """

    __tablename__ = "character_reference"

    id: Mapped[str] = ulid_pk()
    character_id: Mapped[str] = mapped_column(
        ULIDType,
        sa.ForeignKey("series_character.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: The candidate run this image belongs to. Not a FK — see the class
    #: docstring and ``SeriesCharacter.approved_reference_group_id``.
    group_id: Mapped[str] = mapped_column(ULIDType, nullable=False)
    #: Position within the group, 1-based, so "the third candidate" is stable
    #: across queries rather than dependent on insertion order.
    index: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    storage_key: Mapped[str] = mapped_column(sa.Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    mime_type: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default="image/png"
    )
    width: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    height: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    #: The axes M3-07 selects a reference by when generating a scene: a
    #: three-quarter view for a scene that needs one, a front view otherwise.
    #: Free text rather than enums — the useful vocabulary is not known yet,
    #: and guessing it into a Postgres type buys an ``ALTER TYPE`` per guess.
    pose: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="")
    angle: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="")
    expression: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="")
    shot_type: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="")
    #: What produced this image, for reproducibility (§10.3 rule 4). No FK to
    #: ``generation_job``: jobs are prunable operational history and a
    #: reference sheet must outlive them.
    generation_job_id: Mapped[str | None] = mapped_column(ULIDType, nullable=True)
    #: The exact prompt and parameters used. An immutable snapshot in practice
    #: — nothing updates it — kept as jsonb because its shape is the image
    #: provider's, and that is not settled until M3-04.
    generation_snapshot: Mapped[dict[str, Any]] = jsonb_col()
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    __table_args__ = (
        sa.UniqueConstraint("group_id", "index"),
        sa.Index("ix_character_reference_character_id", "character_id"),
        sa.Index("ix_character_reference_group_id", "group_id"),
    )


class SeriesStyle(Base):
    """One version of a series' visual style.

    Replaces ``series.style_preset``, the free-form jsonb column dropped in
    revision ``20bbda6ac985`` **before this table existed** — deliberately,
    while nothing read it. Once a reader attaches, removing it stops being a
    migration and becomes a data migration out of unvalidated jsonb plus a
    ruling on which source wins when the two disagree.

    ``fields`` is the structured description an operator edits; ``prompt_block``
    is what it compiles to (M3-05). Both are stored rather than compiling on
    read, because the compiled text is what actually reached the provider and
    §10.3 rule 4 needs the value used, not the value re-derived by whatever the
    compiler does today.
    """

    __tablename__ = "series_style"

    id: Mapped[str] = ulid_pk()
    series_id: Mapped[str] = mapped_column(
        ULIDType, sa.ForeignKey("series.id", ondelete="CASCADE"), nullable=False
    )
    version_no: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: Structured style: palette, line weight, rendering technique, background
    #: treatment. Shape deliberately open until M3-05 has real examples.
    fields: Mapped[dict[str, Any]] = jsonb_col()
    #: The compiled, reusable prompt fragment.
    prompt_block: Mapped[str] = mapped_column(
        sa.Text, nullable=False, server_default=""
    )
    status: Mapped[BrandingStatus] = mapped_column(
        BRANDING_STATUS, nullable=False, default=BrandingStatus.PENDING
    )
    created_at: Mapped[datetime] = created_at_col()
    updated_at: Mapped[datetime] = updated_at_col()

    __table_args__ = (
        sa.UniqueConstraint("series_id", "version_no"),
        sa.Index("ix_series_style_series_id", "series_id"),
        sa.Index(
            "uq_series_style_one_approved",
            "series_id",
            unique=True,
            postgresql_where=sa.text(f"status = '{_APPROVED}'"),
        ),
    )
