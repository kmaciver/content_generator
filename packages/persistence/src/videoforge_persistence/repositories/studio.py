"""Image Studio queries (ADR-018).

The gallery reads generations together with their images and their latest
job, so ``page`` returns all three in three queries rather than leaving the
API to issue one per generation.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa

from videoforge_persistence.models import GenerationJob, StudioGeneration, StudioImage
from videoforge_persistence.repositories.base import Repository
from videoforge_shared.ids import new_ulid

__all__ = ["StudioEntry", "StudioRepository"]


@dataclass(frozen=True, slots=True)
class StudioEntry:
    """A generation as the gallery shows it."""

    generation: StudioGeneration
    images: tuple[StudioImage, ...]
    #: The most recent job for this generation. ``None`` only if the row was
    #: written without one, which the API never does.
    job: GenerationJob | None


class StudioRepository(Repository):
    def create_generation(
        self,
        *,
        prompt: str,
        model: str,
        aspect_ratio: str,
        count: int,
        references: Sequence[str],
    ) -> StudioGeneration:
        generation = StudioGeneration(
            id=new_ulid(),
            prompt=prompt,
            model=model,
            aspect_ratio=aspect_ratio,
            count=count,
            references=list(references),
        )
        self.session.add(generation)
        return generation

    def generation(self, generation_id: str) -> StudioGeneration | None:
        return self.session.get(StudioGeneration, generation_id)

    def add_image(
        self,
        generation_id: str,
        *,
        index: int,
        storage_key: str,
        content_hash: str,
        mime_type: str,
        width: int,
        height: int,
        provider_meta: dict[str, Any],
    ) -> StudioImage:
        image = StudioImage(
            id=new_ulid(),
            generation_id=generation_id,
            index=index,
            storage_key=storage_key,
            content_hash=content_hash,
            mime_type=mime_type,
            width=width,
            height=height,
            provider_meta=provider_meta,
        )
        self.session.add(image)
        return image

    def entry(self, generation_id: str) -> StudioEntry | None:
        generation = self.generation(generation_id)
        if generation is None:
            return None
        return self._entries([generation])[0]

    def page(self, *, limit: int, before: str | None = None) -> list[StudioEntry]:
        """Newest first. ``before`` is a generation id from the previous page.

        Paged by id rather than offset: ids are ULIDs, so they sort by creation
        time, and a keyset page does not shift when a new generation lands
        while someone is scrolling.
        """
        stmt = sa.select(StudioGeneration).order_by(StudioGeneration.id.desc())
        if before:
            stmt = stmt.where(StudioGeneration.id < before)
        generations = list(self.session.scalars(stmt.limit(limit)))
        return self._entries(generations)

    def _entries(self, generations: Sequence[StudioGeneration]) -> list[StudioEntry]:
        if not generations:
            return []
        ids = [g.id for g in generations]

        images: dict[str, list[StudioImage]] = {i: [] for i in ids}
        for image in self.session.scalars(
            sa.select(StudioImage)
            .where(StudioImage.generation_id.in_(ids))
            .order_by(StudioImage.generation_id, StudioImage.index)
        ):
            images[image.generation_id].append(image)

        # Latest job per generation, in one query. A retried request reuses its
        # job row, so "latest" only matters if a generation is ever re-run.
        jobs: dict[str, GenerationJob] = {}
        for job in self.session.scalars(
            sa.select(GenerationJob)
            .where(GenerationJob.studio_generation_id.in_(ids))
            .order_by(GenerationJob.studio_generation_id, GenerationJob.id.desc())
            .distinct(GenerationJob.studio_generation_id)
        ):
            assert job.studio_generation_id is not None
            jobs[job.studio_generation_id] = job

        return [
            StudioEntry(generation=g, images=tuple(images[g.id]), job=jobs.get(g.id))
            for g in generations
        ]
