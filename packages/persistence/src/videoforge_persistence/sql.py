"""Schema objects SQLAlchemy cannot express: the immutability trigger and the
``artifact_version_status`` view.

These live in Python rather than inline in the migration so that exactly one
definition exists. A view duplicated between a migration and a test is a view
that will disagree with itself by M3.

Both are things ``metadata.create_all()`` will never produce, which is why the
integration harness (``tests/conftest.py``) runs the real Alembic chain instead.
"""

from __future__ import annotations

#: Tables with no update path (SADD §10.2, §10.3).
#:
#: ``outbox_event`` is deliberately absent: the drain worker stamps
#: ``published_at``, which is a legitimate UPDATE. ``comment`` is absent too —
#: fixing a typo in a note is not history worth preserving.
IMMUTABLE_TABLES: tuple[str, ...] = (
    "artifact_version",
    "review_decision",
    "state_transition",
    "audit_event",
    "provider_usage",
    # M2-01. Scene content is versioned the same way script text is: editing a
    # scene produces a new scene-set *version*, never an UPDATE. Their presence
    # here is what makes ``artifact.scene_ref`` safe to point at — a per-scene
    # image artifact whose scene could be rewritten underneath it would make
    # "which scene is this an image of?" unanswerable.
    "scene_set",
    "scene",
    # M5-03. A publishing package is the *output* of a version, built once from
    # inputs that are themselves immutable. Rebuilding after a caption edit
    # produces a new package version and a new row — an UPDATE here would
    # silently change what a manifest a recipient already verified describes.
    "publishing_package",
)

#: **Finding M1-04a: an immutable table may never be the source of an
#: ``ON DELETE SET NULL`` foreign key.**
#:
#: ``SET NULL`` is implemented as an UPDATE, and these tables' triggers forbid
#: UPDATE. The two mechanisms were each correct in isolation and mutually
#: exclusive in combination: deleting a workspace, a project, or a user raised
#: ``restrict_violation`` from a cascade the caller never wrote. Erasing a user
#: — a GDPR operation — was simply impossible.
#:
#: Every FK from an immutable table is therefore one of:
#:
#: * ``CASCADE`` — the row is meaningless without its parent and dies with it
#:   (a version's generating job, its lineage parent, a job's transitions).
#: * **absent** — the row must outlive its parent, so there is no constraint
#:   to violate (every reference to ``app_user``: history outlives actors,
#:   which is the same reasoning ``state_transition.subject_id`` already used).
#:
#: ``tests/test_schema.py::TestImmutableTableForeignKeys`` enforces this
#: against the live database, because a model review will not catch it twice.
FORBIDDEN_FK_ACTION = "SET NULL"

#: One shared trigger function rather than one per table — ``TG_TABLE_NAME``
#: makes the message specific without duplicating the body five times.
#:
#: Only UPDATE is forbidden, matching §10.3. DELETE stays available on purpose:
#: retention and erasure need *some* path, and a table nobody can delete from
#: is a table that eventually forces a migration to work around it. The
#: guarantee being bought here is "history is not rewritten", not "rows are
#: eternal".
FORBID_UPDATE_FUNCTION = """
CREATE OR REPLACE FUNCTION videoforge_forbid_update() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION
        'relation "%" is append-only; UPDATE is forbidden (SADD 10.3)',
        TG_TABLE_NAME
        USING ERRCODE = 'restrict_violation';
END;
$$;
"""

DROP_FORBID_UPDATE_FUNCTION = "DROP FUNCTION IF EXISTS videoforge_forbid_update();"


def forbid_update_trigger(table: str) -> str:
    """DDL attaching the guard to one table.

    ``FOR EACH STATEMENT``, not ``FOR EACH ROW``: the statement never
    legitimately updates anything, so there is no reason to pay per-row, and a
    zero-row UPDATE should still be rejected rather than silently succeed.
    """
    return (
        f"CREATE TRIGGER {table}_forbid_update "
        f"BEFORE UPDATE ON {table} "
        f"FOR EACH STATEMENT EXECUTE FUNCTION videoforge_forbid_update();"
    )


def drop_forbid_update_trigger(table: str) -> str:
    return f"DROP TRIGGER IF EXISTS {table}_forbid_update ON {table};"


#: **Finding B1** — version status, derived rather than stored.
#:
#: The SADD originally said to "mark siblings SUPERSEDED", which is an UPDATE
#: against a table whose trigger raises on UPDATE. This view is the remedy:
#: one definition of what APPROVED means, shared by the API, the domain layer,
#: and the repositories, computed from ``review_decision`` alone.
#:
#: Precedence, highest first — a version matches exactly one:
#:
#: 1. ``APPROVED``   — holds the artifact's most recent APPROVE decision.
#: 2. ``REJECTED``   — its own most recent decision is REJECT. Ranked above
#:    SUPERSEDED so an explicit human "no" is never relabelled as merely
#:    outdated; the audit distinction is the point of keeping rejected
#:    versions queryable forever (§10.3 rule 2).
#: 3. ``SUPERSEDED`` — an approval exists elsewhere AND this version is
#:    *older* than it, or this version's own approval was replaced.
#: 4. ``AWAITING_APPROVAL`` — undecided and not superseded.
#:
#: **Deviation from SADD §12.2, deliberate.** The SADD says "any non-approved
#: sibling version when another is approved: SUPERSEDED", full stop. That rule
#: silently assumes versions are approved in creation order. They are not:
#: regenerating after an approval produces version N+1, which under the literal
#: rule is instantly SUPERSEDED — so the one version actually awaiting a human
#: decision renders in the review UI as obsolete, and nobody looks at it. The
#: version_no comparison below restricts SUPERSEDED to versions the approval
#: has genuinely moved *past*. Verified by the M1-01 integration tests, which
#: cover regenerate-after-approve explicitly.
#:
#: Ties break on ``id DESC``. ULIDs sort by creation time, so this resolves
#: two decisions written inside the same transaction — where ``decided_at``
#: comes from ``now()`` and is therefore *identical*, not merely close.
ARTIFACT_VERSION_STATUS_VIEW = """
CREATE OR REPLACE VIEW artifact_version_status AS
WITH latest_decision AS (
    SELECT DISTINCT ON (rd.artifact_version_id)
           rd.artifact_version_id,
           rd.decision,
           rd.decided_at
    FROM review_decision rd
    ORDER BY rd.artifact_version_id, rd.decided_at DESC, rd.id DESC
),
current_approval AS (
    SELECT DISTINCT ON (av.artifact_id)
           av.artifact_id,
           av.id        AS artifact_version_id,
           av.version_no AS version_no
    FROM artifact_version av
    JOIN latest_decision ld ON ld.artifact_version_id = av.id
    WHERE ld.decision = 'APPROVE'
    ORDER BY av.artifact_id, ld.decided_at DESC, av.id DESC
)
SELECT
    av.id           AS artifact_version_id,
    av.artifact_id  AS artifact_id,
    av.version_no   AS version_no,
    CASE
        WHEN ca.artifact_version_id = av.id THEN 'APPROVED'
        WHEN ld.decision = 'REJECT'         THEN 'REJECTED'
        -- Older than the standing approval: genuinely passed over.
        WHEN ca.version_no > av.version_no  THEN 'SUPERSEDED'
        -- Was approved once, and a different version now holds the approval
        -- (the §12.5 rollback: an older version was re-approved).
        WHEN ca.artifact_version_id IS NOT NULL
             AND ld.decision = 'APPROVE'    THEN 'SUPERSEDED'
        -- Newer than the approval and undecided: this is what the reviewer
        -- is being asked about. NOT superseded.
        ELSE 'AWAITING_APPROVAL'
    END             AS status,
    ld.decided_at   AS decided_at
FROM artifact_version av
LEFT JOIN latest_decision ld  ON ld.artifact_version_id = av.id
LEFT JOIN current_approval ca ON ca.artifact_id = av.artifact_id;
"""

DROP_ARTIFACT_VERSION_STATUS_VIEW = "DROP VIEW IF EXISTS artifact_version_status;"
