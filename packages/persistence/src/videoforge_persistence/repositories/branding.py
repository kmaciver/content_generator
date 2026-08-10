"""Series branding queries (ADR-016, M3-02).

The three writes that carry the rules are ``add_character_version`` /
``add_style_version`` (version allocation) and ``approve_*`` (supersession).
Everything else is a lookup.

**Approval is a two-statement sequence and must stay one transaction.**
Superseding the incumbent and approving the challenger are separate UPDATEs;
committed apart, the partial unique index would reject the second one — which
is the index doing its job, and a confusing way to discover it. The repository
does not commit (see ``Repository``), so the caller's unit of work is what
makes the pair atomic.
"""

from __future__ import annotations

import sqlalchemy as sa

from videoforge_persistence.models import (
    CharacterReference,
    SeriesCharacter,
    SeriesStyle,
)
from videoforge_persistence.repositories.base import Repository
from videoforge_shared.enums import BrandingStatus
from videoforge_shared.ids import new_ulid

__all__ = ["BrandingRepository"]


class BrandingRepository(Repository):
    def _lock_series(self, series_id: str) -> None:
        """Serialise version allocation for one series.

        ``SELECT max(version_no) ... FOR UPDATE`` is what this originally did,
        and Postgres rejects it outright: *FOR UPDATE is not allowed with
        aggregate functions*. There is no row for the aggregate to lock.

        ``ArtifactVersionRepository.add_version`` sidesteps the problem with a
        counter column on the parent (``UPDATE ... RETURNING``), which is not
        available here — ``series`` would need two counters, one per branding
        kind, and they would be the only mutable-for-bookkeeping columns on an
        otherwise declarative table.

        So lock the parent explicitly instead. Two writers adding a version to
        the same series serialise; writers on different series do not touch
        each other. The UNIQUE constraint is still the guarantee — this stops
        it firing in the ordinary case, exactly as ``add_version`` intends.
        """
        self.session.execute(
            sa.text("SELECT id FROM series WHERE id = :id FOR UPDATE"),
            {"id": series_id},
        )

    # -- characters -------------------------------------------------------- #

    def character(self, character_id: str) -> SeriesCharacter | None:
        return self.session.get(SeriesCharacter, character_id)

    def characters(self, series_id: str) -> list[SeriesCharacter]:
        """Every character version of a series, newest first."""
        return list(
            self.session.scalars(
                sa.select(SeriesCharacter)
                .where(SeriesCharacter.series_id == series_id)
                .order_by(SeriesCharacter.version_no.desc())
            )
        )

    def approved_character(self, series_id: str) -> SeriesCharacter | None:
        """The series' approved character, or None.

        At most one can exist — ``uq_series_character_one_approved`` is a
        partial unique index, so this cannot silently return the wrong one of
        two. That guarantee is in the database rather than in this query.
        """
        return self.session.scalars(
            sa.select(SeriesCharacter).where(
                SeriesCharacter.series_id == series_id,
                SeriesCharacter.status == BrandingStatus.APPROVED,
            )
        ).one_or_none()

    def add_character_version(
        self,
        series_id: str,
        *,
        name: str,
        immutable_traits: dict[str, object] | None = None,
        variable_traits: dict[str, object] | None = None,
    ) -> SeriesCharacter:
        """Next version for this series, allocated under the series row lock."""
        self._lock_series(series_id)
        next_no = (
            self.session.execute(
                sa.select(
                    sa.func.coalesce(sa.func.max(SeriesCharacter.version_no), 0)
                ).where(SeriesCharacter.series_id == series_id)
            ).scalar_one()
            + 1
        )
        character = SeriesCharacter(
            id=new_ulid(),
            series_id=series_id,
            version_no=next_no,
            name=name,
            immutable_traits=dict(immutable_traits or {}),
            variable_traits=dict(variable_traits or {}),
            status=BrandingStatus.PENDING,
        )
        self.session.add(character)
        return character

    def approve_character(
        self, character_id: str, *, reference_group_id: str | None = None
    ) -> SeriesCharacter | None:
        """Approve one version and supersede the incumbent, in that order.

        Supersede **first**. The reverse order would briefly have two rows
        APPROVED for one series, which the partial unique index rejects — so
        the wrong order is not a subtle bug that ships, it is an immediate
        IntegrityError. Doing it right anyway means the constraint stays a
        backstop rather than the control flow.
        """
        character = self.character(character_id)
        if character is None:
            return None

        self.session.execute(
            sa.update(SeriesCharacter)
            .where(
                SeriesCharacter.series_id == character.series_id,
                SeriesCharacter.status == BrandingStatus.APPROVED,
                SeriesCharacter.id != character_id,
            )
            .values(status=BrandingStatus.SUPERSEDED)
        )
        self.session.flush()

        character.status = BrandingStatus.APPROVED
        if reference_group_id is not None:
            character.approved_reference_group_id = reference_group_id
        return character

    # -- reference sheets -------------------------------------------------- #

    def add_reference(
        self,
        character_id: str,
        *,
        group_id: str,
        index: int,
        storage_key: str,
        content_hash: str,
        mime_type: str = "image/png",
        width: int = 0,
        height: int = 0,
        pose: str = "",
        angle: str = "",
        expression: str = "",
        shot_type: str = "",
        generation_job_id: str | None = None,
        generation_snapshot: dict[str, object] | None = None,
    ) -> CharacterReference:
        reference = CharacterReference(
            id=new_ulid(),
            character_id=character_id,
            group_id=group_id,
            index=index,
            storage_key=storage_key,
            content_hash=content_hash,
            mime_type=mime_type,
            width=width,
            height=height,
            pose=pose,
            angle=angle,
            expression=expression,
            shot_type=shot_type,
            generation_job_id=generation_job_id,
            generation_snapshot=dict(generation_snapshot or {}),
        )
        self.session.add(reference)
        return reference

    def references(self, group_id: str) -> list[CharacterReference]:
        """One candidate group, in order."""
        return list(
            self.session.scalars(
                sa.select(CharacterReference)
                .where(CharacterReference.group_id == group_id)
                .order_by(CharacterReference.index)
            )
        )

    def approved_references(self, series_id: str) -> list[CharacterReference]:
        """The canonical sheet for a series, or empty.

        Empty rather than raising, for the reason ``SceneRepository`` gives: a
        series with no approved character is the ordinary early state, and
        callers render "not yet" from an empty list more gracefully than from
        an exception. The admission check that turns this into a 409 belongs to
        the dispatch service (M3-06), not here.
        """
        character = self.approved_character(series_id)
        if character is None or character.approved_reference_group_id is None:
            return []
        return self.references(character.approved_reference_group_id)

    # -- styles ------------------------------------------------------------ #

    def style(self, style_id: str) -> SeriesStyle | None:
        return self.session.get(SeriesStyle, style_id)

    def styles(self, series_id: str) -> list[SeriesStyle]:
        return list(
            self.session.scalars(
                sa.select(SeriesStyle)
                .where(SeriesStyle.series_id == series_id)
                .order_by(SeriesStyle.version_no.desc())
            )
        )

    def approved_style(self, series_id: str) -> SeriesStyle | None:
        return self.session.scalars(
            sa.select(SeriesStyle).where(
                SeriesStyle.series_id == series_id,
                SeriesStyle.status == BrandingStatus.APPROVED,
            )
        ).one_or_none()

    def add_style_version(
        self,
        series_id: str,
        *,
        name: str,
        fields: dict[str, object] | None = None,
        prompt_block: str = "",
    ) -> SeriesStyle:
        self._lock_series(series_id)
        next_no = (
            self.session.execute(
                sa.select(
                    sa.func.coalesce(sa.func.max(SeriesStyle.version_no), 0)
                ).where(SeriesStyle.series_id == series_id)
            ).scalar_one()
            + 1
        )
        style = SeriesStyle(
            id=new_ulid(),
            series_id=series_id,
            version_no=next_no,
            name=name,
            fields=dict(fields or {}),
            prompt_block=prompt_block,
            status=BrandingStatus.PENDING,
        )
        self.session.add(style)
        return style

    def approve_style(self, style_id: str) -> SeriesStyle | None:
        """Approve one style version and supersede the incumbent."""
        style = self.style(style_id)
        if style is None:
            return None

        self.session.execute(
            sa.update(SeriesStyle)
            .where(
                SeriesStyle.series_id == style.series_id,
                SeriesStyle.status == BrandingStatus.APPROVED,
                SeriesStyle.id != style_id,
            )
            .values(status=BrandingStatus.SUPERSEDED)
        )
        self.session.flush()

        style.status = BrandingStatus.APPROVED
        return style
