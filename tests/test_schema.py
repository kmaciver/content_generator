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


class TestImmutableTableForeignKeys:
    """Finding M1-04a: `SET NULL` and an UPDATE-forbidding trigger cannot coexist.

    ``ON DELETE SET NULL`` is implemented as an UPDATE. Against a table whose
    trigger raises on UPDATE, the cascade fails — so an FK the author intended
    as "clean this up quietly" became "this parent can never be deleted", and
    the error named a table the operator never touched.
    """

    def test_no_immutable_table_uses_set_null(self, db_session: Session) -> None:
        """Asserted against pg_constraint, not against the models.

        A model-level check would pass on a database migrated before the fix.
        This reads what the database actually enforces, which is the only
        thing that governs at runtime.
        """
        offenders = db_session.execute(
            sa.text(
                "SELECT c.relname, con.conname FROM pg_constraint con"
                " JOIN pg_class c ON c.oid = con.conrelid"
                " WHERE con.contype = 'f'"
                "   AND con.confupdtype IS NOT NULL"
                "   AND con.confdeltype = 'n'"  # 'n' = SET NULL
                "   AND c.relname = ANY(:tables)"
            ),
            {"tables": list(IMMUTABLE_TABLES)},
        ).all()
        assert offenders == [], (
            "immutable tables must not use ON DELETE SET NULL — it is an "
            "UPDATE, which their trigger forbids"
        )

    def test_deleting_a_workspace_works(self, db_session: Session) -> None:
        """The operation the bug made impossible.

        Deleting a workspace cascades through project → artifact → version and
        → job, crossing four of the five immutable tables on the way.
        """
        _seed_project(db_session)
        _add_version(db_session, 1)
        _decide(db_session, 1, "APPROVE", 1)
        db_session.execute(
            sa.text(
                "INSERT INTO audit_event"
                " (id, event_type, subject_type, subject_id, actor_id)"
                " VALUES ('01AE00000000000000000000AA', 'x', 'artifact', :a, :u)"
            ),
            {"a": ARTIFACT_ID, "u": USER_ID},
        )

        db_session.execute(
            sa.text("DELETE FROM workspace WHERE id = :id"), {"id": WORKSPACE_ID}
        )
        remaining = db_session.execute(
            sa.text("SELECT count(*) FROM artifact_version")
        ).scalar_one()
        assert remaining == 0

    def test_erasing_a_user_preserves_the_audit_trail(
        self, db_session: Session
    ) -> None:
        """GDPR erasure must work, and must not rewrite history.

        Both halves are the point. The delete has to succeed (it could not
        before), and the ``review_decision`` recording that someone approved a
        version has to survive it — that row is the most load-bearing fact in
        the audit trail, and an immutable table could not have been NULLed
        anyway.
        """
        _seed_project(db_session)
        _add_version(db_session, 1)
        _decide(db_session, 1, "APPROVE", 1)

        db_session.execute(
            sa.text("DELETE FROM app_user WHERE id = :id"), {"id": USER_ID}
        )

        surviving = (
            db_session.execute(sa.text("SELECT reviewer_id FROM review_decision"))
            .scalars()
            .all()
        )
        assert len(surviving) == 1
        # The id is retained deliberately: the decision still records *who*,
        # even though that user row is gone.
        assert surviving[0] == USER_ID


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

        **[M2-01]** The scenes are real rows now. This test used to invent
        ``scene_ref`` values, which was harmless while the column had no
        foreign key and became a failure the moment it did — the FK doing
        exactly its job on its first day.
        """
        _seed_project(db_session)
        _seed_scene_set(db_session)  # scene 1, and an image artifact for it

        for i in (2, 3):
            scene_id = f"01SC0000000000000000000{i}AA"
            _add_scene(db_session, scene_id, index=i)
            db_session.execute(
                sa.text(
                    "INSERT INTO artifact (id, project_id, kind, scene_ref, state)"
                    " VALUES (:id, :p, 'image', :s, 'PENDING')"
                ),
                {
                    "id": f"01ARIMG00000000000000{i:02d}A",
                    "p": PROJECT_ID,
                    "s": scene_id,
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


# --------------------------------------------------------------------------- #
# M2-01 — scene sets and scenes
# --------------------------------------------------------------------------- #

SCENE_SET_ARTIFACT_ID = "01AR00000000000000000000BB"
SCENE_SET_VERSION_ID = "01AVSS0000000000000000000A"
SCENE_SET_ID = "01SS00000000000000000000AA"
SCENE_ID = "01SC00000000000000000000AA"
IMAGE_ARTIFACT_ID = "01AR00000000000000000000CC"


def _seed_scene_set(session: Session) -> None:
    """script v1 → a scene_set artifact and version → one scene → its image.

    The full chain, because the properties under test are all about what
    happens *along* it: the cascade, the cycle, and the per-scene anchor.
    """
    script_version_id = _add_version(session, 1)
    session.execute(
        sa.text(
            "INSERT INTO artifact (id, project_id, kind, state)"
            " VALUES (:id, :p, 'scene_set', 'AWAITING_APPROVAL')"
        ),
        {"id": SCENE_SET_ARTIFACT_ID, "p": PROJECT_ID},
    )
    session.execute(
        sa.text(
            "INSERT INTO artifact_version"
            " (id, artifact_id, version_no, origin, content_hash, inline_content)"
            " VALUES (:id, :a, 1, 'generated', 'scenehash', '{}')"
        ),
        {"id": SCENE_SET_VERSION_ID, "a": SCENE_SET_ARTIFACT_ID},
    )
    session.execute(
        sa.text(
            "INSERT INTO scene_set (id, artifact_version_id, script_version_id)"
            " VALUES (:id, :av, :sv)"
        ),
        {"id": SCENE_SET_ID, "av": SCENE_SET_VERSION_ID, "sv": script_version_id},
    )
    _add_scene(session, SCENE_ID, index=1)
    session.execute(
        sa.text(
            "INSERT INTO artifact (id, project_id, kind, scene_ref, state)"
            " VALUES (:id, :p, 'image', :s, 'PENDING')"
        ),
        {"id": IMAGE_ARTIFACT_ID, "p": PROJECT_ID, "s": SCENE_ID},
    )


def _add_scene(session: Session, scene_id: str, *, index: int) -> None:
    session.execute(
        sa.text(
            'INSERT INTO scene (id, scene_set_id, "index", narration_text,'
            " visual_brief, target_duration_ms)"
            " VALUES (:id, :ss, :i, 'narration', 'a brief', 4000)"
        ),
        {"id": scene_id, "ss": SCENE_SET_ID, "i": index},
    )


class TestSceneSchema:
    """The FK M1 deferred, and the cascade cycle adding it created."""

    def test_deleting_a_scene_removes_its_per_scene_artifacts(
        self, db_session: Session
    ) -> None:
        """CASCADE, not SET NULL.

        SET NULL was permitted here — ``artifact`` is mutable, so finding
        M1-04a does not apply — and would still have been wrong: it turns an
        image artifact into one indistinguishable from a project-wide one,
        which silently violates what finding S1's unique constraint means.
        """
        _seed_project(db_session)
        _seed_scene_set(db_session)

        db_session.execute(
            sa.text("DELETE FROM scene WHERE id = :id"), {"id": SCENE_ID}
        )
        remaining = db_session.execute(
            sa.text("SELECT count(*) FROM artifact WHERE id = :id"),
            {"id": IMAGE_ARTIFACT_ID},
        ).scalar_one()
        assert remaining == 0

    def test_project_deletion_survives_the_cascade_cycle(
        self, db_session: Session
    ) -> None:
        """The M1-04a regression, re-run with a cycle in the graph.

        ``artifact.scene_ref`` closes a loop: artifact → scene → scene_set →
        artifact_version → artifact, every edge CASCADE. Postgres resolves
        cyclic cascades at runtime, but that is a claim worth holding to,
        because the failure mode is the M1-04a one — a project nobody can
        delete, reported as a constraint violation on a table the operator
        never touched.
        """
        _seed_project(db_session)
        _seed_scene_set(db_session)

        db_session.execute(
            sa.text("DELETE FROM video_project WHERE id = :id"), {"id": PROJECT_ID}
        )

        for table in ("artifact", "scene", "scene_set", "artifact_version"):
            left = db_session.execute(
                sa.text(f"SELECT count(*) FROM {table}")  # noqa: S608 - fixed literals
            ).scalar_one()
            assert left == 0, f"{table} still has rows after the project was deleted"

    def test_scene_index_is_unique_within_a_set(self, db_session: Session) -> None:
        """Two scenes cannot share a position — it is what makes ORDER BY total."""
        _seed_project(db_session)
        _seed_scene_set(db_session)

        # Positive control: the next position is fine.
        _add_scene(db_session, "01SC00000000000000000000BB", index=2)

        with pytest.raises(sa.exc.IntegrityError):
            _add_scene(db_session, "01SC00000000000000000000CC", index=1)

    def test_scene_index_is_one_based(self, db_session: Session) -> None:
        """The SADD and the UI both say "scene 4"; index 0 would make them lie."""
        _seed_project(db_session)
        _seed_scene_set(db_session)

        with pytest.raises(sa.exc.IntegrityError):
            _add_scene(db_session, "01SC00000000000000000000DD", index=0)

    def test_one_scene_set_per_version(self, db_session: Session) -> None:
        """UNIQUE(artifact_version_id): "the scenes of version N" is a lookup.

        Without it, a second row could attach to the same version and the
        answer would depend on whatever order the query happened to return.
        """
        _seed_project(db_session)
        _seed_scene_set(db_session)

        with pytest.raises(sa.exc.IntegrityError):
            db_session.execute(
                sa.text(
                    "INSERT INTO scene_set (id, artifact_version_id, script_version_id)"
                    " VALUES ('01SS00000000000000000000BB', :av, :av)"
                ),
                {"av": SCENE_SET_VERSION_ID},
            )
