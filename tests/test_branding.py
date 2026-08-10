"""M3-02: series branding tables, against a real PostgreSQL.

The reason this is an integration test rather than a unit test is one line of
DDL: ``uq_series_character_one_approved`` is a **partial unique index**, and a
fake would happily accept two approved characters for one series. The guarantee
lives in the database, so the test has to as well — the same argument
``test_double_delivery`` makes about ``ON CONFLICT``.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from videoforge_persistence.models import SeriesCharacter, Workspace
from videoforge_persistence.repositories import (
    BrandingRepository,
    ProjectRepository,
    SeriesRepository,
)
from videoforge_shared.enums import BrandingStatus, SubjectType
from videoforge_shared.ids import new_ulid

pytestmark = pytest.mark.integration


@pytest.fixture()
def series_id(db_session: Session) -> str:
    workspace = Workspace(id=new_ulid(), name="branding-test")
    db_session.add(workspace)
    db_session.flush()
    series = SeriesRepository(db_session).create(
        workspace_id=workspace.id, title="Explainers"
    )
    db_session.flush()
    return str(series.id)


@pytest.fixture()
def branding(db_session: Session) -> BrandingRepository:
    return BrandingRepository(db_session)


class TestCharacterVersions:
    def test_versions_start_at_one_and_increment(
        self, branding: BrandingRepository, series_id: str, db_session: Session
    ) -> None:
        first = branding.add_character_version(series_id, name="Pip")
        db_session.flush()
        second = branding.add_character_version(series_id, name="Pip")
        db_session.flush()
        assert (first.version_no, second.version_no) == (1, 2)

    def test_versions_are_per_series(
        self, branding: BrandingRepository, series_id: str, db_session: Session
    ) -> None:
        """A second show starts its own numbering — the whole point of series
        scoping over workspace scoping."""
        other = SeriesRepository(db_session).create(
            workspace_id=str(
                db_session.execute(sa.text("SELECT id FROM workspace")).scalar_one()
            ),
            title="Second show",
        )
        db_session.flush()

        branding.add_character_version(series_id, name="Pip")
        db_session.flush()
        elsewhere = branding.add_character_version(str(other.id), name="Bo")
        db_session.flush()
        assert elsewhere.version_no == 1

    def test_a_new_version_is_pending(
        self, branding: BrandingRepository, series_id: str, db_session: Session
    ) -> None:
        """Never auto-approved. ADR-016 is explicit that reference sheets are
        approved explicitly and never auto-selected."""
        character = branding.add_character_version(series_id, name="Pip")
        db_session.flush()
        assert character.status is BrandingStatus.PENDING

    def test_duplicate_version_numbers_are_rejected_by_the_database(
        self, series_id: str, db_session: Session
    ) -> None:
        """The constraint is the guarantee; the ``max()+1`` query is only the
        common path. Two concurrent writers computing the same number must
        collide rather than both succeed."""
        db_session.add(
            SeriesCharacter(
                id=new_ulid(), series_id=series_id, version_no=1, name="Pip"
            )
        )
        db_session.add(
            SeriesCharacter(
                id=new_ulid(), series_id=series_id, version_no=1, name="Clash"
            )
        )
        with pytest.raises(IntegrityError):
            db_session.flush()


class TestApproval:
    def test_approving_supersedes_the_incumbent(
        self, branding: BrandingRepository, series_id: str, db_session: Session
    ) -> None:
        v1 = branding.add_character_version(series_id, name="Pip")
        db_session.flush()
        branding.approve_character(str(v1.id))
        db_session.flush()

        v2 = branding.add_character_version(series_id, name="Pip v2")
        db_session.flush()
        branding.approve_character(str(v2.id))
        db_session.flush()

        db_session.refresh(v1)
        assert v1.status is BrandingStatus.SUPERSEDED
        assert branding.approved_character(series_id) is not None
        assert str(branding.approved_character(series_id).id) == str(v2.id)  # type: ignore[union-attr]

    def test_two_approved_characters_are_impossible(
        self, branding: BrandingRepository, series_id: str, db_session: Session
    ) -> None:
        """**The reason this file needs Postgres.**

        A fake repository would accept both. The partial unique index does not,
        which is what makes ``approved_character``'s ``one_or_none()`` safe
        rather than optimistic.
        """
        v1 = branding.add_character_version(series_id, name="Pip")
        v2 = branding.add_character_version(series_id, name="Bo")
        db_session.flush()

        v1.status = BrandingStatus.APPROVED
        v2.status = BrandingStatus.APPROVED
        with pytest.raises(IntegrityError):
            db_session.flush()

    def test_superseded_rows_stay_queryable(
        self, branding: BrandingRepository, series_id: str, db_session: Session
    ) -> None:
        """A pinned project must always be able to explain what it used, which
        is the load-bearing half of ADR-016."""
        v1 = branding.add_character_version(series_id, name="Pip")
        db_session.flush()
        branding.approve_character(str(v1.id))
        v2 = branding.add_character_version(series_id, name="Pip v2")
        db_session.flush()
        branding.approve_character(str(v2.id))
        db_session.flush()

        assert len(branding.characters(series_id)) == 2
        assert branding.character(str(v1.id)) is not None

    def test_approving_a_missing_character_returns_none(
        self, branding: BrandingRepository
    ) -> None:
        assert branding.approve_character(new_ulid()) is None


class TestReferenceGroups:
    def test_a_group_is_approved_as_a_set(
        self, branding: BrandingRepository, series_id: str, db_session: Session
    ) -> None:
        """4-8 candidates, one *group* chosen (ADR-016: "candidate groups are
        not versions")."""
        character = branding.add_character_version(series_id, name="Pip")
        db_session.flush()

        group = new_ulid()
        for index in range(1, 5):
            branding.add_reference(
                str(character.id),
                group_id=group,
                index=index,
                storage_key=f"refs/{group}/{index}.png",
                content_hash=f"sha256:{index:064d}",
            )
        db_session.flush()

        branding.approve_character(str(character.id), reference_group_id=group)
        db_session.flush()

        approved = branding.approved_references(series_id)
        assert [r.index for r in approved] == [1, 2, 3, 4]

    def test_losing_groups_survive(
        self, branding: BrandingRepository, series_id: str, db_session: Session
    ) -> None:
        """A rejected sheet is evidence about what the prompt produces, and
        costs nothing to keep."""
        character = branding.add_character_version(series_id, name="Pip")
        db_session.flush()
        rejected, chosen = new_ulid(), new_ulid()
        for group in (rejected, chosen):
            branding.add_reference(
                str(character.id),
                group_id=group,
                index=1,
                storage_key=f"refs/{group}/1.png",
                content_hash="sha256:" + "0" * 64,
            )
        db_session.flush()
        branding.approve_character(str(character.id), reference_group_id=chosen)
        db_session.flush()

        assert len(branding.references(rejected)) == 1
        assert len(branding.approved_references(series_id)) == 1

    def test_no_approved_character_yields_no_references(
        self, branding: BrandingRepository, series_id: str
    ) -> None:
        """Empty, not an exception: a series with no character yet is the
        ordinary early state. Turning it into a 409 is the dispatch service's
        job (M3-06), not this repository's."""
        assert branding.approved_references(series_id) == []

    def test_index_is_unique_within_a_group(
        self, branding: BrandingRepository, series_id: str, db_session: Session
    ) -> None:
        character = branding.add_character_version(series_id, name="Pip")
        db_session.flush()
        group = new_ulid()
        for _ in range(2):
            branding.add_reference(
                str(character.id),
                group_id=group,
                index=1,
                storage_key="refs/x.png",
                content_hash="sha256:" + "0" * 64,
            )
        with pytest.raises(IntegrityError):
            db_session.flush()


class TestStyles:
    def test_approving_supersedes_the_incumbent(
        self, branding: BrandingRepository, series_id: str, db_session: Session
    ) -> None:
        v1 = branding.add_style_version(series_id, name="Flat", prompt_block="flat")
        db_session.flush()
        branding.approve_style(str(v1.id))
        v2 = branding.add_style_version(series_id, name="Flat 2", prompt_block="flat2")
        db_session.flush()
        branding.approve_style(str(v2.id))
        db_session.flush()

        db_session.refresh(v1)
        assert v1.status is BrandingStatus.SUPERSEDED
        approved = branding.approved_style(series_id)
        assert approved is not None and approved.prompt_block == "flat2"

    def test_the_compiled_block_is_stored_not_recomputed(
        self, branding: BrandingRepository, series_id: str, db_session: Session
    ) -> None:
        """§10.3 rule 4 needs the value that actually reached the provider, not
        one re-derived by whatever the compiler does today."""
        style = branding.add_style_version(
            series_id,
            name="Flat",
            fields={"palette": ["#101010"]},
            prompt_block="flat vector, single light source",
        )
        db_session.flush()
        db_session.refresh(style)
        assert style.prompt_block == "flat vector, single light source"


class TestCascades:
    def test_deleting_a_series_takes_its_branding(
        self, branding: BrandingRepository, series_id: str, db_session: Session
    ) -> None:
        """CASCADE, not SET NULL: these tables are mutable, so finding
        M1-04a's trap does not apply — but a character with no series is not a
        thing, so it should not outlive one."""
        character = branding.add_character_version(series_id, name="Pip")
        db_session.flush()
        branding.add_reference(
            str(character.id),
            group_id=new_ulid(),
            index=1,
            storage_key="refs/x.png",
            content_hash="sha256:" + "0" * 64,
        )
        branding.add_style_version(series_id, name="Flat")
        db_session.flush()

        # Projects reference the series with ON DELETE SET NULL, and would
        # otherwise block the delete.
        ProjectRepository(db_session)
        db_session.execute(
            sa.text("DELETE FROM series WHERE id = :id"), {"id": series_id}
        )
        db_session.flush()

        assert branding.characters(series_id) == []
        assert branding.styles(series_id) == []
        remaining = db_session.execute(
            sa.text("SELECT count(*) FROM character_reference")
        ).scalar_one()
        assert remaining == 0


class TestSubjectTypeLabels:
    def test_branding_subjects_are_writable(self, db_session: Session) -> None:
        """The two ``ALTER TYPE`` labels, exercised rather than assumed.

        Alembic never autogenerates enum value additions, so this is the check
        that the hand-written half of the migration actually ran.
        """
        for subject in (SubjectType.SERIES_CHARACTER, SubjectType.SERIES_STYLE):
            # ``CAST(... AS ...)`` rather than ``:subject::subject_type`` —
            # in a ``text()`` construct the ``::`` cast operator collides with
            # SQLAlchemy's ``:param`` syntax and the statement never reaches
            # Postgres.
            accepted = db_session.execute(
                sa.text("SELECT CAST(:subject AS subject_type)"),
                {"subject": subject.value},
            ).scalar_one()
            assert accepted == subject.value


class TestImmutabilityBoundary:
    def test_branding_tables_are_deliberately_mutable(
        self, branding: BrandingRepository, series_id: str, db_session: Session
    ) -> None:
        """The opposite of §10.3, on purpose.

        The artifact tables became append-only so *history* could not be
        rewritten. Branding history lives in ``state_transition`` instead, so
        an UPDATE here is legitimate — and approving v2 requires one. A trigger
        on these tables would make ADR-016 unimplementable.
        """
        from videoforge_persistence.sql import IMMUTABLE_TABLES

        assert "series_character" not in IMMUTABLE_TABLES
        assert "series_style" not in IMMUTABLE_TABLES
        assert "character_reference" not in IMMUTABLE_TABLES

        character = branding.add_character_version(series_id, name="Pip")
        db_session.flush()
        character.name = "Pip, renamed"
        db_session.flush()  # would raise restrict_violation if triggered
