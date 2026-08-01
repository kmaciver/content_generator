"""Repository base.

SADD §10.1: *repositories are the only code that touches the session.* That
rule is what keeps query shape in one place — when a listing turns out to need
an index, there is exactly one query to find, and it is next to the model that
declares the index.

**Documented deviation.** §10.1 also says repositories "return domain objects".
These return ORM models. Introducing a parallel entity hierarchy for thirteen
tables would double the mapping surface to buy a decoupling this codebase does
not currently need: ``videoforge_domain`` holds *rules*, not entities, so there
are no domain entities to map to. The property that actually matters — ORM
objects never reaching the API — is enforced at the DTO boundary instead
(§10.1: "DTOs never leak ORM objects"), and by ``expire_on_commit=False`` on
the session factory, which stops detached-instance surprises after commit.

Revisit if either becomes true: a second persistence backend appears, or the
services start branching on SQLAlchemy state (``inspect(obj).persistent`` and
friends). Both would mean the seam is being missed.
"""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import CursorResult
from sqlalchemy.engine import Result
from sqlalchemy.orm import Session

__all__ = ["Repository", "affected_rows"]


def affected_rows(result: Result[Any]) -> int:
    """Row count from an UPDATE or DELETE.

    ``Session.execute`` is typed as returning ``Result``, which has no
    ``rowcount`` — but every DML statement actually returns a
    ``CursorResult``, which does. The cast documents that narrowing in one
    place instead of scattering ``# type: ignore`` across every guarded
    write, and the guarded writes are exactly where the row count carries the
    correctness of the system (``claim``, ``mark_succeeded``, ``mark_published``).
    """
    return cast("CursorResult[Any]", result).rowcount


class Repository:
    """Holds the session; owns no transaction.

    Committing is the *service's* call, not the repository's — SADD §10.1's
    unit of work is "one transaction per use case", and a repository that
    commits on its own makes that impossible. A worker inserting an artifact
    version, a state transition, an audit event and an outbox row must land
    all four atomically or none; four self-committing repositories would give
    four separate transactions and, on a crash between them, a version with no
    audit trail.
    """

    __slots__ = ("session",)

    def __init__(self, session: Session) -> None:
        self.session = session
