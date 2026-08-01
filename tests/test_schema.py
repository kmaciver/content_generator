"""M1-01: the schema properties that only exist in the database.

Everything here needs a real PostgreSQL, because everything here is a thing a
fake would happily let you get away with: a trigger that must *raise*,
``UNIQUE NULLS NOT DISTINCT`` (whose whole subtlety is that the default
semantics look identical in a schema dump and behave oppositely), and a view.

The recurring shape is **assert the failure AND assert a positive control**.
A test that only checks "the immutable table rejected my UPDATE" passes just
as well against a database where every UPDATE fails, or where the connection
was already dead. Each guard test is therefore paired with a write that must
succeed.
"""

from __future__ import annotations

from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from videoforge_persistence.repositories.base import affected_rows
from videoforge_persistence.sql import (
    ARTIFACT_VERSION_STATUS_VIEW,
    IMMUTABLE_TABLES,
)

pytestmark = pytest.mark.integration

# Fixed ULID-shaped ids: readable in failure output, and stable so a failing
# assertion names the row it is talking about.
WORKSPACE_ID = "01WS00000000000000000000AA"
USER_ID = "01US00000000000000000000AA"
PROJECT_ID = "01PR00000000000000000000AA"
ARTIFACT_ID = "01AR00000000000000000000AA"


def _seed_project(session: Session) -> None:
    """Workspace → user → project → artifact, the minimum spine."""
    session.execute(
        sa.text("INSERT INTO workspace (id, name) VALUES (:id, 'test')"),
        {"id": WORKSPACE_ID},
    )
    session.execute(
        sa.text(
            "INSERT INTO app_user (id, workspace_id, email, display_name, role)"
            " VALUES (:id, :ws, 'a@example.test', 'A', 'OWNER')"
        ),
        {"id": USER_ID, "ws": WORKSPACE_ID},
    )
    session.execute(
        sa.text(
            "INSERT INTO video_project (id, workspace_id, topic, phase)"
            " VALUES (:id, :ws, 'photosynthesis', 'DRAFT')"
        ),
        {"id": PROJECT_ID, "ws": WORKSPACE_ID},
    )
    session.execute(
        sa.text(
            "INSERT INTO artifact (id, project_id, kind, state)"
            " VALUES (:id, :p, 'script', 'AWAITING_APPROVAL')"
        ),
        {"id": ARTIFACT_ID, "p": PROJECT_ID},
    )


def _version_id(version_no: int) -> str:
    """Deterministic 26-char id carrying the version number where it is visible."""
    return f"01AV{version_no:03d}".ljust(26, "0")


def _add_version(session: Session, version_no: int) -> str:
    version_id = _version_id(version_no)
    session.execute(
        sa.text(
            "INSERT INTO artifact_version"
            " (id, artifact_id, version_no, origin, content_hash, inline_content)"
            " VALUES (:id, :a, :n, 'generated', :h, :c)"
        ),
        {
            "id": version_id,
            "a": ARTIFACT_ID,
            "n": version_no,
            "h": f"hash{version_no}",
            "c": f'{{"draft": {version_no}}}',
        },
    )
    return version_id


def _decide(session: Session, version_no: int, decision: str, offset_min: int) -> None:
    """Record a decision at a controlled point on the timeline.

    ``decided_at`` is set explicitly rather than defaulting to ``now()``:
    inside one transaction ``now()`` is frozen at transaction start, so every
    decision would share a timestamp and the view's ordering would collapse
    onto the id tiebreak alone. That tiebreak is real behaviour worth testing
    — but not what these tests are about, so the timeline is made explicit.

    The id is keyed on ``(version_no, offset_min)``, which is unique per call
    within a test. An earlier version keyed it on a slice of the version id
    that fell entirely inside the zero padding, so every decision collided on
    the primary key — the constraint caught it, which is the argument for
    having it.
    """
    version_id = _version_id(version_no)
    session.execute(
        sa.text(
            "INSERT INTO review_decision"
            " (id, artifact_version_id, decision, reviewer_id, decided_at)"
            " VALUES (:id, :v, :d, :r, now() + make_interval(mins => :off))"
        ),
        {
            "id": f"01RD{version_no:03d}{offset_min:03d}{decision[:1]}".ljust(26, "0"),
            "v": version_id,
            "d": decision,
            "r": USER_ID,
            "off": offset_min,
        },
    )


def _statuses(session: Session) -> dict[int, str]:
    rows = session.execute(
        sa.text(
            "SELECT version_no, status FROM artifact_version_status"
            " WHERE artifact_id = :a ORDER BY version_no"
        ),
        {"a": ARTIFACT_ID},
    ).all()
    return {int(r[0]): str(r[1]) for r in rows}


class TestImmutabilityTriggers:
    """SADD §10.3: append-only tables have no update path."""

    @pytest.mark.parametrize("table", IMMUTABLE_TABLES)
    def test_update_raises(self, db_session: Session, table: str) -> None:
        """Even a zero-row UPDATE must raise.

        The trigger is ``FOR EACH STATEMENT``, so this holds with an empty
        table and no matching rows — an UPDATE that silently affects nothing
        is still an attempt to rewrite history, and a ``FOR EACH ROW`` trigger
        would have let it pass.
        """
        with pytest.raises(sa.exc.DBAPIError) as excinfo:
            db_session.execute(
                sa.text(f"UPDATE {table} SET created_at = now() WHERE id = 'nope'")
            )
        assert "append-only" in str(excinfo.value)

    def test_outbox_event_is_updatable(self, db_session: Session) -> None:
        """Positive control, and a real requirement.

        ``outbox_event`` is deliberately NOT in ``IMMUTABLE_TABLES``: the
        drain worker (M1-05) stamps ``published_at``. If a future change
        "tidied up" by adding the trigger here, the outbox would deadlock
        silently — the drain would publish and then fail to mark, forever.
        """
        db_session.execute(
            sa.text(
                "INSERT INTO outbox_event (id, event_type, payload)"
                " VALUES ('01OB00000000000000000000AA', 'test.event', '{}')"
            )
        )
        result = db_session.execute(
            sa.text(
                "UPDATE outbox_event SET published_at = now()"
                " WHERE id = '01OB00000000000000000000AA'"
            )
        )
        assert affected_rows(result) == 1

    def test_artifact_is_updatable(self, db_session: Session) -> None:
        """Positive control: the mutable half of the artifact split still moves."""
        _seed_project(db_session)
        result = db_session.execute(
            sa.text("UPDATE artifact SET state = 'APPROVED' WHERE id = :id"),
            {"id": ARTIFACT_ID},
        )
        assert affected_rows(result) == 1

    def test_immutable_tables_match_the_database(self, db_session: Session) -> None:
        """``sql.py`` and the migration must not drift apart.

        The migration inlines its own copy of this list on purpose (a
        migration is a snapshot of history and must not change retroactively).
        The cost of that decision is exactly this risk, so it is paid for
        here: if someone adds a table to ``IMMUTABLE_TABLES`` without writing
        a migration, this fails.
        """
        rows = db_session.execute(
            sa.text(
                "SELECT DISTINCT c.relname FROM pg_trigger t"
                " JOIN pg_class c ON c.oid = t.tgrelid"
                " WHERE t.tgname LIKE '%_forbid_update' AND NOT t.tgisinternal"
            )
        ).scalars()
        assert set(rows) == set(IMMUTABLE_TABLES)


class TestArtifactUniqueness:
    """Finding S1."""

    def test_duplicate_project_wide_artifact_rejected(
        self, db_session: Session
    ) -> None:
        """Two `script` artifacts in one project must be impossible.

        This is the case ``NULLS NOT DISTINCT`` exists for. Both rows have
        ``scene_ref IS NULL``, and under Postgres's default NULLS DISTINCT
        semantics ``NULL != NULL`` — so a plain UNIQUE constraint would allow
        both while *looking* correct in the schema dump, leaving
        ``active_pointers['script']`` ambiguous.
        """
        _seed_project(db_session)
        with pytest.raises(sa.exc.IntegrityError) as excinfo:
            db_session.execute(
                sa.text(
                    "INSERT INTO artifact (id, project_id, kind, state)"
                    " VALUES ('01AR00000000000000000000BB', :p, 'script', 'PENDING')"
                ),
                {"p": PROJECT_ID},
            )
        assert "uq_artifact_project_id_kind_scene_ref" in str(excinfo.value)

    def test_per_scene_artifacts_of_one_kind_coexist(self, db_session: Session) -> None:
        """Positive control: distinct ``scene_ref`` values are the normal case.

        Twenty image artifacts per project (§1.0.1) all share
        ``kind='image'``; only ``scene_ref`` separates them. If the constraint
        were over-tight this would fail and the whole media stage with it.
        """
        _seed_project(db_session)
        for i in (1, 2, 3):
            db_session.execute(
                sa.text(
                    "INSERT INTO artifact (id, project_id, kind, scene_ref, state)"
                    " VALUES (:id, :p, 'image', :s, 'PENDING')"
                ),
                {
                    "id": f"01ARIMG00000000000000{i:02d}A",
                    "p": PROJECT_ID,
                    "s": f"01SC0000000000000000000{i}AA",
                },
            )
        count = db_session.execute(
            sa.text(
                "SELECT count(*) FROM artifact WHERE project_id = :p AND kind = 'image'"
            ),
            {"p": PROJECT_ID},
        ).scalar_one()
        assert count == 3


class TestArtifactVersionConstraints:
    def test_content_must_live_in_exactly_one_place(self, db_session: Session) -> None:
        _seed_project(db_session)
        with pytest.raises(sa.exc.IntegrityError):
            db_session.execute(
                sa.text(
                    "INSERT INTO artifact_version"
                    " (id, artifact_id, version_no, origin, content_hash,"
                    "  storage_key, inline_content)"
                    " VALUES ('01AVBOTH000000000000000A', :a, 1, 'generated',"
                    "         'h', 'some/key', '{}')"
                ),
                {"a": ARTIFACT_ID},
            )

    def test_content_in_neither_place_rejected(self, db_session: Session) -> None:
        _seed_project(db_session)
        with pytest.raises(sa.exc.IntegrityError):
            db_session.execute(
                sa.text(
                    "INSERT INTO artifact_version"
                    " (id, artifact_id, version_no, origin, content_hash)"
                    " VALUES ('01AVNONE000000000000000A', :a, 1, 'generated', 'h')"
                ),
                {"a": ARTIFACT_ID},
            )

    def test_enum_stores_values_not_member_names(self, db_session: Session) -> None:
        """``values_callable`` is what makes this pass.

        Without it SQLAlchemy would persist ``SCENE_SET`` where the SADD, the
        API, and every fixture say ``scene_set`` — a mismatch invisible until
        something outside SQLAlchemy reads the column.
        """
        labels = (
            db_session.execute(
                sa.text(
                    "SELECT e.enumlabel FROM pg_enum e"
                    " JOIN pg_type t ON t.oid = e.enumtypid"
                    " WHERE t.typname = 'artifact_kind' ORDER BY e.enumsortorder"
                )
            )
            .scalars()
            .all()
        )
        assert "scene_set" in labels
        assert "SCENE_SET" not in labels


class TestArtifactVersionStatusView:
    """Finding B1: status is derived from ``review_decision``, never stored."""

    def test_undecided_versions_await_approval(self, db_session: Session) -> None:
        _seed_project(db_session)
        _add_version(db_session, 1)
        _add_version(db_session, 2)
        assert _statuses(db_session) == {1: "AWAITING_APPROVAL", 2: "AWAITING_APPROVAL"}

    def test_reject_then_approve(self, db_session: Session) -> None:
        _seed_project(db_session)
        _add_version(db_session, 1)
        _add_version(db_session, 2)
        _decide(db_session, 1, "REJECT", 1)
        _decide(db_session, 2, "APPROVE", 2)
        assert _statuses(db_session) == {1: "REJECTED", 2: "APPROVED"}

    def test_regenerating_after_approval_awaits_review(
        self, db_session: Session
    ) -> None:
        """The deviation from §12.2, pinned.

        Read literally, the SADD marks *every* non-approved sibling
        SUPERSEDED, so a version generated after an approval would render as
        obsolete in the review UI — hiding the one thing the reviewer is meant
        to look at. v3 must await approval, not be superseded by its own
        predecessor.
        """
        _seed_project(db_session)
        _add_version(db_session, 1)
        _add_version(db_session, 2)
        _decide(db_session, 2, "APPROVE", 1)
        _add_version(db_session, 3)
        assert _statuses(db_session) == {
            # Older than the approval and never decided: genuinely passed over.
            1: "SUPERSEDED",
            2: "APPROVED",
            # The one the reviewer is being asked about. Under §12.2 read
            # literally this would also be SUPERSEDED, and invisible.
            3: "AWAITING_APPROVAL",
        }

    def test_approving_newer_supersedes_older(self, db_session: Session) -> None:
        _seed_project(db_session)
        _add_version(db_session, 1)
        _add_version(db_session, 2)
        _decide(db_session, 1, "APPROVE", 1)
        _decide(db_session, 2, "APPROVE", 2)
        assert _statuses(db_session) == {1: "SUPERSEDED", 2: "APPROVED"}

    def test_rollback_by_reapproving_an_older_version(
        self, db_session: Session
    ) -> None:
        """SADD §12.5 rollback, which needs no special case at all.

        Approval always targets an explicit version id, so "go back to v1" is
        just the newest APPROVE — and the view follows without knowing that
        anything unusual happened.
        """
        _seed_project(db_session)
        _add_version(db_session, 1)
        _add_version(db_session, 2)
        _decide(db_session, 1, "APPROVE", 1)
        _decide(db_session, 2, "APPROVE", 2)
        _decide(db_session, 1, "APPROVE", 3)
        assert _statuses(db_session) == {1: "APPROVED", 2: "SUPERSEDED"}

    def test_rejection_outranks_supersession(self, db_session: Session) -> None:
        """An explicit human "no" is never softened into "merely outdated"."""
        _seed_project(db_session)
        _add_version(db_session, 1)
        _add_version(db_session, 2)
        _decide(db_session, 1, "REJECT", 1)
        _decide(db_session, 2, "APPROVE", 2)
        assert _statuses(db_session)[1] == "REJECTED"

    def test_view_definition_has_not_drifted(self, db_session: Session) -> None:
        """``sql.py`` is the single definition; the migration froze a copy.

        Re-applying ``sql.py``'s ``CREATE OR REPLACE VIEW`` and asserting the
        database's normalised definition is unchanged proves the two agree,
        without comparing formatting. If someone edits the view in ``sql.py``
        and forgets the migration, this fails.
        """
        before = db_session.execute(
            sa.text("SELECT pg_get_viewdef('artifact_version_status', true)")
        ).scalar_one()
        db_session.execute(sa.text(ARTIFACT_VERSION_STATUS_VIEW))
        after = db_session.execute(
            sa.text("SELECT pg_get_viewdef('artifact_version_status', true)")
        ).scalar_one()
        assert before == after


class TestNamingConvention:
    """M0-07 installed the convention; M1-01 is the first schema to prove it."""

    def test_constraint_names_are_deterministic(self, db_session: Session) -> None:
        names: dict[str, Any] = {
            row[0]: row[1]
            for row in db_session.execute(
                sa.text(
                    "SELECT conname, contype FROM pg_constraint c"
                    " JOIN pg_class t ON t.oid = c.conrelid"
                    " WHERE t.relname = 'artifact_version'"
                )
            ).all()
        }
        assert "pk_artifact_version" in names
        assert "uq_artifact_version_artifact_id_version_no" in names
        assert "fk_artifact_version_artifact_id_artifact" in names
        assert "ck_artifact_version_content_in_exactly_one_place" in names
