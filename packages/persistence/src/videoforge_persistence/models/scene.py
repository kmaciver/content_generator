"""Scene sets and scenes — the first structured artifact content (SADD §10.2).

Everything before this stage produces prose: a research brief, a script. A
scene set produces *rows*, and that changes what the schema has to carry.

**Deviation from SADD §10.2, and why.** The SADD writes
``scene_set(id, artifact_id→, script_version_id→)`` — hanging off the
*artifact*. That cannot be right, and the SADD contradicts it two sections
later: §20 says reordering or splitting scenes yields "a new scene_set
version". An artifact-scoped scene set has nowhere to put the second version.
It also contradicts §10.3 rule 1, which is the rule the whole schema turns on:
artifact is identity, version is content, and scenes are unambiguously content.

So ``scene_set`` hangs off ``artifact_version_id``, following the precedent
already set by ``script_version`` and by §10.2's own description of
``timeline`` and ``render`` as "artifact_version extensions". The artifact-level
form in §10.2 is treated as a drafting slip rather than a decision.

**Immutable, deliberately.** A human reordering scenes does not UPDATE these
rows; it produces a new ``artifact_version`` of the scene set with a fresh row
set (``origin=human_edit``), exactly like editing a script. That is what makes
``artifact.scene_ref`` safe to point at: a scene, once written, never changes
and is never deleted except with its project.

**The consequence to know about before M3.** Scene ids belong to one scene-set
version, so approving a *revised* scene set produces entirely new scene rows —
and therefore new per-scene image artifacts, even for scenes whose text did not
change. At ~20 images that is the most expensive operation in the system.

The alternative — a stable ``scene_key`` carried across versions so unchanged
scenes keep their images — was considered and not built. It requires deciding
which scenes in a regenerated set are "the same" as scenes in the old one,
which is a matching problem over LLM output with no reliable answer. The
staleness cascade (§12.4) already produces the correct *behaviour*; a key would
only reduce cost, and paying for machinery that can silently mis-match scenes
in order to save provider spend is the wrong trade at this stage. Revisit with
real numbers from M3.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from videoforge_persistence.base import Base
from videoforge_persistence.columns import ULIDType, created_at_col, ulid_pk
from videoforge_persistence.enum_types import SCENE_KIND
from videoforge_shared.enums import SceneKind


class SceneSet(Base):
    """One version's worth of scene breakdown, pinned to the script it came from."""

    __tablename__ = "scene_set"

    id: Mapped[str] = ulid_pk()
    #: The scene-set artifact version this content belongs to. UNIQUE: a
    #: version has exactly one scene set, which is what makes "the scenes of
    #: version N" a lookup rather than a query with an ordering guess.
    artifact_version_id: Mapped[str] = mapped_column(
        ULIDType,
        sa.ForeignKey("artifact_version.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: Which *script* version these scenes were derived from — the
    #: reproducibility pin of §10.3 rule 4, one level below
    #: ``timeline.input_snapshot``. Without it, "why does scene 4 say this?"
    #: has no answer once the script moves on.
    #:
    #: CASCADE rather than SET NULL: this table is immutable, and SET NULL is
    #: an UPDATE (finding M1-04a). Both versions belong to one project and are
    #: only ever deleted together.
    script_version_id: Mapped[str] = mapped_column(
        ULIDType,
        sa.ForeignKey("artifact_version.id", ondelete="CASCADE"),
        nullable=False,
    )
    created_at: Mapped[datetime] = created_at_col()
    # NOTE: no ``updated_at``. Its absence is a claim; the trigger enforces it.

    __table_args__ = (
        sa.UniqueConstraint("artifact_version_id"),
        sa.Index("ix_scene_set_script_version_id", "script_version_id"),
    )


class Scene(Base):
    """One beat of the video: what is said, what is shown, for how long."""

    __tablename__ = "scene"

    id: Mapped[str] = ulid_pk()
    scene_set_id: Mapped[str] = mapped_column(
        ULIDType, sa.ForeignKey("scene_set.id", ondelete="CASCADE"), nullable=False
    )
    #: 1-based, matching how the SADD and the UI talk about scenes ("regenerate
    #: scene 4's image", §12.5). ``index`` is a non-reserved keyword in
    #: PostgreSQL, so unlike ``user`` (finding B7) it needs no renaming.
    index: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    #: What the narrator says. The voice stage reads the concatenation of these
    #: in order; scene boundaries are recovered from word timestamps (§13).
    narration_text: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: What the illustration should show. Input to the prompt stage, never sent
    #: to an image provider directly — the prompt builder owns that translation.
    #:
    #: Written for card scenes too: it is what a *reviewer* reads to judge the
    #: scene, and "a step marker" is still a description of the picture even
    #: when no provider will draw it.
    visual_brief: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: M4-01 (§1.0.3). ``card`` scenes never reach an image provider — they are
    #: rendered locally from a template, so they cost nothing and cannot drift.
    #:
    #: Defaulted in SQL rather than only in Python: every scene row that
    #: existed before this column did was an illustration, and a nullable
    #: column would push that fact into every reader instead of stating it once.
    kind: Mapped[SceneKind] = mapped_column(
        SCENE_KIND, nullable=False, server_default=SceneKind.ILLUSTRATION.value
    )
    #: The words that go **on** the card, verbatim — "Step 5", "1962", "3× more
    #: likely". Separate from ``visual_brief`` on purpose: the brief is prose
    #: for a reader, and a renderer that had to recover the literal text from
    #: it would be parsing English to find a string the model already knew.
    #:
    #: Short by constraint. A card is a display frame, not a paragraph; text
    #: that does not fit at card size is a scene that should have been an
    #: illustration.
    card_text: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    #: The scenes stage's estimate, validated in aggregate against
    #: ``video_project.settings.target_duration_ms`` (finding S11). The
    #: *authoritative* duration comes later, from the voice clip.
    target_duration_ms: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    created_at: Mapped[datetime] = created_at_col()

    __table_args__ = (
        # Two scenes cannot share a position in one set. This is also what makes
        # ORDER BY index a total order rather than a suggestion.
        sa.UniqueConstraint("scene_set_id", "index"),
        sa.CheckConstraint("index > 0", name="scene_index_is_one_based"),
        sa.CheckConstraint("target_duration_ms > 0", name="scene_duration_is_positive"),
        # Both directions, deliberately. A card with no text renders an empty
        # frame; an illustration carrying card text means a stage set the kind
        # and the text disagreed about what this scene is. Neither is
        # recoverable downstream, and both are silent without this.
        sa.CheckConstraint(
            "(kind = 'card') = (card_text IS NOT NULL)",
            name="scene_card_text_matches_kind",
        ),
        sa.CheckConstraint(
            "card_text IS NULL OR char_length(card_text) BETWEEN 1 AND 60",
            name="scene_card_text_fits_a_card",
        ),
    )
