"""core schema

Revision ID: 747ca6bd5ff6
Revises: 3a471aac7638
Create Date: 2026-08-01 08:20:21.049871
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa  # noqa: F401  (used by most generated migrations)
from alembic import op  # noqa: F401
from sqlalchemy.dialects import postgresql

revision: str = "747ca6bd5ff6"
down_revision: str | None = "3a471aac7638"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The enum types, frozen at this revision.
#
# Autogenerate rendered these as ``sa.Enum(..., metadata=MetaData())``, which
# is wrong twice over: ``MetaData`` was never imported, and binding to a fresh
# empty MetaData means SQLAlchemy considers the type externally managed and
# emits no CREATE TYPE at all — every table referencing one would fail. So the
# types are created explicitly here, before any table, and the columns below
# reference them with ``create_type=False``.
#
# Labels are spelled out rather than imported from
# ``videoforge_shared.enums``. A migration is a snapshot of history: if M2
# adds an ``ArtifactKind`` member, this revision must keep creating the
# *nine* labels it created originally, and the new one arrives via its own
# ``ALTER TYPE ... ADD VALUE`` migration (SADD §10.4). An import would
# silently rewrite the past.
_ENUM_TYPES: dict[str, tuple[str, ...]] = {
    "user_role": ("OWNER", "EDITOR", "VIEWER"),
    "project_phase": (
        "DRAFT",
        "RESEARCHING",
        "RESEARCH_REVIEW",
        "SCRIPTING",
        "SCRIPT_REVIEW",
        "SCENING",
        "SCENES_REVIEW",
        "MEDIA_GENERATION",
        "MEDIA_REVIEW",
        "TIMELINE_READY",
        "RENDERING",
        "RENDER_REVIEW",
        "PACKAGING",
        "READY_TO_PUBLISH",
        "PUBLISHED",
    ),
    "artifact_kind": (
        "research",
        "script",
        "scene_set",
        "scene",
        "prompt",
        "image",
        "voice",
        "timeline",
        "render",
        "package",
        "music",
    ),
    "artifact_state": (
        "PENDING",
        "GENERATING",
        "AWAITING_APPROVAL",
        "APPROVED",
        "REJECTED",
        "FAILED",
    ),
    "version_origin": ("generated", "human_edit", "import"),
    "job_status": (
        "QUEUED",
        "RUNNING",
        "SUCCEEDED",
        "FAILED",
        "CANCELLED",
        "ORPHANED",
    ),
    "review_decision_kind": ("APPROVE", "REJECT"),
    "subject_type": ("project_phase", "artifact", "job"),
    "transition_cause": (
        "job_succeeded",
        "job_failed",
        "review",
        "edit",
        "system",
        "reconciler",
    ),
}

#: Tables with no update path (SADD §10.2, §10.3). ``outbox_event`` is absent
#: because the drain worker stamps ``published_at``; ``comment`` is absent
#: because fixing a typo in a note is not history worth preserving.
_IMMUTABLE_TABLES: tuple[str, ...] = (
    "artifact_version",
    "review_decision",
    "state_transition",
    "audit_event",
    "provider_usage",
)

#: One shared function rather than five bodies — ``TG_TABLE_NAME`` makes the
#: message specific. Only UPDATE is forbidden, matching §10.3: DELETE stays
#: available because retention and erasure need some path, and the guarantee
#: being bought is "history is not rewritten", not "rows are eternal".
_FORBID_UPDATE_FUNCTION = """
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

#: **Finding B1** — version status is derived, never stored. The SADD said to
#: "mark siblings SUPERSEDED", which is an UPDATE against a table whose
#: trigger raises on UPDATE. This view is the remedy: one definition of what
#: APPROVED means, shared by the API, the domain layer, and the repositories.
#:
#: Precedence, highest first — every version matches exactly one branch:
#:   1. APPROVED          holds the artifact's most recent APPROVE.
#:   2. REJECTED          its own latest decision is REJECT. Above SUPERSEDED
#:                        so an explicit human "no" is never softened into
#:                        "merely outdated" — that distinction is why rejected
#:                        versions stay queryable forever (§10.3 rule 2).
#:   3. SUPERSEDED        older than the standing approval, or previously
#:                        approved and since replaced.
#:   4. AWAITING_APPROVAL undecided and not superseded.
#:
#: DEVIATION from §12.2, deliberate. The SADD says "any non-approved sibling
#: when another is approved: SUPERSEDED", which assumes versions are approved
#: in creation order. They are not — regenerating after an approval produces
#: version N+1, which under the literal rule is instantly SUPERSEDED, so the
#: one version actually awaiting a decision renders as obsolete and nobody
#: reviews it. The version_no comparison restricts SUPERSEDED to versions the
#: approval has genuinely moved past.
#:
#: Ties break on ``id DESC``. ULIDs sort by creation time, which resolves two
#: decisions written in one transaction — there ``decided_at`` comes from
#: ``now()`` and is *identical*, not merely close.
_ARTIFACT_VERSION_STATUS_VIEW = """
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
        WHEN ca.version_no > av.version_no  THEN 'SUPERSEDED'
        WHEN ca.artifact_version_id IS NOT NULL
             AND ld.decision = 'APPROVE'    THEN 'SUPERSEDED'
        ELSE 'AWAITING_APPROVAL'
    END             AS status,
    ld.decided_at   AS decided_at
FROM artifact_version av
LEFT JOIN latest_decision ld  ON ld.artifact_version_id = av.id
LEFT JOIN current_approval ca ON ca.artifact_id = av.artifact_id;
"""


def upgrade() -> None:
    # Enum types first — every table below references them by name.
    for name, labels in _ENUM_TYPES.items():
        postgresql.ENUM(*labels, name=name).create(op.get_bind(), checkfirst=False)

    # ### commands auto generated by Alembic - please adjust! ###
    op.create_table(
        "outbox_event",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("published_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox_event")),
    )
    op.create_index(
        "ix_outbox_event_unpublished",
        "outbox_event",
        ["created_at"],
        unique=False,
        postgresql_where=sa.text("published_at IS NULL"),
    )
    op.create_table(
        "workspace",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "settings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspace")),
    )
    op.create_table(
        "app_user",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("workspace_id", sa.String(length=26), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column(
            "role",
            postgresql.ENUM(
                "OWNER", "EDITOR", "VIEWER", name="user_role", create_type=False
            ),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_app_user_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_app_user")),
        sa.UniqueConstraint(
            "workspace_id", "email", name=op.f("uq_app_user_workspace_id_email")
        ),
    )
    op.create_table(
        "series",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("workspace_id", sa.String(length=26), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column(
            "style_preset",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "voice_preset",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "music_policy",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("hashtag_template", sa.Text(), nullable=True),
        sa.Column(
            "auto_approve_policy",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_series_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_series")),
    )
    op.create_index("ix_series_workspace_id", "series", ["workspace_id"], unique=False)
    op.create_table(
        "audit_event",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column(
            "subject_type",
            postgresql.ENUM(
                "project_phase",
                "artifact",
                "job",
                name="subject_type",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("subject_id", sa.String(length=26), nullable=False),
        sa.Column("actor_id", sa.String(length=26), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["app_user.id"],
            name=op.f("fk_audit_event_actor_id_app_user"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_event")),
    )
    op.create_index(
        "ix_audit_event_correlation_id", "audit_event", ["correlation_id"], unique=False
    )
    op.create_index(
        "ix_audit_event_event_type_created_at",
        "audit_event",
        ["event_type", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_audit_event_subject",
        "audit_event",
        ["subject_type", "subject_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "video_project",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("workspace_id", sa.String(length=26), nullable=False),
        sa.Column("series_id", sa.String(length=26), nullable=True),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column(
            "phase",
            postgresql.ENUM(
                "DRAFT",
                "RESEARCHING",
                "RESEARCH_REVIEW",
                "SCRIPTING",
                "SCRIPT_REVIEW",
                "SCENING",
                "SCENES_REVIEW",
                "MEDIA_GENERATION",
                "MEDIA_REVIEW",
                "TIMELINE_READY",
                "RENDERING",
                "RENDER_REVIEW",
                "PACKAGING",
                "READY_TO_PUBLISH",
                "PUBLISHED",
                name="project_phase",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "phase_updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "active_pointers",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "settings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["series_id"],
            ["series.id"],
            name=op.f("fk_video_project_series_id_series"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspace.id"],
            name=op.f("fk_video_project_workspace_id_workspace"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_video_project")),
    )
    op.create_index(
        "ix_video_project_series_id", "video_project", ["series_id"], unique=False
    )
    op.create_index(
        "ix_video_project_workspace_id_created_at",
        "video_project",
        ["workspace_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_video_project_workspace_id_phase",
        "video_project",
        ["workspace_id", "phase"],
        unique=False,
    )
    op.create_table(
        "artifact",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("project_id", sa.String(length=26), nullable=False),
        sa.Column(
            "kind",
            postgresql.ENUM(
                "research",
                "script",
                "scene_set",
                "scene",
                "prompt",
                "image",
                "voice",
                "timeline",
                "render",
                "package",
                "music",
                name="artifact_kind",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("scene_ref", sa.String(length=26), nullable=True),
        sa.Column(
            "state",
            postgresql.ENUM(
                "PENDING",
                "GENERATING",
                "AWAITING_APPROVAL",
                "APPROVED",
                "REJECTED",
                "FAILED",
                name="artifact_state",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "current_version_no",
            sa.Integer(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("stale_since", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["video_project.id"],
            name=op.f("fk_artifact_project_id_video_project"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_artifact")),
        sa.UniqueConstraint(
            "project_id",
            "kind",
            "scene_ref",
            name=op.f("uq_artifact_project_id_kind_scene_ref"),
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "ix_artifact_project_id_state",
        "artifact",
        ["project_id", "state"],
        unique=False,
    )
    op.create_index(
        "ix_artifact_stale_since",
        "artifact",
        ["stale_since"],
        unique=False,
        postgresql_where=sa.text("stale_since IS NOT NULL"),
    )
    op.create_table(
        "generation_job",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("project_id", sa.String(length=26), nullable=False),
        sa.Column("artifact_id", sa.String(length=26), nullable=True),
        sa.Column("task_name", sa.Text(), nullable=False),
        sa.Column("queue", sa.Text(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "QUEUED",
                "RUNNING",
                "SUCCEEDED",
                "FAILED",
                "CANCELLED",
                "ORPHANED",
                name="job_status",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("attempt", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "max_attempts", sa.Integer(), server_default=sa.text("3"), nullable=False
        ),
        sa.Column("celery_task_id", sa.Text(), nullable=True),
        sa.Column(
            "input_snapshot",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column(
            "queued_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("started_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "attempt >= 0", name=op.f("ck_generation_job_attempt_non_negative")
        ),
        sa.CheckConstraint(
            "max_attempts > 0", name=op.f("ck_generation_job_max_attempts_positive")
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["artifact.id"],
            name=op.f("fk_generation_job_artifact_id_artifact"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["video_project.id"],
            name=op.f("fk_generation_job_project_id_video_project"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_generation_job")),
        sa.UniqueConstraint(
            "idempotency_key", name=op.f("uq_generation_job_idempotency_key")
        ),
    )
    op.create_index(
        "ix_generation_job_artifact_id", "generation_job", ["artifact_id"], unique=False
    )
    op.create_index(
        "ix_generation_job_project_id_created_at",
        "generation_job",
        ["project_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_generation_job_running_started_at",
        "generation_job",
        ["started_at"],
        unique=False,
        postgresql_where=sa.text("status = 'RUNNING'"),
    )
    op.create_table(
        "artifact_version",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("artifact_id", sa.String(length=26), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column(
            "origin",
            postgresql.ENUM(
                "generated",
                "human_edit",
                "import",
                name="version_origin",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("generation_job_id", sa.String(length=26), nullable=True),
        sa.Column("parent_version_id", sa.String(length=26), nullable=True),
        sa.Column("storage_key", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "inline_content", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
        sa.Column(
            "meta",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("prompt_template_ref", sa.Text(), nullable=True),
        sa.Column("provider_ref", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=26), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "(storage_key IS NULL) <> (inline_content IS NULL)",
            name=op.f("ck_artifact_version_content_in_exactly_one_place"),
        ),
        sa.CheckConstraint(
            "parent_version_id IS NULL OR parent_version_id <> id",
            name=op.f("ck_artifact_version_parent_is_not_self"),
        ),
        sa.CheckConstraint(
            "version_no > 0", name=op.f("ck_artifact_version_version_no_positive")
        ),
        sa.ForeignKeyConstraint(
            ["artifact_id"],
            ["artifact.id"],
            name=op.f("fk_artifact_version_artifact_id_artifact"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["app_user.id"],
            name=op.f("fk_artifact_version_created_by_app_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["generation_job_id"],
            ["generation_job.id"],
            name=op.f("fk_artifact_version_generation_job_id_generation_job"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["parent_version_id"],
            ["artifact_version.id"],
            name=op.f("fk_artifact_version_parent_version_id_artifact_version"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_artifact_version")),
        sa.UniqueConstraint(
            "artifact_id",
            "version_no",
            name=op.f("uq_artifact_version_artifact_id_version_no"),
        ),
    )
    op.create_index(
        "ix_artifact_version_generation_job_id",
        "artifact_version",
        ["generation_job_id"],
        unique=False,
    )
    op.create_table(
        "provider_usage",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("job_id", sa.String(length=26), nullable=False),
        sa.Column("provider", sa.Text(), nullable=False),
        sa.Column("model", sa.Text(), nullable=False),
        sa.Column("operation", sa.Text(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("images", sa.Integer(), nullable=True),
        sa.Column("audio_seconds", sa.Float(), nullable=True),
        sa.Column(
            "unit_cost_estimate",
            sa.Numeric(precision=12, scale=6),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("latency_ms", sa.Integer(), nullable=False),
        sa.Column(
            "raw_meta",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["generation_job.id"],
            name=op.f("fk_provider_usage_job_id_generation_job"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_provider_usage")),
    )
    op.create_index(
        "ix_provider_usage_created_at_provider",
        "provider_usage",
        ["created_at", "provider"],
        unique=False,
    )
    op.create_index(
        "ix_provider_usage_job_id", "provider_usage", ["job_id"], unique=False
    )
    op.create_table(
        "state_transition",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column(
            "subject_type",
            postgresql.ENUM(
                "project_phase",
                "artifact",
                "job",
                name="subject_type",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("subject_id", sa.String(length=26), nullable=False),
        sa.Column("from_state", sa.Text(), nullable=True),
        sa.Column("to_state", sa.Text(), nullable=False),
        sa.Column(
            "cause",
            postgresql.ENUM(
                "job_succeeded",
                "job_failed",
                "review",
                "edit",
                "system",
                "reconciler",
                name="transition_cause",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("actor_id", sa.String(length=26), nullable=True),
        sa.Column("job_id", sa.String(length=26), nullable=True),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["actor_id"],
            ["app_user.id"],
            name=op.f("fk_state_transition_actor_id_app_user"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["job_id"],
            ["generation_job.id"],
            name=op.f("fk_state_transition_job_id_generation_job"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_state_transition")),
    )
    op.create_index(
        "ix_state_transition_correlation_id",
        "state_transition",
        ["correlation_id"],
        unique=False,
    )
    op.create_index(
        "ix_state_transition_subject",
        "state_transition",
        ["subject_type", "subject_id", "created_at"],
        unique=False,
    )
    op.create_table(
        "comment",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("artifact_version_id", sa.String(length=26), nullable=False),
        sa.Column("author_id", sa.String(length=26), nullable=True),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("anchor", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["artifact_version_id"],
            ["artifact_version.id"],
            name=op.f("fk_comment_artifact_version_id_artifact_version"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["author_id"],
            ["app_user.id"],
            name=op.f("fk_comment_author_id_app_user"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_comment")),
    )
    op.create_index(
        "ix_comment_artifact_version_id",
        "comment",
        ["artifact_version_id"],
        unique=False,
    )
    op.create_table(
        "review_decision",
        sa.Column("id", sa.String(length=26), nullable=False),
        sa.Column("artifact_version_id", sa.String(length=26), nullable=False),
        sa.Column(
            "decision",
            postgresql.ENUM(
                "APPROVE", "REJECT", name="review_decision_kind", create_type=False
            ),
            nullable=False,
        ),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("reviewer_id", sa.String(length=26), nullable=True),
        sa.Column(
            "decided_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["artifact_version_id"],
            ["artifact_version.id"],
            name=op.f("fk_review_decision_artifact_version_id_artifact_version"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["reviewer_id"],
            ["app_user.id"],
            name=op.f("fk_review_decision_reviewer_id_app_user"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_review_decision")),
    )
    op.create_index(
        "ix_review_decision_version_decided_at",
        "review_decision",
        [
            "artifact_version_id",
            sa.literal_column("decided_at DESC"),
            sa.literal_column("id DESC"),
        ],
        unique=False,
    )
    # ### end Alembic commands ###

    # --- Objects autogenerate cannot see (SADD §10.3) -----------------------
    #
    # These are why the integration harness runs the real migration chain
    # instead of ``metadata.create_all()``: triggers and views exist only
    # here, so a test against create_all() would pass while production has
    # neither.

    op.execute(_FORBID_UPDATE_FUNCTION)
    for table in _IMMUTABLE_TABLES:
        # FOR EACH STATEMENT, not FOR EACH ROW: the statement is never
        # legitimate, so there is no reason to pay per row — and a zero-row
        # UPDATE should still be rejected rather than quietly succeed.
        op.execute(
            f"CREATE TRIGGER {table}_forbid_update "
            f"BEFORE UPDATE ON {table} "
            f"FOR EACH STATEMENT EXECUTE FUNCTION videoforge_forbid_update();"
        )

    op.execute(_ARTIFACT_VERSION_STATUS_VIEW)


def downgrade() -> None:
    # Reverse order of upgrade: the view depends on the tables, the triggers
    # are attached to them, and the enum types are what the columns are.
    op.execute("DROP VIEW IF EXISTS artifact_version_status;")
    for table in _IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS {table}_forbid_update ON {table};")
    op.execute("DROP FUNCTION IF EXISTS videoforge_forbid_update();")

    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index("ix_review_decision_version_decided_at", table_name="review_decision")
    op.drop_table("review_decision")
    op.drop_index("ix_comment_artifact_version_id", table_name="comment")
    op.drop_table("comment")
    op.drop_index("ix_state_transition_subject", table_name="state_transition")
    op.drop_index("ix_state_transition_correlation_id", table_name="state_transition")
    op.drop_table("state_transition")
    op.drop_index("ix_provider_usage_job_id", table_name="provider_usage")
    op.drop_index("ix_provider_usage_created_at_provider", table_name="provider_usage")
    op.drop_table("provider_usage")
    op.drop_index(
        "ix_artifact_version_generation_job_id", table_name="artifact_version"
    )
    op.drop_table("artifact_version")
    op.drop_index(
        "ix_generation_job_running_started_at",
        table_name="generation_job",
        postgresql_where=sa.text("status = 'RUNNING'"),
    )
    op.drop_index(
        "ix_generation_job_project_id_created_at", table_name="generation_job"
    )
    op.drop_index("ix_generation_job_artifact_id", table_name="generation_job")
    op.drop_table("generation_job")
    op.drop_index(
        "ix_artifact_stale_since",
        table_name="artifact",
        postgresql_where=sa.text("stale_since IS NOT NULL"),
    )
    op.drop_index("ix_artifact_project_id_state", table_name="artifact")
    op.drop_table("artifact")
    op.drop_index("ix_video_project_workspace_id_phase", table_name="video_project")
    op.drop_index(
        "ix_video_project_workspace_id_created_at", table_name="video_project"
    )
    op.drop_index("ix_video_project_series_id", table_name="video_project")
    op.drop_table("video_project")
    op.drop_index("ix_audit_event_subject", table_name="audit_event")
    op.drop_index("ix_audit_event_event_type_created_at", table_name="audit_event")
    op.drop_index("ix_audit_event_correlation_id", table_name="audit_event")
    op.drop_table("audit_event")
    op.drop_index("ix_series_workspace_id", table_name="series")
    op.drop_table("series")
    op.drop_table("app_user")
    op.drop_table("workspace")
    op.drop_index(
        "ix_outbox_event_unpublished",
        table_name="outbox_event",
        postgresql_where=sa.text("published_at IS NULL"),
    )
    op.drop_table("outbox_event")
    # ### end Alembic commands ###

    # Enum types last: a type cannot be dropped while a column still uses it.
    for name in reversed(list(_ENUM_TYPES)):
        postgresql.ENUM(name=name).drop(op.get_bind(), checkfirst=False)
