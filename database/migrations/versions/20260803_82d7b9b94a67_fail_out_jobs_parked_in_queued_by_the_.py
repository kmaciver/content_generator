"""release state stranded by the old failure handling

A data fix, not a schema change. Two stranded states, one cause.

Before revision 16dae6463a8a a failing job was put back to QUEUED whenever
`may_retry` allowed another attempt — but nothing re-dispatches a QUEUED job
(the reconciler, §14.4, is still deferred). Those rows therefore sit in a
*live* status forever, and a live job holds its idempotency key, so the
artifact they belong to can never be regenerated.

The previous revision stops new jobs entering that state. This one releases the
ones already in it. A job that is QUEUED, has recorded an error and has already
started is unambiguously a failure written under the old behaviour: a genuinely
queued job has neither.

Deliberately narrow. `error IS NOT NULL AND started_at IS NOT NULL` cannot
match a job that is merely waiting to run.

**The artifacts are stranded too.** The skeleton never applied
GENERATION_FAILED, so an artifact whose job died stayed in GENERATING — and the
FSM then (correctly) refuses REGENERATE_REQUESTED from that state, so even a
freed idempotency key does not make the project recoverable. Both halves have
to be released or neither is worth doing.

Revision ID: 82d7b9b94a67
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "82d7b9b94a67"
down_revision: str | None = "16dae6463a8a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
        UPDATE generation_job
           SET status = 'FAILED',
               finished_at = COALESCE(finished_at, now())
         WHERE status = 'QUEUED'
           AND error IS NOT NULL
           AND started_at IS NOT NULL
        """)

    # Artifacts are stranded too, and for the same reason: the skeleton never
    # applied GENERATION_FAILED, so an artifact whose job died stayed in
    # GENERATING — and the FSM then correctly refuses REGENERATE_REQUESTED from
    # that state. Freeing the idempotency key alone leaves the project just as
    # unrecoverable; both halves have to be released or neither is worth doing.
    #
    # Scoped through the job rather than by age: an artifact is legitimately
    # GENERATING while its worker runs, and a dead job of its own is the only
    # safe signal that nobody is coming.
    op.execute("""
        UPDATE artifact a
           SET state = 'FAILED'
         WHERE a.state = 'GENERATING'
           AND EXISTS (
                 SELECT 1 FROM generation_job j
                  WHERE j.artifact_id = a.id
                    AND j.status IN ('FAILED', 'CANCELLED', 'ORPHANED')
               )
           AND NOT EXISTS (
                 SELECT 1 FROM generation_job j
                  WHERE j.artifact_id = a.id
                    AND j.status IN ('QUEUED', 'RUNNING')
               )
        """)


def downgrade() -> None:
    """Not reversible, and saying so is better than pretending.

    The rows this touched are indistinguishable afterwards from jobs that
    failed normally, and putting them back into QUEUED would re-create the
    stuck state on purpose.
    """
