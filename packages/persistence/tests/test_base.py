"""Foundations tests for the persistence package (M0-07)."""

from __future__ import annotations

from sqlalchemy import Column, Integer, Table, UniqueConstraint

from videoforge_persistence.base import NAMING_CONVENTION, Base
from videoforge_shared.settings import PostgresSettings


class TestNamingConvention:
    def test_convention_is_installed(self) -> None:
        assert Base.metadata.naming_convention == NAMING_CONVENTION

    def test_generated_constraint_names_are_deterministic(self) -> None:
        """The property migrations depend on: constraint names derive from the
        convention, not from server-side defaults. Uses a throwaway MetaData
        table so the shared Base registry stays clean."""
        table = Table(
            "sample",
            Base.metadata,
            Column("id", Integer, primary_key=True),
            Column("kind", Integer),
            UniqueConstraint("kind"),
        )
        try:
            names = {c.name for c in table.constraints}
            assert "pk_sample" in names
            assert "uq_sample_kind" in names
        finally:
            Base.metadata.remove(table)

    def test_every_model_is_registered(self) -> None:
        """Replaces M0-07's ``test_metadata_is_empty_before_m1``, which existed
        only until there was a schema.

        The inverted assertion is the one that matters now: Alembic compares
        ``Base.metadata`` against the database, so a model module that nothing
        imports is a table autogenerate will happily emit a ``DROP TABLE``
        for. Naming every table (SADD §10.2) means adding a model without
        wiring it into ``models/__init__.py`` fails here rather than in a
        migration review.
        """
        assert set(Base.metadata.tables) == {
            # M1-01 — the thirteen core tables.
            "app_user",
            "artifact",
            "artifact_version",
            "audit_event",
            "comment",
            "generation_job",
            "outbox_event",
            "provider_usage",
            "review_decision",
            "series",
            "state_transition",
            "video_project",
            "workspace",
            # M2-01 — the first structured artifact content.
            "scene_set",
            "scene",
            # M3-02 — series-scoped branding (ADR-016). Not artifacts: an
            # artifact cannot be series-scoped, since `artifact.project_id` is
            # NOT NULL and finding S1's NULLS NOT DISTINCT constraint would
            # allow exactly one character in the entire table.
            "series_character",
            "character_reference",
            "series_style",
        }


class TestUrls:
    def test_sqlalchemy_url_pins_psycopg_driver(self) -> None:
        url = PostgresSettings().sqlalchemy_url
        assert url.startswith("postgresql+psycopg://")
        assert url.endswith("@postgres:5432/videoforge")
