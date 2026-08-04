"""The pipeline as a graph (ADR-009) — declaration in, answers out.

``templates/pipeline.yaml`` says which stages exist and what each one needs.
This module turns that declaration into something the rest of the system can
interrogate: what may run now, what a new approval invalidates, which phase a
project is in.

**Pure, and it takes a mapping rather than a path.** Reading the file is I/O
and belongs at the process edge (``videoforge_shared.pipeline_file``); parsing
the declaration into rules is a workflow concern and belongs here. Keeping the
split means the graph is testable with a dict literal — no fixture file, no
filesystem — which is the property ADR-015 buys and the reason PyYAML is not a
dependency of this package.

**The homogeneity rule (ADR-016).** Every ``requires`` entry must be an
``ArtifactKind`` resolved against *this project's* artifacts. A dependency on
an approved **series** asset — a character, a style — is not expressible here,
and that is deliberate rather than an omission: it resolves against a different
table, it must not cascade staleness, it is satisfied by the project's *pinned*
version rather than the series' current one, and unmet it means *blocked*
(409) rather than *in progress*. Four differences out of four make it an
admission check, enforced in the dispatch service before this graph is
consulted. :meth:`Pipeline.from_mapping` asserts the rule rather than trusting
anyone to remember it.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from videoforge_shared.enums import ArtifactKind, ProjectPhase

__all__ = ["Pipeline", "PipelineError", "Stage"]


class PipelineError(ValueError):
    """The declaration is not a usable pipeline.

    Raised at load time — which means at process boot — so a malformed graph
    stops the worker with a specific message rather than surfacing as a stage
    that mysteriously never becomes available.
    """


@dataclass(frozen=True, slots=True)
class Stage:
    """One step: what it makes, what it needs, and where it runs."""

    produces: ArtifactKind
    requires: frozenset[ArtifactKind]
    queue: str
    #: Whether one job fans out to one artifact per scene (§13). Informational
    #: for dispatch; the graph itself does not care.
    parallelizable_per_scene: bool
    #: The phase while this stage is running, and the phase once it has
    #: produced something awaiting review (§12.4). Declared per stage so that
    #: adding a stage never touches phase-derivation code — several stages may
    #: legitimately share a phase, as image and voice both share
    #: ``MEDIA_GENERATION``.
    phase_generating: ProjectPhase
    phase_review: ProjectPhase


class Pipeline:
    """An immutable, validated stage graph."""

    __slots__ = ("_by_kind", "_order", "_stages")

    def __init__(self, stages: Sequence[Stage]) -> None:
        self._stages = tuple(stages)
        self._by_kind = {stage.produces: stage for stage in self._stages}
        self._order = _topological_order(self._stages, self._by_kind)

    # -- construction ------------------------------------------------------ #

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any]) -> Pipeline:
        """Build from a parsed ``pipeline.yaml``, or raise :class:`PipelineError`.

        Every failure mode here is a configuration mistake that would otherwise
        be silent: a stage nothing can satisfy, two stages claiming one kind, a
        cycle. Each one produces a project that stops advancing with no error
        anywhere, which is the hardest class of bug to attribute.
        """
        raw_stages = data.get("stages")
        if not isinstance(raw_stages, list) or not raw_stages:
            raise PipelineError("pipeline declaration has no 'stages' list")

        stages = [
            _stage_from_mapping(index, raw) for index, raw in enumerate(raw_stages)
        ]

        seen: set[ArtifactKind] = set()
        for stage in stages:
            if stage.produces in seen:
                raise PipelineError(
                    f"two stages both produce {stage.produces.value!r}; "
                    "the active pointer for that kind would be ambiguous"
                )
            seen.add(stage.produces)

        for stage in stages:
            dangling = sorted(k.value for k in stage.requires - seen)
            if dangling:
                raise PipelineError(
                    f"stage {stage.produces.value!r} requires "
                    f"{', '.join(dangling)}, which no stage produces"
                )

        return cls(stages)

    # -- queries ----------------------------------------------------------- #

    @property
    def stages(self) -> tuple[Stage, ...]:
        """Every stage, in dependency order."""
        return self._order

    def stage_for(self, kind: ArtifactKind) -> Stage:
        try:
            return self._by_kind[kind]
        except KeyError:
            raise PipelineError(f"no stage produces {kind.value!r}") from None

    def has_stage(self, kind: ArtifactKind) -> bool:
        return kind in self._by_kind

    def roots(self) -> frozenset[ArtifactKind]:
        """Kinds that need nothing approved first — where a project starts."""
        return frozenset(s.produces for s in self._stages if not s.requires)

    def dependents(self, kind: ArtifactKind) -> frozenset[ArtifactKind]:
        """Kinds that directly require ``kind``."""
        return frozenset(s.produces for s in self._stages if kind in s.requires)

    def descendants(self, kind: ArtifactKind) -> frozenset[ArtifactKind]:
        """Everything downstream, transitively — the staleness blast radius (S2).

        Excludes ``kind`` itself: approving a new script version does not make
        that script stale, it makes everything derived from it stale.
        """
        found: set[ArtifactKind] = set()
        frontier = [kind]
        while frontier:
            for child in self.dependents(frontier.pop()):
                if child not in found:
                    found.add(child)
                    frontier.append(child)
        return frozenset(found)

    def unmet(
        self, kind: ArtifactKind, approved: Iterable[ArtifactKind]
    ) -> frozenset[ArtifactKind]:
        """Which of ``kind``'s requirements are not yet approved.

        Empty means the stage may run. Callers render this directly — "waiting
        on: script" is a better answer than a disabled button with no reason.
        """
        return frozenset(self.stage_for(kind).requires - set(approved))


def _stage_from_mapping(index: int, raw: Any) -> Stage:
    if not isinstance(raw, Mapping):
        raise PipelineError(f"stage #{index} is not a mapping")

    produces = _kind(raw.get("produces"), where=f"stage #{index} 'produces'")
    requires = frozenset(
        _kind(value, where=f"stage {produces.value!r} 'requires'")
        for value in raw.get("requires") or ()
    )
    if produces in requires:
        raise PipelineError(f"stage {produces.value!r} requires itself")

    queue = raw.get("queue")
    if not isinstance(queue, str) or not queue:
        raise PipelineError(f"stage {produces.value!r} has no queue")

    return Stage(
        produces=produces,
        requires=requires,
        queue=queue,
        parallelizable_per_scene=bool(raw.get("parallelizable_per_scene", False)),
        phase_generating=_phase(raw.get("phase_generating"), produces, "generating"),
        phase_review=_phase(raw.get("phase_review"), produces, "review"),
    )


def _kind(value: Any, *, where: str) -> ArtifactKind:
    """Parse an ``ArtifactKind``, naming the alternatives on failure.

    This is where ADR-016's homogeneity rule is enforced: ``character`` or
    ``style`` in a ``requires`` list fails here, with a message listing what a
    stage dependency is actually allowed to be.
    """
    try:
        return ArtifactKind(value)
    except ValueError:
        valid = ", ".join(sorted(k.value for k in ArtifactKind))
        raise PipelineError(
            f"{where}: {value!r} is not an artifact kind. "
            f"Stage dependencies are project-scoped artifact kinds only "
            f"(ADR-016); valid values are: {valid}"
        ) from None


def _phase(value: Any, produces: ArtifactKind, which: str) -> ProjectPhase:
    try:
        return ProjectPhase(value)
    except ValueError:
        raise PipelineError(
            f"stage {produces.value!r} has no valid {which} phase: {value!r}"
        ) from None


def _topological_order(
    stages: Sequence[Stage], by_kind: Mapping[ArtifactKind, Stage]
) -> tuple[Stage, ...]:
    """Kahn's algorithm, using the leftover nodes to name the cycle.

    A cycle cannot be reported as "invalid pipeline" and left there — the
    operator needs to know *which* stages, because the mistake is usually one
    edge in a list of nine.
    """
    remaining = {stage.produces: set(stage.requires) for stage in stages}
    ordered: list[Stage] = []

    while remaining:
        ready = sorted(
            (kind for kind, deps in remaining.items() if not deps),
            key=lambda k: k.value,
        )
        if not ready:
            cycle = ", ".join(sorted(k.value for k in remaining))
            raise PipelineError(f"pipeline has a dependency cycle among: {cycle}")
        for kind in ready:
            ordered.append(by_kind[kind])
            del remaining[kind]
        for deps in remaining.values():
            deps.difference_update(ready)

    return tuple(ordered)
