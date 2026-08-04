"""M1-10: the demo seed.

Worth testing rather than eyeballing, because the seed's two claims are both
easy to break silently: that re-running it is a no-op, and that the states it
sets up are *derived* the same way production derives them. A seed that forced
a status by writing a column the application never writes would demonstrate a
state the system cannot actually reach.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
import sqlalchemy as sa
from sqlalchemy import Engine

from database.seed.demo import (
    DEMO_EMPTY_PROJECT_ID,
    DEMO_PROJECT_ID,
    DEMO_SERIES_ID,
    DEMO_USER_ID,
    DEMO_WORKSPACE_ID,
    seed_demo,
)
from videoforge_domain.approval_policy import ApprovalPolicy
from videoforge_persistence.uow import unit_of_work
from videoforge_shared.enums import ArtifactKind, ArtifactState, VersionStatus

pytestmark = pytest.mark.integration


@pytest.fixture()
def seeded(db_engine: Engine) -> Iterator[Engine]:
    seed_demo(db_engine)
    yield db_engine
    with unit_of_work(db_engine) as uow:
        uow.session.execute(
            sa.text("DELETE FROM workspace WHERE id = :id"),
            {"id": DEMO_WORKSPACE_ID},
        )


class TestIdempotence:
    def test_second_run_is_a_noop(self, db_engine: Engine) -> None:
        """``make seed`` on an already-seeded database is a normal thing to do.

        The check is on the workspace row rather than on a duplicate-key error,
        so a half-applied seed cannot leave the database looking done.
        """
        first = seed_demo(db_engine)
        second = seed_demo(db_engine)
        try:
            assert first.created is True
            assert second.created is False

            with unit_of_work(db_engine) as uow:
                count = uow.session.execute(
                    sa.text("SELECT count(*) FROM video_project WHERE id = :id"),
                    {"id": DEMO_PROJECT_ID},
                ).scalar_one()
            assert count == 1
        finally:
            with unit_of_work(db_engine) as uow:
                uow.session.execute(
                    sa.text("DELETE FROM workspace WHERE id = :id"),
                    {"id": DEMO_WORKSPACE_ID},
                )


class TestDemoContent:
    def test_ids_are_the_fixed_ones(self, seeded: Engine) -> None:
        """Playwright addresses the project by URL; the ids must be stable."""
        with unit_of_work(seeded) as uow:
            assert uow.workspaces.get(DEMO_WORKSPACE_ID) is not None
            assert uow.series.get(DEMO_SERIES_ID) is not None
            assert uow.projects.get(DEMO_PROJECT_ID) is not None
            assert uow.projects.get(DEMO_EMPTY_PROJECT_ID) is not None

    def test_mid_review_project_has_a_rejected_v1_and_a_pending_v2(
        self, seeded: Engine
    ) -> None:
        """The state the review screen is meant to open onto.

        Both statuses come from ``artifact_version_status``, computed from the
        ``review_decision`` row the seed wrote — not from anything the seed set
        directly.
        """
        with unit_of_work(seeded) as uow:
            artifact = uow.artifacts.find(DEMO_PROJECT_ID, ArtifactKind.SCRIPT)
            assert artifact is not None
            assert artifact.state is ArtifactState.AWAITING_APPROVAL

            statuses = {
                row.version_no: row.status
                for row in uow.versions.statuses_for_artifact(artifact.id)
            }
            assert statuses == {
                1: VersionStatus.REJECTED,
                2: VersionStatus.AWAITING_APPROVAL,
            }

    def test_lineage_is_real(self, seeded: Engine) -> None:
        """v2 points at v1 — "show me how the script evolved" must work here."""
        with unit_of_work(seeded) as uow:
            artifact = uow.artifacts.find(DEMO_PROJECT_ID, ArtifactKind.SCRIPT)
            assert artifact is not None
            versions = uow.versions.history(artifact.id)
            assert [v.version_no for v in versions] == [2, 1]
            assert versions[0].parent_version_id == versions[1].id

    def test_empty_project_has_nothing_to_review(self, seeded: Engine) -> None:
        """The other path: generating a first version by hand."""
        with unit_of_work(seeded) as uow:
            assert uow.artifacts.for_project(DEMO_EMPTY_PROJECT_ID) == []

    def test_series_defaults_to_all_manual(self, seeded: Engine) -> None:
        """A demo that auto-approved would hide the gate the product is built
        around."""
        with unit_of_work(seeded) as uow:
            series = uow.series.get(DEMO_SERIES_ID)
            assert series is not None
            policy = ApprovalPolicy.from_jsonb(series.auto_approve_policy)
            assert all(policy.requires_human(kind) for kind in ArtifactKind)

    def test_history_is_present(self, seeded: Engine) -> None:
        """The timeline a reviewer sees, including the rejection."""
        from videoforge_shared.enums import SubjectType

        with unit_of_work(seeded) as uow:
            artifact = uow.artifacts.find(DEMO_PROJECT_ID, ArtifactKind.SCRIPT)
            assert artifact is not None
            history = uow.audit.history_for(SubjectType.ARTIFACT, artifact.id)
            assert [t.to_state for t in history] == [
                "GENERATING",
                "AWAITING_APPROVAL",
                "REJECTED",
                "GENERATING",
                "AWAITING_APPROVAL",
            ]
            rejection = history[2]
            assert rejection.actor_id == DEMO_USER_ID

    def test_seed_publishes_no_events(self, seeded: Engine) -> None:
        """These events notionally happened days ago.

        Publishing them on first boot would tell every future subscriber that a
        script was just generated — a demo fixture triggering real
        notifications is the kind of thing that is only noticed once M5 wires
        a consumer up.
        """
        with unit_of_work(seeded) as uow:
            unpublished = uow.session.execute(
                sa.text(
                    "SELECT count(*) FROM outbox_event"
                    " WHERE payload->>'project_id' = :id"
                ),
                {"id": DEMO_PROJECT_ID},
            ).scalar_one()
        assert unpublished == 0
