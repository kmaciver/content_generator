"""M4-01 — ``scene.kind ∈ {illustration, card}`` (§1.0.3).

Two layers, tested separately because they fail differently.

The **classification** (``_kind_of``) is pure and runs on every scene the model
emits, so its tests need no database. What it is really defending is the two
CHECK constraints below: a stage that wrote a contradiction would fail the
whole job, discarding nineteen good scenes over one bad field.

The **constraints** are the reason the classification has to be careful, so
they are asserted directly rather than trusted. A card with no text renders an
empty frame in a finished video; an illustration carrying card text means two
fields disagree about what a scene is. Neither is visible until a render.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session
from tests.test_schema import SCENE_SET_ID, _seed_project, _seed_scene_set

from videoforge_shared.enums import SceneKind
from videoforge_shared.ids import new_ulid
from videoforge_workers.scenes import _kind_of, _normalise


class TestClassification:
    """``_kind_of`` — what the model said, reduced to what the columns accept."""

    def test_absent_kind_is_an_illustration(self) -> None:
        """The pre-M4 shape. Every scene ever written before this ticket had
        no ``kind`` field, and the safe direction to fail is the old one."""
        assert _kind_of({}) == (SceneKind.ILLUSTRATION, None)

    def test_card_keeps_its_text(self) -> None:
        assert _kind_of({"kind": "card", "card_text": "Step 5"}) == (
            SceneKind.CARD,
            "Step 5",
        )

    def test_card_text_is_stripped(self) -> None:
        assert _kind_of({"kind": "card", "card_text": "  1962\n"})[1] == "1962"

    def test_kind_is_matched_case_insensitively(self) -> None:
        """The schema constrains it, but a model that shouts ``CARD`` got the
        judgement right and the casing wrong."""
        assert _kind_of({"kind": "Card", "card_text": "3× more likely"})[0] is (
            SceneKind.CARD
        )

    def test_card_without_text_is_demoted_not_rejected(self) -> None:
        """The important one.

        A card with no text cannot be rendered, and the CHECK would refuse the
        row — failing the entire scene set. A card that cannot be rendered is
        exactly an illustration, which costs one image instead of the run.
        """
        assert _kind_of({"kind": "card"}) == (SceneKind.ILLUSTRATION, None)
        assert _kind_of({"kind": "card", "card_text": "   "}) == (
            SceneKind.ILLUSTRATION,
            None,
        )

    def test_illustration_discards_stray_card_text(self) -> None:
        """The other direction of the same CHECK. One of the two fields was a
        mistake and we cannot tell which, so the declared kind wins."""
        assert _kind_of({"kind": "illustration", "card_text": "Step 5"}) == (
            SceneKind.ILLUSTRATION,
            None,
        )

    def test_overlong_card_text_is_truncated_to_the_column(self) -> None:
        """60 is what stays legible at card size. A model that wrote 64 got the
        scene right and the brevity wrong — not a reason to fail a job."""
        kind, text = _kind_of({"kind": "card", "card_text": "x" * 200})
        assert kind is SceneKind.CARD
        assert text is not None and len(text) == 60

    def test_unknown_kind_falls_back_rather_than_raising(self) -> None:
        assert _kind_of({"kind": "diagram", "card_text": "Step 5"})[0] is (
            SceneKind.ILLUSTRATION
        )


class TestNormalise:
    """The classification reaches the rows the stage writes."""

    def test_every_scene_carries_a_kind(self) -> None:
        scenes = _normalise(
            {
                "scenes": [
                    {
                        "narration_text": "Water rises.",
                        "visual_brief": "a tide coming in",
                        "target_duration_ms": 4000,
                    },
                    {
                        "narration_text": "Step five.",
                        "visual_brief": "a step marker",
                        "target_duration_ms": 2000,
                        "kind": "card",
                        "card_text": "Step 5",
                    },
                ]
            }
        )
        assert [s["kind"] for s in scenes] == ["illustration", "card"]
        assert [s["card_text"] for s in scenes] == [None, "Step 5"]


@pytest.mark.integration
class TestConstraints:
    """The CHECKs the classification exists to keep satisfied."""

    def _scene_set(self, db_session: Session) -> str:
        """Build the real chain: project → script version → scene set.

        The first version of this read whatever ``scene_set`` happened to be
        in the database and skipped when there was none — which is what it did
        on every run, so five constraint tests passed by not executing. Reusing
        ``test_schema``'s seed helpers is the smaller duplication: they own the
        row shapes, and a second hand-rolled chain here would drift from them.
        """
        _seed_project(db_session)
        _seed_scene_set(db_session)
        return SCENE_SET_ID

    def _insert(self, db_session: Session, **overrides: object) -> None:
        values: dict[str, object] = {
            "id": new_ulid(),
            "scene_set_id": self._scene_set(db_session),
            "index": 9_000,
            "narration_text": "n",
            "visual_brief": "b",
            "target_duration_ms": 1000,
            "kind": "illustration",
            "card_text": None,
        }
        values.update(overrides)
        db_session.execute(
            sa.text(
                'INSERT INTO scene (id, scene_set_id, "index", narration_text,'
                " visual_brief, target_duration_ms, kind, card_text)"
                " VALUES (:id, :scene_set_id, :index, :narration_text,"
                " :visual_brief, :target_duration_ms, :kind, :card_text)"
            ),
            values,
        )

    def test_card_without_text_is_refused(self, db_session: Session) -> None:
        with pytest.raises(sa.exc.IntegrityError):
            self._insert(db_session, kind="card", card_text=None)

    def test_illustration_with_card_text_is_refused(self, db_session: Session) -> None:
        with pytest.raises(sa.exc.IntegrityError):
            self._insert(db_session, kind="illustration", card_text="Step 5")

    def test_overlong_card_text_is_refused(self, db_session: Session) -> None:
        with pytest.raises(sa.exc.IntegrityError):
            self._insert(db_session, kind="card", card_text="x" * 61)

    def test_a_well_formed_card_is_accepted(self, db_session: Session) -> None:
        self._insert(db_session, kind="card", card_text="Step 5")

    def test_kind_defaults_to_illustration(self, db_session: Session) -> None:
        """The claim the migration makes about every row written before it."""
        scene_id = new_ulid()
        db_session.execute(
            sa.text(
                'INSERT INTO scene (id, scene_set_id, "index", narration_text,'
                " visual_brief, target_duration_ms)"
                " VALUES (:id, :s, 9001, 'n', 'b', 1000)"
            ),
            {"id": scene_id, "s": self._scene_set(db_session)},
        )
        assert (
            db_session.execute(
                sa.text("SELECT kind FROM scene WHERE id = :id"), {"id": scene_id}
            ).scalar()
            == "illustration"
        )
