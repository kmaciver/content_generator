"""M2-03 / M2-04: the derived caches, against a real database.

``derive_phase`` is unit-tested in ``packages/domain``. What needs a database is
everything around it: that the cascade selects the right rows, that
``mark_stale`` is genuinely idempotent, and that the phase is written *and* its
movement recorded.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from videoforge_persistence.projection import (
    cascade_staleness,
    recompute_phase,
    refresh_project_state,
)
from videoforge_persistence.uow import UnitOfWork
from videoforge_shared.enums import ArtifactKind, ArtifactState, ProjectPhase

pytestmark = pytest.mark.integration


@pytest.fixture
def uow(db_session: Session) -> UnitOfWork:
    """Built on ``db_session`` so it inherits its rollback isolation.

    A fresh session from the engine would commit nothing and roll back nothing,
    leaving every test's rows visible to the next one — and these tests assert
    on counts.
    """
    return UnitOfWork(db_session)


@pytest.fixture
def world(uow: UnitOfWork) -> dict[str, str]:
    """A project mid-pipeline: approved script, and media built from it."""
    session: Session = uow.session
    session.execute(
        sa.text(
            "INSERT INTO workspace (id, name) VALUES ('01WSPRJ000000000000000000', 'w')"
        )
    )
    session.execute(
        sa.text(
            "INSERT INTO video_project (id, workspace_id, topic, phase)"
            " VALUES ('01PJPRJ000000000000000000',"
            " '01WSPRJ000000000000000000', 'tides', 'DRAFT')"
        )
    )
    ids: dict[str, str] = {"project": "01PJPRJ000000000000000000"}
    for kind, state in (
        # research first: it is the pipeline root, and a world that skips it is
        # a world where the project is still at the research stage no matter
        # what else exists downstream.
        ("research", ArtifactState.APPROVED),
        ("script", ArtifactState.APPROVED),
        ("scene_set", ArtifactState.APPROVED),
        ("prompt", ArtifactState.APPROVED),
        ("image", ArtifactState.APPROVED),
        ("voice", ArtifactState.AWAITING_APPROVAL),
    ):
        artifact_id = f"01AR{kind[:6].upper()}".ljust(26, "0")
        session.execute(
            sa.text(
                "INSERT INTO artifact (id, project_id, kind, state)"
                " VALUES (:id, :p, :k, :s)"
            ),
            {"id": artifact_id, "p": ids["project"], "k": kind, "s": state.value},
        )
        ids[kind] = artifact_id
    session.flush()
    return ids


def _stale(session: Session, artifact_id: str) -> object:
    return session.execute(
        sa.text("SELECT stale_since FROM artifact WHERE id = :id"), {"id": artifact_id}
    ).scalar_one()


class TestCascadeStaleness:
    def test_approving_a_script_stales_everything_downstream(
        self, uow: UnitOfWork, world: dict[str, str]
    ) -> None:
        marked = cascade_staleness(uow, world["project"], ArtifactKind.SCRIPT)
        assert marked == 4  # scene_set, prompt, image, voice

        for kind in ("scene_set", "prompt", "image", "voice"):
            assert _stale(uow.session, world[kind]) is not None, kind

    def test_the_approved_artifact_is_not_stale(
        self, uow: UnitOfWork, world: dict[str, str]
    ) -> None:
        """Approving a script does not make that script out of date. Obvious,
        and exactly the kind of off-by-one a `descendants` that included itself
        would introduce — with the symptom that nothing is ever current."""
        cascade_staleness(uow, world["project"], ArtifactKind.SCRIPT)
        assert _stale(uow.session, world["script"]) is None

    def test_cascading_twice_does_not_move_the_timestamp(
        self, uow: UnitOfWork, world: dict[str, str]
    ) -> None:
        """ "Stale since when?" must keep answering the same thing.

        ``mark_stale`` filters on ``stale_since IS NULL``; without that, every
        later approval resets the clock and the UI can never show how long
        something has been out of date.
        """
        cascade_staleness(uow, world["project"], ArtifactKind.SCRIPT)
        first = _stale(uow.session, world["image"])

        second_pass = cascade_staleness(uow, world["project"], ArtifactKind.SCRIPT)
        assert second_pass == 0
        assert _stale(uow.session, world["image"]) == first

    def test_approving_the_last_stage_stales_nothing(
        self, uow: UnitOfWork, world: dict[str, str]
    ) -> None:
        """Positive control on the other side: a leaf has no descendants, so a
        cascade that marked anything here would be marking the wrong rows."""
        assert cascade_staleness(uow, world["project"], ArtifactKind.PACKAGE) == 0

    def test_a_kind_with_no_stage_is_a_no_op(
        self, uow: UnitOfWork, world: dict[str, str]
    ) -> None:
        """``music`` is a valid ArtifactKind with no stage in the pipeline yet.
        It must not raise — a kind arriving before its stage is a normal state
        of a system that adds stages by config."""
        assert cascade_staleness(uow, world["project"], ArtifactKind.MUSIC) == 0


class TestRecomputePhase:
    def test_the_phase_is_written_and_the_move_recorded(
        self, uow: UnitOfWork, world: dict[str, str]
    ) -> None:
        phase = recompute_phase(uow, world["project"])
        assert phase is ProjectPhase.MEDIA_REVIEW  # voice awaits a human

        # `phase` is set on the mapped object; the reads below are raw SQL,
        # which does not autoflush. Production commits instead — same effect,
        # different trigger.
        uow.flush()

        stored = uow.session.execute(
            sa.text("SELECT phase FROM video_project WHERE id = :id"),
            {"id": world["project"]},
        ).scalar_one()
        assert stored == ProjectPhase.MEDIA_REVIEW.value

        transitions = uow.session.execute(
            sa.text(
                "SELECT from_state, to_state FROM state_transition"
                " WHERE subject_type = 'project_phase' AND subject_id = :id"
            ),
            {"id": world["project"]},
        ).all()
        assert [tuple(row) for row in transitions] == [("DRAFT", "MEDIA_REVIEW")]

    def test_an_unchanged_phase_writes_no_transition(
        self, uow: UnitOfWork, world: dict[str, str]
    ) -> None:
        """The audit trail records movement, not a heartbeat. Every artifact
        transition calls this, so writing unconditionally would bury real
        history under thousands of no-op rows."""
        recompute_phase(uow, world["project"])
        recompute_phase(uow, world["project"])

        count = uow.session.execute(
            sa.text(
                "SELECT count(*) FROM state_transition"
                " WHERE subject_type = 'project_phase' AND subject_id = :id"
            ),
            {"id": world["project"]},
        ).scalar_one()
        assert count == 1

    def test_the_least_advanced_artifact_of_a_kind_decides(
        self, uow: UnitOfWork, world: dict[str, str]
    ) -> None:
        """Twenty image artifacts collapse to one answer, and the laggard wins.

        Nineteen approved images and one still generating is not an approved
        image stage; reporting it as one would let the timeline stage start
        against an input that does not exist yet.
        """
        # A second image artifact needs a distinct ``scene_ref`` — finding S1's
        # constraint treats two NULLs as equal — and since M2-01 that ref must
        # be a real scene. So the per-scene chain gets built for real here.
        session = uow.session
        session.execute(
            sa.text(
                "INSERT INTO artifact_version"
                " (id, artifact_id, version_no, origin, content_hash, inline_content)"
                " VALUES ('01AVSCRIPT00000000000000A', :a, 1, 'generated', 'h', '{}')"
            ),
            {"a": world["script"]},
        )
        session.execute(
            sa.text(
                "INSERT INTO scene_set (id, artifact_version_id, script_version_id)"
                " VALUES ('01SSPRJ000000000000000000',"
                " '01AVSCRIPT00000000000000A', '01AVSCRIPT00000000000000A')"
            )
        )
        session.execute(
            sa.text(
                'INSERT INTO scene (id, scene_set_id, "index", narration_text,'
                " visual_brief, target_duration_ms)"
                " VALUES ('01SCPRJ000000000000000000', '01SSPRJ000000000000000000',"
                " 1, 'n', 'v', 4000)"
            )
        )
        session.execute(
            sa.text(
                "INSERT INTO artifact (id, project_id, kind, scene_ref, state)"
                " VALUES ('01ARIMGLAGGARD0000000000A', :p,"
                " 'image', '01SCPRJ000000000000000000', 'GENERATING')"
            ),
            {"p": world["project"]},
        )

        # One image approved, one still generating. The stage is not done.
        assert recompute_phase(uow, world["project"]) is ProjectPhase.MEDIA_GENERATION


class TestRefreshProjectState:
    def test_an_approval_cascades_and_moves_the_phase(
        self, uow: UnitOfWork, world: dict[str, str]
    ) -> None:
        """The single entry point every transition path calls."""
        refresh_project_state(uow, world["project"], approved_kind=ArtifactKind.SCRIPT)
        uow.flush()
        assert _stale(uow.session, world["image"]) is not None
        assert (
            uow.session.execute(
                sa.text("SELECT phase FROM video_project WHERE id = :id"),
                {"id": world["project"]},
            ).scalar_one()
            == ProjectPhase.MEDIA_REVIEW.value
        )

    def test_without_an_approval_nothing_goes_stale(
        self, uow: UnitOfWork, world: dict[str, str]
    ) -> None:
        """Rejections and edits move the phase but invalidate nothing: nothing
        downstream was ever built on a version that was never approved."""
        refresh_project_state(uow, world["project"])
        assert _stale(uow.session, world["image"]) is None
