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

    def test_metadata_is_empty_before_m1(self) -> None:
        """M0-07 ships no tables; the baseline migration is empty for the same
        reason. When M1 adds the schema, this test is deleted alongside."""
        assert not Base.metadata.tables


class TestUrls:
    def test_sqlalchemy_url_pins_psycopg_driver(self) -> None:
        url = PostgresSettings().sqlalchemy_url
        assert url.startswith("postgresql+psycopg://")
        assert url.endswith("@postgres:5432/videoforge")
