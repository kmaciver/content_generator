"""Unit of work: one transaction per use case (SADD §10.1).

Repositories never commit — that is what makes §10.3 rule 6 achievable, since
a worker must land its artifact version, state transition, audit event and
outbox row atomically or not at all. Somebody still has to own the commit,
and this is that somebody.

Both apps use it. The backend opens one per request; the worker skeleton opens
one around the task body.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from functools import cached_property

from sqlalchemy import Engine
from sqlalchemy.orm import Session, sessionmaker

from videoforge_persistence.repositories import (
    ArtifactRepository,
    ArtifactVersionRepository,
    AuditRepository,
    BrandingRepository,
    CommentRepository,
    JobRepository,
    OutboxRepository,
    ProjectRepository,
    ProviderUsageRepository,
    ReviewRepository,
    SceneRepository,
    SeriesRepository,
    WorkspaceRepository,
)

__all__ = ["UnitOfWork", "unit_of_work"]


@dataclass
class UnitOfWork:
    """One session, every repository, one commit.

    Repositories are built lazily and cached per instance so that a caller
    touching three of them shares one session — the whole point. Constructing
    them eagerly would be eleven objects per request to use two.

    Deliberately **not** ``slots=True``, unlike every other dataclass here:
    ``cached_property`` stores into the instance ``__dict__`` that slots
    removes. The caching is the feature; the few bytes slots would save on one
    object per request are not.
    """

    session: Session

    @cached_property
    def workspaces(self) -> WorkspaceRepository:
        return WorkspaceRepository(self.session)

    @cached_property
    def series(self) -> SeriesRepository:
        return SeriesRepository(self.session)

    @cached_property
    def projects(self) -> ProjectRepository:
        return ProjectRepository(self.session)

    @cached_property
    def artifacts(self) -> ArtifactRepository:
        return ArtifactRepository(self.session)

    @cached_property
    def versions(self) -> ArtifactVersionRepository:
        return ArtifactVersionRepository(self.session)

    @cached_property
    def scenes(self) -> SceneRepository:
        return SceneRepository(self.session)

    @cached_property
    def branding(self) -> BrandingRepository:
        return BrandingRepository(self.session)

    @cached_property
    def jobs(self) -> JobRepository:
        return JobRepository(self.session)

    @cached_property
    def usage(self) -> ProviderUsageRepository:
        return ProviderUsageRepository(self.session)

    @cached_property
    def reviews(self) -> ReviewRepository:
        return ReviewRepository(self.session)

    @cached_property
    def comments(self) -> CommentRepository:
        return CommentRepository(self.session)

    @cached_property
    def audit(self) -> AuditRepository:
        return AuditRepository(self.session)

    @cached_property
    def outbox(self) -> OutboxRepository:
        return OutboxRepository(self.session)

    def flush(self) -> None:
        """Send pending statements without ending the transaction.

        Useful when a caller needs a server-generated value, or wants a
        constraint violation to surface at the line that caused it rather
        than at commit.
        """
        self.session.flush()


@contextmanager
def unit_of_work(
    factory: sessionmaker[Session] | Engine,
) -> Iterator[UnitOfWork]:
    """Open a transaction, commit on success, roll back on any exception.

    Rolling back on *any* exception, rather than catching selectively, is
    deliberate: a half-applied use case is worse than a failed one. If the
    artifact version committed but the audit event did not, the system has
    lost the ability to explain itself, and nothing would ever report that.

    Accepts an Engine as well as a sessionmaker so tests and one-off scripts
    do not have to build a factory to do one thing.
    """
    maker = (
        factory
        if isinstance(factory, sessionmaker)
        else sessionmaker(bind=factory, autoflush=False, expire_on_commit=False)
    )
    session = maker()
    try:
        yield UnitOfWork(session=session)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
